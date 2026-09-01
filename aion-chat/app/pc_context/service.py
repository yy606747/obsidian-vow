"""Server-side PC activity normalization and cached status."""

from __future__ import annotations

import logging
import time
from collections.abc import Mapping
from typing import Any

from .app_map import get_feature_tag, normalize_app_name
from .privacy import REDACTED_TITLE, is_lock_screen_process, matches_sensitive_keyword, strip_url_like, truncate_title
from .schemas import PcActivitySnapshot


log = logging.getLogger(__name__)

OFFLINE_AFTER_SEC = 300
VALID_AGENT_STATES = frozenset({"active", "idle", "locked", "unknown"})

_pc_last_seen_at: float | None = None
_pc_last_snapshot: PcActivitySnapshot | None = None


def ingest_report(report: Mapping[str, Any], *, now: float | None = None) -> dict[str, Any]:
    reference = time.time() if now is None else float(now)
    observed_at = _timestamp(report.get("timestamp"), default=reference)
    state = _normalize_active_state(report.get("active_state"))
    app_value = report.get("app")
    title = _defensive_title(report.get("title"))
    app = normalize_app_name(app_value)
    last_input_age_sec = _last_input_age(report.get("last_input_age_sec"))

    if state == "unknown":
        last_input_age_sec = None
    if state == "locked" and is_lock_screen_process(app_value):
        app = None
        title = None

    snapshot = PcActivitySnapshot(
        observed_at=observed_at,
        active_state=state,
        last_input_age_sec=last_input_age_sec,
        foreground_app=app,
        foreground_title_sanitized=title,
    )
    _set_last_snapshot(snapshot, reference)
    return _entry_from_snapshot(snapshot)


def get_pc_status(
    now: float | None = None,
    *,
    offline_after_sec: int = OFFLINE_AFTER_SEC,
) -> PcActivitySnapshot:
    reference = time.time() if now is None else float(now)
    if (
        _pc_last_seen_at is None
        or _pc_last_snapshot is None
        or reference - _pc_last_seen_at > offline_after_sec
    ):
        return PcActivitySnapshot(reference, "offline", None, None, None)
    return _pc_last_snapshot


def get_pc_status_payload(
    now: float | None = None,
    *,
    offline_after_sec: int = OFFLINE_AFTER_SEC,
) -> dict[str, Any]:
    snapshot = get_pc_status(now, offline_after_sec=offline_after_sec)
    return {
        "mode": "remote_agent",
        "last_seen_at": _pc_last_seen_at,
        "offline_after_sec": offline_after_sec,
        "observed_at": snapshot.observed_at,
        "active_state": snapshot.active_state,
        "last_input_age_sec": snapshot.last_input_age_sec,
        "foreground_app": snapshot.foreground_app,
        "foreground_title_sanitized": snapshot.foreground_title_sanitized,
        "foreground_feature_tag": get_feature_tag(snapshot.foreground_app),
    }


def _set_last_snapshot(snapshot: PcActivitySnapshot, seen_at: float) -> None:
    global _pc_last_seen_at, _pc_last_snapshot
    _pc_last_seen_at = seen_at
    _pc_last_snapshot = snapshot


def _entry_from_snapshot(snapshot: PcActivitySnapshot) -> dict[str, Any]:
    ts = snapshot.observed_at
    return {
        "timestamp": ts,
        "time": time.strftime("%H:%M:%S", time.localtime(ts)),
        "date": time.strftime("%Y-%m-%d", time.localtime(ts)),
        "device": "pc",
        "app": snapshot.foreground_app,
        "title": snapshot.foreground_title_sanitized,
        "active_state": snapshot.active_state,
        "last_input_age_sec": snapshot.last_input_age_sec,
    }


def _normalize_active_state(value: Any) -> str:
    state = str(value or "unknown").strip().lower() or "unknown"
    if state == "offline" or state not in VALID_AGENT_STATES:
        log.warning("Invalid PC active_state %r normalized to unknown", value)
        return "unknown"
    return state


def _defensive_title(value: Any) -> str | None:
    if value is None:
        return None
    title = str(value).strip()
    if not title:
        return ""
    if title == REDACTED_TITLE:
        return REDACTED_TITLE
    title = strip_url_like(title)
    if matches_sensitive_keyword(title):
        return REDACTED_TITLE
    return truncate_title(title)


def _last_input_age(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        age = int(value)
    except (TypeError, ValueError):
        return None
    return age if age >= 0 else None


def _timestamp(value: Any, *, default: float) -> float:
    if value is None or isinstance(value, bool):
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _reset_state_for_tests() -> None:
    global _pc_last_seen_at, _pc_last_snapshot
    _pc_last_seen_at = None
    _pc_last_snapshot = None
