"""Pure evidence-to-context projection; no IO, decisions, or side effects."""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from typing import Any, Iterable, Protocol

from app.events.schemas import EvidenceRecord
from app.location_geofence import (
    LOCATION_GEOFENCE_EVENT_TRANSITION,
    LOCATION_GEOFENCE_PAYLOAD_SCHEMA_VERSION,
    as_current_location_geofence_payload,
    normalize_location_geofence_payload,
)

from .contracts import (
    AvailabilityItem,
    BaselineDeviation,
    ContextDeliveryProjection,
    CurrentContextItem,
    RecentContextEvent,
    SourceStatus,
)
from .policy import (
    CONTEXT_KEY_POLICIES,
    MAX_AVAILABILITY,
    MAX_BASELINE_DEVIATIONS,
    MAX_DEVICE_DERIVED,
    MAX_OBSERVATIONS,
    MAX_RECENT_EVENTS,
    RECENT_EVENT_MAX_AGE_SEC,
)


class BaselineDeviationProvider(Protocol):
    def deviations(self, *, reference_time: float) -> Iterable[BaselineDeviation]: ...


class NullBaselineDeviationProvider:
    def deviations(self, *, reference_time: float) -> tuple[BaselineDeviation, ...]:
        del reference_time
        return ()


@dataclass(frozen=True)
class _Candidate:
    key: str
    value: str | int | float | bool
    source: str
    observed_at: float
    received_at: float
    confidence: float
    explicit_since_at: float | None = None
    payload: dict[str, Any] | None = None


def build_context_delivery_projection(
    records: Iterable[EvidenceRecord],
    *,
    reference_time: float,
    source_statuses: Iterable[SourceStatus] = (),
    baseline_provider: BaselineDeviationProvider | None = None,
) -> ContextDeliveryProjection:
    """Project normalized evidence without inferring what the owner is doing."""

    now = float(reference_time)
    source_records = tuple(records)
    statuses = tuple(source_statuses)
    usable_records = tuple(record for record in source_records if record.observed_at <= now)
    candidates = _record_candidates(usable_records)
    candidates.extend(_status_candidates(statuses, reference_time=now))

    grouped: dict[str, list[_Candidate]] = {}
    for candidate in candidates:
        grouped.setdefault(candidate.key, []).append(candidate)

    current_items = []
    for key, key_candidates in grouped.items():
        policy = _policy_for_key(key)
        if policy is None or policy.section not in {"observations", "device_derived"}:
            continue
        ordered = _ordered_candidates(key_candidates)
        latest = ordered[-1]
        freshness = max(0.0, now - latest.observed_at)
        if freshness > policy.max_age_sec:
            continue
        since_at = latest.explicit_since_at
        if since_at is None:
            since_at = _derived_since_at(ordered)
        item_payload = latest.payload
        if item_payload is not None and latest.key == "location.place":
            item_payload = as_current_location_geofence_payload(item_payload)
        current_items.append(CurrentContextItem(
            key=key,
            value=latest.value,
            source=latest.source,
            observed_at=latest.observed_at,
            received_at=latest.received_at,
            freshness_sec=freshness,
            since_at=since_at,
            confidence=latest.confidence,
            payload=item_payload,
        ))

    # A locked/offline PC cannot lend an older foreground application the
    # appearance of being current, even when both arrived inside five minutes.
    pc_state = next((item.value for item in current_items if item.key == "pc.state"), None)
    if pc_state in {"locked", "offline", "unknown"}:
        current_items = [item for item in current_items if item.key != "pc.foreground_app"]

    observations = _bounded_observations(
        item
        for item in current_items
        if _policy_for_key(item.key).section == "observations"
    )
    device_derived = tuple(sorted(
        (item for item in current_items if _policy_for_key(item.key).section == "device_derived"),
        key=lambda item: item.key,
    )[:MAX_DEVICE_DERIVED])
    recent_events = tuple(_bounded_recent_events(_recent_events(
        grouped,
        usable_records,
        reference_time=now,
    )))
    deviations = tuple((baseline_provider or NullBaselineDeviationProvider()).deviations(reference_time=now))
    availability = tuple(_availability(statuses, reference_time=now)[:MAX_AVAILABILITY])

    return ContextDeliveryProjection(
        generated_at=now,
        observations=observations,
        device_derived=device_derived,
        recent_events=recent_events,
        baseline_deviations=deviations[:MAX_BASELINE_DEVIATIONS],
        availability=availability,
        metrics={
            "input_records": len(source_records),
            "considered_records": len(usable_records),
            "future_records_dropped": len(source_records) - len(usable_records),
            "source_statuses": len(statuses),
        },
    )


