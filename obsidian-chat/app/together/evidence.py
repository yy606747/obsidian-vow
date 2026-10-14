"""Durable, labeled excerpts for optional cognition requests after cleanup."""

import json


def render_source(job, *, user_name, ai_name):
    evidence = job["evidence"]
    proposal = job["payload"]["value"]
    lines = [f"【{ai_name}与{user_name}本场相处的保存依据】",
             f"来源会话：{job['session_id']}；来源轮次：{job['turn_id']}。",
             f"【{ai_name}提出这项认识时给出的依据】\n{proposal['source']}"]
    for message in evidence.get("owner_messages", []):
        lines.append(f"【{user_name}实际发出的文字，时间 {message['created_at']}】\n{message['content']}")
    if not evidence.get("owner_messages"):
        lines.append(f"本轮没有保存到{user_name}的新发言。")
    if evidence.get("expression"):
        lines.append(f"【{ai_name}当时的表达与观察】\n{evidence['expression']}")
    for image in evidence.get("images", []):
        source = {"screen": "手机屏幕", "camera": "手机后置摄像头"}.get(image["source"], image["source"])
        lines.append(f"【当时附图的来源记录】{source}，拍摄时间 {image['captured_at']}；原图未长期保留。")
    lines.append(f"画面或帖子中的文字不等于{user_name}的原话；{ai_name}的观察与推断也不是{user_name}的自述。")
    return "\n\n".join(lines)


async def reload_source(db_factory, value):
    if not value.together_source_id:
        raise ValueError("together_source_missing")
    async with db_factory() as db:
        import aiosqlite
        db.row_factory = aiosqlite.Row
        raw = await (await db.execute("""SELECT w.* FROM together_writebacks w JOIN together_sessions s ON s.id=w.session_id
            WHERE w.id=? AND w.kind='understanding' AND s.conv_id=?""", (value.together_source_id, value.conv_id))).fetchone()
    if not raw:
        raise ValueError("together_source_missing")
    job = dict(raw)
    job["payload"], job["evidence"] = json.loads(job["payload"]), json.loads(job["evidence"])
    if (job["payload"]["value"]["statement"].strip() != value.statement
            or not job["turn_id"] or job["evidence"].get("turn_id") != job["turn_id"]
            or job["evidence"].get("session_id") != job["session_id"]):
        raise ValueError("together_source_mismatch")
    names = job["payload"]["identity"]
    source = render_source(job, user_name=names["user_name"], ai_name=names["ai_name"])
    owners = job["evidence"].get("owner_messages", [])
    owner = owners[-1] if owners else None
    if owner and (not isinstance(owner.get("id"), str) or not owner["id"].strip()
                  or not isinstance(owner.get("content"), str)):
        raise ValueError("together_owner_source_missing")
    return source, owner["content"] if owner else None, owner["id"] if owner else None
