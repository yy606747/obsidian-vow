"""Background Sentinel runtime.

The old local camera monitor has been disabled. This module owns the text-only
Sentinel scheduler and monitor logs; camera compatibility stays in camera.py.
"""

import json, time, re, asyncio, threading, sqlite3, random
from collections.abc import Awaitable, Callable, Mapping
from pathlib import Path
from typing import Any

import aiosqlite

from config import (
    DB_PATH, MONITOR_LOGS_DIR,
    load_worldbook, load_chat_status, load_cam_config, save_cam_config,
    load_ai_behavior, DEFAULT_MODEL,
)
from database import get_db
from ws import manager
from app.memory_v3.timeline import timeline_service
from app.chat.worldbook import build_worldbook_prefix, resolve_worldbook_names
from app.chat.autonomous_capabilities import (
    autonomous_context_delivery_enabled,
    load_autonomous_context_delivery,
    render_autonomous_context_delivery,
)
from app.context_delivery.safety import render_device_proxy_hard_limits
from app.pc_context.app_map import get_feature_tag
from app.pc_context.service import get_pc_status
from app.tools.ledger import tool_invocation_ledger
from app.tools.prompt_renderers import render_registered_capabilities
from app.tools.registry import validate_turn_advertisement
from app.tools.schemas import ToolContext
from app.sentinel import (
    CORE_WAKE_EXECUTION_SCHEMA_VERSION,
    CORE_WAKE_EXECUTION_MODE_DISABLED,
    CORE_WAKE_EXECUTION_MODE_FULL,
    CORE_WAKE_PREFLIGHT_SCHEMA_VERSION,
    build_core_wake_preflight,
    run_core_wake_orchestrator_dry_run,
    run_core_wake_orchestrator_full_execute,
    run_sentinel_chain_dry_run,
)
from _supervisor import log_future_exception
from ai_providers import call_slot_chat
from sentinel_diagnostics import record_sentinel_event
from sentinel_core_wake_adapters import (
    build_legacy_core_wake_ports,
    load_sentinel_timeline_prompt_context,
    load_sentinel_working_model_prompt_context,
    record_sentinel_timeline_injection_usage,
)
from sentinel_runtime_readers import read_core_wake_execution_context, read_sentinel_runtime_context


SENTINEL_V2_SHADOW_SCHEMA_VERSION = "sentinel_v2_shadow.v0"
SENTINEL_V2_PROVIDER_SHADOW_EFFECT = "sentinel_provider_chat_completion"
SentinelJudgmentProvider = Callable[[list[dict[str, str]]], Awaitable[str] | str]
_PUBLIC_SENTINEL_FAILURE_MESSAGE = "⚠️ 哨兵本轮判断失败，已跳过。"
_PUBLIC_MONITOR_PRIVATE_FIELDS = frozenset({
    "context_errors",
    "core_wake_execution",
    "core_wake_preflight",
    "error",
    "error_type",
    "original_error",
    "sentinel_v2_shadow",
})


def _relationship_prompt_text(value, *, user_name: str, ai_name: str) -> str:
    text = str(value or "").replace("用户", user_name)
    return re.sub(r"(?<![A-Za-z])AI(?![A-Za-z])", ai_name, text)


# ── 监控日志文件读写 ──────────────────────────────
def _today_log_path() -> Path:
    return MONITOR_LOGS_DIR / f"{time.strftime('%Y-%m-%d')}.jsonl"


def append_monitor_log(entry: dict):
    path = _today_log_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")


async def append_and_broadcast_monitor_log(entry: dict) -> bool:
    """Best-effort monitor log output. Returns whether durable append worked."""
    appended = False
    try:
        append_monitor_log(entry)
        appended = True
    except Exception as exc:
        print(f"[Sentinel] monitor log append failed: {type(exc).__name__}: {exc}")

    try:
        await manager.broadcast({
            "type": "monitor_log",
            "data": _public_monitor_log_entry(entry),
        })
    except Exception as exc:
        print(f"[Sentinel] monitor log broadcast failed: {type(exc).__name__}: {exc}")

    return appended


def _public_monitor_log_entry(entry: dict) -> dict:
    """Return the presentation-safe form; the durable diagnostic stays intact."""
    public = dict(entry)
    status = str(public.get("status") or "")
    failed = bool(public.get("error") or public.get("error_type")) or status.endswith("_failed")
    for key in _PUBLIC_MONITOR_PRIVATE_FIELDS:
        public.pop(key, None)
    if failed:
        public["monitoringlog"] = _PUBLIC_SENTINEL_FAILURE_MESSAGE
    return public


def _sentinel_monitor_log_entry(
    *,
    status: str,
    monitoringlog: str,
    call_core: bool = False,
    core_reason: str = "",
    summary: str = "",
    **extra,
) -> dict:
    now = time.time()
    entry = {
        "timestamp": now,
        "time": time.strftime("%H:%M:%S", time.localtime(now)),
        "date": time.strftime("%Y-%m-%d", time.localtime(now)),
        "monitoringlog": monitoringlog,
        "summary": summary,
        "score": None,
        "call_core": call_core,
        "core_reason": core_reason,
        "screenshot": "",
        "source": "sentinel",
        "status": status,
    }
    entry.update(extra)
    return entry


def read_monitor_logs(date_str: str = None) -> list:
    if not date_str:
        date_str = time.strftime('%Y-%m-%d')
    path = MONITOR_LOGS_DIR / f"{date_str}.jsonl"
    if not path.exists():
        return []
    entries = []
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    entry = json.loads(line)
                    if isinstance(entry, dict):
                        entries.append(_public_monitor_log_entry(entry))
                except Exception:
                    pass
    return entries


def read_logs_since(since_ts: float) -> list:
    import datetime as _dt
    since_date = _dt.date.fromtimestamp(since_ts)
    result = []
    for logfile in sorted(MONITOR_LOGS_DIR.glob("*.jsonl")):
        try:
            if _dt.date.fromisoformat(logfile.stem) < since_date:
                continue
        except ValueError:
            pass
        with open(logfile, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                    if entry.get("timestamp", 0) >= since_ts:
                        result.append(entry)
                except Exception:
                    pass
    return result


def cleanup_old_logs(keep_days: int = 3):
    import datetime
    cutoff = datetime.date.today() - datetime.timedelta(days=keep_days)
    for logfile in MONITOR_LOGS_DIR.glob("*.jsonl"):
        try:
            file_date = datetime.date.fromisoformat(logfile.stem)
            if file_date < cutoff:
                logfile.unlink()
        except Exception:
            pass


def get_last_user_msg_time() -> float:
    conn = sqlite3.connect(str(DB_PATH))
    try:
        cur = conn.execute("SELECT created_at FROM messages WHERE role='user' ORDER BY created_at DESC LIMIT 1")
        row = cur.fetchone()
        return row[0] if row else 0
    finally:
        conn.close()


async def async_get_last_user_msg_time() -> float:
    async with get_db() as db:
        cur = await db.execute("SELECT created_at FROM messages WHERE role='user' ORDER BY created_at DESC LIMIT 1")
        row = await cur.fetchone()
        return row[0] if row else 0


async def _safe_last_user_msg_time(context_errors: list[str]) -> float:
    try:
        return await async_get_last_user_msg_time()
    except Exception as exc:
        msg = f"last_user_time_failed: {type(exc).__name__}: {exc}"
        context_errors.append(msg)
        print(f"[Sentinel] {msg}")
        return 0


def _safe_logs_since(since_ts: float, context_errors: list[str]) -> list:
    try:
        return read_logs_since(since_ts)
    except Exception as exc:
        msg = f"monitor_logs_failed: {type(exc).__name__}: {exc}"
        context_errors.append(msg)
        print(f"[Sentinel] {msg}")
        return []


def _append_context_error(context_errors: list[str], scope: str, exc: Exception) -> None:
    msg = f"{scope}: {type(exc).__name__}: {exc}"
    context_errors.append(msg)
    print(f"[Sentinel] {msg}")


def _sentinel_cycle_ok(status: str, error_type: str = "") -> bool:
    if error_type:
        return False
    status_text = (status or "").lower()
    return not (
        "failed" in status_text
        or status_text in {"provider_empty", "provider_failed", "parse_fallback"}
    )


def _sentinel_shadow_summary(shadow: dict | None) -> dict:
    if not isinstance(shadow, dict):
        return {}
    gate = shadow.get("gate") if isinstance(shadow.get("gate"), dict) else {}
    preflight = shadow.get("core_wake_preflight") if isinstance(shadow.get("core_wake_preflight"), dict) else {}
    execution = shadow.get("core_wake_execution") if isinstance(shadow.get("core_wake_execution"), dict) else {}
    return {
        "v2_shadow_status": shadow.get("status") or "",
        "v2_judgment_source": shadow.get("judgment_source") or "",
        "v2_gate_status": gate.get("status") or "",
        "v2_gate_blocked_reasons": gate.get("blocked_reasons") or [],
        "v2_wake_package_created": bool(shadow.get("wake_package_created")),
        "v2_fallback_used": bool(shadow.get("fallback_used")),
        "v2_error_type": shadow.get("error_type") or "",
        "v2_core_preflight_status": preflight.get("status") or "",
        "v2_core_execution_status": execution.get("status") or "",
        "v2_core_execution_mode": execution.get("execution_mode") or "",
    }


def _record_sentinel_cycle_summary(
    *,
    request_id: str,
    started_at: float,
    status: str,
    score,
    call_core: bool,
    wake_threshold: int,
    wake_blocked_reason: str,
    context_errors: list[str],
    signal_meta: dict,
    provider_enabled: bool,
    full_wake_enabled: bool,
    legacy_fallback_enabled: bool,
    used_v2_primary: bool,
    legacy_call_core: bool | None,
    parse_fallback: bool,
    monitor_log_appended: bool | None,
    sentinel_v2_shadow: dict | None,
    full_wake_unavailable_reason: str = "",
    error_type: str = "",
    error: str = "",
) -> None:
    meta = {
        "score": score,
        "call_core": bool(call_core),
        "wake_threshold": wake_threshold,
        "wake_blocked_reason": wake_blocked_reason,
        "context_error_count": len(context_errors),
        "context_errors": list(context_errors),
        "provider_enabled": bool(provider_enabled),
        "full_wake_enabled": bool(full_wake_enabled),
        "legacy_fallback_enabled": bool(legacy_fallback_enabled),
        "used_v2_primary": bool(used_v2_primary),
        "legacy_call_core": legacy_call_core,
        "parse_fallback": bool(parse_fallback),
        "monitor_log_appended": monitor_log_appended,
        "full_wake_unavailable_reason": full_wake_unavailable_reason,
    }
    meta.update(signal_meta or {})
    meta.update(_sentinel_shadow_summary(sentinel_v2_shadow))
    if error:
        meta["error"] = error
    try:
        record_sentinel_event({
            "request_id": request_id,
            "scope": "sentinel:cycle_summary",
            "ok": _sentinel_cycle_ok(status, error_type),
            "status": status,
            "error_type": error_type,
            "elapsed_ms": (time.perf_counter() - started_at) * 1000,
            "message": status,
            "meta": meta,
        })
    except Exception as exc:
        print(f"[Sentinel] cycle summary failed: {type(exc).__name__}: {exc}")


def _regex_extract_sentinel(raw: str) -> tuple:
    """JSON 解析失败时用 regex 从残缺输出中抠字段。返回 (log, score, summary, core_reason)。"""
    monitoring_log = ""
    score = 0
    summary = ""
    core_reason = ""

    def _extract(key: str) -> str:
        m = re.search(rf'"{key}"\s*:\s*"((?:[^"\\]|\\.)*)"', raw)
        return m.group(1).replace('\\"', '"').replace('\\n', '\n') if m else ""

    monitoring_log = _extract("monitoringlog")
    summary = _extract("summary")
    core_reason = _extract("core_reason")

    m_score = re.search(r'"score"\s*:\s*(\d+)', raw)
    if m_score:
        score = min(10, max(0, int(m_score.group(1))))

    if not monitoring_log:
        cleaned = re.sub(r'[{}"\\]', '', raw)
        cleaned = re.sub(r'(monitoringlog|summary|score|core_reason)\s*:', '', cleaned)
        cleaned = re.sub(r'\s\d+\s', ' ', cleaned)
        monitoring_log = cleaned.strip()[:500] or "[Sentinel 输出无法解析]"

    return monitoring_log, score, summary, core_reason


def _legacy_attention_input_payload(
    *,
    reference_time: float,
    location_text: str = "",
    sensing_timeline: str = "",
    activity_summary_text: str = "",
    pc_context_text: str = "",
    recent_chat_text: str = "",
    chat_status_text: str = "",
    log_history: str = "",
    context_projection: Mapping[str, Any] | None = None,
) -> dict:
    """Build a replay-style Attention input from material already read by legacy runtime."""
    raw_signals = []
    structured_location_signals = _projection_attention_location_signals(
        context_projection,
    )
    raw_signals.extend(structured_location_signals)
    if (
        not structured_location_signals
        and not isinstance(context_projection, Mapping)
        and location_text.strip()
    ):
        raw_signals.append({
            "kind": "location.fix",
            "source": "legacy.location",
            "text": location_text.strip(),
        })
    if sensing_timeline.strip():
        raw_signals.append({
            "kind": "sensing.screen",
            "source": "legacy.sensing",
            "text": sensing_timeline.strip(),
        })
    if activity_summary_text.strip():
        raw_signals.append({
            "kind": "activity.app",
            "source": "legacy.activity",
            "text": activity_summary_text.strip(),
        })
    if pc_context_text.strip():
        raw_signals.append({
            "kind": "activity.app",
            "source": "pc.activity",
            "text": pc_context_text.strip(),
        })
    if chat_status_text.strip():
        raw_signals.append({
            "kind": "chat.recent",
            "source": "legacy.chat_status",
            "text": chat_status_text.strip(),
        })
    if log_history.strip():
        raw_signals.append({
            "kind": "sentinel.log",
            "source": "legacy.monitor_logs",
            "text": log_history.strip(),
        })

    recent_chat = [
        line.strip()
        for line in recent_chat_text.splitlines()
        if line.strip()
    ][-10:]
    return {
        "reference_time": time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime(reference_time)),
        "raw_signals": raw_signals,
        "recent_chat": recent_chat,
    }


