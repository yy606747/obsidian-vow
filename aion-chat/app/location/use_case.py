from __future__ import annotations

import time
import sys
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from .places import Place
from .service import LocationService


LOCATION_STALE_SEC = 30 * 60
LOCATION_ENRICHMENT_TTL_SEC = 20 * 60


def _noop_record_event(event: dict) -> dict:
    return dict(event)


@dataclass(frozen=True)
class LocationUseCasePorts:
    load_config: Callable[[], dict]
    load_status: Callable[[], dict]
    save_status: Callable[[dict], None]
    load_places: Callable[[Any], list[Place]]
    places_path: Any
    is_quiet_hours: Callable[[], bool]
    regeo: Callable[[float, float, str], Awaitable[dict | None]]
    weather: Callable[[str, str], Awaitable[dict]]
    poi_search: Callable[[float, float, str, str, int], Awaitable[list]]
    set_status_line: Callable[[str, str], str]
    publish_location_update: Callable[[dict], Awaitable[None]]
    publish_chat_status: Callable[[str, float], Awaitable[None]]
    load_worldbook: Callable[[], dict]
    record_transition_log: Callable[[dict], Awaitable[None]]
    request_sentinel_evaluation: Callable[[], None]
    record_event: Callable[[dict], dict] = _noop_record_event
    now: Callable[[], float] = time.time


