"""Adapters that shadow old sensing/activity/location inputs into Evidence."""

from __future__ import annotations

import logging
from typing import Any, Mapping

from app.events import EvidenceLedger, EvidenceRecord, evidence_ledger
from app.location_geofence import (
    LOCATION_GEOFENCE_EVENT_CURRENT,
    LOCATION_GEOFENCE_EVENT_TRANSITION,
    LOCATION_GEOFENCE_PAYLOAD_SCHEMA_VERSION,
    normalize_location_geofence_payload,
)


log = logging.getLogger(__name__)

_SCREEN_STATES = {
    "screen_on": "on",
    "亮屏": "on",
    "screen_off": "off",
    "锁屏": "off",
}


def _payload(data: Mapping[str, Any] | None) -> dict[str, Any]:
    return {key: value for key, value in dict(data or {}).items() if value is not None}


def _timestamp(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _confidence(value: Any, *, default: float = 1.0) -> float:
    if value is None:
        return default
    try:
        confidence = float(value)
    except (TypeError, ValueError):
        return default
    if confidence > 1.0:
        confidence = confidence / 100.0
    return max(0.0, min(1.0, confidence))


def record_sensing_entry(
    entry: Mapping[str, Any],
    *,
    ledger: EvidenceLedger = evidence_ledger,
) -> EvidenceRecord:
    entry_type = str(entry.get("type") or "unknown").strip() or "unknown"
    data = _payload(entry.get("data") if isinstance(entry.get("data"), Mapping) else {})
    confidence = _confidence(data.get("motion_confidence"))
    return ledger.record(
        kind=f"sensing.{entry_type}",
        source="android.sensing",
        observed_at=_timestamp(entry.get("timestamp")),
        confidence=confidence,
        payload=data,
        metadata={
            "legacy_entry_type": entry_type,
            "legacy_date": entry.get("date", ""),
            "legacy_time": entry.get("time", ""),
        },
    )


def record_activity_entry(
    entry: Mapping[str, Any],
    *,
    ledger: EvidenceLedger = evidence_ledger,
) -> EvidenceRecord:
    device = str(entry.get("device") or "unknown").strip() or "unknown"
    if device == "phone":
        source = "android.activity"
    elif device == "pc":
        source = "pc.activity"
    else:
        source = "legacy.activity"
    payload = {
        "device": device,
        "app": entry.get("app", ""),
        "title": entry.get("title", ""),
    }
    screen_state = _SCREEN_STATES.get(str(entry.get("app") or "").strip())
    if device in {"phone", "tablet"} and screen_state:
        payload["screen_state"] = screen_state
    if device == "pc":
        payload["active_state"] = entry.get("active_state")
        payload["last_input_age_sec"] = entry.get("last_input_age_sec")
    # 保留多设备身份，使手机/平板在 Evidence 中可区分（legacy device 太粗）。
    for field in ("device_id", "device_name", "device_type", "platform"):
        value = entry.get(field)
        if value:
            payload[field] = value
    return ledger.record(
        kind="activity.app",
        source=source,
        observed_at=_timestamp(entry.get("timestamp")),
        confidence=1.0,
        payload=_payload(payload),
        metadata={
            "legacy_date": entry.get("date", ""),
            "legacy_time": entry.get("time", ""),
            "device_id": entry.get("device_id", ""),
            "device_type": entry.get("device_type", ""),
        },
    )


def record_location_heartbeat(
    body: Mapping[str, Any],
    result: Mapping[str, Any],
    *,
    ledger: EvidenceLedger = evidence_ledger,
) -> EvidenceRecord:
    body_payload = _payload(body)
    result_payload = _payload(result)
    payload = {
        "lng": body_payload.get("lng"),
        "lat": body_payload.get("lat"),
        "accuracy": body_payload.get("accuracy", 0.0),
        "is_gcj02": body_payload.get("is_gcj02", False),
        "state": result_payload.get("state"),
        "old_state": result_payload.get("old_state"),
        "state_changed": result_payload.get("state_changed"),
        "distance_from_home": result_payload.get("distance_from_home"),
        "home_not_set": result_payload.get("home_not_set"),
        "full_api": result_payload.get("full_api"),
        "moved_distance": result_payload.get("moved_distance"),
    }
    return ledger.record(
        kind="location.fix",
        source="android.location",
        confidence=_location_confidence(body_payload),
        payload=_payload(payload),
        metadata={
            "legacy_route": "/api/location/heartbeat",
            "force": body_payload.get("force", False),
            "result_keys": sorted(str(key) for key in result_payload.keys()),
        },
    )


def record_location_state(
    result: Mapping[str, Any],
    *,
    ledger: EvidenceLedger = evidence_ledger,
) -> EvidenceRecord:
    payload = _location_geofence_payload(result)
    return ledger.record(
        kind="location.state",
        source="location.v2",
        payload=payload,
        observed_at=payload.get("last_fix_at"),
        metadata={"contract": LOCATION_GEOFENCE_PAYLOAD_SCHEMA_VERSION},
    )


def record_sensing_entry_safely(entry: Mapping[str, Any]) -> EvidenceRecord | None:
    record = _safe_record("sensing", record_sensing_entry, entry)
    _record_context_trigger_shadow_safely(record)
    return record


def record_activity_entry_safely(entry: Mapping[str, Any]) -> EvidenceRecord | None:
    record = _safe_record("activity", record_activity_entry, entry)
    _record_context_trigger_shadow_safely(record)
    return record


def record_location_heartbeat_safely(
    body: Mapping[str, Any],
    result: Mapping[str, Any],
) -> EvidenceRecord | None:
    record = _safe_record("location", record_location_heartbeat, body, result)
    try:
        from app.daily_signals.runtime import (
            record_location_heartbeat_safely as record_daily_location,
        )

        record_daily_location(result)
    except Exception as exc:
        # 日聚合是影子写入，不能让定位心跳因本地持久化失败而失败。
        log.warning("Daily location shadow write skipped: %s", exc)
    return record


def record_location_state_safely(result: Mapping[str, Any]) -> EvidenceRecord | None:
    record = _safe_record("location_state", record_location_state, result)
    _record_context_trigger_shadow_safely(record)
    return record


def _record_context_trigger_shadow_safely(record: EvidenceRecord | None) -> None:
    if record is None:
        return
    try:
        from context_delivery_shadow_runtime import context_trigger_shadow_runtime

        context_trigger_shadow_runtime.process_evidence_safely(record)
    except Exception as exc:
        # Trigger shadow is observational only; ingestion must never depend on it.
        log.warning("Context trigger shadow adapter skipped: %s", exc)


def _location_geofence_payload(result: Mapping[str, Any]) -> dict[str, Any]:
    value = _payload(result)
    state = value.get("v2_state")
    if not isinstance(state, Mapping):
        raise ValueError("location geofence Evidence requires v2_state")
    state = _payload(state)

    boundary_side = {
        "at_home": "inside",
        "outside": "outside",
    }.get(str(value.get("state") or "").strip())
    if boundary_side is None:
        raise ValueError("location geofence Evidence requires at_home or outside state")

    old_state = str(value.get("old_state") or "").strip()
    direction = {
        ("at_home", "outside"): "inside_to_outside",
        ("outside", "at_home"): "outside_to_inside",
    }.get((old_state, str(value.get("state") or "").strip()))
    event_type = (
        LOCATION_GEOFENCE_EVENT_TRANSITION
        if bool(value.get("state_changed")) and direction
        else LOCATION_GEOFENCE_EVENT_CURRENT
    )
    payload = {
        "payload_schema": LOCATION_GEOFENCE_PAYLOAD_SCHEMA_VERSION,
        "event_type": event_type,
        "boundary_side": boundary_side,
        "distance_m": value.get("distance_from_home"),
        "accuracy_m": state.get("accuracy_m"),
        "configured_enter_m": value.get("configured_enter_m"),
        "configured_exit_m": value.get("configured_exit_m"),
        "place_id": state.get("place_id"),
        "place_name": state.get("place_name"),
        "place_kind": state.get("place_kind"),
        "last_fix_at": state.get("last_fix_at"),
        "state_updated_at": state.get("state_updated_at"),
    }
    if event_type == LOCATION_GEOFENCE_EVENT_TRANSITION:
        payload["geofence_direction"] = direction
    return normalize_location_geofence_payload(_payload(payload))


def _safe_record(label: str, func, *args) -> EvidenceRecord | None:
    try:
        return func(*args)
    except Exception as exc:
        log.warning("Evidence shadow write skipped for %s: %s", label, exc)
        return None


def _location_confidence(body: Mapping[str, Any]) -> float:
    accuracy = body.get("accuracy")
    try:
        accuracy_m = float(accuracy)
    except (TypeError, ValueError):
        return 0.7
    if accuracy_m <= 0:
        return 0.7
    if accuracy_m <= 30:
        return 1.0
    if accuracy_m <= 100:
        return 0.8
    if accuracy_m <= 500:
        return 0.55
    return 0.35


__all__ = [
    "record_activity_entry",
    "record_activity_entry_safely",
    "record_location_heartbeat",
    "record_location_heartbeat_safely",
    "record_location_state",
    "record_location_state_safely",
    "record_sensing_entry",
    "record_sensing_entry_safely",
]
