"""
设置、世界书、模型列表、TTS 路由
"""

from __future__ import annotations

import json, re, time

from fastapi import APIRouter, HTTPException
from fastapi.responses import Response, FileResponse
from pydantic import BaseModel
from typing import Optional

import httpx

from config import (
    SETTINGS, MODELS, save_settings, get_key, load_worldbook, save_worldbook,
    load_chat_status, TTS_CACHE_DIR,
    list_core_models, load_ai_behavior, save_ai_behavior, DEFAULT_AI_BEHAVIOR,
    is_smart_ring_quiet_hours, is_smart_ring_touch_active, get_endpoint,
    _ensure_endpoints_and_slots,
)
from ai_providers import test_endpoint as _test_endpoint
from provider_status import (
    classify_exception, classify_http_status, new_request_id,
    recent_provider_events, record_provider_event, summarize_provider_events,
)
from location_diagnostics import recent_location_events, summarize_location_events

router = APIRouter()
SILICONFLOW_BASE = "https://api.siliconflow.cn/v1"


def _record_sf_aux_provider(scope: str, request_id: str, start: float, *,
                            ok: bool, model: str, path: str,
                            http_status: int | None = None,
                            error_type: str | None = None,
                            retryable: bool = False,
                            message: str = "",
                            meta: dict | None = None):
    if http_status is not None and not error_type:
        error_type, retryable = classify_http_status(http_status)
    record_provider_event({
        "request_id": request_id,
        "scope": scope,
        "provider_type": "openai",
        "provider_label": "SiliconFlow auxiliary",
        "endpoint_name": "SiliconFlow auxiliary",
        "base_url": f"{SILICONFLOW_BASE}{path}",
        "model": model,
        "ok": ok,
        "http_status": http_status,
        "error_type": error_type or ("ok" if ok else "unknown"),
        "retryable": retryable,
        "elapsed_ms": (time.perf_counter() - start) * 1000,
        "message": message,
        "meta": meta or {},
    })


def _record_sf_aux_exception(scope: str, request_id: str, start: float, *,
                             model: str, path: str, exc: Exception,
                             meta: dict | None = None):
    if isinstance(exc, (json.JSONDecodeError, KeyError, IndexError, ValueError, TypeError)):
        error_type, retryable = "parse_error", False
    else:
        error_type, retryable = classify_exception(exc)
    _record_sf_aux_provider(
        scope, request_id, start,
        ok=False, model=model, path=path,
        error_type=error_type, retryable=retryable,
        message=exc.__class__.__name__, meta=meta,
    )

# ── 模型列表（含预设 + 用户自定义）────────────────
@router.get("/api/models")
async def list_models():
    """前端模型下拉用。预设带 provider，自定义带 endpoint_id + model。"""
    out = []
    for k in list_core_models():
        if k in MODELS:
            v = MODELS[k]
            out.append({
                "key": k,
                "provider": v["provider"],
                "model": v["model"],
                "kind": "preset",
                "audio_input": v.get("audio_input") is True,
            })
        else:
            um = SETTINGS.get("user_models", {}).get(k, {})
            out.append({
                "key": k, "kind": "custom",
                "endpoint": um.get("endpoint", ""),
                "model": um.get("model", ""),
                "audio_input": um.get("audio_input") is True,
            })
    return out

# ── 设置 ──────────────────────────────────────────
class SettingsUpdate(BaseModel):
    gemini_key: Optional[str] = None
    siliconflow_key: Optional[str] = None
    gemini_free_key: Optional[str] = None
    aipro_key: Optional[str] = None
    tavily_api_key: Optional[str] = None
    netease_music_u: Optional[str] = None
    smart_ring_touch_enabled: Optional[bool] = None
    smart_ring_keep_connected: Optional[bool] = None
    smart_ring_quiet_hours_enabled: Optional[bool] = None
    smart_ring_quiet_hours_start: Optional[str] = None
    smart_ring_quiet_hours_end: Optional[str] = None

