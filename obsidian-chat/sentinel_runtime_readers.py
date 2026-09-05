"""Runtime readers for preparing Sentinel dry-run context packs.

This module is intentionally outside ``app.sentinel`` because it touches legacy
runtime IO boundaries: database and monitor log files.
"""

from __future__ import annotations

import json
import inspect
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import aiosqlite

from app.context_delivery import ContextDeliveryProjection
from app.sentinel import build_sentinel_runtime_context
from app.chat.audio_input import is_audio_attachment, parse_attachments
from app.chat.worldbook import resolve_worldbook_names
from config import DEFAULT_MODEL, MONITOR_LOGS_DIR, load_ai_behavior, load_cam_config, load_worldbook
from database import get_db


DEFAULT_RUNTIME_READER_RECENT_CHAT_LIMIT = 10
DEFAULT_RUNTIME_READER_SENTINEL_LOG_LIMIT = 20
DEFAULT_RUNTIME_READER_SENTINEL_LOG_LOOKBACK_SEC = 6 * 3600
DEFAULT_CORE_WAKE_EXECUTION_CONTEXT_MESSAGE_LIMIT = 20


async def collect_sentinel_runtime_context_payload(
    *,
    reference_time: float | None = None,
    db_factory: Callable | None = None,
    monitor_logs_dir: str | Path | None = None,
    worldbook_loader: Callable[[], dict] | None = None,
    ai_behavior_loader: Callable[[], dict] | None = None,
    cam_config_loader: Callable[[], dict] | None = None,
    recent_chat_limit: int = DEFAULT_RUNTIME_READER_RECENT_CHAT_LIMIT,
    sentinel_log_limit: int = DEFAULT_RUNTIME_READER_SENTINEL_LOG_LIMIT,
    sentinel_log_lookback_sec: int = DEFAULT_RUNTIME_READER_SENTINEL_LOG_LOOKBACK_SEC,
    context_projection_reader: Callable[..., ContextDeliveryProjection] | None = None,
) -> dict[str, Any]:
    """Read runtime material and return the raw payload expected by the context normalizer."""
    reference = time.time() if reference_time is None else float(reference_time)
    db_factory = db_factory or get_db
    monitor_logs_dir = Path(monitor_logs_dir or MONITOR_LOGS_DIR)
    worldbook = (worldbook_loader or load_worldbook)()
    ai_behavior = (ai_behavior_loader or load_ai_behavior)()
    cam_config = (cam_config_loader or load_cam_config)()

    user_name, ai_name = resolve_worldbook_names(worldbook)
    recent_chat = await _read_recent_chat(
        db_factory,
        limit=_positive_limit(recent_chat_limit, key="recent_chat_limit"),
    )
    last_user_ts = await _read_last_user_message_ts(db_factory)
    sentinel_log_limit = _positive_limit(sentinel_log_limit, key="sentinel_log_limit")
    sentinel_log_entries = _read_recent_sentinel_log_entries(
        monitor_logs_dir,
        reference_time=reference,
        lookback_sec=_positive_limit(sentinel_log_lookback_sec, key="sentinel_log_lookback_sec"),
    )
    recent_sentinel_logs = [item for _timestamp, item in sentinel_log_entries[-sentinel_log_limit:]]
    last_wake_ts = _last_wake_timestamp(sentinel_log_entries)
    if context_projection_reader is None:
        from app.presence.summon import SummonEventRepository
        from context_delivery_runtime_readers import read_context_delivery_projection_async

        latest_conversation = await _read_latest_conversation(db_factory)
        context_projection = await read_context_delivery_projection_async(
            reference_time=reference,
            conv_id=(
                latest_conversation["conv_id"]
                if latest_conversation is not None
                else None
            ),
            summon_repository=SummonEventRepository(
                get_db_factory=db_factory,
            ),
        )
    else:
        context_projection = context_projection_reader(reference_time=reference)
        if inspect.isawaitable(context_projection):
            context_projection = await context_projection
    if not isinstance(context_projection, ContextDeliveryProjection):
        raise ValueError("sentinel runtime reader context projection must use ContextDeliveryProjection")
    payload: dict[str, Any] = {
        "now": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(reference)),
        "user_name": user_name,
        "ai_name": ai_name,
        "last_user_chat_time": _format_timestamp(last_user_ts),
        "recent_chat": recent_chat,
        "recent_sentinel_logs": recent_sentinel_logs,
        "context_projection": context_projection.to_dict(),
        "sentinel_call_core_criteria": _text_or_default(
            ai_behavior.get("sentinel_call_core_criteria"),
            "score >= 7 只是参考；你必须自己判断 wake_intent。",
        ),
        "quiet_hours_active": is_quiet_hours(cam_config, reference_time=reference),
        "clear_sleep": False,
        "device_effect_requested": False,
        "device_effect_allowed": False,
        "urgent_risk": False,
    }
    if last_user_ts > 0:
        payload["last_user_message_age_sec"] = max(0.0, reference - last_user_ts)
    if last_wake_ts > 0:
        payload["last_wake_age_sec"] = max(0.0, reference - last_wake_ts)
    return payload