def _projection_attention_location_signals(
    projection: Mapping[str, Any] | None,
) -> list[dict[str, Any]]:
    if not isinstance(projection, Mapping):
        return []
    recent_events = projection.get("recent_events")
    if isinstance(recent_events, list):
        structured_events = [
            item
            for item in recent_events
            if isinstance(item, Mapping)
            and item.get("key") == "location.place"
            and isinstance(item.get("payload"), Mapping)
        ]
        if structured_events:
            latest = max(
                structured_events,
                key=lambda item: float(item.get("observed_at") or 0.0),
            )
            return [{
                "kind": "location.geofence",
                "source": "context_delivery.projection",
                "text": "设备围栏记录（自然语言不参与判断）",
                "payload": dict(latest["payload"]),
            }]

    observations = projection.get("observations")
    if not isinstance(observations, list):
        return []
    current = next(
        (
            item
            for item in observations
            if isinstance(item, Mapping)
            and item.get("key") == "location.place"
            and isinstance(item.get("payload"), Mapping)
        ),
        None,
    )
    if current is None:
        return []
    return [{
        "kind": "location.geofence",
        "source": "context_delivery.projection",
        "text": "设备围栏记录（自然语言不参与判断）",
        "payload": dict(current["payload"]),
    }]


def _read_location_text_for_sentinel(context_errors: list[str] | None = None) -> str:
    try:
        from location import format_location_for_sentinel
        return format_location_for_sentinel()
    except Exception as sentinel_exc:
        try:
            from location import format_location_for_prompt
            return format_location_for_prompt()
        except Exception as prompt_exc:
            if context_errors is not None:
                _append_context_error(
                    context_errors,
                    "location_text_failed",
                    RuntimeError(
                        "sentinel="
                        f"{type(sentinel_exc).__name__}: {sentinel_exc}; "
                        f"prompt={type(prompt_exc).__name__}: {prompt_exc}"
                    ),
                )
            return ""


def _pc_context_attention_text(reference_time: float) -> str:
    snapshot = get_pc_status(reference_time)
    state = snapshot.active_state
    parts = [f"pc_state={state}", f"pc_{state}"]
    if state == "offline":
        return "PC activity remote agent offline; pc_state=offline pc_offline"
    if snapshot.foreground_app:
        parts.append(f"app={snapshot.foreground_app}")
        tag = get_feature_tag(snapshot.foreground_app)
        if tag:
            parts.append(tag)
    if snapshot.foreground_title_sanitized:
        parts.append(f"title={snapshot.foreground_title_sanitized}")
    if state == "active":
        parts.append("PC active is context only and not a wake trigger")
    return "PC activity " + " ".join(parts)


def _legacy_shadow_judgment_payload(
    *,
    monitoring_log: str,
    score: int,
    summary: str,
    core_reason: str,
    call_core: bool,
    wake_blocked_reason: str,
) -> dict:
    """Convert the already-paid legacy Sentinel result into the new judgment schema."""
    score = min(10, max(0, int(score or 0)))
    monitoring_log = (monitoring_log or "旧 Sentinel 未生成有效日志。").strip()
    summary = (summary or monitoring_log).strip()
    wake_intent = bool(call_core)
    if wake_intent:
        normalized_core_reason = (core_reason or monitoring_log).strip()
        restraint_reason = ""
        tone_hint = normalized_core_reason[:120]
    else:
        normalized_core_reason = ""
        if wake_blocked_reason:
            restraint_reason = f"旧 runtime 已因 {wake_blocked_reason} 阻断唤醒。"
        elif score < 7:
            restraint_reason = "旧 Sentinel 分数未达到旧唤醒阈值。"
        else:
            restraint_reason = "旧 Sentinel 本轮没有请求 Core 唤醒。"
        tone_hint = ""

    return {
        "monitoringlog": monitoring_log,
        "summary": summary,
        "score": score,
        "confidence": 0.65 if monitoring_log else 0.45,
        "wake_intent": wake_intent,
        "call_core": wake_intent,
        "core_reason": normalized_core_reason,
        "restraint_reason": restraint_reason,
        "uncertainty": "这是旧 Sentinel 输出转换成的新链路 shadow judgment，尚未调用新 Sentinel provider。",
        "suggested_next_check_sec": 600,
        "tone_hint": tone_hint,
    }


def _config_bool(value) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return bool(value)


def _sentinel_v2_provider_enabled(ai_beh: dict) -> bool:
    if "sentinel_v2_provider_enabled" in ai_beh:
        return _config_bool(ai_beh.get("sentinel_v2_provider_enabled"))
    return _config_bool(ai_beh.get("sentinel_v2_provider_shadow_enabled", False))


def _sentinel_v2_full_wake_enabled(ai_beh: dict) -> bool:
    return _config_bool(ai_beh.get("sentinel_v2_full_wake_enabled", False))


def _sentinel_v2_full_wake_legacy_fallback_enabled(ai_beh: dict) -> bool:
    return _config_bool(ai_beh.get("sentinel_v2_full_wake_legacy_fallback_enabled", False))


async def _runtime_sentinel_provider(messages: list[dict[str, str]]) -> str:
    raw_text = await call_slot_chat(
        "sentinel",
        messages=messages,
        expect_json=True,
        timeout=60,
        temperature=0.2,
    )
    if not raw_text:
        raise RuntimeError("sentinel provider returned empty output")
    return raw_text


def _shadow_failure_payload(
    *,
    request_id: str,
    judgment_source: str,
    side_effects: list[str],
    exc: Exception,
) -> dict:
    return {
        "schema_version": SENTINEL_V2_SHADOW_SCHEMA_VERSION,
        "runtime_mode": "dry_run",
        "status": "failed",
        "request_id": request_id,
        "judgment_source": judgment_source,
        "side_effects": list(side_effects),
        "production_side_effects": [],
        "fallback_used": False,
        "fallback_reason": "",
        "error_type": type(exc).__name__,
        "error": str(exc),
    }


def _compact_core_wake_preflight(preflight: dict) -> dict:
    compact = {
        "schema_version": preflight["schema_version"],
        "runtime_mode": preflight["runtime_mode"],
        "status": preflight["status"],
        "side_effects": list(preflight["side_effects"]),
        "production_side_effects": list(preflight["production_side_effects"]),
        "planned_production_side_effects": list(preflight.get("planned_production_side_effects", [])),
        "fallback_used": preflight["fallback_used"],
        "fallback_reason": preflight["fallback_reason"],
        "conv_id": preflight.get("conv_id", ""),
        "model_key": preflight.get("model_key", ""),
        "would_call_core": preflight.get("would_call_core", False),
        "would_write_monitor_log": preflight.get("would_write_monitor_log", ""),
        "wake_reason": preflight.get("wake_reason", ""),
    }
    if preflight.get("gate"):
        compact["gate"] = {
            "status": preflight["gate"].get("status"),
            "wake_allowed": preflight["gate"].get("wake_allowed"),
            "blocked_reasons": list(preflight["gate"].get("blocked_reasons", [])),
        }
    if preflight.get("core_request"):
        compact["core_request"] = {
            "history_message_count": preflight["core_request"]["history_message_count"],
            "prompt_char_count": preflight["core_request"]["prompt_char_count"],
            "system_notice": preflight["core_request"]["system_notice"],
        }
    if preflight.get("error_type"):
        compact["error_type"] = preflight["error_type"]
        compact["error"] = preflight.get("error", "")
    return compact