@router.get("/api/settings")
async def get_settings():
    """前端只拿掩码；真实 key 仅从环境变量 / settings.json 在后端使用。"""
    import os
    return {
        "gemini_key_masked": _mask_key(SETTINGS.get("gemini_key", "")),
        "siliconflow_key_masked": _mask_key(SETTINGS.get("siliconflow_key", "")),
        "gemini_free_key_masked": _mask_key(SETTINGS.get("gemini_free_key", "")),
        "aipro_key_masked": _mask_key(SETTINGS.get("aipro_key", "")),
        "tavily_api_key_masked": _mask_key(SETTINGS.get("tavily_api_key", "")),
        "netease_music_u_masked": _mask_key(SETTINGS.get("netease_music_u", "")),
        "smart_ring_touch_enabled": bool(SETTINGS.get("smart_ring_touch_enabled", False)),
        "smart_ring_keep_connected": bool(SETTINGS.get("smart_ring_keep_connected", False)),
        "smart_ring_quiet_hours_enabled": bool(SETTINGS.get("smart_ring_quiet_hours_enabled", False)),
        "smart_ring_quiet_hours_start": SETTINGS.get("smart_ring_quiet_hours_start", "00:00"),
        "smart_ring_quiet_hours_end": SETTINGS.get("smart_ring_quiet_hours_end", "08:00"),
        "smart_ring_is_quiet_hours": is_smart_ring_quiet_hours(),
        "smart_ring_touch_active": is_smart_ring_touch_active(),
        # 提示：哪些 key 已经被环境变量覆盖
        "env_overrides": {
            "gemini_key": bool(os.environ.get("AION_GEMINI_KEY")),
            "siliconflow_key": bool(os.environ.get("AION_SILICONFLOW_KEY")),
            "gemini_free_key": bool(os.environ.get("AION_GEMINI_FREE_KEY")),
            "aipro_key": bool(os.environ.get("AION_AIPRO_KEY")),
            "tavily_api_key": bool(os.environ.get("AION_TAVILY_API_KEY")),
            "netease_music_u": bool(os.environ.get("AION_NETEASE_MUSIC_U")),
        },
    }

def _is_mask_value(v: str) -> bool:
    """前端传回的掩码形如 "abcd****efgh"，表示用户没改；此时不覆盖。"""
    return bool(v) and "*" in v

@router.put("/api/settings")
async def update_settings(body: SettingsUpdate):
    def _update(field: str, val):
        if val is None:
            return
        if _is_mask_value(val):
            return
        SETTINGS[field] = val
    _update("gemini_key", body.gemini_key)
    _update("siliconflow_key", body.siliconflow_key)
    _update("gemini_free_key", body.gemini_free_key)
    _update("aipro_key", body.aipro_key)
    _update("tavily_api_key", body.tavily_api_key)
    if body.netease_music_u is not None and not _is_mask_value(body.netease_music_u):
        old_mu = SETTINGS.get("netease_music_u", "")
        SETTINGS["netease_music_u"] = body.netease_music_u
        if body.netease_music_u != old_mu:
            try:
                from music import reload_login
                reload_login()
            except Exception:
                pass
    if body.smart_ring_touch_enabled is not None:
        SETTINGS["smart_ring_touch_enabled"] = bool(body.smart_ring_touch_enabled)
    if body.smart_ring_keep_connected is not None:
        SETTINGS["smart_ring_keep_connected"] = bool(body.smart_ring_keep_connected)
    if body.smart_ring_quiet_hours_enabled is not None:
        SETTINGS["smart_ring_quiet_hours_enabled"] = bool(body.smart_ring_quiet_hours_enabled)
    if body.smart_ring_quiet_hours_start is not None:
        SETTINGS["smart_ring_quiet_hours_start"] = body.smart_ring_quiet_hours_start
    if body.smart_ring_quiet_hours_end is not None:
        SETTINGS["smart_ring_quiet_hours_end"] = body.smart_ring_quiet_hours_end
    _ensure_endpoints_and_slots(SETTINGS)
    save_settings(SETTINGS)
    return {
        "ok": True,
        "smart_ring_touch_active": is_smart_ring_touch_active(),
        "smart_ring_is_quiet_hours": is_smart_ring_quiet_hours(),
    }

# ── 温度设置 ──────────────────────────────────────
class TempUpdate(BaseModel):
    temperature: float

@router.put("/api/settings/temperature")
async def update_temperature(body: TempUpdate):
    SETTINGS["temperature"] = body.temperature
    save_settings(SETTINGS)
    return {"ok": True}