async def read_sentinel_runtime_context(
    **kwargs,
) -> dict[str, Any]:
    """Read runtime material and return a normalized Sentinel runtime context pack."""
    payload = await collect_sentinel_runtime_context_payload(**kwargs)
    return build_sentinel_runtime_context(payload)


async def read_core_wake_execution_context(
    *,
    reference_time: float | None = None,
    db_factory: Callable | None = None,
    worldbook_loader: Callable[[], dict] | None = None,
    control_session_service_obj: Any | None = None,
    device_service_obj: Any | None = None,
    recent_message_limit: int = DEFAULT_CORE_WAKE_EXECUTION_CONTEXT_MESSAGE_LIMIT,
) -> dict[str, Any]:
    """Read the execution context needed to dry-run a Core wake call."""
    reference = time.time() if reference_time is None else float(reference_time)
    db_factory = db_factory or get_db
    worldbook = (worldbook_loader or load_worldbook)()
    user_name, ai_name = resolve_worldbook_names(worldbook)
    conversation = await _read_latest_conversation(db_factory)
    payload: dict[str, Any] = {
        "user_name": user_name,
        "ai_name": ai_name,
        "recent_messages": [],
    }
    try:
        from app.presence.prompt_context import (
            build_presence_identity_block,
            presence_identity_head,
        )
        from app.presence.sprites import SpriteLibrary

        identity_head = await presence_identity_head(
            library=SpriteLibrary(get_db_factory=db_factory)
        )
        presence_identity_block = build_presence_identity_block(
            identity_head,
            user_name=user_name,
            ai_name=ai_name,
        )
        if presence_identity_block:
            payload["presence_identity_block"] = presence_identity_block
    except Exception:
        pass
    ai_persona = _optional_worldbook_text(worldbook.get("ai_persona"), key="ai_persona")
    user_persona = _optional_worldbook_text(worldbook.get("user_persona"), key="user_persona")
    if ai_persona:
        payload["ai_persona"] = ai_persona
    if user_persona:
        payload["user_persona"] = user_persona
    if conversation is not None:
        payload["conv_id"] = conversation["conv_id"]
        if conversation["model_key"]:
            payload["model_key"] = conversation["model_key"]
        if control_session_service_obj is None:
            from app.control.service import ControlSessionService

            control_session_service_obj = ControlSessionService(get_db_factory=db_factory)
        from app.control.toy_capability import resolve_toy_capability_snapshot

        toy_capability = await resolve_toy_capability_snapshot(
            conv_id=conversation["conv_id"],
            session_service=control_session_service_obj,
            device_service_adapter=device_service_obj,
        )
        payload.update(toy_capability.to_execution_context())
        payload["recent_messages"] = await _read_recent_messages_for_conversation(
            db_factory,
            conv_id=conversation["conv_id"],
            limit=_positive_limit(recent_message_limit, key="recent_message_limit"),
        )
        try:
            from app.mobile_screen.autonomous import autonomous_mobile_screen_target

            mobile_target = await autonomous_mobile_screen_target(
                model_key=payload["model_key"],
                now=reference,
            )
            if mobile_target:
                payload["autonomous_mobile_screen_target"] = mobile_target
        except Exception:
            pass

    last_user_ts = await _read_last_user_message_ts(db_factory)
    if last_user_ts > 0:
        payload["last_user_message_age_sec"] = max(0.0, reference - last_user_ts)
    return payload


