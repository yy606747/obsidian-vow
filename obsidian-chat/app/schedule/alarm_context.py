"""Creation-time conversational context for alarm turns.

This is a schedule-owned sidecar.  It deliberately stores only visible
user/assistant messages and never copies provider prompts or injected memory.
"""

from __future__ import annotations

import json
import logging
import time
from datetime import datetime
from zoneinfo import ZoneInfo

import aiosqlite

from database import get_db


log = logging.getLogger("schedule")

SCHEMA_VERSION = "alarm_creation_context.v1"
MAX_CONTEXT_MESSAGES = 6
MAX_MESSAGE_CHARS = 1000
MAX_CONTEXT_CHARS = 4200
MAX_PROMPT_CHARS = 5600
OWNER_TZ = ZoneInfo("Asia/Shanghai")


async def init_alarm_context_tables(db) -> None:
    await db.execute(
        """
        CREATE TABLE IF NOT EXISTS alarm_creation_contexts (
            schedule_id TEXT PRIMARY KEY,
            conv_id TEXT NOT NULL,
            source_message_ids_json TEXT NOT NULL DEFAULT '[]',
            messages_json TEXT NOT NULL DEFAULT '[]',
            schema_version TEXT NOT NULL,
            captured_at REAL NOT NULL,
            FOREIGN KEY (schedule_id) REFERENCES schedules(id) ON DELETE CASCADE
        )
        """
    )
    await db.execute(
        "CREATE INDEX IF NOT EXISTS idx_alarm_creation_contexts_conv "
        "ON alarm_creation_contexts(conv_id, captured_at DESC)"
    )


def _one_line(value: object) -> str:
    return " ".join(str(value or "").split()).strip()


def _decode_messages(value: object) -> list[dict]:
    try:
        parsed = json.loads(str(value or "[]"))
    except (TypeError, json.JSONDecodeError):
        return []
    if not isinstance(parsed, list):
        return []
    messages: list[dict] = []
    for raw in parsed:
        if not isinstance(raw, dict) or raw.get("role") not in {"user", "assistant"}:
            continue
        content = _one_line(raw.get("content"))
        message_id = str(raw.get("id") or "").strip()
        if not content or not message_id:
            continue
        messages.append(
            {
                "id": message_id,
                "role": raw["role"],
                "content": content,
                "created_at": float(raw.get("created_at") or 0),
            }
        )
    return messages


def _bounded_snapshot(rows: list[dict]) -> list[dict]:
    selected_reversed: list[dict] = []
    used_chars = 0
    for row in rows:
        content = _one_line(row.get("content"))[:MAX_MESSAGE_CHARS]
        if not content:
            continue
        remaining = MAX_CONTEXT_CHARS - used_chars
        if remaining <= 0:
            break
        content = content[:remaining]
        selected_reversed.append(
            {
                "id": str(row.get("id") or ""),
                "role": str(row.get("role") or ""),
                "content": content,
                "created_at": float(row.get("created_at") or 0),
            }
        )
        used_chars += len(content)
    return list(reversed(selected_reversed))


async def capture_creation_context(
    schedule_id: str,
    conv_id: str,
    *,
    source_message_id: str | None = None,
) -> dict:
    """Freeze the visible conversation ending at the alarm-setting user turn."""

    schedule_id = str(schedule_id or "").strip()
    conv_id = str(conv_id or "").strip()
    if not schedule_id or not conv_id:
        return {"status": "invalid", "message_count": 0}

    async with get_db() as db:
        db.row_factory = aiosqlite.Row
        anchor_at: float | None = None
        source_message_id = str(source_message_id or "").strip() or None
        if source_message_id:
            cur = await db.execute(
                "SELECT created_at FROM messages WHERE id=? AND conv_id=? "
                "AND role IN ('user','assistant')",
                (source_message_id, conv_id),
            )
            anchor = await cur.fetchone()
            if anchor is None:
                return {
                    "status": "source_missing",
                    "message_count": 0,
                    "source_message_ids": [],
                }
            anchor_at = float(anchor["created_at"])

        where_anchor = " AND created_at<=?" if anchor_at is not None else ""
        params: tuple[object, ...] = (
            (conv_id, anchor_at)
            if anchor_at is not None
            else (conv_id,)
        )
        cur = await db.execute(
            "SELECT id, role, content, created_at FROM messages WHERE conv_id=? "
            "AND role IN ('user','assistant')"
            f"{where_anchor} ORDER BY created_at DESC, id DESC LIMIT ?",
            (*params, MAX_CONTEXT_MESSAGES),
        )
        rows = [dict(row) for row in await cur.fetchall()]
        messages = _bounded_snapshot(rows)
        if not messages:
            return {"status": "empty", "message_count": 0}

        captured_at = time.time()
        source_ids = [message["id"] for message in messages]
        cur = await db.execute(
            "INSERT OR IGNORE INTO alarm_creation_contexts "
            "(schedule_id, conv_id, source_message_ids_json, messages_json, "
            "schema_version, captured_at) VALUES (?,?,?,?,?,?)",
            (
                schedule_id,
                conv_id,
                json.dumps(source_ids, ensure_ascii=False),
                json.dumps(messages, ensure_ascii=False),
                SCHEMA_VERSION,
                captured_at,
            ),
        )
        await db.commit()
        return {
            "status": "captured" if int(cur.rowcount or 0) > 0 else "exists",
            "message_count": len(messages),
            "source_message_ids": source_ids,
        }


