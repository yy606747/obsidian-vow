"""Legacy location helpers kept behind the Location V2 service boundary."""

import json
import time

import httpx

from config import DATA_DIR
from location_diagnostics import (
    classify_amap_payload, record_location_event, record_location_exception,
)
from provider_status import classify_http_status, new_request_id

LOCATION_CONFIG_PATH = DATA_DIR / "location_config.json"
LOCATION_STATUS_PATH = DATA_DIR / "location_status.json"
LOCATION_SENTINEL_STALE_SEC = 30 * 60
LOCATION_SENTINEL_TRANSITION_SEC = 60 * 60

DEFAULT_LOCATION_CONFIG = {
    "amap_key": "",                   # 高德 Web 服务 API Key
    "home_lng": 0.0,                  # 家的经度 (GCJ-02)
    "home_lat": 0.0,                  # 家的纬度 (GCJ-02)
    "home_threshold": 500,            # 离家阈值（米）
    "heartbeat_outdoor_min": 10,      # 外出时心跳间隔（分钟）
    "heartbeat_home_min": 10,         # 在家时心跳间隔（分钟）
    "poi_types": {                    # POI 搜索类型
        "餐饮美食": "050000",
        "风景名胜": "110000",
        "休闲娱乐": "100000",
        "购物": "060000",
    },
    "poi_radius": 2000,               # POI 搜索半径（米）
    "movement_threshold": 500,        # 外出时"显著移动"判定距离（米）
    "enabled": False,                 # 定位功能总开关
    "quiet_hours_enabled": False,     # 静默时段开关
    "quiet_hours_start": "00:00",     # 静默开始
    "quiet_hours_end": "08:00",       # 静默结束
}


def load_location_config() -> dict:
    if LOCATION_CONFIG_PATH.exists():
        cfg = json.loads(LOCATION_CONFIG_PATH.read_text(encoding="utf-8"))
        for k, v in DEFAULT_LOCATION_CONFIG.items():
            cfg.setdefault(k, v)
        return cfg
    return dict(DEFAULT_LOCATION_CONFIG)


def save_location_config(cfg: dict):
    LOCATION_CONFIG_PATH.write_text(
        json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8"
    )


# ── 位置状态 ──────────────────────────────────────
DEFAULT_LOCATION_STATUS = {
    "state": "unknown",        # unknown / at_home / outside
    "lng": 0.0,
    "lat": 0.0,
    "accuracy": 0.0,
    "address": "",
    "adcode": "",
    "weather": {},             # 实况天气
    "forecast": [],            # 天气预报
    "nearby_pois": {},         # {类型名: [poi...]}
    "updated_at": 0,
    "state_changed_at": 0,
    "distance_from_home": 0,
}


def load_location_status() -> dict:
    if LOCATION_STATUS_PATH.exists():
        data = json.loads(LOCATION_STATUS_PATH.read_text(encoding="utf-8"))
        for k, v in DEFAULT_LOCATION_STATUS.items():
            data.setdefault(k, v)
        return data
    return dict(DEFAULT_LOCATION_STATUS)