async def _read_latest_conversation(db_factory: Callable) -> dict[str, str] | None:
    async with db_factory() as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("SELECT id, model FROM conversations ORDER BY updated_at DESC LIMIT 1")
        row = await cur.fetchone()
    if not row:
        return None
    conv_id = _required_text(_row_get(row, "id", 0), key="conversation.id")
    model = _row_get(row, "model", 1)
    model_key = _text_or_default(model, DEFAULT_MODEL)
    return {
        "conv_id": conv_id,
        "model_key": model_key,
    }


async def _read_recent_messages_for_conversation(
    db_factory: Callable,
    *,
    conv_id: str,
    limit: int,
) -> list[dict[str, str]]:
    async with db_factory() as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT id, role, content, attachments FROM messages "
            "WHERE conv_id=? AND role IN ('user','assistant') "
            "ORDER BY created_at DESC LIMIT ?",
            (conv_id, limit),
        )
        rows = await cur.fetchall()
    result = []
    for row in reversed(rows):
        content = _message_text(
            _row_get(row, "content", 2),
            _row_get(row, "attachments", 3),
        )
        if not content:
            continue
        result.append({
            "id": _required_text(_row_get(row, "id", 0), key="message.id"),
            "role": _required_text(_row_get(row, "role", 1), key="message.role"),
            "content": content,
        })
    return result


async def _read_recent_chat(db_factory: Callable, *, limit: int) -> list[dict[str, str]]:
    async with db_factory() as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("SELECT id FROM conversations ORDER BY updated_at DESC LIMIT 1")
        conv = await cur.fetchone()
        if not conv:
            return []
        conv_id = _row_get(conv, "id", 0)
        cur = await db.execute(
            "SELECT role, content, attachments FROM messages "
            "WHERE conv_id=? AND role IN ('user','assistant') "
            "ORDER BY created_at DESC LIMIT ?",
            (conv_id, limit),
        )
        rows = await cur.fetchall()
    return _compact_message_rows(rows)


def _compact_message_rows(rows: list[Any]) -> list[dict[str, str]]:
    result = []
    for row in reversed(rows):
        content = _message_text(
            _row_get(row, "content", 1),
            _row_get(row, "attachments", 2),
        )
        if not content:
            continue
        result.append({
            "role": _required_text(_row_get(row, "role", 0), key="message.role"),
            "content": content,
        })
    return result


def _message_text(content: Any, attachments: Any) -> str:
    if not isinstance(content, str):
        raise ValueError("sentinel runtime reader message.content must be text")
    text = content.strip()
    if text:
        return text

    transcripts = []
    for attachment in parse_attachments(attachments):
        if not isinstance(attachment, dict) or not is_audio_attachment(attachment):
            continue
        transcript = str(attachment.get("transcript") or "").strip()
        if transcript:
            transcripts.append(transcript)
    return "\n".join(transcripts)


async def _read_last_user_message_ts(db_factory: Callable) -> float:
    async with db_factory() as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT created_at FROM messages WHERE role='user' ORDER BY created_at DESC LIMIT 1"
        )
        row = await cur.fetchone()
    if not row:
        return 0.0
    value = _row_get(row, "created_at", 0)
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise ValueError("sentinel runtime reader last user message timestamp must be a number")
    return float(value)


