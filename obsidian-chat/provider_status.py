"""
Provider 调用状态记录。

只记录排障需要的摘要信息，不保存 prompt、response body、API key。
"""

from __future__ import annotations

import json
import sys
import threading
import time
import uuid
from collections import deque
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

from config import DATA_DIR


EVENTS_PATH = DATA_DIR / "provider_events.jsonl"
MAX_EVENTS_FILE_BYTES = 2 * 1024 * 1024
MAX_EVENTS_FILE_LINES = 2000
_RECENT_EVENTS = deque(maxlen=200)
_LOCK = threading.Lock()

SENSITIVE_META_KEYS = {
    "prompt",
    "prompts",
    "messages",
    "message_body",
    "request_body",
    "response",
    "response_body",
    "raw_response",
    "body",
    "content",
    "api_key",
    "apikey",
    "key",
    "token",
    "access_token",
    "authorization",
    "password",
    "secret",
}


def new_request_id(prefix: str = "req") -> str:
    return f"{prefix}_{uuid.uuid4().hex[:10]}"


def mask_url(url: str) -> str:
    """去掉 query/userinfo，避免 Gemini key 或代理密码进日志。"""
    if not url:
        return ""
    try:
        parts = urlsplit(str(url))
        netloc = parts.hostname or ""
        if parts.port:
            netloc += f":{parts.port}"
        path = (parts.path or "").rstrip("/")
        return urlunsplit((parts.scheme, netloc, path, "", ""))
    except Exception:
        return str(url).split("?", 1)[0]


def classify_http_status(status_code: int | None) -> tuple[str, bool]:
    if status_code is None:
        return "unknown", False
    if status_code in (401, 403):
        return "auth_error", False
    if status_code == 404:
        return "not_found", False
    if status_code == 408:
        return "timeout", True
    if status_code == 429:
        return "rate_limited", True
    if status_code == 503:
        return "upstream_overloaded", True
    if status_code in (502, 504):
        return "gateway_error", True
    if status_code == 500:
        return "server_error", True
    if 400 <= status_code < 500:
        return "bad_request", False
    if status_code >= 500:
        return "server_error", True
    return "ok", False


def classify_exception(exc: Exception) -> tuple[str, bool]:
    name = exc.__class__.__name__.lower()
    text = str(exc).lower()
    if "timeout" in name or "timeout" in text:
        return "timeout", True
    if "connect" in name or "network" in name or "readerror" in name:
        return "network_error", True
    return "exception", False


def _sanitize_meta(meta: dict | None) -> dict:
    """只保留排障摘要，避免 prompt/response/body/key 进入诊断日志。"""
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
        or normalized.startswith("raw_")
        or normalized.endswith("_prompt")
        or normalized.endswith("_prompts")
        or normalized.endswith("_messages")
        or normalized.endswith("_body")
        or normalized.endswith("_content")
        or normalized.endswith("_key")
        or normalized.endswith("_token")
        or normalized.endswith("_secret")
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
            f"[ProviderStatus] compact_failed path={EVENTS_PATH} error={exc.__class__.__name__}: {exc}",
            file=sys.stderr,
        )


def record_provider_event(event: dict) -> dict:
    clean = {
        "ts": event.get("ts") or time.time(),
        "request_id": event.get("request_id") or new_request_id(),
        "scope": event.get("scope") or "unknown",
        "provider_type": event.get("provider_type") or "",
        "provider_label": event.get("provider_label") or "",
        "endpoint_id": event.get("endpoint_id") or "",
        "endpoint_name": event.get("endpoint_name") or "",
        "base_url": mask_url(event.get("base_url") or ""),
        "model": event.get("model") or "",
        "ok": bool(event.get("ok")),
        "http_status": event.get("http_status", event.get("status_code")),
        "error_type": event.get("error_type") or ("ok" if event.get("ok") else "unknown"),
        "retryable": bool(event.get("retryable")),
        "elapsed_ms": int(event.get("elapsed_ms") or 0),
        "proxy_enabled": bool(event.get("proxy_enabled")),
        "proxy_url": mask_url(event.get("proxy_url") or ""),
        "message": str(event.get("message") or "")[:300],
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
                f"[ProviderStatus] write_failed path={EVENTS_PATH} error={exc.__class__.__name__}: {exc}",
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


def recent_provider_events(limit: int = 50) -> list[dict]:
    limit = max(1, min(int(limit or 50), 200))
    with _LOCK:
        memory_events = list(_RECENT_EVENTS)[-limit:]
    combined = _load_file_tail(limit) + memory_events
    seen = set()
    deduped = []
    for ev in combined:
        key = (
            ev.get("request_id"), ev.get("ts"), ev.get("scope"), ev.get("endpoint_id"),
            ev.get("model"), ev.get("elapsed_ms"), ev.get("ok"),
        )
        if key in seen:
            continue
        seen.add(key)
        deduped.append(ev)
    deduped.sort(key=lambda ev: ev.get("ts", 0), reverse=True)
    return deduped[:limit]


def summarize_provider_events(limit: int = 200) -> list[dict]:
    events = list(reversed(recent_provider_events(limit)))
    groups: dict[str, dict] = {}
    for ev in events:
        key = ev.get("endpoint_id") or f"{ev.get('provider_label')}:{ev.get('base_url')}"
        group = groups.setdefault(key, {
            "endpoint_id": ev.get("endpoint_id") or "",
            "endpoint_name": ev.get("endpoint_name") or ev.get("provider_label") or "",
            "provider_type": ev.get("provider_type") or "",
            "model": ev.get("model") or "",
            "ok_count": 0,
            "fail_count": 0,
            "last": None,
            "consecutive_failures": 0,
            "cache_metric_calls": 0,
            "cache_hit_calls": 0,
            "prompt_tokens": 0,
            "cache_read_tokens": 0,
            "cache_write_tokens": 0,
        })
        if ev.get("ok"):
            group["ok_count"] += 1
            group["consecutive_failures"] = 0
        else:
            group["fail_count"] += 1
            group["consecutive_failures"] += 1
        group["last"] = ev
        group["model"] = ev.get("model") or group["model"]
        group["provider_type"] = ev.get("provider_type") or group["provider_type"]
        group["endpoint_name"] = ev.get("endpoint_name") or group["endpoint_name"]
        meta = ev.get("meta") if isinstance(ev.get("meta"), dict) else {}
        if meta.get("cache_metrics_reported"):
            group["cache_metric_calls"] += 1
            group["cache_hit_calls"] += int(bool(meta.get("cache_hit")))
            group["prompt_tokens"] += int(meta.get("prompt_tokens") or 0)
            group["cache_read_tokens"] += int(meta.get("cache_read_tokens") or 0)
            group["cache_write_tokens"] += int(meta.get("cache_write_tokens") or 0)

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
        measured = item.get("cache_metric_calls", 0)
        prompt_tokens = item.get("prompt_tokens", 0)
        item["cache_hit_rate"] = (
            item.get("cache_hit_calls", 0) / measured if measured else None
        )
        item["cache_token_rate"] = (
            item.get("cache_read_tokens", 0) / prompt_tokens if prompt_tokens else None
        )
        out.append(item)
    out.sort(key=lambda x: (x.get("last") or {}).get("ts", 0), reverse=True)
    return out
