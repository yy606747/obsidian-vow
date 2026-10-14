from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Identity:
    user_name: str
    ai_name: str
    ai_persona: str
    user_persona: str = ""
    working_model: str = ""


async def load_identity():
    from config import load_ai_behavior, load_worldbook, load_working_model
    from app.chat.worldbook import resolve_worldbook_names
    worldbook = load_worldbook()
    names = resolve_worldbook_names(worldbook)
    if load_ai_behavior().get("working_model_v2_injection_enabled", False):
        from database import get_db
        from app.working_model.repository import get_head
        async with get_db() as db:
            head = await get_head(db)
        if head is None:
            raise ValueError("together_working_model_missing")
        working_model = str(head.get("content") or "")
    else:
        working_model = str(load_working_model().get("content") or "")
    return Identity(*names, str(worldbook.get("ai_persona") or ""),
                    str(worldbook.get("user_persona") or ""), working_model)


async def resolve_entry(conv_id, count, *, db_factory=None):
    """The same recent N utterances as chat, captured once without media."""
    from database import get_db
    from app.daily_chat.service import resolve_conversation_id
    if type(count) is not int or not 0 <= count <= 100:
        raise ValueError("together_invalid_history_count")
    conv_id = resolve_conversation_id(conv_id)
    async with (db_factory or get_db)() as db:
        conversation = await (await db.execute("SELECT model FROM conversations WHERE id=?", (conv_id,))).fetchone()
        if not conversation:
            raise ValueError("together_conversation_missing")
        cursor = await db.execute("""SELECT id,role,content,created_at FROM messages
            WHERE conv_id=? AND role IN ('user','assistant')
            ORDER BY created_at DESC,rowid DESC LIMIT ?""", (conv_id, count))
        columns = [column[0] for column in cursor.description]
        rows = [dict(zip(columns, row)) for row in await cursor.fetchall()]
        from app.daily_chat.service import inherited_messages_in_tx
        from .shared_history import context_rows_in_tx, select_shared_window, ended_context_note
        inherited = await inherited_messages_in_tx(db, conv_id)
        together = await context_rows_in_tx(db, conv_id, limit=count)
        current, old = select_shared_window(rows, inherited, together, conv_id=conv_id, limit=count)
    return conv_id, str(conversation[0]), [
        {"role": row['role'], "content": row['content'] + ended_context_note(row),
         "created_at": row['created_at']} for row in old + current]


def bounded_history(entry, messages, *, max_messages=40, max_characters=16000):
    """Keep the newest text, never attach old images or silently summarize."""
    if max_messages <= 0:
        return []
    selected = ([dict(m) for m in entry] + [dict(m) for m in messages])[-max_messages:]
    # A single current message may use the whole window. Server input limits
    # ensure this is normally unnecessary; oldest content is always evicted first.
    remaining = max_characters
    kept = []
    for item in reversed(selected):
        text = str(item.get("content") or "")
        if not text:
            continue
        if remaining <= 0:
            break
        text = text[-remaining:]
        kept.append({"role": item["role"], "content": text})
        remaining -= len(text)
    return list(reversed(kept))
