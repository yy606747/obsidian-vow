"""Legacy IO boundary for the shared context-delivery projection."""

from __future__ import annotations

import time
from collections.abc import Callable, Iterable
from typing import Any

from app.context_delivery import (
    BaselineDeviationProvider,
    ContextDeliveryProjection,
    NullBaselineDeviationProvider,
    SourceStatus,
    build_context_delivery_projection,
    render_context_delivery_projection,
)
from app.events.schemas import EvidenceRecord
from app.context_delivery.policy import (
    LOCATION_ADDRESS_MAX_AGE_SEC,
    LOCATION_CURRENT_MAX_AGE_SEC,
    PC_CURRENT_MAX_AGE_SEC,
    PHONE_CURRENT_MAX_AGE_SEC,
    PROJECTION_KINDS,
    PROJECTION_SOURCES,
    RECENT_EVENT_MAX_AGE_SEC,
)
from app.events import EvidenceLedger, evidence_ledger


def read_context_delivery_projection(
    *,
    reference_time: float | None = None,
    ledger: EvidenceLedger | None = None,
    baseline_provider: BaselineDeviationProvider | None = None,
    pc_status_loader: Callable[[float], dict[str, Any]] | None = None,
    activity_enabled_loader: Callable[[], bool] | None = None,
    location_config_loader: Callable[[], dict[str, Any]] | None = None,
    location_status_loader: Callable[[], dict[str, Any]] | None = None,
    additional_records: Iterable[EvidenceRecord] = (),
) -> ContextDeliveryProjection:
    """Read only the existing runtime stores and return one structured view."""

    now = time.time() if reference_time is None else float(reference_time)
    target_ledger = ledger or evidence_ledger
    snapshot = target_ledger.snapshot(
        max_age_sec=RECENT_EVENT_MAX_AGE_SEC,
        kinds=PROJECTION_KINDS,
        sources=PROJECTION_SOURCES,
        reference_time=now,
        include_future=False,
    )
    records = (*snapshot.records, *tuple(additional_records))
    statuses = [
        *_pc_statuses(
            now,
            status_loader=pc_status_loader,
            enabled_loader=activity_enabled_loader,
        ),
        *_location_statuses(
            now,
            config_loader=location_config_loader,
            status_loader=location_status_loader,
        ),
        *_phone_sensing_statuses(records),
    ]
    return build_context_delivery_projection(
        records,
        reference_time=now,
        source_statuses=statuses,
        baseline_provider=baseline_provider or NullBaselineDeviationProvider(),
    )


async def read_context_delivery_projection_async(
    *,
    conv_id: str | None,
    reference_time: float | None = None,
    exclude_summon_id: str | None = None,
    summon_repository=None,
    **kwargs,
) -> ContextDeliveryProjection:
    """Add 24-hour durable summons without widening the evidence snapshot."""

    now = time.time() if reference_time is None else float(reference_time)
    additional_records: list[EvidenceRecord] = []
    normalized_conv = str(conv_id or "").strip()
    if normalized_conv:
        if summon_repository is None:
            from app.presence.summon import summon_event_repository

            summon_repository = summon_event_repository
        rows = await summon_repository.recent_facts(
            conv_id=normalized_conv,
            now=now,
            exclude_summon_id=exclude_summon_id,
        )
        additional_records = [
            EvidenceRecord(
                id=f"presence_summon:{row['summon_id']}",
                kind="presence.summon",
                source="presence.summon",
                observed_at=float(row["occurred_at"]),
                received_at=float(row["occurred_at"]),
                payload={},
                metadata={},
            )
            for row in rows
        ]
    return read_context_delivery_projection(
        reference_time=now,
        additional_records=additional_records,
        **kwargs,
    )


def render_current_context_delivery(
    *, user_name: str, ai_name: str, **kwargs
) -> str:
    """Render at a final prompt/diagnostic boundary, never inside the projection."""

    return render_context_delivery_projection(
        read_context_delivery_projection(**kwargs),
        user_name=user_name,
        ai_name=ai_name,
    )


async def render_current_context_delivery_async(
    *, user_name: str, ai_name: str, conv_id: str | None, **kwargs
) -> str:
    projection = await read_context_delivery_projection_async(
        conv_id=conv_id,
        **kwargs,
    )
    return render_context_delivery_projection(
        projection,
        user_name=user_name,
        ai_name=ai_name,
    )


