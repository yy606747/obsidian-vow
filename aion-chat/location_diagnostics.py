"""
Location 外部事实链路诊断。

记录高德 API 等定位事实来源的摘要，不保存经纬度、地址或 API key。
"""

from __future__ import annotations

import json
import sys
import threading
import time
from collections import deque

from config import DATA_DIR
from provider_status import classify_exception, new_request_id


EVENTS_PATH = DATA_DIR / "location_events.jsonl"
MAX_EVENTS_FILE_BYTES = 2 * 1024 * 1024
MAX_EVENTS_FILE_LINES = 2000
_RECENT_EVENTS = deque(maxlen=200)
_LOCK = threading.Lock()

SENSITIVE_META_KEYS = {
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
    "amap_key",
    "key",
    "home_lat",
    "home_lng",
    "place_name",
    "monitoringlog",
}


def classify_amap_payload(data: dict | None) -> tuple[str, bool]:
    if not isinstance(data, dict):
        return "parse_error", False
    if data.get("status") == "1":
        return "ok", False
    info = str(data.get("info") or "").lower()
    infocode = str(data.get("infocode") or "")
    if "key" in info or infocode in {"10001", "10002", "10009"}:
        return "auth_error", False
    if "quota" in info or "limit" in info or infocode in {"10003", "10004", "10010", "10014"}:
        return "rate_limited", True
    return "amap_error", False


def _sanitize_meta(meta: dict | None) -> dict:
    if not isinstance(meta, dict):
        return {}
    out = {}
    for key, value in meta.items():
        if not isinstance(key, str):
            continue
        if _is_sensitive_meta_key(key):
            continue
        if isinstance(value, (bool, int, float)) or value is None:
            out[key[:40]] = value
        else:
            out[key[:40]] = str(value)[:120]
    return out


def _is_sensitive_meta_key(key: str) -> bool:
    normalized = key.strip().lower()
    return (
        normalized in SENSITIVE_META_KEYS
        or normalized.endswith("_lat")
        or normalized.endswith("_lng")
        or normalized.endswith("_longitude")
        or normalized.endswith("_latitude")
    )


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
            f"[LocationDiagnostics] compact_failed path={EVENTS_PATH} error={exc.__class__.__name__}: {exc}",
            file=sys.stderr,
        )


def record_location_event(event: dict) -> dict:
    clean = {
        "ts": event.get("ts") or time.time(),
        "request_id": event.get("request_id") or new_request_id("loc"),
        "scope": event.get("scope") or "location:unknown",
        "ok": bool(event.get("ok")),
        "http_status": event.get("http_status", event.get("status_code")),
        "amap_status": str(event.get("amap_status") or ""),
        "infocode": str(event.get("infocode") or ""),
        "info": str(event.get("info") or "")[:120],
        "error_type": event.get("error_type") or ("ok" if event.get("ok") else "unknown"),
        "retryable": bool(event.get("retryable")),
        "elapsed_ms": int(event.get("elapsed_ms") or 0),
        "empty_result": bool(event.get("empty_result")),
        "message": str(event.get("message") or "")[:200],
        "meta": _sanitize_meta(event.get("meta")),
    }
    with _LOCK:
        _RECENT_EVENTS.append(clean)
        try:
            EVENTS_PATH.parent.mkdir(exist_ok=True)
            _compact_events_file_if_needed()
            with EVENTS_PATH.open("a", encoding="utf-8") as f:
                f.write(json.dumps(clean, ensure_ascii=False) + "\n")
        except Exception as exc:
            print(
                f"[LocationDiagnostics] write_failed path={EVENTS_PATH} error={exc.__class__.__name__}: {exc}",
                file=sys.stderr,
            )
    return clean


def record_location_exception(*, scope: str, request_id: str, start: float,
                              exc: Exception, meta: dict | None = None) -> dict:
    if isinstance(exc, (json.JSONDecodeError, KeyError, IndexError, ValueError, TypeError)):
        error_type, retryable = "parse_error", False
    else:
        error_type, retryable = classify_exception(exc)
    return record_location_event({
        "request_id": request_id,
        "scope": scope,
        "ok": False,
        "error_type": error_type,
        "retryable": retryable,
        "elapsed_ms": (time.perf_counter() - start) * 1000,
        "message": exc.__class__.__name__,
        "meta": meta or {},
    })


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


def recent_location_events(limit: int = 50) -> list[dict]:
    limit = max(1, min(int(limit or 50), 200))
    with _LOCK:
        memory_events = list(_RECENT_EVENTS)[-limit:]
    combined = _load_file_tail(limit) + memory_events
    seen = set()
    deduped = []
    for ev in combined:
        key = (
            ev.get("request_id"), ev.get("ts"), ev.get("scope"),
            ev.get("elapsed_ms"), ev.get("ok"),
        )
        if key in seen:
            continue
        seen.add(key)
        deduped.append(ev)
    deduped.sort(key=lambda ev: ev.get("ts", 0), reverse=True)
    return deduped[:limit]


def summarize_location_events(limit: int = 200) -> list[dict]:
    events = list(reversed(recent_location_events(limit)))
    groups: dict[str, dict] = {}
    for ev in events:
        key = ev.get("scope") or "location:unknown"
        group = groups.setdefault(key, {
            "scope": key,
            "ok_count": 0,
            "fail_count": 0,
            "empty_count": 0,
            "last": None,
            "consecutive_failures": 0,
        })
        if ev.get("ok"):
            group["ok_count"] += 1
            group["consecutive_failures"] = 0
        else:
            group["fail_count"] += 1
            group["consecutive_failures"] += 1
        if ev.get("empty_result"):
            group["empty_count"] += 1
        group["last"] = ev

    out = []
    for item in groups.values():
        last = item.get("last") or {}
        if not last:
            status = "unknown"
        elif last.get("ok"):
            status = "ok"
        elif item.get("consecutive_failures", 0) >= 2:
            status = "down"
        else:
            status = "degraded"
        item["status"] = status
        out.append(item)
    out.sort(key=lambda x: (x.get("last") or {}).get("ts", 0), reverse=True)
    return out