# ── 密语模式兼容接口 ─────────────────────────────
# INERT / DEPRECATED: 前端旧代码仍会调用这里，但密语是否可用只由当前
# control session + DeviceService 实时状态决定；此接口不得再写 SETTINGS。
class WhisperUpdate(BaseModel):
    active: bool

@router.put("/api/settings/whisper")
async def update_whisper(body: WhisperUpdate):
    return {"ok": True, "active": False, "deprecated": True}

@router.get("/api/settings/whisper")
async def get_whisper():
    return {"active": False, "deprecated": True}

# ── 世界书 ────────────────────────────────────────
class WorldBookUpdate(BaseModel):
    ai_persona: str = ""
    user_persona: str = ""
    ai_name: str = "AI"
    user_name: str = "你"

@router.get("/api/worldbook")
async def get_worldbook():
    return load_worldbook()

@router.put("/api/worldbook")
async def update_worldbook(body: WorldBookUpdate):
    save_worldbook({"ai_persona": body.ai_persona, "user_persona": body.user_persona,
                    "ai_name": body.ai_name, "user_name": body.user_name})
    return {"ok": True}

# ── 聊天状态 ──────────────────────────────────────
@router.get("/api/chat_status")
async def get_chat_status_api():
    return load_chat_status()


def _render_current_context_status() -> str:
    from app.chat.worldbook import resolve_worldbook_names
    from context_delivery_runtime_readers import render_current_context_delivery

    user_name, ai_name = resolve_worldbook_names(load_worldbook())
    return render_current_context_delivery(user_name=user_name, ai_name=ai_name)


@router.get("/api/context_delivery/current")
async def get_current_context_delivery_api():
    """Return the maintained context snapshot for the monitor page."""

    from app.context_delivery import SCHEMA_VERSION

    return {
        "status": _render_current_context_status(),
        "generated_at": time.time(),
        "source": SCHEMA_VERSION,
    }

# ── TTS 语音合成 ──────────────────────────────────
class TTSRequest(BaseModel):
    text: str
    voice: str = ""
    msg_id: Optional[str] = None

@router.post("/api/tts")
async def tts_synthesize(body: TTSRequest):
    key = get_key("siliconflow")
    if not key:
        return Response(content=json.dumps({"error": "未配置硅基流动 API Key"}), status_code=400, media_type="application/json")
    if not body.text.strip():
        return Response(content=json.dumps({"error": "文本不能为空"}), status_code=400, media_type="application/json")
    if not body.voice:
        return Response(content=json.dumps({"error": "未选择语音"}), status_code=400, media_type="application/json")
    model = "FunAudioLLM/CosyVoice2-0.5B"
    path = "/audio/speech"
    request_id = new_request_id("tts")
    start = time.perf_counter()
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(
                f"{SILICONFLOW_BASE}{path}",
                headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
                json={
                    "model": model,
                    "input": body.text.strip(),
                    "voice": body.voice,
                    "response_format": "mp3",
                    "speed": 1.0,
                    "gain": 0
                }
            )
        if resp.status_code != 200:
            error_type, retryable = classify_http_status(resp.status_code)
            _record_sf_aux_provider(
                "tts:speech", request_id, start,
                ok=False, model=model, path=path,
                http_status=resp.status_code, error_type=error_type,
                retryable=retryable, message=f"HTTP {resp.status_code}",
                meta={"input_chars": len(body.text.strip()), "voice_selected": bool(body.voice)},
            )
            return Response(content=json.dumps({"error": f"TTS API 错误: {resp.status_code}"}), status_code=502, media_type="application/json")
        audio_data = resp.content
        _record_sf_aux_provider(
            "tts:speech", request_id, start,
            ok=True, model=model, path=path,
            http_status=resp.status_code,
            meta={
                "input_chars": len(body.text.strip()),
                "voice_selected": bool(body.voice),
                "audio_bytes": len(audio_data),
            },
        )
        # 如果提供了 msg_id，将音频缓存到服务器
        if body.msg_id:
            import re
            safe_id = re.sub(r'[^a-zA-Z0-9_\-]', '', body.msg_id)
            if safe_id:
                cache_path = TTS_CACHE_DIR / f"{safe_id}.mp3"
                cache_path.write_bytes(audio_data)
        return Response(content=audio_data, media_type="audio/mpeg")
    except Exception as e:
        _record_sf_aux_exception(
            "tts:speech", request_id, start,
            model=model, path=path, exc=e,
            meta={"input_chars": len(body.text.strip()), "voice_selected": bool(body.voice)},
        )
        return Response(content=json.dumps({"error": str(e)}), status_code=500, media_type="application/json")

