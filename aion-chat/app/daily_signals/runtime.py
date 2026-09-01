from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timedelta
from typing import Mapping
from zoneinfo import ZoneInfo

from .aggregation import reconcile_daily_signals
from .config import daily_timezone_name, reconcile_interval_sec
from .store import DailySignalStore, get_default_store


log = logging.getLogger(__name__)


def record_location_heartbeat_safely(
    result: Mapping,
    *,
    store: DailySignalStore | None = None,
    timezone_name: str | None = None,
    now: float | None = None,
) -> dict | None:
    """Merge one heartbeat directly into its local-day row; never break the route."""

    try:
        zone_name = timezone_name or daily_timezone_name()
        timezone = ZoneInfo(zone_name)
        observed_at = _heartbeat_timestamp(result)
        if observed_at is None:
            observed_at = time.time() if now is None else float(now)
        local = datetime.fromtimestamp(observed_at, timezone)
        state = str(result.get("state") or "unknown").strip().lower() or "unknown"
        target_store = store or get_default_store()
        row = target_store.merge_location_heartbeat(
            local.date().isoformat(),
            zone_name,
            bin_label=f"{local.hour:02d}:{(local.minute // 10) * 10:02d}",
            observed_at=observed_at,
            state=state,
        )
        if (
            local.date() == datetime.now(timezone).date()
            and target_store.biometric_day_state(local.date().isoformat()) is None
        ):
            # A live heartbeat may create today's row before the first periodic
            # reconcile. It must not make the new mutable day look like an
            # un-migrated historical summary to the first biometric tick.
            target_store.set_biometric_day_state(local.date().isoformat(), "ready")
        return row
    except Exception as exc:
        log.warning("Daily location heartbeat merge skipped: %s", exc)
        return None


async def run_daily_signal_reconcile_loop(
    *,
    interval_sec: int | None = None,
) -> None:
    """Periodically reconcile the two mutable days, including after midnight."""

    interval = interval_sec or reconcile_interval_sec()
    while True:
        await asyncio.sleep(interval)
        try:
            zone_name = daily_timezone_name()
            timezone = ZoneInfo(zone_name)
            today = datetime.now(timezone).date()
            await asyncio.to_thread(
                reconcile_daily_signals,
                timezone_name=zone_name,
                dates=(today - timedelta(days=1), today),
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.warning("Daily signal reconcile skipped: %s", exc)


def _heartbeat_timestamp(result: Mapping) -> float | None:
    status = result.get("status") if isinstance(result.get("status"), Mapping) else {}
    v2_state = result.get("v2_state") if isinstance(result.get("v2_state"), Mapping) else {}
    candidates = (
        result.get("heartbeat_received_at"),
        status.get("heartbeat_received_at"),
        status.get("updated_at"),
        v2_state.get("last_fix_at"),
    )
    for value in candidates:
        if value is None or isinstance(value, bool):
            continue
        try:
            return float(value)
        except (TypeError, ValueError):
            continue
    return None