def _record_candidates(records: Iterable[EvidenceRecord]) -> list[_Candidate]:
    candidates: list[_Candidate] = []
    for record in records:
        payload = dict(record.payload)
        if record.kind == "sensing.sensor" and record.source == "android.sensing":
            _append_if_present(candidates, record, "phone.screen", payload, "screen_on", transform=lambda value: "on" if bool(value) else "off")
            _append_if_present(candidates, record, "phone.motion", payload, "motion", confidence=record.confidence)
            _append_if_present(candidates, record, "phone.light_lux", payload, "light_lux")
            _append_if_present(candidates, record, "phone.battery", payload, "battery_pct")
            _append_if_present(candidates, record, "phone.charging", payload, "charging")
            _append_if_present(candidates, record, "phone.wifi", payload, "wifi_ssid")
            continue

        if record.kind == "activity.app":
            device = str(payload.get("device") or "").strip().lower()
            screen_state = _screen_state(payload)
            if screen_state and device in {"phone", "tablet"}:
                candidates.append(_candidate(record, "phone.screen", screen_state, confidence=1.0))
                continue
            app = _usable_text(payload.get("app"))
            if device in {"phone", "tablet"} and app:
                device_id = _device_id(payload, record)
                candidates.append(_candidate(
                    record,
                    f"mobile.{device_id}.foreground_app",
                    app,
                    confidence=1.0,
                ))
            elif device == "pc":
                state = _usable_text(payload.get("active_state"))
                if state:
                    candidates.append(_candidate(record, "pc.state", state.lower(), confidence=1.0))
                if app:
                    candidates.append(_candidate(record, "pc.foreground_app", app, confidence=1.0))
            continue

        if record.kind == "location.state" and record.source == "location.v2":
            geofence_payload = None
            payload_schema = payload.get("payload_schema")
            if payload_schema is not None:
                if payload_schema != LOCATION_GEOFENCE_PAYLOAD_SCHEMA_VERSION:
                    raise ValueError(f"unknown location.state payload_schema: {payload_schema!r}")
                geofence_payload = normalize_location_geofence_payload(payload)
            value = _location_value(payload)
            if value:
                candidates.append(_candidate(
                    record,
                    "location.place",
                    value,
                    confidence=1.0,
                    explicit_since_at=_optional_float(payload.get("state_updated_at")),
                    payload=geofence_payload,
                ))
    return candidates


def _status_candidates(statuses: Iterable[SourceStatus], *, reference_time: float) -> list[_Candidate]:
    candidates = []
    for status in statuses:
        if not status.enabled or status.observed_at is None or status.observed_at > reference_time:
            continue
        if reference_time - status.observed_at > status.max_age_sec:
            continue
        received_at = status.received_at if status.received_at is not None else status.observed_at
        for key, value in status.values.items():
            if _policy_for_key(key) is None:
                continue
            candidates.append(_Candidate(
                key=key,
                value=value,
                source=status.source,
                observed_at=status.observed_at,
                received_at=received_at,
                confidence=1.0,
                explicit_since_at=status.since_at,
            ))
    return candidates