@router.get("/api/tts/audio/{msg_id}")
async def tts_audio(msg_id: str):
    import re
    safe_id = re.sub(r'[^a-zA-Z0-9_\-]', '', msg_id)
    if not safe_id:
        return Response(status_code=404)
    cache_path = TTS_CACHE_DIR / f"{safe_id}.mp3"
    if not cache_path.exists():
        return Response(status_code=404)
    return FileResponse(cache_path, media_type="audio/mpeg", filename=f"{safe_id}.mp3")

# ── 端点池管理 ────────────────────────────────────
def _mask_key(k: str) -> str:
    if not k or len(k) < 8:
        return k
    return k[:4] + "*" * (len(k) - 8) + k[-4:]

def _sanitize_endpoint_out(ep: dict) -> dict:
    """返回前端的版本：key 仅展示掩码，避免 WS/截图意外泄漏。"""
    out = dict(ep)
    out["api_key_masked"] = _mask_key(ep.get("api_key", ""))
    out.pop("api_key", None)
    return out


def _endpoint_usage_map() -> dict:
    used: dict[str, list[str]] = {}
    for slot_name, slot in SETTINGS.get("slots", {}).items():
        endpoint_id = slot.get("endpoint")
        if endpoint_id:
            used.setdefault(endpoint_id, []).append(f"slot:{slot_name}")
    for model_key, model_cfg in SETTINGS.get("user_models", {}).items():
        endpoint_id = model_cfg.get("endpoint")
        if endpoint_id:
            used.setdefault(endpoint_id, []).append(f"user_model:{model_key}")
    return used


@router.get("/api/endpoints")
async def list_endpoints():
    summary = {
        item.get("endpoint_id"): item
        for item in summarize_provider_events()
        if item.get("endpoint_id")
    }
    usage = _endpoint_usage_map()
    out = []
    for ep in SETTINGS.get("endpoints", []):
        item = _sanitize_endpoint_out(ep)
        item["provider_status"] = summary.get(ep.get("id"))
        item["used_by"] = usage.get(ep.get("id"), [])
        out.append(item)
    return out

class EndpointUpsert(BaseModel):
    id: str
    name: str
    base_url: str
    type: str = "openai"
    api_key: Optional[str] = None  # 为 None 表示保留原值
    proxy_url: Optional[str] = None
    timeout_sec: Optional[float] = None

@router.put("/api/endpoints")
async def upsert_endpoint(body: EndpointUpsert):
    if not body.id or not re.match(r"^[a-zA-Z0-9_\-]+$", body.id):
        return Response(content=json.dumps({"error": "id 仅允许字母数字和 _-"}),
                        status_code=400, media_type="application/json")
    eps = SETTINGS.setdefault("endpoints", [])
    existing = next((e for e in eps if e.get("id") == body.id), None)
    endpoint_type = (body.type or "openai").strip().lower()
    if existing:
        existing["name"] = body.name
        existing["base_url"] = body.base_url.rstrip("/")
        existing["type"] = endpoint_type
        existing["proxy_url"] = (body.proxy_url or "").strip()
        existing["timeout_sec"] = body.timeout_sec
        if body.api_key is not None:
            existing["api_key"] = body.api_key
    else:
        eps.append({
            "id": body.id, "name": body.name,
            "base_url": body.base_url.rstrip("/"),
            "type": endpoint_type,
            "api_key": body.api_key or "",
            "proxy_url": (body.proxy_url or "").strip(),
            "timeout_sec": body.timeout_sec,
        })
    save_settings(SETTINGS)
    return {"ok": True}

