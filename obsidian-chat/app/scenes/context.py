from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class SceneIdentity:
    user_name: str
    ai_name: str
    ai_persona: str
    working_model: str


async def load_identity() -> SceneIdentity:
    from config import load_ai_behavior, load_worldbook, load_working_model
    from app.chat.worldbook import resolve_worldbook_names

    worldbook = load_worldbook()
    user_name, ai_name = resolve_worldbook_names(worldbook)
    if load_ai_behavior().get("working_model_v2_injection_enabled", False):
        from database import get_db
        from app.working_model.repository import get_head
        async with get_db() as db:
            head = await get_head(db)
        if head is None:
            raise ValueError("scene_working_model_missing")
        working_model = str(head.get("content") or "")
    else:
        working_model = str(load_working_model().get("content") or "")[:1200]
    return SceneIdentity(user_name, ai_name, str(worldbook.get("ai_persona") or ""), working_model)


async def conversation_model(conv_id: str) -> tuple[str, str]:
    from database import get_db
    from app.daily_chat.service import resolve_conversation_id
    conv_id = resolve_conversation_id(conv_id)
    async with get_db() as db:
        row = await (await db.execute("SELECT model FROM conversations WHERE id=?", (conv_id,))).fetchone()
    if not row:
        raise ValueError("scene_conversation_missing")
    return conv_id, str(row[0])