def _core_wake_preflight_failure_payload(exc: Exception) -> dict:
    return {
        "schema_version": CORE_WAKE_PREFLIGHT_SCHEMA_VERSION,
        "runtime_mode": "dry_run",
        "status": "failed",
        "side_effects": [],
        "production_side_effects": [],
        "planned_production_side_effects": [],
        "fallback_used": False,
        "fallback_reason": "",
        "conv_id": "",
        "model_key": "",
        "would_call_core": False,
        "would_write_monitor_log": "core_preflight_reader_failed",
        "error_type": type(exc).__name__,
        "error": str(exc),
    }


def _compact_core_wake_execution(execution: dict) -> dict:
    compact = {
        "schema_version": execution["schema_version"],
        "runtime_mode": execution["runtime_mode"],
        "execution_mode": execution["execution_mode"],
        "status": execution["status"],
        "request_id": execution.get("request_id", ""),
        "side_effects": list(execution["side_effects"]),
        "production_side_effects": list(execution["production_side_effects"]),
        "planned_production_side_effects": list(execution.get("planned_production_side_effects", [])),
        "fallback_used": execution["fallback_used"],
        "fallback_reason": execution["fallback_reason"],
        "execution_enabled": execution["execution_enabled"],
        "would_call_core": execution["would_call_core"],
        "would_write_monitor_log": execution["would_write_monitor_log"],
        "conv_id": execution.get("conv_id", ""),
        "model_key": execution.get("model_key", ""),
        "wake_reason": execution.get("wake_reason", ""),
        "core_request": dict(execution.get("core_request") or {}),
        "execution_steps": list(execution.get("execution_steps") or []),
        "context_errors": list(execution.get("context_errors") or []),
    }
    if execution.get("simulated_outcome"):
        compact["simulated_outcome"] = execution["simulated_outcome"]
    if execution.get("execution_policy"):
        compact["execution_policy"] = dict(execution["execution_policy"])
    if execution.get("core_attempts"):
        compact["core_attempts"] = list(execution["core_attempts"])
    if execution.get("timing_events"):
        compact["timing_events"] = list(execution["timing_events"])
    if execution.get("error_type"):
        compact["error_type"] = execution["error_type"]
        compact["error"] = execution.get("error", "")
    return compact


def _core_wake_execution_failure_payload(exc: Exception) -> dict:
    return {
        "schema_version": CORE_WAKE_EXECUTION_SCHEMA_VERSION,
        "runtime_mode": "dry_run",
        "execution_mode": CORE_WAKE_EXECUTION_MODE_DISABLED,
        "status": "failed",
        "side_effects": [],
        "production_side_effects": [],
        "planned_production_side_effects": [],
        "fallback_used": False,
        "fallback_reason": "",
        "execution_enabled": False,
        "would_call_core": False,
        "would_write_monitor_log": "core_orchestrator_reader_failed",
        "conv_id": "",
        "model_key": "",
        "wake_reason": "",
        "core_request": {},
        "execution_steps": [],
        "context_errors": [],
        "error_type": type(exc).__name__,
        "error": str(exc),
    }


def _core_wake_full_execution_exception_payload(*, exc: BaseException, request_id: str) -> dict:
    return {
        "schema_version": CORE_WAKE_EXECUTION_SCHEMA_VERSION,
        "runtime_mode": "full",
        "execution_mode": CORE_WAKE_EXECUTION_MODE_FULL,
        "status": "failed",
        "request_id": request_id,
        "side_effects": [],
        "production_side_effects": [],
        "planned_production_side_effects": [],
        "fallback_used": False,
        "fallback_reason": "",
        "execution_enabled": False,
        "would_call_core": False,
        "would_write_monitor_log": "core_orchestrator_full_failed",
        "conv_id": "",
        "model_key": "",
        "wake_reason": "",
        "core_request": {},
        "execution_steps": [],
        "context_errors": [],
        "error_type": type(exc).__name__,
        "error": str(exc),
    }


async def _build_shadow_core_wake_traces(
    *,
    wake_package: dict | None,
    reference_time: float,
    request_id: str,
) -> tuple[dict | None, dict | None]:
    if wake_package is None:
        return None, None
    try:
        execution_context = await read_core_wake_execution_context(reference_time=reference_time)
        preflight = build_core_wake_preflight(
            wake_package=wake_package,
            execution_context=execution_context,
        )
        execution = run_core_wake_orchestrator_dry_run(
            wake_package=wake_package,
            execution_context=execution_context,
            execution_mode=CORE_WAKE_EXECUTION_MODE_DISABLED,
            request_id=request_id,
            preflight=preflight,
        )
        return _compact_core_wake_preflight(preflight), _compact_core_wake_execution(execution)
    except Exception as exc:
        return _core_wake_preflight_failure_payload(exc), _core_wake_execution_failure_payload(exc)


async def _run_sentinel_v2_shadow_dry_run(
    *,
    reference_time: float,
    location_text: str = "",
    sensing_timeline: str = "",
    activity_summary_text: str = "",
    pc_context_text: str = "",
    recent_chat_text: str = "",
    chat_status_text: str = "",
    log_history: str = "",
    monitoring_log: str,
    score: int,
    summary: str,
    core_reason: str,
    call_core: bool,
    wake_blocked_reason: str,
    provider_enabled: bool = False,
    judgment_provider: SentinelJudgmentProvider | None = None,
    conv_id: str = "",
) -> dict:
    """Run the new Sentinel chain as a sidecar trace without changing legacy behavior."""
    material = await _run_sentinel_v2_shadow_material(
        reference_time=reference_time,
        location_text=location_text,
        sensing_timeline=sensing_timeline,
        activity_summary_text=activity_summary_text,
        pc_context_text=pc_context_text,
        recent_chat_text=recent_chat_text,
        chat_status_text=chat_status_text,
        log_history=log_history,
        monitoring_log=monitoring_log,
        score=score,
        summary=summary,
        core_reason=core_reason,
        call_core=call_core,
        wake_blocked_reason=wake_blocked_reason,
        provider_enabled=provider_enabled,
        judgment_provider=judgment_provider,
        conv_id=conv_id,
    )
    return material["shadow"]


async def _run_sentinel_v2_shadow_material(
    *,
    reference_time: float,
    location_text: str = "",
    sensing_timeline: str = "",
    activity_summary_text: str = "",
    pc_context_text: str = "",
    recent_chat_text: str = "",
    chat_status_text: str = "",
    log_history: str = "",
    monitoring_log: str,
    score: int,
    summary: str,
    core_reason: str,
    call_core: bool,
    wake_blocked_reason: str,
    provider_enabled: bool = False,
    judgment_provider: SentinelJudgmentProvider | None = None,
    conv_id: str = "",
) -> dict:
    """Run the new Sentinel chain and keep the wake package out of monitor logs."""
    request_id = f"sentinel_v2_shadow_{int(reference_time * 1000)}"
    judgment_source = "provider" if provider_enabled else "legacy_shadow"
    side_effects: list[str] = []
    try:
        runtime_context = await read_sentinel_runtime_context(reference_time=reference_time)
        projection = runtime_context.get("wake_context", {}).get("context_projection")
        input_payload = _legacy_attention_input_payload(
            reference_time=reference_time,
            location_text=location_text,
            sensing_timeline=sensing_timeline,
            activity_summary_text=activity_summary_text,
            pc_context_text=pc_context_text,
            recent_chat_text=recent_chat_text,
            chat_status_text=chat_status_text,
            log_history=log_history,
            context_projection=projection,
        )

        if provider_enabled:
            selected_provider = judgment_provider or _runtime_sentinel_provider

            async def provider(messages):
                if SENTINEL_V2_PROVIDER_SHADOW_EFFECT not in side_effects:
                    side_effects.append(SENTINEL_V2_PROVIDER_SHADOW_EFFECT)
                invocation_id = tool_invocation_ledger.new_invocation_id(
                    "sentinel_judgment"
                )
                context = ToolContext(
                    conv_id=conv_id or "__sentinel__",
                    request_id=request_id,
                    model_key="slot:sentinel",
                    metadata={
                        "source": "sentinel_judgment",
                        "source_chain": "sentinel",
                        "invocation_id": invocation_id,
                        "advertised_tools": (),
                    },
                )
                await tool_invocation_ledger.record_model_request(
                    context,
                    invocation_id=invocation_id,
                    request_snapshot=messages,
                    advertised_tools=(),
                    metadata={"judgment_source": judgment_source},
                )
                raw_value = ""
                error = ""
                try:
                    raw_result = selected_provider(messages)
                    raw_value = (
                        await raw_result
                        if hasattr(raw_result, "__await__")
                        else raw_result
                    )
                    return raw_value
                except Exception as exc:
                    error = str(exc)
                    raise
                finally:
                    await tool_invocation_ledger.record_model_output(
                        context,
                        invocation_id=invocation_id,
                        raw_output=raw_value,
                        outcome="failed" if error else "succeeded",
                        error=error,
                    )
                    await tool_invocation_ledger.record_turn(
                        context,
                        prompt_source="sentinel_judgment",
                        advertised_tools=(),
                        turn_outcome="failed" if error else "succeeded",
                    )
        else:
            judgment_payload = _legacy_shadow_judgment_payload(
                monitoring_log=monitoring_log,
                score=score,
                summary=summary,
                core_reason=core_reason,
                call_core=call_core,
                wake_blocked_reason=wake_blocked_reason,
            )

            async def provider(_messages):
                return json.dumps(judgment_payload, ensure_ascii=False)

        result = await run_sentinel_chain_dry_run(
            input_payload,
            judgment_provider=provider,
            runtime_context=runtime_context,
            request_id=request_id,
        )
    except Exception as exc:
        return {
            "shadow": _shadow_failure_payload(
                request_id=request_id,
                judgment_source=judgment_source,
                side_effects=side_effects,
                exc=exc,
            ),
            "wake_package": None,
            "request_id": request_id,
        }

    judgment = result["judgment_run"]["judgment"]
    gate = result["gate_result"]
    hypotheses = result["handoff"]["hypotheses"]
    core_wake_preflight, core_wake_execution = await _build_shadow_core_wake_traces(
        wake_package=result["wake_package"],
        reference_time=reference_time,
        request_id=request_id,
    )
    shadow = {
        "schema_version": SENTINEL_V2_SHADOW_SCHEMA_VERSION,
        "runtime_mode": "dry_run",
        "status": "ok",
        "request_id": request_id,
        "judgment_source": judgment_source,
        "side_effects": side_effects,
        "production_side_effects": [],
        "fallback_used": False,
        "fallback_reason": "",
        "metrics": dict(result["metrics"]),
        "runtime_context_metrics": dict(runtime_context["metrics"]),
        "attention": {
            "compact_text": result["handoff"]["compact_text"],
            "attention_targets": list(result["handoff"]["attention_targets"]),
            "hypothesis_labels": [
                item.get("label")
                for item in hypotheses
                if isinstance(item, dict) and item.get("label")
            ],
        },
        "judgment": {
            "monitoringlog": judgment["monitoringlog"],
            "summary": judgment["summary"],
            "score": judgment["score"],
            "confidence": judgment["confidence"],
            "wake_intent": judgment["wake_intent"],
            "call_core": judgment["call_core"],
            "core_reason": judgment["core_reason"],
            "restraint_reason": judgment["restraint_reason"],
            "uncertainty": judgment["uncertainty"],
            "tone_hint": judgment["tone_hint"],
        },
        "gate": {
            "status": gate["status"],
            "action": gate["action"],
            "wake_allowed": gate["wake_allowed"],
            "blocked_reasons": list(gate["blocked_reasons"]),
        },
        "wake_package_created": result["wake_package"] is not None,
        "core_wake_preflight": core_wake_preflight,
        "core_wake_execution": core_wake_execution,
    }
    return {
        "shadow": shadow,
        "wake_package": result["wake_package"],
        "request_id": request_id,
    }


