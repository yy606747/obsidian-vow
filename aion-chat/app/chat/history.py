"""History and worldbook context preparation for chat turns."""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Literal

import aiosqlite

from config import DEFAULT_MODEL, load_worldbook
from database import get_db

from .commands import _SYSTEM_MSG_CONTEXT_KEYWORDS
from .audio_input import audio_transcript_context
from .worldbook import build_worldbook_prefix, resolve_worldbook_names


AttachmentPolicy = Literal["last_message", "last_user"]

# 「昨日续点」：新框第一轮才考虑接上一段最近的真实对话
PREVIOUS_CONVERSATION_MAX_AGE_SECONDS = 48 * 60 * 60
PREVIOUS_CONVERSATION_CURRENT_MESSAGE_LIMIT = 2


@dataclass
class ChatHistoryContext:
    model_key: str
    history: list[dict]
    actual_recent: list[dict]
    visible_message_ids: list[str]
    wb: dict
    prefix: list[dict]
    cap_idx: int
    latest_user_message_id: str | None = None
    previous_turn_assistant_ids: tuple[str, ...] = ()
    previous_conversation_id: str | None = None
    previous_conversation_source: dict | None = None


def build_handoff_note_block(
    note: str,
    *,
    user_name: str = "她",
    source: dict | None = None,
) -> str:
    """把「昨日续点」便签包成一个静默背景块；模型用来保持连贯，但不复述。"""
    note = " ".join(str(note or "").split())
    if not note:
        return ""
    lines = [
        "[昨日续点]",
        f"下面是你和{user_name}上一段对话的续点，仅供你心里有数地保持连贯：",
    ]
    title = str((source or {}).get("title") or "").strip()
    if title:
        lines.append(f"（来源：{title}）")
    lines.append(f"· {note}")
    lines.append(
        f"使用规则：如果{user_name}这次的新消息延续了它，就自然地接着聊；如果是新话题，就当它不存在。"
        f"情绪/状态可以轻轻关心一句，具体话题等{user_name}自己提起再接。任何情况下都不要提到、复述或解释这条续点。"
        "上一段里的设备/密语/场景/位置状态都已结束，当前状态只以本轮实时能力为准。"
    )
    return "\n".join(lines).strip()


def _normalize_history_row(row) -> dict | None:
    msg = dict(row)
    if msg["role"] == "trigger":
        msg["role"] = "user"
        msg["attachments"] = []
        return msg

    if msg["role"] == "system":
        if not any(kw in msg["content"] for kw in _SYSTEM_MSG_CONTEXT_KEYWORDS):
            return None
        msg["role"] = "user"
        msg["content"] = f"[系统事件] {msg['content']}"
        msg["attachments"] = []
        return msg

    try:
        msg["attachments"] = json.loads(msg.get("attachments") or "[]") if msg.get("attachments") else []
    except Exception:
        msg["attachments"] = []

    transcript_context = audio_transcript_context(msg["attachments"])
    if transcript_context:
        msg["content"] = "\n".join(
            part for part in (str(msg.get("content") or "").strip(), transcript_context)
            if part
        )

    if msg.get("created_at"):
        dt = datetime.fromtimestamp(msg["created_at"])
        msg["content"] = f"{msg['content']}\n<meta>发送时间：{dt.month}月{dt.day}日 {dt.strftime('%H:%M')}</meta>"
    return msg


def _strip_history_attachments(history: list[dict], policy: AttachmentPolicy) -> None:
    if policy == "last_message":
        for msg in history[:-1]:
            msg["attachments"] = []
        return

    last_user_idx = -1
    for i in range(len(history) - 1, -1, -1):
        if history[i]["role"] == "user":
            last_user_idx = i
            break
    for i, msg in enumerate(history):
        if i != last_user_idx:
            msg["attachments"] = []


