from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from typing import Any, Mapping

from config import DATA_DIR, load_worldbook, set_chat_status_line
import location as legacy_location
from location_diagnostics import record_location_event
from ws import manager

from app.legacy_adapters.evidence import (
    record_location_heartbeat_safely,
    record_location_state_safely,
)
from app.location import LocationService, Place, load_places
from app.location.use_case import (
    LocationUseCase,
    LocationUseCasePorts,
    legacy_home_place,
    location_freshness,
)


LOCATION_PLACES_PATH = DATA_DIR / "location_places.json"


class LocationRuntime:
    """Legacy IO adapter for the Location use case.

    Routes call this adapter instead of importing old global modules directly.
    The domain/use-case layer still receives explicit ports, so this file is the
    compatibility boundary for JSON storage, WebSocket broadcasts, AMap calls,
    chat_status updates, and Sentinel wake checks.
    """

    def __init__(
        self,
        *,
        service: LocationService | None = None,
        places_path: Path = LOCATION_PLACES_PATH,
        now=time.time,
    ):
        self.service = service or LocationService()
        self.places_path = places_path
        self.now = now

    async def heartbeat_response(self, body: Mapping[str, Any]) -> dict:
        cfg = self.load_config()
        if not cfg.get("enabled") and not body.get("force"):
            self.record_use_case_event({
                "scope": "location:heartbeat_rejected",
                "ok": False,
                "error_type": "disabled",
                "message": "定位功能未启用",
                "meta": heartbeat_body_meta(body),
            })
            return {"ok": False, "error": "定位功能未启用"}

        result = await self.process_heartbeat(
            body["lng"],
            body["lat"],
            body.get("accuracy", 0.0),
            body.get("is_gcj02", False),
            provider=body.get("provider"),
            location_age_ms=body.get("location_age_ms"),
            is_mock=body.get("is_mock"),
        )
        record_location_heartbeat_safely(dict(body), result)
        if (
            result.get("v2_fix_accepted", result.get("v2_state_changed"))
            and result.get("v2_state")
            and result.get("configured_enter_m") is not None
            and result.get("configured_exit_m") is not None
        ):
            record_location_state_safely(result)
        return {"ok": True, **result}

    def record_diagnostic(self, body: Mapping[str, Any]) -> dict:
        event = clean_location_diagnostic_label(body.get("event"))
        meta = dict(body.get("meta") or {})
        if body.get("provider"):
            meta["provider"] = body.get("provider")
        if body.get("location_age_ms") is not None:
            meta["location_age_ms"] = max(0.0, float(body.get("location_age_ms")))

        recorded = record_location_event({
            "scope": f"android_location:{event}",
            "ok": bool(body.get("ok")),
            "error_type": "ok" if body.get("ok") else event,
            "retryable": bool(body.get("retryable")) if body.get("retryable") is not None else False,
            "elapsed_ms": max(0.0, float(body.get("elapsed_ms") or 0.0)),
            "message": body.get("message") or "",
            "meta": meta,
        })
        return {"ok": True, "event": recorded}

    async def process_heartbeat(
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
        return await self.build_use_case().receive_heartbeat(
            lng=lng,
            lat=lat,
            accuracy=accuracy,
            is_gcj02=is_gcj02,
            provider=provider,
            location_age_ms=location_age_ms,
            is_mock=is_mock,
        )

    def build_use_case(self) -> LocationUseCase:
        return LocationUseCase(
            service=self.service,
            ports=LocationUseCasePorts(
                load_config=self.load_config,
                load_status=self.load_status,
                save_status=self.save_status,
                load_places=self.load_places,
                places_path=self.places_path,
                is_quiet_hours=self.is_quiet_hours,
                regeo=self.regeo,
                weather=self.weather,
                poi_search=self.poi_search,
                set_status_line=self.set_status_line,
                publish_location_update=self.publish_location_update,
                publish_chat_status=self.publish_chat_status,
                load_worldbook=self.load_worldbook,
                record_transition_log=self.record_transition_log,
                request_sentinel_evaluation=self.request_sentinel_evaluation,
                record_event=self.record_use_case_event,
                now=self.now,
            ),
        )

    def status_payload(self) -> dict:
        status = self.load_status()
        cfg = self.load_config()
        return {
            "enabled": cfg.get("enabled", False),
            **status,
            **location_freshness(status, now=self.now()),
        }

    async def poi_search_response(self, *, category: str, radius: int | None = None) -> dict:
        cfg = self.load_config()
        amap_key = cfg.get("amap_key", "")
        if not amap_key:
            return {"ok": False, "error": "高德 API Key 未配置"}

        status = self.load_status()
        if status.get("state") == "unknown" or status.get("lng", 0) == 0:
            return {"ok": False, "error": "当前位置未知"}

        poi_types = cfg.get("poi_types", {})
        type_code = poi_types.get(category)
        if not type_code:
            return {"ok": False, "error": f"未知的 POI 类型: {category}", "available": list(poi_types.keys())}

        search_radius = radius or cfg.get("poi_radius", 2000)
        pois = await self.poi_search(status["lng"], status["lat"], type_code, amap_key, search_radius)
        status.setdefault("nearby_pois", {})[category] = pois
        status["enriched_at"] = self.now()
        self.save_status(status)
        return {"ok": True, "category": category, "count": len(pois), "pois": pois}

    def cached_pois_payload(self) -> dict:
        status = self.load_status()
        return {
            "state": status.get("state", "unknown"),
            "address": status.get("address", ""),
            "nearby_pois": status.get("nearby_pois", {}),
            "prompt_text": self.format_nearby_pois_for_prompt(),
        }

    def config_payload(self) -> dict:
        cfg = self.load_config()
        masked_key = mask_key(cfg.get("amap_key", ""))
        return {
            **cfg,
            "amap_key": masked_key,
            "amap_key_masked": masked_key,
            "active": cfg.get("enabled", False) and not self.is_quiet_hours(),
        }

    def update_config(self, body: Mapping[str, Any]) -> dict:
        cfg = self.load_config()
        amap_key = body.get("amap_key")
        if amap_key is not None and str(amap_key).strip() and "*" not in str(amap_key):
            cfg["amap_key"] = amap_key
        if body.get("home_lng") is not None:
            cfg["home_lng"] = body["home_lng"]
        if body.get("home_lat") is not None:
            cfg["home_lat"] = body["home_lat"]
        if body.get("home_threshold") is not None:
            cfg["home_threshold"] = max(50, body["home_threshold"])
        if body.get("heartbeat_outdoor_min") is not None:
            cfg["heartbeat_outdoor_min"] = max(1, body["heartbeat_outdoor_min"])
        if body.get("heartbeat_home_min") is not None:
            cfg["heartbeat_home_min"] = max(5, body["heartbeat_home_min"])
        if body.get("poi_radius") is not None:
            cfg["poi_radius"] = max(500, min(10000, body["poi_radius"]))
        if body.get("enabled") is not None:
            cfg["enabled"] = body["enabled"]
        if body.get("quiet_hours_enabled") is not None:
            cfg["quiet_hours_enabled"] = body["quiet_hours_enabled"]
        if body.get("quiet_hours_start") is not None:
            cfg["quiet_hours_start"] = body["quiet_hours_start"]
        if body.get("quiet_hours_end") is not None:
            cfg["quiet_hours_end"] = body["quiet_hours_end"]
        self.save_config(cfg)
        self.sync_home_place(cfg)
        return {"ok": True}

    def set_home_response(self) -> dict:
        status = self.load_status()
        if status.get("lng", 0) == 0 or status.get("lat", 0) == 0:
            return {"ok": False, "error": "当前位置未知，请先上报一次定位"}
        cfg = self.load_config()
        cfg["home_lng"] = status["lng"]
        cfg["home_lat"] = status["lat"]
        self.save_config(cfg)
        self.sync_home_place(cfg)
        return {
            "ok": True,
            "home_lng": status["lng"],
            "home_lat": status["lat"],
            "address": status.get("address", ""),
        }

    def load_config(self) -> dict:
        return legacy_location.load_location_config()

    def save_config(self, cfg: dict) -> None:
        legacy_location.save_location_config(cfg)

    def load_status(self) -> dict:
        return legacy_location.load_location_status()

    def save_status(self, status: dict) -> None:
        legacy_location.save_location_status(status)

    def load_places(self, path) -> list[Place]:
        return load_places(path)

    def is_quiet_hours(self) -> bool:
        return legacy_location.is_location_quiet_hours()

    async def regeo(self, lng: float, lat: float, key: str) -> dict | None:
        return await legacy_location.amap_regeo(lng, lat, key)

    async def weather(self, adcode: str, key: str) -> dict:
        return await legacy_location.amap_weather(adcode, key)

    async def poi_search(self, lng: float, lat: float, types: str, key: str, radius: int = 2000) -> list:
        return await legacy_location.amap_poi_search(lng, lat, types, key, radius)

    def set_status_line(self, prefix: str, line: str) -> str:
        return set_chat_status_line(prefix, line)

    async def publish_location_update(self, data: dict) -> None:
        await manager.broadcast({"type": "location_update", "data": data})

    async def publish_chat_status(self, merged_status: str, updated_at: float) -> None:
        await manager.broadcast({
            "type": "chat_status",
            "data": {"status": merged_status, "updated_at": updated_at},
        })

    def load_worldbook(self) -> dict:
        return load_worldbook()

    def record_use_case_event(self, event: dict) -> dict:
        return record_location_event(event)

    async def record_transition_log(self, log_entry: dict) -> None:
        try:
            from sentinel_runtime import append_and_broadcast_monitor_log
            await append_and_broadcast_monitor_log(log_entry)
        except Exception as exc:
            self.record_use_case_event({
                "scope": "location:transition_log_failed",
                "ok": False,
                "error_type": "exception",
                "message": exception_message(exc),
                "meta": {
                    "source": log_entry.get("source", ""),
                    "has_monitoringlog": bool(log_entry.get("monitoringlog")),
                },
            })

    def request_sentinel_evaluation(self) -> None:
        try:
            from sentinel_runtime import sentinel_runtime
            if not getattr(sentinel_runtime, "monitoring", False):
                self.record_use_case_event({
                    "scope": "location:sentinel_trigger_skipped",
                    "ok": True,
                    "error_type": "not_monitoring",
                    "meta": {"reason": "not_monitoring"},
                })
                return
            loop = getattr(sentinel_runtime, "_loop", None)
            coro = sentinel_runtime._analyze_and_log()
            from app.background_tasks import create_tracked_task, run_tracked_threadsafe
            if loop is not None and loop.is_running():
                run_tracked_threadsafe(coro, loop, name="location_sentinel_evaluation")
                scheduler = "threadsafe"
            else:
                create_tracked_task(coro, name="location_sentinel_evaluation")
                scheduler = "create_task"
            self.record_use_case_event({
                "scope": "location:sentinel_trigger_requested",
                "ok": True,
                "meta": {"scheduler": scheduler},
            })
        except Exception as exc:
            self.record_use_case_event({
                "scope": "location:sentinel_trigger_failed",
                "ok": False,
                "error_type": "exception",
                "message": exception_message(exc),
            })

    def format_nearby_pois_for_prompt(self) -> str:
        return legacy_location.format_nearby_pois_for_prompt()

    def sync_home_place(self, cfg: dict) -> None:
        home = legacy_home_place(cfg)
        if home is None:
            return
        places = [place for place in self.load_places(self.places_path) if place.id != "home"]
        places.insert(0, home)
        self.places_path.write_text(
            json.dumps([place.__dict__ for place in places], ensure_ascii=False, indent=2),
            encoding="utf-8",
        )


def clean_location_diagnostic_label(value: str | None) -> str:
    text = str(value or "unknown").strip().lower()
    cleaned = "".join(ch if ch.isalnum() or ch in "_:-." else "_" for ch in text)
    return cleaned[:80] or "unknown"


def mask_key(value: str) -> str:
    key = str(value or "")
    if not key:
        return ""
    if len(key) < 8:
        return "*" * len(key)
    return key[:4] + "*" * (len(key) - 8) + key[-4:]


def heartbeat_body_meta(body: Mapping[str, Any]) -> dict:
    return {
        "provider": body.get("provider") or "",
        "accuracy_m": rounded_float(body.get("accuracy")),
        "is_gcj02": bool(body.get("is_gcj02")),
        "has_location_age_ms": body.get("location_age_ms") is not None,
        "location_age_ms": rounded_float(body.get("location_age_ms")),
        "is_mock": body.get("is_mock"),
        "force": bool(body.get("force")),
    }


def rounded_float(value, digits: int = 1):
    if value is None:
        return None
    try:
        return round(float(value), digits)
    except (TypeError, ValueError):
        return None


def exception_message(exc: Exception) -> str:
    text = str(exc)
    if text:
        return f"{type(exc).__name__}: {text}"
    return type(exc).__name__


location_runtime = LocationRuntime()
