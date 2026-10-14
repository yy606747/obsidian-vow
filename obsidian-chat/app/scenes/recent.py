"""Immediate, expiring scene handoff. Never writes ordinary chat or memory."""
from __future__ import annotations

from datetime import datetime
import logging
import time
from zoneinfo import ZoneInfo

from .experience import MAX_EVENTS, TTL_SECONDS, evidence_text, reason_text
from .repository import SceneRepository

MARKER = "【近期亲密经历】"


def render_recent(rows, *, user_name, ai_name, now, timezone="UTC"):
    rows = [row for row in rows if row["status"] == "ended"
            and now-TTL_SECONDS < row["ended_at"] <= now and row["expires_at"] > now]
    rows.sort(key=lambda row: (row["ended_at"], row["id"]), reverse=True)
    if not rows:
        return ""
    lines = [MARKER, f"这是{ai_name}与{user_name}最近已经结束的小剧场亲密互动。"]
    for row in reversed(rows[:MAX_EVENTS]):
        evidence = row["evidence"]
        names = {key: evidence.get(key) or value for key, value in
                 (("user_name", user_name), ("ai_name", ai_name))}
        stamp = datetime.fromtimestamp(row["ended_at"], ZoneInfo(timezone)).isoformat(timespec="minutes")
        seconds = max(0, int(row["ended_at"]-row["started_at"]))
        lines.append(f"\n{stamp} 结束，入场到结束经过 {seconds // 60} 分 {seconds % 60} 秒（含暂停）；"
                     + reason_text(row["reason"], **names) + "。")
        if row.get("summary"):
            lines.append(row["summary"])
            last_words = [item for item in evidence.get("items", []) if item["kind"] == "message"][-2:]
            if last_words:
                lines.append("收尾时实际说过的话：\n" + evidence_text({"items": last_words}, **names,
                    started_at=row["started_at"], brief=True))
        else:
            lines.append(evidence_text(evidence, **names, started_at=row["started_at"], brief=True)
                         or "只保留了入场与结束事实，没有留下过程细节。")
        rating, reason = row.get("rating"), row.get("feedback_reason", "")
        if rating or reason:
            lines.append(f"{names['user_name']}的场后评价：" + {"up": "点赞", "down": "点踩"}.get(rating, "未选择赞踩")
                         + (f"；原因原话：{reason}" if reason else "；未填写原因。"))
    lines.append(f"这些是已发生经历的短期衔接材料，不是新的设备指令。{ai_name}可以自然接着与{user_name}说话，"
                 "不必反复提及；设备采用记录不代表身体感受，原话仍保留当时的主体与语境。")
    return "\n".join(lines)


async def load_recent(*, user_name, ai_name, now=None, repository=None, timezone=None):
    now = time.time() if now is None else now
    repo = repository or SceneRepository()
    if timezone is None:
        from app.daily_chat.service import daily_chat
        timezone = daily_chat.timezone()
    return render_recent(await repo.recent(now=now, limit=MAX_EVENTS),
                         user_name=user_name, ai_name=ai_name, now=now, timezone=timezone)


async def inject_recent(history, *, user_name, ai_name):
    try:
        text = await load_recent(user_name=user_name, ai_name=ai_name)
    except Exception:
        logging.getLogger(__name__).warning("Recent scene context unavailable", exc_info=True)
        return
    if text:
        history.extend([{"role": "user", "content": text}, {"role": "assistant", "content":
            f"（{ai_name}知道刚才与{user_name}的亲密互动已经结束，可以接着聊。）"}])