def _pc_statuses(
    now: float,
    *,
    status_loader: Callable[[float], dict[str, Any]] | None,
    enabled_loader: Callable[[], bool] | None,
) -> tuple[SourceStatus, ...]:
    if status_loader is None:
        from app.pc_context.service import get_pc_status_payload

        status_loader = get_pc_status_payload
    if enabled_loader is None:
        from activity import is_activity_tracking_enabled

        enabled_loader = is_activity_tracking_enabled
    enabled = bool(enabled_loader())
    if not enabled:
        return (SourceStatus(
            source="pc.context",
            enabled=False,
            expected_periodic=False,
            max_age_sec=PC_CURRENT_MAX_AGE_SEC,
        ),)
    payload = dict(status_loader(now) or {})
    last_seen_at = _optional_float(payload.get("last_seen_at"))
    state = str(payload.get("active_state") or "").strip().lower()
    is_fresh = bool(last_seen_at is not None and now - last_seen_at <= PC_CURRENT_MAX_AGE_SEC)
    values: dict[str, str | int | float | bool] = {}
    if is_fresh and state and state != "offline":
        values["pc.state"] = state
        foreground = _text(payload.get("foreground_app"))
        if foreground and state not in {"locked", "unknown"}:
            values["pc.foreground_app"] = foreground
    observed_at = (
        _optional_float(payload.get("observed_at"))
        if is_fresh
        else last_seen_at
    )
    return (SourceStatus(
        source="pc.context",
        enabled=True,
        expected_periodic=True,
        max_age_sec=PC_CURRENT_MAX_AGE_SEC,
        observed_at=observed_at,
        received_at=last_seen_at,
        values=values,
    ),)


def _location_statuses(
    now: float,
    *,
    config_loader: Callable[[], dict[str, Any]] | None,
    status_loader: Callable[[], dict[str, Any]] | None,
) -> tuple[SourceStatus, ...]:
    if config_loader is None or status_loader is None:
        import location

        config_loader = config_loader or location.load_location_config
        status_loader = status_loader or location.load_location_status
    config = dict(config_loader() or {})
    enabled = bool(config.get("enabled"))
    if not enabled:
        return (SourceStatus(
            source="location.v2",
            enabled=False,
            expected_periodic=False,
            max_age_sec=LOCATION_CURRENT_MAX_AGE_SEC,
        ),)
    status = dict(status_loader() or {})
    state = dict(status.get("v2_state") or {})
    observed_at = _positive_float(state.get("last_fix_at") or status.get("updated_at"))
    since_at = _positive_float(state.get("state_updated_at") or status.get("state_changed_at"))
    place_id = _text(state.get("place_id"))
    place_value = _text(state.get("place_name")) or _text(state.get("place_kind"))
    if not place_id and observed_at:
        place_value = "unmatched"
    values = {"location.place": place_value} if place_value else {}
    statuses = [SourceStatus(
        source="location.v2",
        enabled=True,
        expected_periodic=True,
        max_age_sec=LOCATION_CURRENT_MAX_AGE_SEC,
        observed_at=observed_at,
        received_at=_optional_float(status.get("heartbeat_received_at") or status.get("updated_at")),
        since_at=since_at,
        values=values,
    )]

    address = _text(status.get("address"))
    address_observed_at = _positive_float(status.get("address_updated_at"))
    location_is_fresh = bool(
        observed_at is not None
        and observed_at <= now
        and now - observed_at <= LOCATION_CURRENT_MAX_AGE_SEC
    )
    address_is_fresh = bool(
        address
        and address_observed_at is not None
        and address_observed_at <= now
        and now - address_observed_at <= LOCATION_ADDRESS_MAX_AGE_SEC
        and not bool(status.get("address_stale", False))
    )
    if location_is_fresh and address_is_fresh:
        statuses.append(SourceStatus(
            source="location.v2",
            enabled=True,
            expected_periodic=False,
            max_age_sec=LOCATION_ADDRESS_MAX_AGE_SEC,
            observed_at=address_observed_at,
            received_at=address_observed_at,
            values={"location.address": address},
        ))
    return tuple(statuses)


def _phone_sensing_statuses(records) -> tuple[SourceStatus, ...]:
    sensing_records = [
        record for record in records
        if record.kind == "sensing.sensor" and record.source == "android.sensing"
    ]
    # There is no server-side capability/authorization heartbeat yet.  Only a
    # record in this process proves that phone sensing is expected; an empty
    # post-restart ledger must not be presented as owner-side silence.
    if not sensing_records:
        return ()
    latest = max(sensing_records, key=lambda item: (item.observed_at, item.received_at, item.id))
    return (SourceStatus(
        source="android.sensing",
        enabled=True,
        expected_periodic=True,
        max_age_sec=PHONE_CURRENT_MAX_AGE_SEC,
        observed_at=latest.observed_at,
        received_at=latest.received_at,
    ),)


def _text(value: Any) -> str | None:
    if value is None:
        return None
    result = str(value).strip()
    return result or None


def _optional_float(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _positive_float(value: Any) -> float | None:
    result = _optional_float(value)
    return result if result is not None and result > 0 else None


__all__ = [
    "read_context_delivery_projection",
    "read_context_delivery_projection_async",
    "render_current_context_delivery",
    "render_current_context_delivery_async",
]