@router.delete("/api/endpoints/{endpoint_id}")
async def delete_endpoint(endpoint_id: str):
    eps = SETTINGS.get("endpoints", [])
    # 若被 slots 或 user_models 占用则拒绝删除
    used_by = []
    for slot_name, slot in SETTINGS.get("slots", {}).items():
        if slot.get("endpoint") == endpoint_id:
            used_by.append(f"slot:{slot_name}")
    for mk, mv in SETTINGS.get("user_models", {}).items():
        if mv.get("endpoint") == endpoint_id:
            used_by.append(f"user_model:{mk}")
    if used_by:
        return Response(
            content=json.dumps({"error": f"端点被占用：{','.join(used_by)}，请先解绑"}),
            status_code=400, media_type="application/json")
    SETTINGS["endpoints"] = [e for e in eps if e.get("id") != endpoint_id]
    save_settings(SETTINGS)
    return {"ok": True}

class EndpointTestRequest(BaseModel):
    id: str
    model: Optional[str] = None

@router.post("/api/endpoints/test")
async def test_endpoint_api(body: EndpointTestRequest):
    ep = get_endpoint(body.id)
    if not ep:
        return {
            "ok": False,
            "message": "端点不存在",
            "status_code": None,
            "error_type": "endpoint_not_found",
            "latency_ms": 0,
            "request_id": new_request_id("cfg"),
        }
    return await _test_endpoint(ep, body.model)


@router.get("/api/provider/events")
async def provider_events(limit: int = 50):
    return recent_provider_events(limit)


@router.get("/api/provider/summary")
async def provider_summary():
    usage = _endpoint_usage_map()
    out = []
    for item in summarize_provider_events():
        item = dict(item)
        item["used_by"] = usage.get(item.get("endpoint_id"), [])
        out.append(item)
    return out


@router.get("/api/location/diagnostics/events")
async def location_diagnostic_events(limit: int = 50):
    return recent_location_events(limit)


@router.get("/api/location/diagnostics/summary")
async def location_diagnostic_summary():
    return summarize_location_events()

# ── Slot 绑定（sentinel / asr ...）────────────────
@router.get("/api/slots")
async def get_slots():
    return SETTINGS.get("slots", {})

class SlotUpdate(BaseModel):
    name: str
    endpoint: str
    model: str
    extras: Optional[dict] = None  # 比如 asr 的 path

@router.put("/api/slots")
async def put_slot(body: SlotUpdate):
    slots = SETTINGS.setdefault("slots", {})
    slot = {"endpoint": body.endpoint, "model": body.model}
    if body.extras:
        slot.update(body.extras)
    slots[body.name] = slot
    save_settings(SETTINGS)
    return {"ok": True}

# ── 用户自定义主脑模型 ────────────────────────────
@router.get("/api/user_models")
async def get_user_models():
    return SETTINGS.get("user_models", {})

class UserModelUpsert(BaseModel):
    key: str
    endpoint: str
    model: str
    audio_input: bool = False

@router.put("/api/user_models")
async def put_user_model(body: UserModelUpsert):
    ums = SETTINGS.setdefault("user_models", {})
    ums[body.key] = {
        "endpoint": body.endpoint,
        "model": body.model,
        "audio_input": body.audio_input is True,
    }
    save_settings(SETTINGS)
    return {"ok": True}

@router.delete("/api/user_models/{key}")
async def del_user_model(key: str):
    ums = SETTINGS.get("user_models", {})
    ums.pop(key, None)
    save_settings(SETTINGS)
    return {"ok": True}

# ── AI 行为 prompt ────────────────────────────────
@router.get("/api/ai_behavior")
async def get_ai_behavior():
    return {"current": load_ai_behavior(), "defaults": DEFAULT_AI_BEHAVIOR}

class AIBehaviorUpdate(BaseModel):
    heart_whisper_prompt: Optional[str] = None
    sentinel_call_core_criteria: Optional[str] = None
    sentinel_v2_provider_enabled: Optional[bool] = None
    sentinel_v2_provider_shadow_enabled: Optional[bool] = None
    sentinel_v2_full_wake_enabled: Optional[bool] = None
    sentinel_v2_full_wake_legacy_fallback_enabled: Optional[bool] = None
    opportunity_enabled: Optional[bool] = None
    presence_summon_enabled: Optional[bool] = None
    presence_long_duration_enabled: Optional[bool] = None
    night_round_enabled: Optional[bool] = None
    night_round_start: Optional[str] = None
    night_round_end: Optional[str] = None
    web_search_enabled: Optional[bool] = None
    context_delivery_chat_enabled: Optional[bool] = None
    context_delivery_autonomous_enabled: Optional[bool] = None
    context_trigger_shadow_enabled: Optional[bool] = None
    working_model_reflection_enabled: Optional[bool] = None
    tool_result_feedback_enabled: Optional[bool] = None
    tool_ledger_snapshot_max_bytes: Optional[int] = None
    tool_ledger_retention_days: Optional[int] = None
    opportunity_intervals_min: Optional[list[int]] = None

