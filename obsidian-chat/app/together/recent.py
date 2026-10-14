"""Bounded, dated context after a completed session; no extra model calls."""
from __future__ import annotations

import logging
import time
from datetime import datetime
from zoneinfo import ZoneInfo

from .prompts import event_text
from .repository import TogetherRepository

TTL_SECONDS = 24 * 60 * 60
MAX_EVENTS = 3
MARKER = "【近期共同经历】"


def render_recent(rows, *, user_name, ai_name, now, timezone="UTC"):
    current = [row for row in rows if row.get("status") == "ended"
               and now-TTL_SECONDS < float(row.get("ended_at") or 0) <= now]
    current.sort(key=lambda row: row["ended_at"], reverse=True)
    if not current:
        return ""
    lines = [MARKER]
    for row in reversed(current[:MAX_EVENTS]):
        stamp = datetime.fromtimestamp(row["ended_at"], ZoneInfo(timezone)).isoformat(timespec="minutes")
        lines.append(f"{stamp} 结束：" + event_text(user_name=user_name, ai_name=ai_name,
                                                 active_seconds=row["active_seconds"]))
        note = " ".join(str(row.get("note") or "").split())[:100]
        if note:
            lines.append(f"{ai_name}在这场相处中主动留下的笔记：{note}")
    lines.append(f"以上相处均已结束。{ai_name}与{user_name}可以自然接续话题，不必反复提及，也不据此假定仍有实时画面。")
    return "\n".join(lines)


async def load_recent(*, user_name, ai_name, now=None, repository=None, timezone=None):
    now = time.time() if now is None else now
    repo = repository or TogetherRepository()
    if timezone is None:
        from app.daily_chat.service import daily_chat
        timezone = daily_chat.timezone()
    rows = await repo.read("""SELECT id,status,ended_at,active_seconds FROM together_sessions
        WHERE status='ended' AND ended_at>? AND ended_at<=? ORDER BY ended_at DESC LIMIT ?""",
        (now-TTL_SECONDS, now, MAX_EVENTS), many=True)
    for row in rows:
        note = await repo.read("""SELECT payload FROM together_writebacks
            WHERE session_id=? AND kind='note' ORDER BY created_at DESC LIMIT 1""", (row["id"],))
        if note:
            row["note"] = note["payload"].get("value", "")
    return render_recent(rows, user_name=user_name, ai_name=ai_name, now=now, timezone=timezone)


async def inject_recent(history, *, user_name, ai_name):
    # Insert in the dynamic tail, outside the stable cache prefix. Never retain
    # this hint as chat history, a summary, or a second memory write.
    try:
        text = await load_recent(user_name=user_name, ai_name=ai_name)
    except Exception:
        logging.getLogger(__name__).warning("Recent companionship context unavailable", exc_info=True)
        return
    if text:
        history.extend([{"role": "user", "content": text},
                        {"role": "assistant", "content": f"（{ai_name}记得与{user_name}这段已经结束的相处。）"}])