def _read_recent_sentinel_log_entries(
    logs_dir: Path,
    *,
    reference_time: float,
    lookback_sec: int,
) -> list[tuple[float, dict[str, Any]]]:
    if not logs_dir.exists():
        return []
    since_ts = reference_time - lookback_sec
    entries = []
    for path in sorted(logs_dir.glob("*.jsonl")):
        if not path.is_file():
            continue
        for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"sentinel monitor log {path.name}:{line_number} invalid JSON: {exc}") from exc
            if not isinstance(entry, dict):
                raise ValueError(f"sentinel monitor log {path.name}:{line_number} must be an object")
            timestamp = entry.get("timestamp", 0)
            if isinstance(timestamp, bool) or not isinstance(timestamp, int | float):
                raise ValueError(f"sentinel monitor log {path.name}:{line_number} timestamp must be a number")
            if timestamp < since_ts:
                continue
            if entry.get("source", "sentinel") != "sentinel":
                continue
            monitoringlog = _required_text(
                entry.get("monitoringlog"),
                key=f"monitor log {path.name}:{line_number}.monitoringlog",
            )
            compact = {
                "monitoringlog": monitoringlog,
            }
            for key in ("time", "status"):
                value = str(entry.get(key) or "").strip()
                if value:
                    compact[key] = value
            if entry.get("score") is not None:
                if isinstance(entry["score"], bool) or not isinstance(entry["score"], int | float):
                    raise ValueError(f"sentinel monitor log {path.name}:{line_number} score must be a number")
                compact["score"] = entry["score"]
            if "call_core" in entry:
                if not isinstance(entry["call_core"], bool):
                    raise ValueError(f"sentinel monitor log {path.name}:{line_number} call_core must be a boolean")
                compact["call_core"] = entry["call_core"]
            entries.append((float(timestamp), compact))
    entries.sort(key=lambda item: item[0])
    return entries


def _last_wake_timestamp(entries: list[tuple[float, dict[str, Any]]]) -> float:
    for timestamp, entry in reversed(entries):
        if entry.get("call_core") is True:
            return timestamp
    return 0.0


def is_quiet_hours(config: Any, *, reference_time: float) -> bool:
    if config is None:
        config = {}
    if not isinstance(config, dict):
        raise ValueError("sentinel runtime reader cam config must be an object")
    enabled = config.get("quiet_hours_enabled", False)
    if not isinstance(enabled, bool):
        raise ValueError("sentinel runtime reader quiet_hours_enabled must be a boolean")
    if not enabled:
        return False
    start = _parse_hhmm(config.get("quiet_hours_start", "00:00"), key="quiet_hours_start")
    end = _parse_hhmm(config.get("quiet_hours_end", "09:00"), key="quiet_hours_end")
    now = time.localtime(reference_time)
    current = now.tm_hour * 60 + now.tm_min
    if start <= end:
        return start <= current < end
    return current >= start or current < end


def _parse_hhmm(value: Any, *, key: str) -> int:
    if not isinstance(value, str):
        raise ValueError(f"sentinel runtime reader {key} must be HH:MM text")
    parts = value.strip().split(":")
    if len(parts) != 2:
        raise ValueError(f"sentinel runtime reader {key} must be HH:MM text")
    try:
        hour = int(parts[0])
        minute = int(parts[1])
    except ValueError as exc:
        raise ValueError(f"sentinel runtime reader {key} must be HH:MM text") from exc
    if not 0 <= hour <= 23 or not 0 <= minute <= 59:
        raise ValueError(f"sentinel runtime reader {key} must be HH:MM text")
    return hour * 60 + minute


def _row_get(row: Any, key: str, index: int) -> Any:
    try:
        return row[key]
    except (KeyError, TypeError, IndexError):
        return row[index]


def _format_timestamp(ts: float) -> str:
    if ts <= 0:
        return "未知"
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts))


def _text_or_default(value: Any, default: str) -> str:
    text = str(value or "").strip()
    return text or default


def _optional_worldbook_text(value: Any, *, key: str) -> str:
    if value is None:
        return ""
    if not isinstance(value, str):
        raise ValueError(f"sentinel runtime reader worldbook {key} must be text")
    return value.strip()


def _required_text(value: Any, *, key: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"sentinel runtime reader {key} must be non-empty text")
    return value.strip()


def _positive_limit(value: Any, *, key: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"sentinel runtime reader {key} must be an integer")
    if value <= 0:
        raise ValueError(f"sentinel runtime reader {key} must be positive")
    return value


__all__ = [
    "DEFAULT_CORE_WAKE_EXECUTION_CONTEXT_MESSAGE_LIMIT",
    "DEFAULT_RUNTIME_READER_RECENT_CHAT_LIMIT",
    "DEFAULT_RUNTIME_READER_SENTINEL_LOG_LIMIT",
    "DEFAULT_RUNTIME_READER_SENTINEL_LOG_LOOKBACK_SEC",
    "collect_sentinel_runtime_context_payload",
    "is_quiet_hours",
    "read_core_wake_execution_context",
    "read_sentinel_runtime_context",
]