@router.put("/api/ai_behavior")
async def put_ai_behavior(body: AIBehaviorUpdate):
    cur = load_ai_behavior()
    if body.heart_whisper_prompt is not None:
        cur["heart_whisper_prompt"] = body.heart_whisper_prompt
    if body.sentinel_call_core_criteria is not None:
        cur["sentinel_call_core_criteria"] = body.sentinel_call_core_criteria
    if body.sentinel_v2_provider_enabled is not None:
        cur["sentinel_v2_provider_enabled"] = body.sentinel_v2_provider_enabled
        cur["sentinel_v2_provider_shadow_enabled"] = body.sentinel_v2_provider_enabled
    if body.sentinel_v2_provider_shadow_enabled is not None:
        cur["sentinel_v2_provider_enabled"] = body.sentinel_v2_provider_shadow_enabled
        cur["sentinel_v2_provider_shadow_enabled"] = body.sentinel_v2_provider_shadow_enabled
    if body.sentinel_v2_full_wake_enabled is not None:
        cur["sentinel_v2_full_wake_enabled"] = body.sentinel_v2_full_wake_enabled
    if body.sentinel_v2_full_wake_legacy_fallback_enabled is not None:
        cur["sentinel_v2_full_wake_legacy_fallback_enabled"] = body.sentinel_v2_full_wake_legacy_fallback_enabled
    if body.opportunity_enabled is not None:
        cur["opportunity_enabled"] = body.opportunity_enabled
    if body.presence_summon_enabled is not None:
        cur["presence_summon_enabled"] = body.presence_summon_enabled
    if body.presence_long_duration_enabled is not None:
        cur["presence_long_duration_enabled"] = body.presence_long_duration_enabled
    if body.night_round_enabled is not None:
        cur["night_round_enabled"] = body.night_round_enabled
    if body.night_round_start is not None or body.night_round_end is not None:
        night_start = (
            str(body.night_round_start).strip()
            if body.night_round_start is not None
            else str(cur.get("night_round_start") or "02:00").strip()
        )
        night_end = (
            str(body.night_round_end).strip()
            if body.night_round_end is not None
            else str(cur.get("night_round_end") or "05:00").strip()
        )
        hhmm = re.compile(r"^(?:[01]\d|2[0-3]):[0-5]\d$")
        if not hhmm.fullmatch(night_start) or not hhmm.fullmatch(night_end):
            raise HTTPException(status_code=422, detail="invalid_night_round_time")
        if night_start == night_end:
            raise HTTPException(status_code=422, detail="night_round_window_empty")
        if body.night_round_start is not None:
            cur["night_round_start"] = night_start
        if body.night_round_end is not None:
            cur["night_round_end"] = night_end
    if body.web_search_enabled is not None:
        cur["web_search_enabled"] = body.web_search_enabled
    if body.context_delivery_chat_enabled is not None:
        cur["context_delivery_chat_enabled"] = body.context_delivery_chat_enabled
    if body.context_delivery_autonomous_enabled is not None:
        cur["context_delivery_autonomous_enabled"] = body.context_delivery_autonomous_enabled
    if body.context_trigger_shadow_enabled is not None:
        cur["context_trigger_shadow_enabled"] = body.context_trigger_shadow_enabled
    if body.working_model_reflection_enabled is not None:
        cur["working_model_reflection_enabled"] = body.working_model_reflection_enabled
    if body.tool_result_feedback_enabled is not None:
        cur["tool_result_feedback_enabled"] = body.tool_result_feedback_enabled
    if body.tool_ledger_snapshot_max_bytes is not None:
        cur["tool_ledger_snapshot_max_bytes"] = max(
            1024,
            int(body.tool_ledger_snapshot_max_bytes),
        )
    if body.tool_ledger_retention_days is not None:
        cur["tool_ledger_retention_days"] = max(
            1,
            int(body.tool_ledger_retention_days),
        )
    if body.opportunity_intervals_min is not None:
        cur["opportunity_intervals_min"] = list(body.opportunity_intervals_min)
    save_ai_behavior(cur)
    return {"ok": True}

