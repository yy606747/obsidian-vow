from __future__ import annotations

import os
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import config as root_config


DEFAULT_ACTIVITY_RAW_RETENTION_DAYS = 14
DEFAULT_SENSING_RAW_RETENTION_DAYS = 14
DEFAULT_ACTIVITY_RAW_MAX_TOTAL_MB = 512
DEFAULT_RECONCILE_INTERVAL_SEC = 600


def activity_raw_retention_days() -> int:
    return _positive_int_setting(
        "activity_raw_retention_days",
        DEFAULT_ACTIVITY_RAW_RETENTION_DAYS,
    )


def sensing_raw_retention_days() -> int:
    return _positive_int_setting(
        "sensing_raw_retention_days",
        DEFAULT_SENSING_RAW_RETENTION_DAYS,
    )


def activity_raw_max_total_mb() -> int:
    return _positive_int_setting(
        "activity_raw_max_total_mb",
        DEFAULT_ACTIVITY_RAW_MAX_TOTAL_MB,
    )


def reconcile_interval_sec() -> int:
    return _positive_int_setting(
        "signal_daily_reconcile_interval_sec",
        DEFAULT_RECONCILE_INTERVAL_SEC,
    )


def daily_timezone_name() -> str:
    configured = str(
        os.environ.get("OBSIDIAN_SIGNAL_DAILY_TIMEZONE")
        or root_config.SETTINGS.get("signal_daily_timezone")
        or ""
    ).strip()
    candidates = [configured, _system_timezone_name(), "UTC"]
    for candidate in candidates:
        if not candidate:
            continue
        try:
            ZoneInfo(candidate)
        except ZoneInfoNotFoundError:
            continue
        return candidate
    return "UTC"


def daily_timezone() -> ZoneInfo:
    return ZoneInfo(daily_timezone_name())


def _positive_int_setting(name: str, default: int) -> int:
    try:
        value = int(root_config.SETTINGS.get(name, default))
    except (TypeError, ValueError):
        return default
    return max(1, value)


def _system_timezone_name() -> str:
    timezone_file = Path("/etc/timezone")
    try:
        value = timezone_file.read_text(encoding="utf-8").strip()
        if value:
            return value
    except OSError:
        pass

    try:
        resolved = Path("/etc/localtime").resolve().as_posix()
    except OSError:
        return ""
    marker = "/zoneinfo/"
    if marker in resolved:
        return resolved.split(marker, 1)[1]
    return ""