def _recent_events(
    grouped: dict[str, list[_Candidate]],
    records: Iterable[EvidenceRecord],
    *,
    reference_time: float,
) -> list[RecentContextEvent]:
    events: list[RecentContextEvent] = []
    notification_records: list[tuple[EvidenceRecord, str]] = []
    stateful_keys = {
        key for key in grouped
        if key == "phone.screen"
        or key == "pc.state"
        or key == "pc.foreground_app"
        or key == "location.place"
        or (key.startswith("mobile.") and key.endswith(".foreground_app"))
    }
    for key in stateful_keys:
        previous: _Candidate | None = None
        for candidate in _ordered_candidates(grouped[key]):
            if (
                key == "location.place"
                and candidate.payload is not None
                and candidate.payload["event_type"] == LOCATION_GEOFENCE_EVENT_TRANSITION
                and reference_time - candidate.observed_at <= RECENT_EVENT_MAX_AGE_SEC
            ):
                direction = candidate.payload["geofence_direction"]
                from_side, to_side = direction.split("_to_", 1)
                events.append(RecentContextEvent(
                    key=key,
                    event="transition",
                    from_value=from_side,
                    to_value=to_side,
                    observed_at=candidate.observed_at,
                    source=candidate.source,
                    confidence=candidate.confidence,
                    payload=candidate.payload,
                ))
                previous = candidate
                continue
            if previous is not None and candidate.value != previous.value:
                if reference_time - candidate.observed_at <= RECENT_EVENT_MAX_AGE_SEC:
                    events.append(RecentContextEvent(
                        key=key,
                        event="transition",
                        from_value=previous.value,
                        to_value=candidate.value,
                        observed_at=candidate.observed_at,
                        source=candidate.source,
                        confidence=candidate.confidence,
                    ))
            previous = candidate

    for record in records:
        event_key = _record_event_key(record)
        if event_key is None:
            continue
        policy = _policy_for_key(event_key)
        if (
            policy is None
            or policy.section != "recent_events"
            or reference_time - record.observed_at > policy.max_age_sec
        ):
            continue
        if event_key == "phone.unlock":
            events.append(RecentContextEvent(
                key="phone.unlock",
                event="occurred",
                to_value="unlocked",
                observed_at=record.observed_at,
                source=record.source,
                confidence=1.0,
            ))
        elif event_key == "phone.notification":
            app = _usable_text(record.payload.get("app"))
            if app:
                notification_records.append((record, app))
        elif event_key == "relationship.summon":
            events.append(RecentContextEvent(
                key="relationship.summon",
                event="occurred",
                to_value="summoned",
                observed_at=record.observed_at,
                source=record.source,
                confidence=1.0,
            ))

    if notification_records:
        ordered_notifications = sorted(
            notification_records,
            key=lambda item: (
                item[0].observed_at,
                item[0].received_at,
                item[0].id,
            ),
        )
        app_counts = Counter(app for _record, app in ordered_notifications)
        count = len(ordered_notifications)
        app_summary = "、".join(
            f"{app} {app_count}" if count > 1 else app
            for app, app_count in sorted(
                app_counts.items(),
                key=lambda item: (-item[1], item[0]),
            )
        )
        first_record = ordered_notifications[0][0]
        latest_record = ordered_notifications[-1][0]
        events.append(RecentContextEvent(
            key="phone.notification",
            event="occurred",
            to_value=app_summary,
            observed_at=latest_record.observed_at,
            source=latest_record.source,
            confidence=min(record.confidence for record, _app in ordered_notifications),
            occurrence_count=count,
            first_observed_at=(
                first_record.observed_at
                if count > 1
                else None
            ),
        ))

    deduplicated: dict[tuple[Any, ...], RecentContextEvent] = {}
    for event in events:
        signature = (
            event.key,
            event.event,
            event.from_value,
            event.to_value,
            event.observed_at,
            event.source,
        )
        deduplicated[signature] = event
    return sorted(
        deduplicated.values(),
        key=lambda item: (item.observed_at, item.key, str(item.to_value), item.source),
    )


def _record_event_key(record: EvidenceRecord) -> str | None:
    if record.kind == "sensing.unlock" and record.source == "android.sensing":
        return "phone.unlock"
    if record.kind == "sensing.notification" and record.source == "android.sensing":
        return "phone.notification"
    if record.kind == "presence.summon" and record.source == "presence.summon":
        return "relationship.summon"
    return None


def _bounded_recent_events(
    events: Iterable[RecentContextEvent],
) -> list[RecentContextEvent]:
    """Count summons as one rendered line while preserving every click time."""

    ordered = sorted(
        events,
        key=lambda item: (item.observed_at, item.key, str(item.to_value), item.source),
    )
    summons = [item for item in ordered if item.key == "relationship.summon"]
    ordinary = [item for item in ordered if item.key != "relationship.summon"]
    if not summons:
        return ordinary[-MAX_RECENT_EVENTS:]
    selected = [*summons, *ordinary[-(MAX_RECENT_EVENTS - 1):]]
    return sorted(
        selected,
        key=lambda item: (item.observed_at, item.key, str(item.to_value), item.source),
    )


