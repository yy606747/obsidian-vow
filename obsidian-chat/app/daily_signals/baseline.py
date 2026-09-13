"""Read-only DailySignals adapter for the shared context-delivery interface."""

from __future__ import annotations

import logging
import sqlite3
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from app.context_delivery.contracts import BaselineDeviation
from app.context_delivery.policy import MAX_BASELINE_DEVIATIONS

from .config import daily_timezone_name
from .store import DailySignalStore, get_default_store
from .usage import LOOKBACK_DAYS, UsageDeviation, compare_device_usage


log = logging.getLogger(__name__)


class DailySignalBaselineProvider:
    def __init__(self, *, store: DailySignalStore | None = None, timezone_name: str | None = None):
        self.store = store
        self.timezone_name = timezone_name

    def deviations(self, *, reference_time: float) -> tuple[BaselineDeviation, ...]:
        try:
            timezone = ZoneInfo(self.timezone_name or daily_timezone_name())
            today = datetime.fromtimestamp(reference_time, timezone).date()
            rows = (self.store or get_default_store()).read_range(
                (today - timedelta(days=LOOKBACK_DAYS)).isoformat(), today.isoformat(),
            )
            values = compare_device_usage(rows, reference_time=reference_time, timezone=timezone)
            return tuple(_to_context(value) for value in values[:MAX_BASELINE_DEVIATIONS])
        except (sqlite3.Error, OSError, ValueError, TypeError, KeyError, OverflowError) as exc:
            # A broken/locked/missing daily store must not blank current device
            # evidence or trigger a legacy formatter fallback.
            log.warning("Daily baseline unavailable: %s", type(exc).__name__)
            return ()


def _to_context(value: UsageDeviation) -> BaselineDeviation:
    device = {"pc": "电脑", "phone": "手机", "tablet": "平板"}[value.device]
    scope = "同一设备" if value.identity_scope == "device" else "同一旧版上报来源（未区分硬件）"
    metric = "前台活跃" if value.metric == "active" else f"应用「{_app_label(value.app)}」前台"
    direction = "偏多" if value.direction == "more" else "偏少"
    start, end = value.window_start, value.window_end
    group = "工作日" if end.weekday() < 5 else "周末"
    return BaselineDeviation(
        key=f"device_usage:{value.stream_key}:{value.metric}:{value.app or ''}",
        source="daily_signals.activity",
        current_summary=(
            f"{device}{metric}记录约 {value.current_seconds / 60:.0f} 分钟，比历史同时段{direction}"
            f"（本次记录覆盖 {value.current_coverage:.0%}）"
        ),
        baseline_summary=(
            f"{scope}同时段记录中位数 {value.baseline_seconds / 60:.0f} 分钟"
            f"，历史各日覆盖至少 {value.baseline_coverage:.0%}"
        ),
        sample_window=(
            f"{end.tzinfo.key}；本次 {start:%Y-%m-%d %H:%M}–{end:%H:%M}；"
            f"前 {LOOKBACK_DAYS} 天的{group}，有效 {len(value.baseline_dates)} 天："
            + "、".join(value.baseline_dates)
        ),
        coverage=min(value.current_coverage, value.baseline_coverage),
    )


def _app_label(value: str | None) -> str:
    # Keep noisy application/window labels from occupying the entire renderer
    # budget or injecting additional lines. Raw labels remain in daily facts.
    return " ".join(str(value or "").split())[:40]