class LocationUseCase:
    def __init__(self, *, ports: LocationUseCasePorts, service: LocationService | None = None):
        self._ports = ports
        self._service = service or LocationService()

    async def receive_heartbeat(
        self,
        lng: float,
        lat: float,
        accuracy: float = 0.0,
        is_gcj02: bool = False,
        *,
        provider: str | None = None,
        location_age_ms: float | None = None,
        is_mock: bool | None = None,
    ) -> dict:
        heartbeat_received_at = self._ports.now()
        cfg = self._ports.load_config()
        old_status = self._ports.load_status()
        places = self._load_places_for_config(cfg)
        home_place = self._home_place_for_distance(places, cfg)
        observed_at = observed_location_time(heartbeat_received_at, location_age_ms)
        result = self._service.process_fix(
            lng=lng,
            lat=lat,
            accuracy_m=accuracy,
            is_gcj02=is_gcj02,
            places=places,
            previous_status=old_status,
            now=observed_at,
            home_place=home_place,
        )
        if home_place is not None:
            # These are the exact hysteresis radii used by the location
            # matcher. Do not substitute the legacy display threshold here.
            result["configured_enter_m"] = float(home_place.enter_m)
            result["configured_exit_m"] = float(home_place.exit_m)
        new_status = result.pop("status")
        new_status["heartbeat_received_at"] = heartbeat_received_at
        if provider is not None:
            new_status["provider"] = provider
        if location_age_ms is not None:
            new_status["location_age_ms"] = max(0.0, float(location_age_ms))
        if is_mock is not None:
            new_status["is_mock"] = bool(is_mock)

        significant_move = significant_location_move(cfg, result)
        moved_or_changed = bool(result.get("state_changed") or significant_move)
        if moved_or_changed:
            clear_stale_location_enrichment(new_status)
            self._record_event(
                "location:enrichment_cleared",
                meta={
                    **self._state_meta(result, new_status),
                    "reason": "state_changed" if result.get("state_changed") else "significant_move",
                },
            )

        self._ports.save_status(new_status)
        freshness = location_freshness(new_status, now=heartbeat_received_at)
        await self._publish_location_update(result, new_status, heartbeat_received_at, freshness, phase="base_status")
        if moved_or_changed:
            await self._refresh_location_status_line(result, new_status)
        if result.get("state_changed"):
            await self._announce_location_transition(result, new_status)

        enrichment_trace: dict = {}
        enrichment = await self._maybe_refresh_location_enrichment(
            cfg,
            old_status,
            new_status,
            result,
            trace=enrichment_trace,
        )
        if enrichment:
            current = self._ports.load_status()
            if is_same_fix(current, new_status):
                enrichment_trace["result"] = "applied"
                current.update(enrichment)
                self._ports.save_status(current)
                new_status = current
                freshness = location_freshness(new_status, now=heartbeat_received_at)
                self._record_event(
                    "location:enrichment_applied",
                    meta={
                        **self._state_meta(result, new_status, freshness),
                        **enrichment_meta(enrichment),
                    },
                )
                await self._publish_location_update(
                    result,
                    new_status,
                    heartbeat_received_at,
                    freshness,
                    phase="enriched_status",
                )
                if moved_or_changed and enrichment.get("address"):
                    await self._refresh_location_status_line(result, new_status)
            else:
                enrichment_trace["result"] = "discarded"
                enrichment_trace["reason"] = "newer_heartbeat_won"
                self._record_event(
                    "location:enrichment_discarded",
                    error_type="newer_heartbeat_won",
                    message="Location enrichment result was discarded because a newer heartbeat was saved.",
                    meta={
                        **self._state_meta(result, new_status),
                        "reason": "newer_heartbeat_won",
                        "current_heartbeat_received_at": rounded_float(current.get("heartbeat_received_at")),
                        "new_status_heartbeat_received_at": rounded_float(new_status.get("heartbeat_received_at")),
                    },
                )

        result.update({
            "address": new_status.get("address", ""),
            "weather": new_status.get("weather", {}),
            "nearby_pois": new_status.get("nearby_pois", {}),
            "provider": new_status.get("provider", ""),
            "location_age_ms": new_status.get("location_age_ms"),
            "is_mock": new_status.get("is_mock"),
            "heartbeat_received_at": heartbeat_received_at,
            **freshness,
        })
        self._record_event(
            "location:heartbeat_summary",
            meta={
                **self._state_meta(result, new_status, freshness),
                "places_count": len(places),
                "config_enabled": bool(cfg.get("enabled")),
                "significant_move": significant_move,
                "moved_or_changed": moved_or_changed,
                "is_gcj02": bool(is_gcj02),
                "enrichment_result": enrichment_trace.get("result", "unknown"),
                "enrichment_reason": enrichment_trace.get("reason", ""),
                "enrichment_stage": enrichment_trace.get("stage", ""),
                "enrichment_refresh_pois": bool(enrichment_trace.get("refresh_pois")),
            },
        )
        return result

    async def _publish_location_update(
        self,
        result: dict,
        status: dict,
        heartbeat_received_at: float,
        freshness: dict,
        *,
        phase: str,
    ) -> None:
        try:
            await self._ports.publish_location_update({
                "state": result["state"],
                "lng": result["lng"],
                "lat": result["lat"],
                "accuracy": result["accuracy"],
                "address": status.get("address", ""),
                "distance_from_home": result["distance_from_home"],
                "weather": status.get("weather", {}),
                "nearby_pois": status.get("nearby_pois") or None,
                "updated_at": status.get("updated_at", 0),
                "state_changed": result["state_changed"],
                "v2_state": result["v2_state"],
                "provider": status.get("provider", ""),
                "location_age_ms": status.get("location_age_ms"),
                "is_mock": status.get("is_mock"),
                "heartbeat_received_at": heartbeat_received_at,
                **freshness,
            })
        except Exception as exc:
            self._record_event(
                "location:broadcast_failed",
                ok=False,
                error_type="exception",
                message=exception_message(exc),
                meta={**self._state_meta(result, status, freshness), "phase": phase},
            )
            raise

    async def _refresh_location_status_line(self, result: dict, status: dict) -> None:
        merged_status = self._ports.set_status_line(
            "[位置]",
            build_chat_status_location_line(
                result.get("state"),
                status.get("address", ""),
                result.get("distance_from_home", -1),
            ),
        )
        try:
            await self._ports.publish_chat_status(merged_status, self._ports.now())
        except Exception as exc:
            self._record_event(
                "location:chat_status_broadcast_failed",
                ok=False,
                error_type="exception",
                message=exception_message(exc),
                meta=self._state_meta(result, status),
            )
            raise
        self._record_event(
            "location:chat_status_broadcast_sent",
            meta=self._state_meta(result, status),
        )

    async def _announce_location_transition(self, result: dict, status: dict) -> None:
        new_state = result.get("state")
        address = status.get("address", "")
        user_name = self._ports.load_worldbook().get("user_name", "你")
        if new_state == "outside":
            event_desc = f"{user_name}离开家外出了"
            if address:
                event_desc += f"，当前位置：{address}"
        elif new_state == "at_home":
            event_desc = f"{user_name}回到家了"
        else:
            event_desc = f"{user_name}的位置状态变为 {new_state}"

        now = self._ports.now()
        log_entry = {
            "timestamp": now,
            "time": time.strftime("%H:%M:%S", time.localtime(now)),
            "date": time.strftime("%Y-%m-%d", time.localtime(now)),
            "monitoringlog": f"📍 {event_desc}",
            "summary": "",
            "score": None,
            "call_core": False,
            "core_reason": "",
            "screenshot": "",
            "source": "location",
        }
        await self._ports.record_transition_log(log_entry)
        self._ports.request_sentinel_evaluation()
        self._record_event(
            "location:transition_announced",
            meta={
                **self._state_meta(result, status),
                "transition_state": new_state or "",
                "monitor_log_has_address": bool(address),
            },
        )

    async def _maybe_refresh_location_enrichment(
        self,
        cfg: dict,
        old_status: dict,
        new_status: dict,
        result: dict,
        trace: dict,
    ) -> dict | None:
        amap_key = str(cfg.get("amap_key") or "").strip()
        if not amap_key:
            self._record_enrichment_skip("missing_amap_key", trace)
            return None
        if self._ports.is_quiet_hours():
            self._record_enrichment_skip("quiet_hours", trace)
            return None
        freshness = location_freshness(new_status, now=self._ports.now())
        if freshness["location_stale"]:
            self._record_enrichment_skip("stale_location", trace)
            return None
        if not self._should_refresh_location_enrichment(cfg, old_status, new_status, result):
            self._record_enrichment_skip("not_needed", trace)
            return None

        refresh_pois = result.get("state") == "outside" and self._should_refresh_pois(cfg, old_status, result)
        poi_types = cfg.get("poi_types") or {}
        trace.update({"result": "started", "refresh_pois": bool(refresh_pois)})
        self._record_event(
            "location:enrichment_started",
            meta={
                **self._state_meta(result, new_status, freshness),
                "refresh_pois": bool(refresh_pois),
                "poi_category_count": len([type_code for type_code in poi_types.values() if type_code]),
            },
        )
        start = time.perf_counter()
        changed: dict = {}
        try:
            geo_info = await self._ports.regeo(new_status["lng"], new_status["lat"], amap_key)
        except Exception as exc:
            trace.update({"result": "failed", "stage": "regeo"})
            self._record_event(
                "location:enrichment_failed",
                ok=False,
                error_type="exception",
                retryable=True,
                elapsed_ms=elapsed_ms_since(start),
                message=exception_message(exc),
                meta={**self._state_meta(result, new_status, freshness), "stage": "regeo"},
            )
            raise
        enriched_at = self._ports.now()
        if geo_info:
            changed.update({
                "address": geo_info.get("address", ""),
                "adcode": geo_info.get("adcode", ""),
                "province": geo_info.get("province", ""),
                "city": geo_info.get("city", ""),
                "district": geo_info.get("district", ""),
                "address_updated_at": enriched_at,
            })
            if geo_info.get("adcode"):
                try:
                    weather_data = await self._ports.weather(geo_info["adcode"], amap_key)
                except Exception as exc:
                    trace.update({"result": "failed", "stage": "weather"})
                    self._record_event(
                        "location:enrichment_failed",
                        ok=False,
                        error_type="exception",
                        retryable=True,
                        elapsed_ms=elapsed_ms_since(start),
                        message=exception_message(exc),
                        meta={**self._state_meta(result, new_status, freshness), "stage": "weather"},
                    )
                    raise
                changed["weather"] = weather_data.get("live", {})
                changed["forecast"] = weather_data.get("forecast", [])
                changed["weather_updated_at"] = self._ports.now()

        if refresh_pois:
            refreshed: dict[str, list] = {}
            for category, type_code in poi_types.items():
                if not type_code:
                    continue
                try:
                    refreshed[category] = await self._ports.poi_search(
                        new_status["lng"],
                        new_status["lat"],
                        type_code,
                        amap_key,
                        cfg.get("poi_radius", 2000),
                    )
                except Exception as exc:
                    trace.update({"result": "failed", "stage": "poi_search"})
                    self._record_event(
                        "location:enrichment_failed",
                        ok=False,
                        error_type="exception",
                        retryable=True,
                        elapsed_ms=elapsed_ms_since(start),
                        message=exception_message(exc),
                        meta={
                            **self._state_meta(result, new_status, freshness),
                            "stage": "poi_search",
                            "category": category,
                        },
                    )
                    raise
            if refreshed:
                changed["nearby_pois"] = refreshed
                changed["enriched_at"] = self._ports.now()

        trace.update({
            "result": "finished" if changed else "empty",
            "changed_fields": enrichment_meta(changed)["changed_fields"],
        })
        self._record_event(
            "location:enrichment_finished" if changed else "location:enrichment_empty",
            empty_result=not bool(changed),
            elapsed_ms=elapsed_ms_since(start),
            meta={
                **self._state_meta(result, new_status, freshness),
                **enrichment_meta(changed),
                "refresh_pois": bool(refresh_pois),
            },
        )
        return changed or None

    def _record_enrichment_skip(
        self,
        reason: str,
        trace: dict,
    ) -> None:
        trace.update({"result": "skipped", "reason": reason})

    def _should_refresh_location_enrichment(
        self,
        cfg: dict,
        old_status: dict,
        new_status: dict,
        result: dict,
    ) -> bool:
        if not new_status.get("address"):
            return True
        if result.get("state_changed"):
            return True
        if significant_location_move(cfg, result):
            return True
        address_updated_at = float(old_status.get("address_updated_at") or 0)
        if address_updated_at <= 0:
            return False
        return self._ports.now() - address_updated_at > LOCATION_ENRICHMENT_TTL_SEC

    def _should_refresh_pois(self, cfg: dict, old_status: dict, result: dict) -> bool:
        if result.get("state_changed"):
            return True
        if significant_location_move(cfg, result):
            return True
        enriched_at = float(old_status.get("enriched_at") or 0)
        if not enriched_at:
            return True
        return self._ports.now() - enriched_at > LOCATION_ENRICHMENT_TTL_SEC

    def _load_places_for_config(self, cfg: dict) -> list[Place]:
        places = self._ports.load_places(self._ports.places_path)
        home = legacy_home_place(cfg)
        if home and not any(place.id == home.id for place in places):
            places = [home, *places]
        return sorted(places, key=lambda place: place.enter_m)

    def _home_place_for_distance(self, places: list[Place], cfg: dict) -> Place | None:
        for place in places:
            if place.id == "home" or place.kind == "home":
                return place
        return legacy_home_place(cfg)

    def _record_event(
        self,
        scope: str,
        *,
        ok: bool = True,
        error_type: str = "ok",
        retryable: bool = False,
        empty_result: bool = False,
        elapsed_ms: float = 0.0,
        message: str = "",
        meta: dict | None = None,
    ) -> None:
        try:
            self._ports.record_event({
                "scope": scope,
                "ok": ok,
                "error_type": error_type,
                "retryable": retryable,
                "empty_result": empty_result,
                "elapsed_ms": elapsed_ms,
                "message": message,
                "meta": meta or {},
            })
        except Exception as exc:
            print(
                f"[LocationDiagnostics] record_event_failed scope={scope} error={exception_message(exc)}",
                file=sys.stderr,
            )

    def _state_meta(self, result: dict, status: dict, freshness: dict | None = None) -> dict:
        v2_state = result.get("v2_state") or status.get("v2_state") or {}
        meta = {
            "state": result.get("state") or status.get("state") or "unknown",
            "old_state": result.get("old_state") or "",
            "state_changed": bool(result.get("state_changed")),
            "v2_state_changed": bool(result.get("v2_state_changed")),
            "old_place_id": result.get("v2_old_place_id") or "",
            "new_place_id": result.get("v2_new_place_id") or v2_state.get("place_id") or "",
            "place_kind": v2_state.get("place_kind") or "",
            "distance_from_home_m": rounded_float(result.get("distance_from_home")),
            "moved_distance_m": rounded_float(result.get("moved_distance")),
            "accuracy_m": rounded_float(result.get("accuracy") or status.get("accuracy")),
            "provider": status.get("provider", ""),
            "location_age_ms": rounded_float(status.get("location_age_ms")),
            "has_location_age_ms": status.get("location_age_ms") is not None,
            "is_mock": status.get("is_mock"),
            "has_address": bool(status.get("address")),
            "has_weather": bool(status.get("weather")),
            "poi_category_count": len(status.get("nearby_pois") or {}),
        }
        if freshness:
            meta.update({
                "location_age_sec": rounded_float(freshness.get("location_age_sec")),
                "location_stale": bool(freshness.get("location_stale")),
                "prompt_usable": bool(freshness.get("prompt_usable")),
                "address_stale": bool(freshness.get("address_stale")),
                "poi_stale": bool(freshness.get("poi_stale")),
            })
        return meta


