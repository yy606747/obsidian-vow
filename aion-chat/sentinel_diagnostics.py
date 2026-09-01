"""Durable Sentinel runtime diagnostics.

This log is for debugging Sentinel decisions. It stores bounded metadata only:
no prompts, recent chat text, raw provider output, or location text.
"""

from __future__ import annotations

import json
import sys
import threading
import time
from collections import deque

from config import DATA_DIR
from provider_status import new_request_id


EVENTS_PATH = DATA_DIR / "sentinel_events.jsonl"
MAX_EVENTS_FILE_BYTES = 2 * 1024 * 1024
MAX_EVENTS_FILE_LINES = 2000
_RECENT_EVENTS = deque(maxlen=200)
_LOCK = threading.Lock()

SENSITIVE_META_KEYS = {
    "prompt",
    "prompts",
    "messages",
    "recent_chat",
    "recent_chat_text",
    "raw_output",
    "raw_text",
    "response",
    "response_body",
    "monitoringlog",
    "summary",
    "core_reason",
    "location_text",
    "sensing_timeline",
    "activity_summary_text",
    "pc_context_text",
    "chat_status_text",
    "log_history",
    "lat",
    "lng",
    "lon",
    "latitude",
    "longitude",
    "address",
    "adcode",
    "province",
    "city",
    "district",
}


def _is_sensitive_meta_key(key: str) -> bool:
    normalized = key.strip().lower()
    return (
        normalized in SENSITIVE_META_KEYS
        or normalized.startswith("raw_")
        or normalized.endswith("_text")
        or normalized.endswith("_prompt")
        or normalized.endswith("_prompts")
        or normalized.endswith("_messages")
        or normalized.endswith("_message")
        or normalized.endswith("_lat")
        or normalized.endswith("_lng")
        or normalized.endswith("_latitude")
        or normalized.endswith("_longitude")
    )


def _sanitize_value(value):
    if isinstance(value, (bool, int, float)) or value is None:
        return value
    if isinstance(value, str):
        return value[:160]
    if isinstance(value, (list, tuple)):
        out = []
        for item in value[:20]:
            if isinstance(item, (bool, int, float)) or item is None:
                out.append(item)
            elif isinstance(item, dict):
                out.append(_sanitize_meta(item))
            else:
                out.append(str(item)[:160])
        return out
    if isinstance(value, dict):
        return _sanitize_meta(value)
    return str(value)[:160]


def _sanitize_meta(meta: dict | None) -> dict:
    if not isinstance(meta, dict):
        return {}
    out = {}
    for key, value in meta.items():
        if not isinstance(key, str):
            continue
        if _is_sensitive_meta_key(key):
            continue
        out[key[:60]] = _sanitize_value(value)
    return out


def _compact_events_file_if_needed() -> None:
    try:
        if not EVENTS_PATH.exists() or EVENTS_PATH.stat().st_size <= MAX_EVENTS_FILE_BYTES:
            return
        lines = EVENTS_PATH.read_text(encoding="utf-8").splitlines()[-MAX_EVENTS_FILE_LINES:]
        tmp_path = EVENTS_PATH.with_name(EVENTS_PATH.name + ".tmp")
        tmp_path.write_text(("\n".join(lines) + "\n") if lines else "", encoding="utf-8")
        tmp_path.replace(EVENTS_PATH)
    except Exception as exc:
        print(
            f"[SentinelDiagnostics] compact_failed path={EVENTS_PATH} error={exc.__class__.__name__}: {exc}",
            file=sys.stderr,
        )


def record_sentinel_event(event: dict) -> dict:
    clean = {
        "ts": event.get("ts") or time.time(),
        "request_id": event.get("request_id") or new_request_id("sentinel"),
        "scope": event.get("scope") or "sentinel:unknown",
        "ok": bool(event.get("ok")),
        "status": str(event.get("status") or ""),
        "error_type": str(event.get("error_type") or ""),
        "retryable": bool(event.get("retryable")),
        "elapsed_ms": int(event.get("elapsed_ms") or 0),
        "message": str(event.get("message") or "")[:200],
        "meta": _sanitize_meta(event.get("meta")),
    }
    with _LOCK:
        _RECENT_EVENTS.append(clean)
        try:
            EVENTS_PATH.parent.mkdir(parents=True, exist_ok=True)
            _compact_events_file_if_needed()
            with EVENTS_PATH.open("a", encoding="utf-8") as f:
                f.write(json.dumps(clean, ensure_ascii=False) + "\n")
        except Exception as exc:
            print(
                f"[SentinelDiagnostics] write_failed path={EVENTS_PATH} error={exc.__class__.__name__}: {exc}",
                file=sys.stderr,
            )
    return clean


def _load_file_tail(limit: int) -> list[dict]:
    if not EVENTS_PATH.exists():
        return []
    try:
        lines = EVENTS_PATH.read_text(encoding="utf-8").splitlines()[-limit:]
    except Exception:
        return []
    out = []
    for line in lines:
        try:
            out.append(json.loads(line))
        except Exception:
            continue
    return out


def recent_sentinel_events(limit: int = 50) -> list[dict]:
    limit = max(1, min(int(limit or 50), 200))
    with _LOCK:
        memory_events = list(_RECENT_EVENTS)[-limit:]
    combined = _load_file_tail(limit) + memory_events
    seen = set()
    deduped = []
    for ev in combined:
        key = (
            ev.get("request_id"), ev.get("ts"), ev.get("scope"),
            ev.get("elapsed_ms"), ev.get("ok"), ev.get("status"),
        )
        if key in seen:
            continue
        seen.add(key)
        deduped.append(ev)
    deduped.sort(key=lambda ev: ev.get("ts", 0), reverse=True)
    return deduped[:limit]


__all__ = [
    "EVENTS_PATH",
    "MAX_EVENTS_FILE_BYTES",
    "MAX_EVENTS_FILE_LINES",
    "record_sentinel_event",
    "recent_sentinel_events",
]