async def _load_contexts(schedule_ids: list[str]) -> dict[str, dict]:
    ids = list(dict.fromkeys(str(value or "") for value in schedule_ids if str(value or "")))
    if not ids:
        return {}
    placeholders = ",".join("?" for _ in ids)
    try:
        async with get_db() as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute(
                "SELECT schedule_id, conv_id, messages_json, schema_version, captured_at "
                f"FROM alarm_creation_contexts WHERE schedule_id IN ({placeholders})",
                ids,
            )
            rows = [dict(row) for row in await cur.fetchall()]
    except aiosqlite.OperationalError as exc:
        if "no such table" in str(exc).lower():
            return {}
        raise
    return {
        str(row["schedule_id"]): {
            **row,
            "messages": _decode_messages(row.get("messages_json")),
        }
        for row in rows
    }


def _format_time(timestamp: float) -> str:
    return datetime.fromtimestamp(float(timestamp), OWNER_TZ).strftime("%Y-%m-%d %H:%M")


def _render_prompt_block(
    alarms: list[dict],
    contexts: dict[str, dict],
    *,
    user_name: str,
    ai_name: str,
) -> str:
    header = [
        "[设置闹钟时的上下文]",
        "以下是创建闹钟时冻结的原始可见对话，只用于理解当时为什么这样约定。",
        "它是历史快照，不自动代表现在；闹钟正文与最近三天的新事实、纠正优先。",
    ]
    lines = list(header)
    included = 0
    for alarm in alarms:
        context = contexts.get(str(alarm.get("id") or "")) or {}
        messages = context.get("messages") or []
        if not messages:
            continue
        section = [
            "",
            f"闹钟：{alarm.get('trigger_at')} — {_one_line(alarm.get('content'))}",
        ]
        for message in messages:
            speaker = user_name if message["role"] == "user" else ai_name
            section.append(
                f"- {_format_time(message['created_at'])} {speaker}：{message['content']}"
            )
        trial = "\n".join([*lines, *section])
        if len(trial) > MAX_PROMPT_CHARS:
            break
        lines.extend(section)
        included += 1
    return "\n".join(lines) if included else ""


async def load_prompt_context(
    items: list[dict],
    *,
    user_name: str,
    ai_name: str,
) -> dict:
    alarms = [item for item in items if item.get("type") == "alarm"]
    contexts = await _load_contexts([str(item.get("id") or "") for item in alarms])
    block = _render_prompt_block(
        alarms,
        contexts,
        user_name=user_name,
        ai_name=ai_name,
    )
    visible_messages = [
        {"id": message["id"]}
        for alarm in alarms
        for message in (contexts.get(str(alarm.get("id") or "")) or {}).get("messages", [])
    ]
    return {
        "status": "loaded" if block else "missing",
        "block": block,
        "schedule_ids": [str(item.get("id") or "") for item in alarms],
        "visible_messages": visible_messages,
        "message_count": len(visible_messages),
    }


__all__ = [
    "SCHEMA_VERSION",
    "capture_creation_context",
    "init_alarm_context_tables",
    "load_prompt_context",
]