def _availability(statuses: Iterable[SourceStatus], *, reference_time: float) -> list[AvailabilityItem]:
    result = []
    for status in statuses:
        if not status.enabled or not status.expected_periodic:
            continue
        if status.observed_at is None or status.observed_at > reference_time:
            result.append(AvailabilityItem(
                source=status.source,
                status="missing",
                reason="已启用，但当前进程还没有可用数据",
            ))
            continue
        if reference_time - status.observed_at > status.max_age_sec:
            result.append(AvailabilityItem(
                source=status.source,
                status="stale",
                last_observed_at=status.observed_at,
                reason="最近没有达到 freshness 要求的数据",
            ))
    return sorted(result, key=lambda item: item.source)


def _append_if_present(
    candidates: list[_Candidate],
    record: EvidenceRecord,
    key: str,
    payload: dict[str, Any],
    field: str,
    *,
    transform=None,
    confidence: float = 1.0,
) -> None:
    if field not in payload or payload[field] is None:
        return
    value = transform(payload[field]) if transform else payload[field]
    if isinstance(value, str) and not value.strip():
        return
    candidates.append(_candidate(record, key, value, confidence=confidence))


def _candidate(
    record: EvidenceRecord,
    key: str,
    value: str | int | float | bool,
    *,
    confidence: float,
    explicit_since_at: float | None = None,
    payload: dict[str, Any] | None = None,
) -> _Candidate:
    return _Candidate(
        key=key,
        value=value,
        source=record.source,
        observed_at=record.observed_at,
        received_at=record.received_at,
        confidence=confidence,
        explicit_since_at=explicit_since_at,
        payload=payload,
    )


def _ordered_candidates(candidates: Iterable[_Candidate]) -> list[_Candidate]:
    deduplicated: dict[tuple[Any, ...], _Candidate] = {}
    for candidate in candidates:
        signature = (
            candidate.key,
            candidate.value,
            candidate.source,
            candidate.observed_at,
            candidate.received_at,
        )
        deduplicated[signature] = candidate
    return sorted(
        deduplicated.values(),
        key=lambda item: (item.observed_at, item.received_at, item.source, str(item.value)),
    )


def _derived_since_at(ordered: list[_Candidate]) -> float | None:
    if len(ordered) < 2:
        return None
    segment_start = ordered[0].observed_at
    saw_transition = False
    previous_value = ordered[0].value
    for candidate in ordered[1:]:
        if candidate.value != previous_value:
            segment_start = candidate.observed_at
            saw_transition = True
        previous_value = candidate.value
    return segment_start if saw_transition else None


def _policy_for_key(key: str):
    if key.startswith("mobile.") and key.endswith(".foreground_app"):
        return CONTEXT_KEY_POLICIES["mobile.*.foreground_app"]
    return CONTEXT_KEY_POLICIES.get(key)


def _screen_state(payload: dict[str, Any]) -> str | None:
    value = str(payload.get("screen_state") or "").strip().lower()
    return value if value in {"on", "off"} else None


def _location_value(payload: dict[str, Any]) -> str | None:
    for field in ("place_name", "place_kind"):
        value = _usable_text(payload.get(field))
        if value:
            return value
    if payload.get("last_fix_at"):
        return "unmatched"
    return None


def _bounded_observations(
    items: Iterable[CurrentContextItem],
) -> tuple[CurrentContextItem, ...]:
    """Count the location range and address as one rendered observation."""

    selected: list[CurrentContextItem] = []
    occupied_slots: set[str] = set()
    for item in sorted(items, key=lambda value: value.key):
        slot = (
            "location.current"
            if item.key in {"location.address", "location.place"}
            else item.key
        )
        if slot not in occupied_slots and len(occupied_slots) >= MAX_OBSERVATIONS:
            continue
        selected.append(item)
        occupied_slots.add(slot)
    return tuple(selected)


def _device_id(payload: dict[str, Any], record: EvidenceRecord) -> str:
    raw = (
        record.metadata.get("device_id")
        or payload.get("device_id")
        or payload.get("device_type")
        or payload.get("device")
        or "unknown"
    )
    safe = "".join(ch if ch.isalnum() or ch in {"-", "_"} else "_" for ch in str(raw).strip())
    return safe or "unknown"


def _usable_text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _optional_float(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


__all__ = [
    "BaselineDeviationProvider",
    "NullBaselineDeviationProvider",
    "build_context_delivery_projection",
]