async def prepare_chat_history(
    conv_id: str,
    *,
    context_limit: int,
    attachment_policy: AttachmentPolicy,
    retracted: bool = False,
) -> ChatHistoryContext:
    previous_conv_id: str | None = None
    previous_source: dict | None = None
    async with get_db() as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("SELECT model FROM conversations WHERE id=?", (conv_id,))
        conv = await cur.fetchone()
        model_key = conv["model"] if conv else DEFAULT_MODEL

        cur = await db.execute(
            "SELECT id, role, content, attachments, created_at FROM messages WHERE conv_id=? AND role IN ('user','assistant','system','trigger') ORDER BY created_at DESC LIMIT ?",
            (conv_id, context_limit),
        )
        rows = await cur.fetchall()

        cur = await db.execute(
            "SELECT COUNT(*) AS count FROM messages WHERE conv_id=? AND role IN ('user','assistant')",
            (conv_id,),
        )
        count_row = await cur.fetchone()
        current_message_count = int(count_row["count"] if count_row else 0)
        if current_message_count <= PREVIOUS_CONVERSATION_CURRENT_MESSAGE_LIMIT:
            # 按「用户最后一句话的时间」选最近的真实对话——绕开后台任务篡改 updated_at，
            # 也天然排除了没有用户消息的纯系统/后台对话。
            cur = await db.execute(
                "SELECT c.id AS id, c.title AS title, "
                "(SELECT MAX(m.created_at) FROM messages m WHERE m.conv_id=c.id AND m.role='user') AS last_user_at "
                "FROM conversations c WHERE c.id != ? "
                "AND EXISTS (SELECT 1 FROM messages m WHERE m.conv_id=c.id AND m.role='user') "
                "ORDER BY last_user_at DESC LIMIT 1",
                (conv_id,),
            )
            previous_conv = await cur.fetchone()
            if previous_conv and previous_conv["last_user_at"] is not None:
                last_user_at = float(previous_conv["last_user_at"])
                if time.time() - last_user_at <= PREVIOUS_CONVERSATION_MAX_AGE_SECONDS:
                    previous_conv_id = previous_conv["id"]
                    previous_source = {
                        "conv_id": previous_conv["id"],
                        "title": previous_conv["title"],
                        "last_user_at": last_user_at,
                    }

    ordered_rows = list(reversed(rows))
    real_user_indexes = [
        index for index, row in enumerate(ordered_rows) if row["role"] == "user"
    ]
    previous_turn_assistant_ids: tuple[str, ...] = ()
    if len(real_user_indexes) >= 2:
        previous_user_index, current_user_index = real_user_indexes[-2:]
        previous_turn_assistant_ids = tuple(
            str(row["id"])
            for row in ordered_rows[previous_user_index + 1:current_user_index]
            if row["role"] == "assistant" and str(row["id"] or "")
        )

    wb = load_worldbook()
    user_name, _ai_name = resolve_worldbook_names(wb)

    history = []
    for row in ordered_rows:
        msg = _normalize_history_row(row)
        if msg is not None:
            history.append(msg)

    _strip_history_attachments(history, attachment_policy)

    if retracted and history:
        history.insert(-1, {
            "role": "user",
            "content": f"[系统事件] {user_name}刚刚偷偷撤回了一条消息",
            "attachments": [],
        })

    latest_user_message_id = next(
        (
            str(message.get("id") or "")
            for message in reversed(history)
            if message.get("role") == "user" and str(message.get("id") or "")
        ),
        "",
    )
    recent_with_ids = [m for m in history if m["role"] in ("user", "assistant")][-8:]
    visible_message_ids = [
        str(message.get("id") or "")
        for message in history
        if message["role"] in ("user", "assistant")
        if str(message.get("id") or "")
    ]
    # Stable database IDs are retained beside history for provenance dedupe,
    # but never enter the provider-facing message dictionaries.
    actual_recent = [
        {key: value for key, value in message.items() if key != "id"}
        for message in recent_with_ids
    ]
    for message in history:
        message.pop("id", None)

    prefix = build_worldbook_prefix(wb)
    if prefix:
        history = prefix + history

    return ChatHistoryContext(
        model_key=model_key,
        history=history,
        actual_recent=actual_recent,
        visible_message_ids=visible_message_ids,
        wb=wb,
        prefix=prefix,
        cap_idx=len(prefix) if prefix else 0,
        latest_user_message_id=latest_user_message_id or None,
        previous_turn_assistant_ids=previous_turn_assistant_ids,
        previous_conversation_id=previous_conv_id,
        previous_conversation_source=previous_source,
    )