def legacy_home_place(cfg: dict) -> Place | None:
    lng = float(cfg.get("home_lng", 0) or 0)
    lat = float(cfg.get("home_lat", 0) or 0)
    if lng == 0 and lat == 0:
        return None
    enter_m = float(cfg.get("home_threshold", 500) or 500)
    return Place(
        id="home",
        name="家",
        kind="home",
        lat=lat,
        lng=lng,
        enter_m=enter_m,
        exit_m=max(enter_m + 100.0, enter_m * 1.4),
    )


def observed_location_time(heartbeat_received_at: float, location_age_ms: float | None) -> float:
    if location_age_ms is None:
        return heartbeat_received_at
    try:
        age_sec = max(0.0, float(location_age_ms) / 1000.0)
    except (TypeError, ValueError):
        return heartbeat_received_at
    return max(0.0, heartbeat_received_at - age_sec)


def rounded_float(value, digits: int = 1):
    if value is None:
        return None
    try:
        return round(float(value), digits)
    except (TypeError, ValueError):
        return None


def elapsed_ms_since(start: float) -> float:
    return (time.perf_counter() - start) * 1000


def exception_message(exc: Exception) -> str:
    text = str(exc)
    if text:
        return f"{type(exc).__name__}: {text}"
    return type(exc).__name__