# ── 配置健康检查（首次启动引导用）────────────────
@router.get("/api/settings/health")
async def settings_health():
    """快速诊断关键配置是否可用；UI 用来弹首次引导横幅。
    返回 ok=False + issues[] 时，前端应提示去设置页。"""
    issues = []

    # 1. 至少一个端点有 key（考虑 env 覆盖）
    endpoints = SETTINGS.get("endpoints", [])
    if not endpoints:
        issues.append({"code": "no_endpoints", "msg": "尚未配置任何 API 端点"})
    else:
        # 只要有一个端点能取到 key 就算通过
        has_any_key = any(
            (get_endpoint(ep["id"]) or {}).get("api_key") for ep in endpoints
        )
        if not has_any_key:
            issues.append({"code": "no_keys", "msg": "所有端点都缺 API Key"})

    # 2. 硅基流动 key（记忆/哨兵/TTS/ASR 全依赖它）
    sf_key = get_key("siliconflow")
    if not sf_key:
        issues.append({"code": "no_siliconflow", "msg": "缺少硅基流动 API Key（记忆/语音/分析功能需要）"})

    # 3. 槽位绑定到了有效端点
    slots = SETTINGS.get("slots", {})
    ep_ids = {ep["id"] for ep in endpoints}
    # relational_card_generation is an already-deployed, separately owned
    # health slot.  Working Model only adds its conditional gate requirement.
    required_slots = ["sentinel", "memory_digest", "relational_card_generation", "asr"]
    if load_ai_behavior().get("working_model_v2_write_enabled", False):
        required_slots.append("working_model_gate")
    for slot_name in required_slots:
        slot = slots.get(slot_name)
        if not slot:
            issues.append({"code": f"slot_{slot_name}_missing", "msg": f"{slot_name} 槽位未配置"})
        elif slot.get("endpoint") not in ep_ids:
            issues.append({"code": f"slot_{slot_name}_bad_endpoint",
                           "msg": f"{slot_name} 绑定的端点不存在"})
        elif slot_name == "working_model_gate" and not str(slot.get("model") or "").strip():
            issues.append({"code": "slot_working_model_gate_missing_model",
                           "msg": "working_model_gate 槽位未配置模型"})
        elif slot_name == "working_model_gate" and not (
            (get_endpoint(str(slot.get("endpoint") or "")) or {}).get("api_key")
        ):
            issues.append({"code": "slot_working_model_gate_missing_key",
                           "msg": "working_model_gate 绑定的端点缺少 API Key"})

    return {"ok": not issues, "issues": issues}


@router.get("/api/tts/voices")
async def tts_voice_list():
    key = get_key("siliconflow")
    if not key:
        return {"voices": [], "error": "未配置硅基流动 API Key"}
    model = "FunAudioLLM/CosyVoice2-0.5B"
    path = "/audio/voice/list"
    request_id = new_request_id("tts")
    start = time.perf_counter()
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(
                f"{SILICONFLOW_BASE}{path}",
                headers={"Authorization": f"Bearer {key}"}
            )
        if resp.status_code != 200:
            error_type, retryable = classify_http_status(resp.status_code)
            _record_sf_aux_provider(
                "tts:voices", request_id, start,
                ok=False, model=model, path=path,
                http_status=resp.status_code, error_type=error_type,
                retryable=retryable, message=f"HTTP {resp.status_code}",
            )
            return {"voices": [], "error": "获取音色列表失败"}
        data = resp.json()
        voices = data.get("result") or data.get("voices") or data.get("data") or []
        _record_sf_aux_provider(
            "tts:voices", request_id, start,
            ok=True, model=model, path=path,
            http_status=resp.status_code,
            meta={"voices_count": len(voices) if isinstance(voices, list) else 0},
        )
        return {"voices": voices}
    except Exception as e:
        _record_sf_aux_exception(
            "tts:voices", request_id, start,
            model=model, path=path, exc=e,
        )
        return {"voices": [], "error": str(e)}