def save_location_status(data: dict):
    LOCATION_STATUS_PATH.write_text(
        json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def _record_amap_event(
    scope: str,
    request_id: str,
    start: float,
    *,
    ok: bool,
    http_status: int | None,
    amap_data: dict | None,
    empty_result: bool = False,
    meta: dict | None = None,
) -> dict:
    if ok:
        error_type, retryable = "ok", False
    elif http_status is not None and int(http_status) >= 400:
        error_type, retryable = classify_http_status(http_status)
    else:
        error_type, retryable = classify_amap_payload(amap_data)
    data = amap_data if isinstance(amap_data, dict) else {}
    return record_location_event({
        "request_id": request_id,
        "scope": scope,
        "ok": bool(ok),
        "http_status": http_status,
        "amap_status": data.get("status", ""),
        "infocode": data.get("infocode", ""),
        "info": data.get("info", ""),
        "error_type": error_type,
        "retryable": retryable,
        "elapsed_ms": (time.perf_counter() - start) * 1000,
        "empty_result": bool(empty_result),
        "meta": meta or {},
    })


# ── 高德 API 调用 ────────────────────────────────
async def amap_regeo(lng: float, lat: float, key: str) -> dict | None:
    """逆地理编码：坐标 → 地址 + adcode"""
    url = "https://restapi.amap.com/v3/geocode/regeo"
    params = {"key": key, "location": f"{lng},{lat}", "extensions": "base"}
    request_id = new_request_id("loc")
    start = time.perf_counter()
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(url, params=params)
            data = resp.json()
            ok_payload = data.get("status") == "1"
            empty = ok_payload and not bool(data.get("regeocode"))
            _record_amap_event(
                "location:amap_regeo", request_id, start,
                ok=ok_payload, http_status=resp.status_code,
                amap_data=data, empty_result=empty,
                meta={"extensions": "base"},
            )
            if ok_payload and data.get("regeocode"):
                rc = data["regeocode"]
                return {
                    "address": rc.get("formatted_address", ""),
                    "adcode": rc.get("addressComponent", {}).get("adcode", ""),
                    "province": rc.get("addressComponent", {}).get("province", ""),
                    "city": rc.get("addressComponent", {}).get("city", ""),
                    "district": rc.get("addressComponent", {}).get("district", ""),
                }
    except Exception as e:
        record_location_exception(
            scope="location:amap_regeo", request_id=request_id, start=start,
            exc=e, meta={"extensions": "base"},
        )
        print(f"[Location] 逆地理编码失败: {e}")
    return None


async def amap_weather(adcode: str, key: str) -> dict:
    """天气查询：实况 + 预报"""
    result = {"live": {}, "forecast": []}
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            live_request_id = new_request_id("loc")
            live_start = time.perf_counter()
            live_resp = await client.get(
                "https://restapi.amap.com/v3/weather/weatherInfo",
                params={"key": key, "city": adcode, "extensions": "base"},
            )
            live_data = live_resp.json()
            live_ok = live_data.get("status") == "1"
            live_empty = live_ok and not bool(live_data.get("lives"))
            _record_amap_event(
                "location:amap_weather_live", live_request_id, live_start,
                ok=live_ok, http_status=live_resp.status_code,
                amap_data=live_data, empty_result=live_empty,
                meta={"extensions": "base"},
            )
            if live_ok and live_data.get("lives"):
                result["live"] = live_data["lives"][0]

            fc_request_id = new_request_id("loc")
            fc_start = time.perf_counter()
            fc_resp = await client.get(
                "https://restapi.amap.com/v3/weather/weatherInfo",
                params={"key": key, "city": adcode, "extensions": "all"},
            )
            fc_data = fc_resp.json()
            fc_ok = fc_data.get("status") == "1"
            fc_empty = fc_ok and not bool(fc_data.get("forecasts"))
            _record_amap_event(
                "location:amap_weather_forecast", fc_request_id, fc_start,
                ok=fc_ok, http_status=fc_resp.status_code,
                amap_data=fc_data, empty_result=fc_empty,
                meta={"extensions": "all"},
            )
            if fc_ok and fc_data.get("forecasts"):
                result["forecast"] = fc_data["forecasts"][0].get("casts", [])
    except Exception as e:
        record_location_event({
            "scope": "location:amap_weather",
            "ok": False,
            "error_type": "exception",
            "message": e.__class__.__name__,
        })
        print(f"[Location] 天气查询失败: {e}")
    return result


async def amap_poi_search(lng: float, lat: float, types: str, key: str, radius: int = 2000) -> list:
    """周边 POI 搜索"""
    url = "https://restapi.amap.com/v3/place/around"
    params = {
        "key": key,
        "location": f"{lng},{lat}",
        "types": types,
        "radius": radius,
        "offset": 10,
        "page": 1,
        "extensions": "all",
        "sortrule": "distance",
    }
    request_id = new_request_id("loc")
    start = time.perf_counter()
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(url, params=params)
            data = resp.json()
            ok_payload = data.get("status") == "1"
            pois = data.get("pois", []) if ok_payload else []
            _record_amap_event(
                "location:amap_poi_search", request_id, start,
                ok=ok_payload, http_status=resp.status_code,
                amap_data=data, empty_result=ok_payload and not bool(pois),
                meta={"radius": radius, "types_present": bool(types)},
            )
            if ok_payload:
                pois = data.get("pois", [])
                # 只保留关键字段，减少存储体积
                return [
                    {
                        "name": p.get("name", ""),
                        "type": p.get("type", ""),
                        "address": p.get("address", ""),
                        "distance": p.get("distance", ""),
                        "tel": p.get("tel", "") if p.get("tel") != "[]" else "",
                        "rating": (p.get("biz_ext") or {}).get("rating", ""),
                        "cost": (p.get("biz_ext") or {}).get("cost", ""),
                        "location": p.get("location", ""),
                        "photos": [ph.get("url", "") for ph in (p.get("photos") or []) if ph.get("url")][:1],
                    }
                    for p in pois
                ]
    except Exception as e:
        record_location_exception(
            scope="location:amap_poi_search", request_id=request_id, start=start,
            exc=e, meta={"radius": radius, "types_present": bool(types)},
        )
        print(f"[Location] POI搜索失败: {e}")
    return []


# ── 位置信息格式化（供 prompt 注入） ───────────────
def format_location_for_prompt() -> str:
    """格式化当前位置状态，供 Core prompt 使用。"""
    status = load_location_status()
    cfg = load_location_config()
    if not cfg.get("enabled"):
        return ""
    from app.location import state_from_payload

    state = state_from_payload(status.get("v2_state"))
    if state is None:
        return ""
    return state.for_prompt(
        time.time(),
        geofence_radius_m=_configured_geofence_radius_m(cfg, state),
    )


def format_location_for_sentinel() -> str:
    """格式化定位运行状态，供 Sentinel 判断变化使用。"""
    status = load_location_status()
    cfg = load_location_config()
    if not cfg.get("enabled"):
        return ""

    from app.location import state_from_payload

    now = time.time()
    v2_state = state_from_payload(status.get("v2_state"))
    last_fix_at = _location_last_fix_at(status, v2_state)
    if last_fix_at <= 0:
        return ""

    age_min = max(0, int((now - last_fix_at) // 60))
    legacy_state = str(status.get("state") or "unknown")
    accuracy = status.get("accuracy")
    if accuracy is None and v2_state is not None:
        accuracy = v2_state.accuracy_m
    changed_at = float(status.get("state_changed_at") or 0)
    recent_transition = changed_at > 0 and now - changed_at <= LOCATION_SENTINEL_TRANSITION_SEC
    stale = now - last_fix_at > LOCATION_SENTINEL_STALE_SEC
    home_radius_m = _configured_home_geofence_radius_m(cfg)

    if recent_transition and legacy_state == "outside":
        return _format_home_geofence_transition_for_sentinel(
            direction="inside_to_outside",
            distance_m=status.get("distance_from_home"),
            radius_m=home_radius_m,
            accuracy_m=accuracy,
            age_min=age_min if stale else None,
        )
    if recent_transition and legacy_state == "at_home":
        return _format_home_geofence_transition_for_sentinel(
            direction="outside_to_inside",
            distance_m=status.get("distance_from_home"),
            radius_m=home_radius_m,
            accuracy_m=accuracy,
            age_min=age_min if stale else None,
        )

    if stale:
        return f"位置数据过期，更新时间{age_min}分钟前，不能作为当前状态。"

    if legacy_state == "outside":
        return _format_stable_home_geofence_state_for_sentinel(
            boundary_side="outside",
            distance_m=status.get("distance_from_home"),
            radius_m=home_radius_m,
            accuracy_m=accuracy,
            age_min=age_min,
        )
    if v2_state is not None:
        text = v2_state.for_sentinel(
            now,
            geofence_radius_m=_configured_geofence_radius_m(cfg, v2_state),
        )
        if text:
            return text
    if legacy_state == "at_home":
        return _format_stable_home_geofence_state_for_sentinel(
            boundary_side="inside",
            distance_m=status.get("distance_from_home"),
            radius_m=home_radius_m,
            accuracy_m=accuracy,
            age_min=age_min,
        )
    measurements = _location_measurements_for_sentinel(accuracy_m=accuracy)
    return f"当前设备定位状态未知{measurements}。"


def _configured_geofence_radius_m(cfg: dict, state) -> float | None:
    """Return an existing configured radius without inventing confidence."""

    if state is None or not state.place_id:
        return None
    if state.place_id != "home" and state.place_kind != "home":
        return None
    return _configured_home_geofence_radius_m(cfg)


def _configured_home_geofence_radius_m(cfg: dict) -> float | None:
    """Read the configured home boundary without turning it into confidence."""

    try:
        radius = float(cfg.get("home_threshold"))
    except (TypeError, ValueError):
        return None
    return radius if radius > 0 else None


def _location_last_fix_at(status: dict, v2_state) -> float:
    if v2_state is not None:
        return float(v2_state.last_fix_at or 0)
    return float(status.get("updated_at") or 0)


def _format_home_geofence_transition_for_sentinel(
    *,
    direction: str,
    distance_m,
    radius_m,
    accuracy_m,
    age_min: int | None,
) -> str:
    if direction == "inside_to_outside":
        transition = "从你们标注的「家」范围里走到了范围外"
    elif direction == "outside_to_inside":
        transition = "从你们标注的「家」范围外回到了范围里"
    else:
        raise ValueError(f"unknown geofence transition direction: {direction}")

    measurements = _location_measurements_for_sentinel(
        distance_m=distance_m,
        radius_m=radius_m,
        accuracy_m=accuracy_m,
        age_min=age_min,
    )
    stale_warning = "，旧定位不能作为当前位置" if age_min is not None else ""
    return (
        f"手机的定位{transition}{measurements}；"
        f"这只是手机越过了那条边界{stale_warning}，"
        "说明不了她去了哪、回了哪、在做什么，"
        "也说明不了手机是不是在她身上。"
    )


def _format_stable_home_geofence_state_for_sentinel(
    *,
    boundary_side: str,
    distance_m,
    radius_m,
    accuracy_m,
    age_min: int,
) -> str:
    if boundary_side == "inside":
        state_text = "在你们标注的「家」范围里"
    elif boundary_side == "outside":
        state_text = "在你们标注的「家」范围外"
    else:
        raise ValueError(f"unknown geofence boundary side: {boundary_side}")

    measurements = _location_measurements_for_sentinel(
        distance_m=distance_m,
        radius_m=radius_m,
        accuracy_m=accuracy_m,
        age_min=age_min,
    )
    return (
        f"最近一次定位{state_text}{measurements}，之后没有新的进出记录；"
        "说明不了她去了哪、回了哪、在做什么，"
        "也说明不了手机是不是在她身上。"
    )


def _location_measurements_for_sentinel(
    *,
    distance_m=None,
    radius_m=None,
    accuracy_m=None,
    age_min: int | None = None,
) -> str:
    parts = []
    distance = _positive_or_zero_float(distance_m)
    radius = _positive_float(radius_m)
    accuracy = _positive_float(accuracy_m)
    if distance is not None:
        parts.append(f"距围栏中心约{distance:.0f}米")
    if radius is not None:
        parts.append(f"配置半径约{radius:.0f}米")
    if accuracy is not None:
        parts.append(f"定位精度约{accuracy:.0f}米")
    if age_min is not None:
        parts.append(f"定位更新时间约{max(0, int(age_min))}分钟前")
    return f"（{'，'.join(parts)}）" if parts else ""


def _positive_float(value) -> float | None:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def _positive_or_zero_float(value) -> float | None:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed >= 0 else None


def format_nearby_pois_for_prompt(*, user_name: str | None = None) -> str:
    """格式化周边 POI 数据，供 Core 回答伴侣提问时使用。"""
    if not str(user_name or "").strip():
        from app.chat.worldbook import load_worldbook_names

        user_name = load_worldbook_names()[0]
    else:
        user_name = str(user_name).strip()
    status = load_location_status()
    # 周边 POI 只有“外出中”才有意义；回家后即使缓存未过期也不注入，避免把外出地点带进 prompt。
    if status.get("state") != "outside":
        return ""
    enriched_at = status.get("enriched_at", 0)
    if not enriched_at or time.time() - enriched_at > 20 * 60:
        return ""
    pois = status.get("nearby_pois", {})
    if not pois:
        return ""

    lines = [f"以下是{user_name}当前位置周边的信息："]
    for category, items in pois.items():
        if not items:
            continue
        lines.append(f"\n【{category}】")
        for p in items[:8]:
            entry = f"  - {p['name']}"
            if p.get("distance"):
                d = int(p["distance"])
                entry += f"（{d}m）"
            if p.get("rating") and p["rating"] != "[]":
                entry += f" ⭐{p['rating']}"
            if p.get("cost") and p["cost"] != "[]":
                entry += f" 人均¥{p['cost']}"
            if p.get("address") and p["address"] != "[]":
                entry += f" | {p['address']}"
            lines.append(entry)

    return "\n".join(lines)


# ── 静默时段检查 ─────────────────────────────────
def is_location_quiet_hours() -> bool:
    """检查当前是否处于定位静默时段"""
    cfg = load_location_config()
    if not cfg.get("quiet_hours_enabled", False):
        return False
    start_str = cfg.get("quiet_hours_start", "00:00")
    end_str = cfg.get("quiet_hours_end", "08:00")
    sh, sm = map(int, start_str.split(":"))
    eh, em = map(int, end_str.split(":"))
    now = time.localtime()
    cur = now.tm_hour * 60 + now.tm_min
    start = sh * 60 + sm
    end = eh * 60 + em
    if start <= end:
        return start <= cur < end
    else:  # 跨午夜
        return cur >= start or cur < end