def enrichment_meta(enrichment: dict | None) -> dict:
    enrichment = enrichment or {}
    nearby = enrichment.get("nearby_pois") or {}
    return {
        "has_address": bool(enrichment.get("address")),
        "has_weather": bool(enrichment.get("weather")),
        "poi_category_count": len(nearby),
        "changed_fields": ",".join(sorted(str(key) for key in enrichment)),
    }


def clear_stale_location_enrichment(status: dict) -> None:
    status["address"] = ""
    status["adcode"] = ""
    status["province"] = ""
    status["city"] = ""
    status["district"] = ""
    status["weather"] = {}
    status["forecast"] = []
    status["address_updated_at"] = 0
    status["weather_updated_at"] = 0
    status["nearby_pois"] = {}
    status["enriched_at"] = 0


def is_same_fix(current: dict, new_status: dict) -> bool:
    return float(current.get("heartbeat_received_at") or 0) == float(
        new_status.get("heartbeat_received_at") or 0
    )


def build_chat_status_location_line(new_state: str, address: str, distance) -> str:
    state_label = {"at_home": "在家", "outside": "外出中"}.get(new_state, "")
    loc_line = f"[位置] {state_label}"
    if address:
        loc_line += f"，当前在：{address}"
    try:
        d = float(distance)
    except (TypeError, ValueError):
        d = -1
    if d > 0:
        d_str = f"{d / 1000:.1f}km" if d >= 1000 else f"{int(d)}m"
        loc_line += f"，距离家{d_str}"
    return loc_line