async def _safe_sentinel_v2_shadow_dry_run(**kwargs) -> dict:
    try:
        material = await _run_sentinel_v2_shadow_material(**kwargs)
        return material["shadow"]
    except Exception as exc:
        provider_enabled = bool(kwargs.get("provider_enabled", kwargs.get("provider_shadow_enabled")))
        return _shadow_failure_payload(
            request_id="",
            judgment_source="provider" if provider_enabled else "legacy_shadow",
            side_effects=[],
            exc=exc,
        )


async def _safe_sentinel_v2_shadow_material(**kwargs) -> dict:
    try:
        return await _run_sentinel_v2_shadow_material(**kwargs)
    except Exception as exc:
        provider_enabled = bool(kwargs.get("provider_enabled", kwargs.get("provider_shadow_enabled")))
        return {
            "shadow": _shadow_failure_payload(
                request_id="",
                judgment_source="provider" if provider_enabled else "legacy_shadow",
                side_effects=[],
                exc=exc,
            ),
            "wake_package": None,
            "request_id": "",
        }


# ── 哨兵调度器 ────────────────────────────────────
class SentinelRuntime:
    """
    Text-only background Sentinel runtime.
    - 不再打开摄像头
    - 按 cfg 里配置的分钟区间随机触发分析
    - 分析输入改为 sensing/activity/location 合并的文字 timeline
    """

    def __init__(self):
        self.cfg = load_cam_config()
        self.monitoring = False
        self._monitor_thread = None
        self._loop = None
        self._next_capture_at = 0

    # ── 生命周期 ──────────────────────────────
    def set_event_loop(self, loop):
        self._loop = loop

    def start_monitoring(self):
        if self.monitoring:
            return
        self.monitoring = True
        self.cfg["monitor_enabled"] = True
        save_cam_config(self.cfg)
        self._next_capture_at = time.time() + self._random_interval_seconds()
        self._monitor_thread = threading.Thread(target=self._monitor_loop, daemon=True)
        self._monitor_thread.start()

    def stop_monitoring(self):
        self.monitoring = False
        self._next_capture_at = 0
        self.cfg["monitor_enabled"] = False
        save_cam_config(self.cfg)

    def status_payload(self) -> dict:
        remaining = 0
        if self.monitoring and self._next_capture_at > 0:
            remaining = max(0, self._next_capture_at - time.time())
        return {
            "enabled": self.monitoring,
            "monitoring": self.monitoring,
            "auto_interval_min": self.cfg.get("auto_interval_min", 10),
            "auto_interval_max": self.cfg.get("auto_interval_max", 20),
            "quiet_hours_enabled": self.cfg.get("quiet_hours_enabled", False),
            "quiet_hours_start": self.cfg.get("quiet_hours_start", "00:00"),
            "quiet_hours_end": self.cfg.get("quiet_hours_end", "09:00"),
            "is_quiet_hours": self._is_quiet_hours(),
            "next_check_in": round(remaining),
            "thread_alive": self._monitor_thread is not None and self._monitor_thread.is_alive(),
        }

    # ── 内部循环 ──────────────────────────────
    def _random_interval_seconds(self) -> int:
        lo = max(1, self.cfg.get("auto_interval_min", 10))
        hi = max(lo, self.cfg.get("auto_interval_max", 20))
        return random.randint(lo, hi) * 60

    def _is_quiet_hours(self) -> bool:
        if not self.cfg.get("quiet_hours_enabled", False):
            return False
        start_str = self.cfg.get("quiet_hours_start", "00:00")
        end_str = self.cfg.get("quiet_hours_end", "09:00")
        try:
            sh, sm = map(int, start_str.split(":"))
            eh, em = map(int, end_str.split(":"))
        except (ValueError, AttributeError):
            return False
        now = time.localtime()
        cur = now.tm_hour * 60 + now.tm_min
        start = sh * 60 + sm
        end = eh * 60 + em
        if start <= end:
            return start <= cur < end
        return cur >= start or cur < end

    def _monitor_loop(self):
        print("[Sentinel] 监控线程已启动（文字版）")
        # 整个 while 都在 try/except 里，单次出错继续；线程不退出
        while self.monitoring:
            try:
                now = time.time()
                if now < self._next_capture_at:
                    time.sleep(1)
                    continue
                self._next_capture_at = time.time() + self._random_interval_seconds()
                if self._is_quiet_hours():
                    print("[Sentinel] 静默时段，跳过")
                    continue
                if self._loop:
                    fut = asyncio.run_coroutine_threadsafe(self._analyze_and_log(), self._loop)
                    fut.add_done_callback(log_future_exception("sentinel_analyze"))
            except Exception as e:
                print(f"[Sentinel] monitor_loop iter error: {e}")
                import traceback; traceback.print_exc()
                time.sleep(5)
        print("[Sentinel] 监控线程退出")

    # ── 核心：读取 timeline → 调用 LLM → 落日志 ──
    async def _analyze_and_log(self, _unused=None):
        crash_started_at = time.perf_counter()
        crash_request_id = f"sentinel_cycle_{int(time.time() * 1000)}_{random.randint(1000, 9999)}"
        try:
            return await self._analyze_and_log_impl(_unused)
        except Exception as exc:
            error_type = type(exc).__name__
            error = str(exc)
            context_errors = [f"cycle_crashed: {error_type}: {error}"]
            _record_sentinel_cycle_summary(
                request_id=crash_request_id,
                started_at=crash_started_at,
                status="cycle_crashed",
                score=None,
                call_core=False,
                wake_threshold=0,
                wake_blocked_reason="cycle_crashed",
                context_errors=context_errors,
                signal_meta={},
                provider_enabled=False,
                full_wake_enabled=False,
                legacy_fallback_enabled=False,
                used_v2_primary=False,
                legacy_call_core=None,
                parse_fallback=False,
                monitor_log_appended=None,
                sentinel_v2_shadow=None,
                error_type=error_type,
                error=error,
            )
            print(f"[Sentinel] cycle crashed: {error_type}: {error}")
            import traceback; traceback.print_exc()
            return None

    async def _analyze_and_log_impl(self, _unused=None):
        cleanup_old_logs(3)
        cycle_started_at = time.perf_counter()
        cycle_request_id = f"sentinel_cycle_{int(time.time() * 1000)}_{random.randint(1000, 9999)}"
        context_errors: list[str] = []

        wb = load_worldbook()
        user_name, ai_name = resolve_worldbook_names(wb)
        now_str = time.strftime("%Y年%m月%d日  %H时:%M分:%S秒")
        last_user_ts = await _safe_last_user_msg_time(context_errors)
        last_user_time_str = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(last_user_ts)) if last_user_ts > 0 else "未知"

        recent_logs = _safe_logs_since(time.time() - 3600 * 6, context_errors)
        log_history = ""
        if recent_logs:
            log_lines = [f"[{e.get('time','')}] score:{e.get('score','?')}{' →唤醒' if e.get('call_core') else ''} {e.get('monitoringlog','')}" for e in recent_logs[-20:]]
            log_history = "\n".join(log_lines)

        # 上次唤醒时间（用于冷却判定）
        last_wake_ts = 0
        for e in reversed(recent_logs):
            if e.get("call_core"):
                last_wake_ts = e.get("timestamp", 0)
                break

        # legacy chat_status stopped being refreshed when normal chat moved to
        # local_instant_digest.  Treating that durable free text as current
        # evidence can keep a days-old activity alive indefinitely.  Recent
        # chat and the shared context projection are the maintained sources.
        chat_status_text = ""

        location_text = _read_location_text_for_sentinel(context_errors)

        # 最近 10 条聊天上下文
        recent_chat_text = ""
        latest_conv_id = ""
        try:
            async with get_db() as db:
                db.row_factory = aiosqlite.Row
                cur = await db.execute(
                    "SELECT c.id FROM conversations c ORDER BY c.updated_at DESC LIMIT 1"
                )
                conv = await cur.fetchone()
                if conv:
                    latest_conv_id = str(conv["id"] or "")
                    cur2 = await db.execute(
                        "SELECT role, content FROM messages WHERE conv_id=? AND role IN ('user','assistant') ORDER BY created_at DESC LIMIT 10",
                        (conv["id"],)
                    )
                    chat_rows = await cur2.fetchall()
                    if chat_rows:
                        lines = []
                        for r in reversed(chat_rows):
                            name = user_name if r["role"] == "user" else ai_name
                            text = r["content"][:200] + "..." if len(r["content"]) > 200 else r["content"]
                            lines.append(f"{name}: {text}")
                        recent_chat_text = "\n".join(lines)
        except Exception as exc:
            recent_chat_text = ""
            _append_context_error(context_errors, "recent_chat_failed", exc)

        # 设备活动摘要（手机前台 App 使用）
        activity_summary_text = ""
        try:
            from activity import get_activity_summary_for_prompt
            activity_summary_text = get_activity_summary_for_prompt(6)
        except Exception as exc:
            _append_context_error(context_errors, "activity_summary_failed", exc)

        pc_context_text = ""
        try:
            pc_context_text = _pc_context_attention_text(time.time())
        except Exception as exc:
            _append_context_error(context_errors, "pc_context_failed", exc)

        # 体感/体征/社交脉搏 timeline
        sensing_timeline = ""
        try:
            from sensing import format_sensing_for_prompt
            sensing_timeline = format_sensing_for_prompt(hours=3)
        except Exception as exc:
            _append_context_error(context_errors, "sensing_timeline_failed", exc)

        ai_beh = load_ai_behavior()
        shared_context_enabled = autonomous_context_delivery_enabled(ai_beh)
        provider_enabled = _sentinel_v2_provider_enabled(ai_beh)
        full_wake_enabled = _sentinel_v2_full_wake_enabled(ai_beh)
        legacy_fallback_enabled = _sentinel_v2_full_wake_legacy_fallback_enabled(ai_beh)
        reference_time = time.time()
        signal_meta = {
            "recent_logs_count": len(recent_logs),
            "log_history_present": bool(log_history),
            "chat_status_present": bool(chat_status_text),
            "location_present": bool(location_text),
            "recent_chat_present": bool(recent_chat_text),
            "activity_present": bool(activity_summary_text),
            "pc_context_present": bool(pc_context_text),
            "sensing_present": bool(sensing_timeline),
            "context_delivery_enabled": shared_context_enabled,
        }
        if last_user_ts > 0:
            signal_meta["last_user_age_sec"] = int(max(0, reference_time - last_user_ts))
        if last_wake_ts > 0:
            signal_meta["last_wake_age_sec"] = int(max(0, reference_time - last_wake_ts))
        if full_wake_enabled and provider_enabled:
            await self._analyze_and_log_v2_primary(
                reference_time=reference_time,
                cycle_started_at=cycle_started_at,
                cycle_request_id=cycle_request_id,
                location_text=location_text,
                sensing_timeline=sensing_timeline,
                activity_summary_text=activity_summary_text,
                pc_context_text=pc_context_text,
                recent_chat_text=recent_chat_text,
                chat_status_text=chat_status_text,
                log_history=log_history,
                recent_logs=recent_logs,
                last_user_ts=last_user_ts,
                wake_threshold=int(ai_beh.get("sentinel_wake_threshold", 7)),
                legacy_fallback_enabled=legacy_fallback_enabled,
                context_errors=context_errors,
                signal_meta=signal_meta,
                conv_id=latest_conv_id,
            )
            return

        # ── 稳定前缀（system）：每次调用一样，可命中 prompt cache ──
        system_prompt = f"""你是{ai_name}的分身——{user_name}不在的时候，你替{ai_name}盯着她。
她是你的人，你盯着她是理所当然的。她在干嘛、状态怎么样、有没有好好的——这些你都要知道。{ai_name}自己决定怎么说话，你负责判断时机。

{ai_beh["sentinel_call_core_criteria"]}

你的职责：
1. 根据信号推测{user_name}当前的状态、在做什么、心情如何。
2. 信号明确就直说，信号模糊就标注不确定，信号不足就写「信号不足」。禁止脑补没有的事。
3. 判断「{ai_name}现在该不该去找她」（0-10 分）。不只是出问题才出现——{user_name}闲下来了、到了日常节点、心情好适合撩、需要陪伴，这些都是该出现的理由。
4. 聊天记录是核心信号源。{user_name}的情绪、和{ai_name}的关系状态（冲突、冷战、还是刚分享了开心的事）直接影响评分。

日志口吻：简短，报信号和推测，不要抒情。
升级逻辑：历史日志中「→唤醒」代表之前叫过主脑。如果那个问题到现在还没解决，score 应比上次更高。

严格按以下 JSON 回复，不要包含其他任何内容：
{{"monitoringlog":"信号+推测，一两句话。","summary":"综合历史日志概括整体状况，一两句话。","score":0,"core_reason":""}}

字段说明：
- monitoringlog: 信号事实 + 推测。明确的直说，模糊的标注不确定。
- summary: 综合聊天状态和信号的整体概括。
- score: 0-10 整数，「想去找她的程度」。
- core_reason: score >= 7 时填写，告诉主脑为什么该出现（该管就写该管的理由，该陪就写该陪的理由，适合撩就写适合撩的理由）。"""

        system_prompt += "\n\n" + render_device_proxy_hard_limits(user_name)

        # ── 动态部分（user）：每次变化，不影响前缀缓存 ──
        if shared_context_enabled:
            try:
                device_context_text = await load_autonomous_context_delivery(
                    user_name=user_name,
                    ai_name=ai_name,
                    conv_id=latest_conv_id,
                    reference_time=reference_time,
                )
            except Exception as exc:
                device_context_text = ""
                _append_context_error(
                    context_errors,
                    "context_delivery_failed",
                    exc,
                )
            signal_meta["context_delivery_present"] = bool(device_context_text)
            device_context_prompt = (
                device_context_text
                or "（暂无达到新鲜度要求的设备与环境上下文）"
            )
        else:
            legacy_device_parts = []
            if location_text:
                legacy_device_parts.append(location_text)
            legacy_device_parts.append(
                f"{user_name}近 3 小时体感/体征/社交脉搏"
                "（手机传感器 + 手环 Health Connect + 微信/QQ 消息密度 + 解锁事件）：\n"
                f"{sensing_timeline if sensing_timeline else '（暂无数据）'}"
            )
            legacy_device_parts.append(
                f"{user_name}近一小时手机使用（每 10 分钟一条摘要）：\n"
                f"{activity_summary_text if activity_summary_text else '（暂无活动记录）'}"
            )
            device_context_prompt = "\n\n".join(legacy_device_parts)

        user_prompt = f"""当前时间：{now_str}
{user_name}最后一次聊天的时间：{last_user_time_str}

最近的聊天记录：
{recent_chat_text if recent_chat_text else "（暂无聊天记录）"}

{device_context_prompt}

历史哨兵日志（最近 6 小时，含评分和唤醒标记）：
{log_history if log_history else "（暂无历史日志）"}"""

        print(f"[Sentinel] 调用 slot=sentinel")

        monitoring_log = ""
        score = 0
        summary = ""
        core_reason = ""
        status = "decided"
        error_type = ""
        error = ""
        parse_fallback = False

        judgment_messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ]
        judgment_invocation_id = tool_invocation_ledger.new_invocation_id(
            "sentinel_legacy_judgment"
        )
        judgment_context = ToolContext(
            conv_id=latest_conv_id or "__sentinel__",
            request_id=cycle_request_id,
            model_key="slot:sentinel",
            metadata={
                "source": "sentinel_legacy_judgment",
                "source_chain": "sentinel",
                "invocation_id": judgment_invocation_id,
                "advertised_tools": (),
            },
        )
        await tool_invocation_ledger.record_model_request(
            judgment_context,
            invocation_id=judgment_invocation_id,
            request_snapshot=judgment_messages,
            advertised_tools=(),
        )
        raw_text = ""
        try:
            raw_text = await call_slot_chat(
                "sentinel",
                messages=judgment_messages,
                expect_json=True,
                timeout=60,
            )
        except Exception as exc:
            raw_text = ""
            status = "provider_failed"
            error_type = type(exc).__name__
            error = str(exc)
            monitoring_log = f"[Sentinel 调用失败 / 端点异常] {error_type}: {error}"
        await tool_invocation_ledger.record_model_output(
            judgment_context,
            invocation_id=judgment_invocation_id,
            raw_output=raw_text,
            outcome="failed" if error else (
                "succeeded" if raw_text else "unknown"
            ),
            error=error,
        )
        await tool_invocation_ledger.record_turn(
            judgment_context,
            prompt_source="sentinel_legacy_judgment",
            advertised_tools=(),
            turn_outcome="failed" if error else (
                "succeeded" if raw_text else "invalid_output"
            ),
        )

        if not raw_text and not monitoring_log:
            status = "provider_empty"
            monitoring_log = "[Sentinel 无响应 / 端点失败]"
        elif raw_text:
            try:
                cleaned = raw_text.strip()
                if cleaned.startswith("```"):
                    cleaned = re.sub(r"^```\w*\n?", "", cleaned)
                    cleaned = re.sub(r"\n?```$", "", cleaned)
                    cleaned = cleaned.strip()
                parsed = json.loads(cleaned)
                monitoring_log = parsed.get("monitoringlog", "")
                score = min(10, max(0, int(parsed.get("score", 0))))
                summary = parsed.get("summary", "")
                core_reason = parsed.get("core_reason", "")
            except (json.JSONDecodeError, Exception) as e:
                print(f"[Sentinel] JSON 解析失败，走 regex 兜底: {e}")
                status = "parse_fallback"
                error_type = type(e).__name__
                error = str(e)
                parse_fallback = True
                monitoring_log, score, summary, core_reason = \
                    _regex_extract_sentinel(raw_text)

        # 阈值判定 + score 驱动冷却
        wake_threshold = int(ai_beh.get("sentinel_wake_threshold", 7))
        call_core = score >= wake_threshold

        now = time.time()
        # 冷却时间随 score 递减：7-8 → 25min, 9-10 → 10min
        if score >= 9:
            cooldown_seconds = 10 * 60
        else:
            cooldown_seconds = 25 * 60
        # 正在聊天中的冷却固定 10 分钟（不管 score）
        chat_cooldown = 10 * 60
        wake_blocked_reason = ""
        if call_core and last_wake_ts > 0 and (now - last_wake_ts) < cooldown_seconds:
            print(f"[Sentinel] score={score} 达到阈值但冷却中（距上次唤醒 {int((now - last_wake_ts) / 60)} 分钟，需 {cooldown_seconds // 60} 分钟），跳过")
            call_core = False
            core_reason = ""
            wake_blocked_reason = "wake_cooldown"
            status = "wake_cooldown"
        if call_core and last_user_ts > 0 and (now - last_user_ts) < chat_cooldown:
            print(f"[Sentinel] score={score} 达到阈值但正在聊天中（距上次消息 {int((now - last_user_ts) / 60)} 分钟），跳过")
            call_core = False
            core_reason = ""
            wake_blocked_reason = "chat_cooldown"
            status = "chat_cooldown"
        if call_core and status == "decided":
            status = "core_wake_requested"

        legacy_call_core = call_core
        shadow_material = await _safe_sentinel_v2_shadow_material(
            reference_time=now,
            location_text=location_text,
            sensing_timeline=sensing_timeline,
            activity_summary_text=activity_summary_text,
            pc_context_text=pc_context_text,
            recent_chat_text=recent_chat_text,
            chat_status_text=chat_status_text,
            log_history=log_history,
            monitoring_log=monitoring_log,
            score=score,
            summary=summary,
            core_reason=core_reason,
            call_core=legacy_call_core,
            wake_blocked_reason=wake_blocked_reason,
            provider_enabled=provider_enabled,
            conv_id=latest_conv_id,
        )
        sentinel_v2_shadow = shadow_material["shadow"]
        full_wake_unavailable_reason = ""
        if full_wake_enabled:
            if shadow_material.get("wake_package") is not None:
                call_core = True
                status = "core_wake_requested"
                shadow_judgment = sentinel_v2_shadow.get("judgment") if isinstance(sentinel_v2_shadow, dict) else None
                if isinstance(shadow_judgment, dict) and shadow_judgment.get("core_reason"):
                    core_reason = shadow_judgment["core_reason"]
            elif legacy_call_core:
                if sentinel_v2_shadow.get("status") == "ok":
                    full_wake_unavailable_reason = "sentinel_v2_gate_blocked"
                    status = full_wake_unavailable_reason
                else:
                    full_wake_unavailable_reason = "sentinel_v2_shadow_failed"
                    status = "sentinel_v2_full_wake_unavailable"
                if legacy_fallback_enabled:
                    call_core = True
                else:
                    call_core = False
                    core_reason = ""
                    wake_blocked_reason = full_wake_unavailable_reason
            else:
                call_core = False

        print(f"[Sentinel] 分析完成, score={score}, call_core={call_core}, log长度={len(monitoring_log)}")
        log_entry = {
            "timestamp": now,
            "time": time.strftime("%H:%M:%S", time.localtime(now)),
            "date": time.strftime("%Y-%m-%d", time.localtime(now)),
            "monitoringlog": monitoring_log,
            "summary": summary,
            "score": score,
            "call_core": call_core,
            "core_reason": core_reason,
            "screenshot": "",
            "source": "sentinel",
            "status": status,
            "timeline_used": sensing_timeline[-500:] if sensing_timeline else "",
            "wake_threshold": wake_threshold,
            "wake_blocked_reason": wake_blocked_reason,
            "parse_fallback": parse_fallback,
            "context_errors": context_errors,
            "legacy_call_core": legacy_call_core,
            "sentinel_v2_full_wake_enabled": full_wake_enabled,
            "sentinel_v2_full_wake_legacy_fallback_enabled": legacy_fallback_enabled,
        }
        if full_wake_unavailable_reason:
            log_entry["sentinel_v2_full_wake_unavailable_reason"] = full_wake_unavailable_reason
        if error_type:
            log_entry["error_type"] = error_type
        if error:
            log_entry["error"] = error
        log_entry["sentinel_v2_shadow"] = sentinel_v2_shadow
        monitor_log_appended = await append_and_broadcast_monitor_log(log_entry)
        _record_sentinel_cycle_summary(
            request_id=cycle_request_id,
            started_at=cycle_started_at,
            status=status,
            score=score,
            call_core=call_core,
            wake_threshold=wake_threshold,
            wake_blocked_reason=wake_blocked_reason,
            context_errors=context_errors,
            signal_meta=signal_meta,
            provider_enabled=provider_enabled,
            full_wake_enabled=full_wake_enabled,
            legacy_fallback_enabled=legacy_fallback_enabled,
            used_v2_primary=False,
            legacy_call_core=legacy_call_core,
            parse_fallback=parse_fallback,
            monitor_log_appended=monitor_log_appended,
            sentinel_v2_shadow=sentinel_v2_shadow,
            full_wake_unavailable_reason=full_wake_unavailable_reason,
            error_type=error_type,
            error=error,
        )

        if full_wake_enabled:
            wake_package = shadow_material.get("wake_package")
            if wake_package is not None:
                full_result = await self._call_core_v2_full_wake(
                    wake_package=wake_package,
                    reference_time=now,
                    request_id=shadow_material.get("request_id", ""),
                    trigger_log=monitoring_log,
                    summary=summary,
                    core_reason=core_reason,
                    allow_legacy_fallback=legacy_fallback_enabled,
                )
                if full_result.get("should_fallback"):
                    await self._call_core(monitoring_log, last_user_ts, summary, core_reason, recent_logs)
            elif legacy_call_core and legacy_fallback_enabled:
                await append_and_broadcast_monitor_log(_sentinel_monitor_log_entry(
                    status="sentinel_v2_full_wake_fallback_legacy",
                    monitoringlog=(
                        "⚠️ Sentinel V2 full wake 未生成可执行 wake package，"
                        f"显式回退旧 Core 唤醒。原因：{full_wake_unavailable_reason or 'unknown'}"
                    ),
                    call_core=True,
                    core_reason=core_reason,
                    summary=summary,
                    fallback_used=True,
                    fallback_reason="legacy_core_wake",
                    source_path="sentinel_v2.full_wake",
                    original_error=full_wake_unavailable_reason or "missing_wake_package",
                    request_id=shadow_material.get("request_id", ""),
                ))
                await self._call_core(monitoring_log, last_user_ts, summary, core_reason, recent_logs)
        elif call_core:
            await self._call_core(monitoring_log, last_user_ts, summary, core_reason, recent_logs)

    async def _analyze_and_log_v2_primary(
        self,
        *,
        reference_time: float,
        cycle_started_at: float,
        cycle_request_id: str,
        location_text: str,
        sensing_timeline: str,
        activity_summary_text: str,
        pc_context_text: str,
        recent_chat_text: str,
        chat_status_text: str,
        log_history: str,
        recent_logs: list,
        last_user_ts: float,
        wake_threshold: int,
        legacy_fallback_enabled: bool,
        context_errors: list[str],
        signal_meta: dict,
        conv_id: str = "",
    ) -> None:
        shadow_material = await _safe_sentinel_v2_shadow_material(
            reference_time=reference_time,
            location_text=location_text,
            sensing_timeline=sensing_timeline,
            activity_summary_text=activity_summary_text,
            pc_context_text=pc_context_text,
            recent_chat_text=recent_chat_text,
            chat_status_text=chat_status_text,
            log_history=log_history,
            monitoring_log="",
            score=0,
            summary="",
            core_reason="",
            call_core=False,
            wake_blocked_reason="",
            provider_enabled=True,
            conv_id=conv_id,
        )
        sentinel_v2_shadow = shadow_material["shadow"]
        now = reference_time
        if sentinel_v2_shadow.get("status") != "ok":
            monitor_log_appended = await append_and_broadcast_monitor_log({
                "timestamp": now,
                "time": time.strftime("%H:%M:%S", time.localtime(now)),
                "date": time.strftime("%Y-%m-%d", time.localtime(now)),
                "monitoringlog": _PUBLIC_SENTINEL_FAILURE_MESSAGE,
                "summary": "",
                "score": None,
                "call_core": False,
                "core_reason": "",
                "screenshot": "",
                "source": "sentinel",
                "status": "sentinel_v2_primary_failed",
                "timeline_used": sensing_timeline[-500:] if sensing_timeline else "",
                "wake_threshold": wake_threshold,
                "wake_blocked_reason": "sentinel_v2_primary_failed",
                "parse_fallback": False,
                "context_errors": context_errors,
                "legacy_call_core": False,
                "sentinel_v2_primary_enabled": True,
                "sentinel_v2_full_wake_enabled": True,
                "sentinel_v2_full_wake_legacy_fallback_enabled": legacy_fallback_enabled,
                "error_type": sentinel_v2_shadow.get("error_type", "RuntimeError"),
                "error": sentinel_v2_shadow.get("error", ""),
                "sentinel_v2_shadow": sentinel_v2_shadow,
            })
            _record_sentinel_cycle_summary(
                request_id=cycle_request_id,
                started_at=cycle_started_at,
                status="sentinel_v2_primary_failed",
                score=None,
                call_core=False,
                wake_threshold=wake_threshold,
                wake_blocked_reason="sentinel_v2_primary_failed",
                context_errors=context_errors,
                signal_meta=signal_meta,
                provider_enabled=True,
                full_wake_enabled=True,
                legacy_fallback_enabled=legacy_fallback_enabled,
                used_v2_primary=True,
                legacy_call_core=False,
                parse_fallback=False,
                monitor_log_appended=monitor_log_appended,
                sentinel_v2_shadow=sentinel_v2_shadow,
                error_type=sentinel_v2_shadow.get("error_type", "RuntimeError"),
                error=sentinel_v2_shadow.get("error", ""),
            )
            return

        judgment = sentinel_v2_shadow["judgment"]
        gate = sentinel_v2_shadow["gate"]
        wake_package = shadow_material.get("wake_package")
        wake_blocked_reason = ""
        if wake_package is not None:
            status = "core_wake_requested"
            call_core = True
            core_reason = judgment["core_reason"]
        elif judgment["wake_intent"]:
            status = "sentinel_v2_gate_blocked"
            call_core = False
            core_reason = ""
            blocked_reasons = gate.get("blocked_reasons") or []
            wake_blocked_reason = ",".join(blocked_reasons) or "sentinel_v2_gate_blocked"
        else:
            status = "decided"
            call_core = False
            core_reason = ""

        monitoring_log = judgment["monitoringlog"]
        summary = judgment["summary"]
        log_entry = {
            "timestamp": now,
            "time": time.strftime("%H:%M:%S", time.localtime(now)),
            "date": time.strftime("%Y-%m-%d", time.localtime(now)),
            "monitoringlog": monitoring_log,
            "summary": summary,
            "score": judgment["score"],
            "confidence": judgment["confidence"],
            "call_core": call_core,
            "core_reason": core_reason,
            "restraint_reason": judgment["restraint_reason"],
            "uncertainty": judgment["uncertainty"],
            "tone_hint": judgment["tone_hint"],
            "screenshot": "",
            "source": "sentinel",
            "status": status,
            "timeline_used": sensing_timeline[-500:] if sensing_timeline else "",
            "wake_threshold": wake_threshold,
            "wake_blocked_reason": wake_blocked_reason,
            "parse_fallback": False,
            "context_errors": context_errors,
            "legacy_call_core": False,
            "sentinel_v2_primary_enabled": True,
            "sentinel_v2_full_wake_enabled": True,
            "sentinel_v2_full_wake_legacy_fallback_enabled": legacy_fallback_enabled,
            "sentinel_v2_shadow": sentinel_v2_shadow,
        }
        monitor_log_appended = await append_and_broadcast_monitor_log(log_entry)
        _record_sentinel_cycle_summary(
            request_id=cycle_request_id,
            started_at=cycle_started_at,
            status=status,
            score=judgment["score"],
            call_core=call_core,
            wake_threshold=wake_threshold,
            wake_blocked_reason=wake_blocked_reason,
            context_errors=context_errors,
            signal_meta=signal_meta,
            provider_enabled=True,
            full_wake_enabled=True,
            legacy_fallback_enabled=legacy_fallback_enabled,
            used_v2_primary=True,
            legacy_call_core=False,
            parse_fallback=False,
            monitor_log_appended=monitor_log_appended,
            sentinel_v2_shadow=sentinel_v2_shadow,
        )

        if wake_package is not None:
            full_result = await self._call_core_v2_full_wake(
                wake_package=wake_package,
                reference_time=now,
                request_id=shadow_material.get("request_id", ""),
                trigger_log=monitoring_log,
                summary=summary,
                core_reason=core_reason,
                allow_legacy_fallback=legacy_fallback_enabled,
            )
            if full_result.get("should_fallback"):
                await self._call_core(monitoring_log, last_user_ts, summary, core_reason, recent_logs)

    async def _call_core_v2_full_wake(
        self,
        *,
        wake_package: dict,
        reference_time: float,
        request_id: str,
        trigger_log: str,
        summary: str,
        core_reason: str,
        allow_legacy_fallback: bool,
    ) -> dict:
        try:
            execution_context = await read_core_wake_execution_context(reference_time=reference_time)
            preflight = build_core_wake_preflight(
                wake_package=wake_package,
                execution_context=execution_context,
            )
            ports = build_legacy_core_wake_ports(
                observation_conv_id=str(execution_context.get("conv_id") or ""),
                observation_request_id=request_id,
            )
            execution = await run_core_wake_orchestrator_full_execute(
                wake_package=wake_package,
                execution_context=execution_context,
                ports=ports,
                request_id=request_id,
                preflight=preflight,
                allow_production_side_effects=True,
            )
            compact_execution = _compact_core_wake_execution(execution)
            if execution["status"] == "preflight_failed":
                await append_and_broadcast_monitor_log(_sentinel_monitor_log_entry(
                    status="sentinel_v2_full_wake_preflight_failed",
                    monitoringlog=f"⚠️ Sentinel V2 full wake preflight failed：{execution.get('error', '')}",
                    call_core=False,
                    core_reason=core_reason,
                    summary=summary,
                    error_type=execution.get("error_type", "preflight_failed"),
                    error=execution.get("error", "core wake preflight failed"),
                    fallback_used=allow_legacy_fallback,
                    fallback_reason="legacy_core_wake" if allow_legacy_fallback else "",
                    source_path="sentinel_v2.full_wake",
                    original_error=execution.get("error", "core wake preflight failed"),
                    request_id=request_id,
                    core_wake_preflight=_compact_core_wake_preflight(preflight),
                    core_wake_execution=compact_execution,
                ))
            return {
                "status": execution["status"],
                "should_fallback": allow_legacy_fallback and execution["status"] != "core_succeeded",
                "core_wake_execution": compact_execution,
            }
        except asyncio.CancelledError as exc:
            cancellation = _core_wake_full_execution_exception_payload(
                exc=exc,
                request_id=request_id,
            )
            cancellation.update({
                "status": "core_cancelled",
                "would_write_monitor_log": "core_cancelled",
                "error": "core wake cancelled",
            })
            cancellation_log = _sentinel_monitor_log_entry(
                status="core_cancelled",
                monitoringlog="⚠️ Sentinel Core wake cancelled before completion",
                call_core=False,
                core_reason=core_reason,
                summary=summary,
                error_type="CancelledError",
                error="core wake cancelled",
                fallback_used=False,
                fallback_reason="",
                source_path="sentinel_v2.full_wake",
                original_error="CancelledError: core wake cancelled",
                request_id=request_id,
                trigger_log=trigger_log,
                core_wake_execution=_compact_core_wake_execution(cancellation),
            )
            try:
                await append_and_broadcast_monitor_log(cancellation_log)
            except asyncio.CancelledError:
                # append_and_broadcast_monitor_log persists before its first
                # await, so a second shutdown cancellation may only skip WS.
                pass
            raise
        except Exception as exc:
            failure = _core_wake_full_execution_exception_payload(exc=exc, request_id=request_id)
            await append_and_broadcast_monitor_log(_sentinel_monitor_log_entry(
                status="sentinel_v2_full_wake_failed",
                monitoringlog=f"⚠️ Sentinel V2 full wake failed：{type(exc).__name__}: {exc}",
                call_core=False,
                core_reason=core_reason,
                summary=summary,
                error_type=type(exc).__name__,
                error=str(exc),
                fallback_used=allow_legacy_fallback,
                fallback_reason="legacy_core_wake" if allow_legacy_fallback else "",
                source_path="sentinel_v2.full_wake",
                original_error=f"{type(exc).__name__}: {exc}",
                request_id=request_id,
                trigger_log=trigger_log,
                core_wake_execution=_compact_core_wake_execution(failure),
            ))
            return {
                "status": "failed",
                "should_fallback": allow_legacy_fallback,
                "core_wake_execution": _compact_core_wake_execution(failure),
            }

    async def _call_core(self, trigger_log: str, last_user_ts: float, summary: str = "",
                         core_reason: str = "", cached_logs: list = None):
        wb = load_worldbook()
        user_name, ai_name = resolve_worldbook_names(wb)
        trigger_log = _relationship_prompt_text(
            trigger_log,
            user_name=user_name,
            ai_name=ai_name,
        )
        summary = _relationship_prompt_text(
            summary,
            user_name=user_name,
            ai_name=ai_name,
        )
        core_reason = _relationship_prompt_text(
            core_reason,
            user_name=user_name,
            ai_name=ai_name,
        )

        if last_user_ts > 0:
            elapsed = time.time() - last_user_ts
            hours = int(elapsed // 3600)
            minutes = int((elapsed % 3600) // 60)
            time_ago = f"{hours}小时{minutes}分钟" if hours > 0 else f"{minutes}分钟"
        else:
            time_ago = "很长时间"

        context_errors: list[str] = []
        if cached_logs is not None:
            all_logs = cached_logs[-24:]
        else:
            all_logs = _safe_logs_since(
                last_user_ts if last_user_ts > 0 else time.time() - 3600 * 6,
                context_errors,
            )[-24:]
        recent_detail = "\n".join([
            f"[{e.get('time','')}] "
            + _relationship_prompt_text(
                e.get("monitoringlog", ""),
                user_name=user_name,
                ai_name=ai_name,
            )
            for e in all_logs[-5:]
        ])
        if not recent_detail:
            recent_detail = trigger_log

        async with get_db() as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute("SELECT * FROM conversations ORDER BY updated_at DESC LIMIT 1")
            conv = await cur.fetchone()
            if not conv:
                await append_and_broadcast_monitor_log(_sentinel_monitor_log_entry(
                    status="core_no_conversation",
                    monitoringlog=f"⚠️ 哨兵无法唤醒 Core：没有可用对话。最新日志：{trigger_log}",
                    call_core=False,
                    core_reason=core_reason,
                    summary=summary,
                    error_type="no_conversation",
                    context_errors=context_errors,
                ))
                return
            conv_id = conv["id"]
            model_key = conv["model"] or DEFAULT_MODEL

            cur = await db.execute(
                "SELECT id, role, content, attachments FROM messages WHERE conv_id=? AND role IN ('user','assistant') ORDER BY created_at DESC LIMIT 20",
                (conv_id,)
            )
            rows = await cur.fetchall()
            history = []
            visible_message_ids = []
            for r in reversed(rows):
                d = dict(r)
                visible_message_ids.append(str(d.pop("id")))
                d["attachments"] = []
                history.append(d)

        prefix = build_worldbook_prefix(wb)

        core_parts = [
            f"{user_name}是你的人——{user_name}这么久没动静，你该去看看了。",
            f"【{user_name}】已经{time_ago}没有和你说话了。",
        ]
        if core_reason:
            core_parts.append(f"哨兵唤醒你的原因：{core_reason}")
        if summary:
            core_parts.append(f"这段时间{user_name}的整体状况：{summary}")
        core_parts.append(f"最新一条哨兵日志：{trigger_log}")
        core_parts.append(f"最近的哨兵记录：\n{recent_detail}")
        if autonomous_context_delivery_enabled(load_ai_behavior()):
            try:
                context_text = await load_autonomous_context_delivery(
                    user_name=user_name,
                    ai_name=ai_name,
                    conv_id=conv_id,
                    reference_time=time.time(),
                )
                if context_text:
                    core_parts.append(f"\n{context_text}")
            except Exception as exc:
                _append_context_error(
                    context_errors,
                    "context_delivery_failed",
                    exc,
                )
        else:
            try:
                from location import format_location_for_prompt
                loc_info = format_location_for_prompt()
                if loc_info:
                    core_parts.append(f"\n{loc_info}")
            except Exception:
                pass
            try:
                from sensing import format_sensing_for_prompt
                st = format_sensing_for_prompt(hours=2)
                if st:
                    core_parts.append(f"\n最近 2 小时体感/体征：\n{st}")
            except Exception:
                pass
        core_parts.append(
            f"\n【重要】只基于以上信号、聊天记录和本轮注入上下文说话。不要编造{user_name}没说过的事、没发生过的场景。"
            f"信号不足不代表没话说——结合最近聊天和注入的近期时间线，找一个{user_name}会在意的点自然地搭话。"
            f"实在没有切入点，就用你了解的{user_name}的习惯或日常节点（饭点、睡前、刚醒）来关心{user_name}。"
            f"不要说「我来看看你」「想你了所以来找你」这种暴露哨兵机制的话。"
        )
        core_parts.append(render_device_proxy_hard_limits(user_name))

        from app.control.toy_capability import resolve_toy_capability_snapshot

        toy_capability = await resolve_toy_capability_snapshot(conv_id=conv_id)
        available_tools = {"device.toy"} if toy_capability.allowed else set()
        rendered_tools = render_registered_capabilities(
            "sentinel_legacy",
            capabilities=available_tools,
            context={
                "user_name": user_name,
                "toy_available": toy_capability.allowed,
                "toy_variant": "sentinel",
            },
        )
        advertised_tools = tuple(
            tool_name for tool_name, _prose in rendered_tools
        )
        validate_turn_advertisement(available_tools, advertised_tools)
        core_parts.extend(f"\n{prose}" for _tool_name, prose in rendered_tools)

        core_prompt = "\n".join(core_parts)

        # 誓约常驻注入（誓约设计 §5.1）；读取失败 → 系统主动路径，跳过本次生成
        # 并记录（§5.2），不插系统提示、不开口。
        from app.vows.service import VowReadError, vow_service
        try:
            vow_block, _ = await vow_service.load_vow_prompt_context()
        except VowReadError as exc:
            await append_and_broadcast_monitor_log(_sentinel_monitor_log_entry(
                status="vow_read_failed",
                monitoringlog=f"⚠️ 哨兵唤醒前誓约读取失败，本次跳过：{exc}",
                call_core=False,
                core_reason=core_reason,
                summary=summary,
                error_type="vow_read_failed",
                conv_id=conv_id,
                context_errors=context_errors,
            ))
            return
        vow_inject = []
        if vow_block:
            vow_inject = [
                {"role": "user", "content": vow_block},
                {"role": "assistant", "content": "（嗯，这些一直都算数。）"},
            ]

        # P1 relationship context: the existing Working Model V2 gate controls
        # both heads, which are loaded together to avoid a mixed-version prompt.
        try:
            working_model_block, desire_block = await load_sentinel_working_model_prompt_context()
        except Exception as exc:
            await append_and_broadcast_monitor_log(_sentinel_monitor_log_entry(
                status="working_model_read_failed",
                monitoringlog=f"⚠️ 哨兵唤醒前认识层/欲望层读取失败，本次跳过：{exc}",
                call_core=False,
                core_reason=core_reason,
                summary=summary,
                error_type="working_model_read_failed",
                error=str(exc),
                conv_id=conv_id,
                context_errors=context_errors,
            ))
            return
        relationship_inject = []
        if working_model_block:
            relationship_inject.extend([
                {"role": "user", "content": working_model_block},
                {"role": "assistant", "content": "（嗯，这是我此刻对她的认识。）"},
            ])
        if desire_block:
            relationship_inject.extend([
                {"role": "user", "content": desire_block},
                {"role": "assistant", "content": "（嗯，这是我此刻想带进这段关系里的姿态。）"},
            ])

        timeline_meta = {"status": "unavailable", "block": "", "entries": []}
        timeline_inject = []
        try:
            timeline_meta = await load_sentinel_timeline_prompt_context(
                visible_message_ids=visible_message_ids,
                now=time.time(),
            )
            timeline_block = str(timeline_meta.get("block") or "").strip()
            if timeline_block:
                timeline_inject = [
                    {"role": "user", "content": timeline_block},
                    {"role": "assistant", "content": "（嗯，近几天的事我还记得。）"},
                ]
        except Exception as exc:
            msg = f"timeline_context_failed: {type(exc).__name__}: {exc}"
            context_errors.append(msg)
            print(f"[Sentinel] {msg}")

        await manager.broadcast({"type": "monitor_alert", "data": {"content": f"哨兵唤醒了{ai_name}"}})

        # 先插一条系统提示，让聊天页面知道"哨兵唤醒了 AI"
        sys_now = time.time()
        sys_msg_id = f"msg_{int(sys_now*1000)}_sentinel_sys"
        reason_short = core_reason[:60] if core_reason else "该管管了"
        sys_content = f"💭 {ai_name}的哨兵唤醒了主脑 · {reason_short}"
        async with get_db() as db:
            await db.execute(
                "INSERT INTO messages (id, conv_id, role, content, created_at, attachments) VALUES (?,?,?,?,?,?)",
                (sys_msg_id, conv_id, "system", sys_content, sys_now, "[]")
            )
            await db.commit()
        sys_msg = {"id": sys_msg_id, "conv_id": conv_id, "role": "system",
                   "content": sys_content, "created_at": sys_now, "attachments": []}
        await manager.broadcast({"type": "msg_created", "data": sys_msg})

        await asyncio.sleep(5)

        from ai_providers import stream_ai
        from config import SETTINGS
        from app.web_search import web_search_service
        from app.web_search.intent import web_search_ability_block

        web_bound_turn_id = f"sentinel_legacy:{sys_msg_id}"
        try:
            web_search_meta = await web_search_service.prepare_dialogue_turn(
                conv_id=conv_id,
                bound_turn_id=web_bound_turn_id,
            )
        except Exception as exc:
            context_errors.append(
                f"web_search_prepare_failed: {type(exc).__name__}: {exc}"
            )
            web_search_meta = {"status": "disabled", "block": ""}
        web_inject = []
        if web_search_meta.get("status") != "disabled":
            web_block = "\n\n".join(filter(None, (
                web_search_ability_block(),
                str(web_search_meta.get("block") or "").strip(),
            )))
            web_inject = [
                {"role": "user", "content": web_block},
                {"role": "assistant", "content": "（嗯，查询能力和已经返回的资料我都清楚。）"},
            ]
        messages = prefix + vow_inject + relationship_inject + timeline_inject + history + web_inject + [
            {"role": "user", "content": core_prompt}
        ]
        _temp = SETTINGS.get("temperature")
        max_attempts = 2
        for attempt in range(1, max_attempts + 1):
            core_invocation_id = tool_invocation_ledger.new_invocation_id(
                "sentinel_legacy_core"
            )
            core_context = ToolContext(
                conv_id=conv_id,
                request_id=f"{sys_msg_id}:attempt:{attempt}",
                model_key=model_key,
                capabilities=advertised_tools,
                metadata={
                    "source": "sentinel_core",
                    "source_chain": "sentinel",
                    "invocation_id": core_invocation_id,
                    "advertised_tools": advertised_tools,
                },
            )
            await tool_invocation_ledger.record_model_request(
                core_context,
                invocation_id=core_invocation_id,
                request_snapshot=messages,
                advertised_tools=advertised_tools,
                metadata={"attempt": attempt},
            )
            try:
                full_content = ""
                try:
                    async for chunk in stream_ai(messages, model_key, temperature=_temp):
                        full_content += chunk
                except Exception as provider_exc:
                    await tool_invocation_ledger.record_model_output(
                        core_context,
                        invocation_id=core_invocation_id,
                        raw_output=full_content,
                        outcome="failed",
                        error=str(provider_exc),
                        metadata={"attempt": attempt},
                    )
                    await tool_invocation_ledger.record_turn(
                        core_context,
                        prompt_source="sentinel_core",
                        advertised_tools=advertised_tools,
                        turn_outcome="failed",
                        metadata={"attempt": attempt},
                    )
                    raise
                await tool_invocation_ledger.record_model_output(
                    core_context,
                    invocation_id=core_invocation_id,
                    raw_output=full_content,
                    outcome="succeeded" if full_content.strip() else "unknown",
                    metadata={"attempt": attempt},
                )
                await tool_invocation_ledger.record_turn(
                    core_context,
                    prompt_source="sentinel_core",
                    advertised_tools=advertised_tools,
                    turn_outcome=(
                        "succeeded" if full_content.strip() else "invalid_output"
                    ),
                    metadata={"attempt": attempt},
                )
                # strip 先于任何工具解析（誓约设计 §4.4 硬约束）：本路径不允许立约，
                # 完整/未闭合 [VOW:] 内的 TOY 等标记是惰性文本，剥除后绝不进入解析；
                # 剥空按空内容处理，不落库。
                from app.vows.service import strip_vow_markers
                from app.memory_v3.recall_intent import strip_recall_intent_markers
                from app.web_search.intent import extract_web_search_intent
                full_content = strip_vow_markers(full_content)
                full_content, web_search_intent = extract_web_search_intent(full_content)
                full_content = strip_recall_intent_markers(full_content)
                if not full_content.strip():
                    if attempt < max_attempts:
                        print(f"[Sentinel→Core] 第 {attempt} 次调用返回空，{10}s 后重试")
                        await asyncio.sleep(10)
                        continue
                    await append_and_broadcast_monitor_log(_sentinel_monitor_log_entry(
                        status="core_empty",
                        monitoringlog=f"⚠️ 哨兵唤醒 Core 后返回空内容。原因：{core_reason or trigger_log}",
                        call_core=False,
                        core_reason=core_reason,
                        summary=summary,
                        error_type="core_empty",
                        conv_id=conv_id,
                        context_errors=context_errors,
                    ))
                    return
                toy_matches = re.findall(r'\[TOY:([^\]]+)\]', full_content)
                if toy_matches:
                    full_content = re.sub(r'\[TOY:[^\]]+\]', '', full_content).strip()
                    for marker_index, command in enumerate(toy_matches, 1):
                        await tool_invocation_ledger.record_marker(
                            core_context,
                            invocation_id=core_invocation_id,
                            marker_name="TOY",
                            raw_text=f"[TOY:{command}]",
                            normalized={
                                "tool_name": "device.toy",
                                "command": command,
                            },
                            marker_index=marker_index,
                        )

                now = time.time()
                msg_id = f"msg_{int(now*1000)}_sentinel"
                async with get_db() as db:
                    await db.execute(
                        "INSERT INTO messages (id, conv_id, role, content, created_at, attachments) VALUES (?,?,?,?,?,?)",
                        (msg_id, conv_id, "assistant", full_content, now, "[]")
                    )
                    await db.execute("UPDATE conversations SET updated_at=? WHERE id=?", (now, conv_id))
                    await db.commit()
                ai_msg = {"id": msg_id, "conv_id": conv_id, "role": "assistant",
                          "content": full_content, "created_at": now, "attachments": []}
                await manager.broadcast({"type": "msg_created", "data": ai_msg})
                try:
                    await web_search_service.finalize_independent(
                        conv_id=conv_id,
                        bound_turn_id=str(web_search_meta.get("bound_turn_id") or web_bound_turn_id),
                        assistant_message_id=msg_id,
                        intent_text=web_search_intent,
                        origin_source="sentinel_core",
                        allow_new_intent=True,
                        now=now,
                    )
                except Exception as exc:
                    context_errors.append(
                        f"web_search_finalize_failed: {type(exc).__name__}: {exc}"
                    )
                await tool_invocation_ledger.record_visible_message(
                    core_context,
                    invocation_id=core_invocation_id,
                    cleaned_content=full_content,
                    message_id=msg_id,
                )
                try:
                    await record_sentinel_timeline_injection_usage(
                        timeline_meta,
                        conv_id=conv_id,
                        assistant_message_id=msg_id,
                        response_text=full_content,
                    )
                except Exception as exc:
                    context_errors.append(
                        f"timeline_usage_record_failed: {type(exc).__name__}: {exc}"
                    )
                timeline_service.start_background_refresh()

                toy_delivery = None
                if toy_matches:
                    toy_ports = build_legacy_core_wake_ports(
                        linked_invocation_id=core_invocation_id,
                    )
                    toy_delivery = await toy_ports.broadcast_toy_command(
                        commands=toy_matches,
                        msg_id=msg_id,
                        conv_id=conv_id,
                        toy_capability_allowed=toy_capability.allowed,
                        control_session_id=toy_capability.control_session_id or None,
                        control_epoch=toy_capability.control_epoch,
                        owner_client_id=toy_capability.owner_client_id or None,
                        control_device_id=toy_capability.control_device_id or None,
                        request_id=msg_id,
                        wake_id=msg_id,
                    )
                await append_and_broadcast_monitor_log(_sentinel_monitor_log_entry(
                    status="core_succeeded",
                    monitoringlog=f"🧠 哨兵唤醒 Core 并生成回复：{full_content[:80]}...",
                    call_core=False,
                    core_reason=core_reason,
                    summary=summary,
                    conv_id=conv_id,
                    core_msg_id=msg_id,
                    toy_commands=toy_matches,
                    toy_command_delivery=toy_delivery,
                    context_errors=context_errors,
                ))
                break
            except Exception as e:
                print(f"[Sentinel→Core] 第 {attempt} 次调用失败: {e}")
                if attempt < max_attempts:
                    await asyncio.sleep(10)
                else:
                    await append_and_broadcast_monitor_log(_sentinel_monitor_log_entry(
                        status="core_failed",
                        monitoringlog=f"⚠️ 哨兵唤醒 Core 连续失败：{type(e).__name__}: {e}",
                        call_core=False,
                        core_reason=core_reason,
                        summary=summary,
                        error_type=type(e).__name__,
                        error=str(e),
                        conv_id=conv_id,
                        context_errors=context_errors,
                    ))
                    import traceback; traceback.print_exc()


sentinel_runtime = SentinelRuntime()
