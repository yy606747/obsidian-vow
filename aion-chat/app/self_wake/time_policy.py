"""Owner-timezone parsing, horizon, expiry and quiet-hour policy."""

from __future__ import annotations

from datetime import date, datetime, time as datetime_time, timedelta
import re
import time
from typing import Any, Mapping
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from app.daily_signals.config import daily_timezone_name


MAX_WAKE_HORIZON_DAYS = 30
WAKE_EXPIRY_SECONDS = 2 * 60 * 60
_NAIVE_LOCAL_ISO_PATTERN = re.compile(
    r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}(?::\d{2})?"
)


class SelfWakeTimeError(ValueError):
    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


def owner_timezone_name() -> str:
    return daily_timezone_name()


def _timezone(name: str | None) -> ZoneInfo:
    candidate = str(name or owner_timezone_name()).strip()
    try:
        return ZoneInfo(candidate)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise SelfWakeTimeError("invalid_owner_timezone") from exc


def validate_wake_timestamp(wake_at: float, *, now: float | None = None) -> float:
    current = time.time() if now is None else float(now)
    try:
        value = float(wake_at)
    except (TypeError, ValueError) as exc:
        raise SelfWakeTimeError("invalid_wake_at") from exc
    if value <= current:
        raise SelfWakeTimeError("wake_at_not_future")
    if value > current + MAX_WAKE_HORIZON_DAYS * 24 * 60 * 60:
        raise SelfWakeTimeError("wake_horizon_exceeded")
    return value


def _valid_local_candidates(naive: datetime, timezone: ZoneInfo) -> list[datetime]:
    candidates: list[datetime] = []
    seen_timestamps: set[float] = set()
    for fold in (0, 1):
        aware = naive.replace(tzinfo=timezone, fold=fold)
        timestamp = aware.timestamp()
        roundtrip = datetime.fromtimestamp(timestamp, timezone).replace(tzinfo=None)
        if roundtrip != naive or timestamp in seen_timestamps:
            continue
        candidates.append(aware)
        seen_timestamps.add(timestamp)
    return candidates


def parse_wake_at(
    raw: str,
    *,
    now: float | None = None,
    timezone_name: str | None = None,
) -> float:
    text = str(raw or "").strip()
    if not text:
        raise SelfWakeTimeError("invalid_wake_at")
    timezone = _timezone(timezone_name)

    iso_text = text[:-1] + "+00:00" if text.endswith(("Z", "z")) else text
    try:
        parsed = datetime.fromisoformat(iso_text)
    except ValueError:
        parsed = None

    if parsed is None:
        raise SelfWakeTimeError("invalid_wake_at")
    if parsed.tzinfo is not None:
        timestamp = parsed.timestamp()
    else:
        if _NAIVE_LOCAL_ISO_PATTERN.fullmatch(text) is None:
            raise SelfWakeTimeError("invalid_wake_at")
        naive = parsed
        candidates = _valid_local_candidates(naive, timezone)
        if not candidates:
            raise SelfWakeTimeError("nonexistent_local_time")
        if len(candidates) > 1:
            raise SelfWakeTimeError("ambiguous_local_time_requires_offset")
        timestamp = candidates[0].timestamp()

    return validate_wake_timestamp(timestamp, now=now)


def owner_day_bounds(
    timestamp: float,
    *,
    timezone_name: str | None = None,
) -> tuple[float, float]:
    timezone = _timezone(timezone_name)
    local_day = datetime.fromtimestamp(float(timestamp), timezone).date()
    start = datetime.combine(local_day, datetime_time.min, tzinfo=timezone)
    next_day = datetime.combine(
        local_day + timedelta(days=1),
        datetime_time.min,
        tzinfo=timezone,
    )
    return start.timestamp(), next_day.timestamp()


def format_owner_time(
    timestamp: float,
    *,
    timezone_name: str | None = None,
) -> str:
    timezone = _timezone(timezone_name)
    return datetime.fromtimestamp(float(timestamp), timezone).isoformat(timespec="minutes")


def _hhmm(value: Any) -> int | None:
    try:
        hour_text, minute_text = str(value).split(":", 1)
        hour, minute = int(hour_text), int(minute_text)
    except (TypeError, ValueError):
        return None
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        return None
    return hour * 60 + minute


def quiet_hours_snapshot(config: Mapping[str, Any] | None = None) -> dict[str, Any]:
    if config is None:
        try:
            from config import load_cam_config

            config = load_cam_config()
        except Exception:
            config = {}
    enabled = bool(config.get("quiet_hours_enabled", False))
    start_text = str(config.get("quiet_hours_start", "00:00") or "00:00")
    end_text = str(config.get("quiet_hours_end", "09:00") or "09:00")
    start = _hhmm(start_text)
    end = _hhmm(end_text)
    if start is None or end is None:
        enabled = False
    return {
        "enabled": enabled,
        "start": start_text,
        "end": end_text,
        "start_minute": start,
        "end_minute": end,
    }


def is_quiet_at(
    wake_at: float,
    *,
    timezone_name: str | None = None,
    config: Mapping[str, Any] | None = None,
) -> bool:
    snapshot = quiet_hours_snapshot(config)
    if not snapshot["enabled"]:
        return False
    timezone = _timezone(timezone_name)
    local = datetime.fromtimestamp(float(wake_at), timezone)
    current = local.hour * 60 + local.minute
    start = int(snapshot["start_minute"])
    end = int(snapshot["end_minute"])
    if start <= end:
        return start <= current < end
    return current >= start or current < end


def validate_not_quiet(
    wake_at: float,
    *,
    timezone_name: str | None = None,
    config: Mapping[str, Any] | None = None,
) -> None:
    if is_quiet_at(wake_at, timezone_name=timezone_name, config=config):
        raise SelfWakeTimeError("wake_at_in_quiet_hours")


__all__ = [
    "MAX_WAKE_HORIZON_DAYS",
    "SelfWakeTimeError",
    "WAKE_EXPIRY_SECONDS",
    "format_owner_time",
    "is_quiet_at",
    "owner_day_bounds",
    "owner_timezone_name",
    "parse_wake_at",
    "quiet_hours_snapshot",
    "validate_not_quiet",
    "validate_wake_timestamp",
]