def significant_location_move(cfg: dict, result: dict) -> bool:
    try:
        moved_distance = float(result.get("moved_distance"))
    except (TypeError, ValueError):
        return True
    if moved_distance < 0:
        return True
    movement_threshold = float(cfg.get("movement_threshold", 500) or 500)
    return moved_distance >= movement_threshold


def location_freshness(
    status: dict,
    *,
    now: float | None = None,
) -> dict:
    v2_state = status.get("v2_state") or {}
    last_fix_at = float(v2_state.get("last_fix_at") or status.get("updated_at") or 0)
    address_updated_at = float(status.get("address_updated_at") or 0)
    poi_updated_at = float(status.get("enriched_at") or 0)
    reference_time = time.time() if now is None else now
    if last_fix_at <= 0:
        return {
            "location_age_sec": None,
            "location_stale": True,
            "prompt_usable": False,
            "address_age_sec": None,
            "address_stale": bool(status.get("address")),
            "poi_age_sec": None,
            "poi_stale": bool(status.get("nearby_pois")),
        }
    age_sec = max(0.0, reference_time - last_fix_at)
    stale = age_sec > LOCATION_STALE_SEC
    address_age_sec = max(0.0, reference_time - address_updated_at) if address_updated_at > 0 else None
    poi_age_sec = max(0.0, reference_time - poi_updated_at) if poi_updated_at > 0 else None
    return {
        "location_age_sec": round(age_sec, 1),
        "location_stale": stale,
        "prompt_usable": bool(v2_state.get("place_id")) and not stale,
        "address_age_sec": round(address_age_sec, 1) if address_age_sec is not None else None,
        "address_stale": bool(status.get("address")) and (
            stale
            or address_updated_at <= 0
            or (address_age_sec is not None and address_age_sec > LOCATION_ENRICHMENT_TTL_SEC)
        ),
        "poi_age_sec": round(poi_age_sec, 1) if poi_age_sec is not None else None,
        "poi_stale": bool(status.get("nearby_pois")) and (
            stale
            or poi_updated_at <= 0
            or (poi_age_sec is not None and poi_age_sec > LOCATION_ENRICHMENT_TTL_SEC)
        ),
    }
