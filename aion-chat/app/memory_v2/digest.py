"""Memory V2 digest: extract multiple short notes into memory_items."""

from __future__ import annotations

from datetime import datetime
import hashlib
import json
import time

import aiosqlite

from config import load_digest_anchor, load_worldbook, save_digest_anchor
from database import get_db
from app.chat.worldbook import resolve_worldbook_names
from memory import _call_flash_lite
from ws import manager

from . import embedding
from .chunks import extract_keywords
from .migrations import detect_emotion, infer_kind, infer_namespace, infer_secondary_namespaces, normalize_keywords
from .v2_repository import MemoryRepository, new_id


def local_instant_digest(recent_messages: list[dict]) -> dict:
    """Cheap online digest for recall query construction; no model call."""
    messages = [
        message for message in recent_messages
        if message.get("role") in {"user", "assistant"} and str(message.get("content") or "").strip()
    ]
    last_user = next(
        (
            str(message.get("content") or "").strip()
            for message in reversed(messages)
            if message.get("role") == "user"
        ),
        "",
    )
    query_text = "\n".join(
        str(message.get("content") or "").strip()[:240]
        for message in messages[-6:]
        if str(message.get("content") or "").strip()
    )
    keywords = extract_keywords(f"{last_user}\n{query_text}", limit=8)
    topic = last_user[:160] if last_user else query_text[:160]
    return {
        "keywords": keywords,
        "status": "",
        "topic": topic,
        "source": "local",
    }


HANDOFF_NOTE_MAX_TURNS = 40
HANDOFF_NOTE_TIMEOUT = 12.0  # 短超时：别让小模型卡住新会话第一条回复


def _handoff_note_prompt(messages_text: str, *, user_name: str, ai_name: str) -> str:
    return (
        "你在帮一个长期陪伴 AI 写一张「昨日续点」便签，给它下一次开新对话时心里有数。\n"
        "请读完整段对话，用一两句自然中文概括：主要聊了什么、停在哪里、用户当时的情绪/状态、有没有没说完或在等回应的事。\n\n"
        "要求：\n"
        "1. 只写一两句，抓整段重点，不要被结尾的道晚安/客套带偏。\n"
        f"2. 指代双方用“{user_name}”和“{ai_name}”。\n"
        "3. 只概括「聊过什么」，不要把上一段里的设备连接、密语、AI Dom 控制、aftercare、位置等当前状态写成仍然有效。\n"
        "4. 如果整段没什么值得续接的（纯寒暄/无实质内容），note 输出空字符串。\n\n"
        "严格只输出 JSON 对象：{\"note\": \"...\"}\n\n"
        f"【对话记录】\n{messages_text}"
    )


def _handoff_window_sig(messages: list[dict]) -> str:
    """窗口内容签名：任何新增/编辑/删除都会变，用作缓存 key，避免删改后残留旧摘要。"""
    raw = "\n".join(f"{m['id']}:{m.get('content') or ''}" for m in messages)
    return hashlib.sha1(raw.encode("utf-8", errors="ignore")).hexdigest()


async def write_handoff_note(conv_id: str) -> str | None:
    """为「昨日续点」生成一两句话便签；按窗口内容签名缓存——内容不变就不重算
    （一段冻结的旧对话通常只算一次）。
    返回语义：非空字符串=便签；""=模型判断无需续点（合法，已缓存）；None=失败
    （无消息/调用失败/结构不对，不缓存，下次重试）。"""
    async with get_db() as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT handoff_note, handoff_note_sig FROM conversations WHERE id=?",
            (conv_id,),
        )
        conv = await cur.fetchone()
        cur = await db.execute(
            "SELECT id, role, content, created_at FROM messages "
            "WHERE conv_id=? AND role IN ('user','assistant') "
            "ORDER BY created_at DESC LIMIT ?",
            (conv_id, HANDOFF_NOTE_MAX_TURNS),
        )
        rows = await cur.fetchall()

    if not rows:
        return None
    messages = [dict(row) for row in reversed(rows)]
    sig = _handoff_window_sig(messages)
    if conv and conv["handoff_note_sig"] == sig:
        return conv["handoff_note"] or ""  # 缓存命中（"" 表示已缓存的“无需续点”）

    wb = load_worldbook()
    user_name, ai_name = resolve_worldbook_names(wb)
    prompt = _handoff_note_prompt(
        _messages_text(messages, user_name=user_name, ai_name=ai_name),
        user_name=user_name,
        ai_name=ai_name,
    )
    result = await _call_flash_lite(prompt, scope="memory:handoff_note", timeout=HANDOFF_NOTE_TIMEOUT)
    if not isinstance(result, dict) or not isinstance(result.get("note"), str):
        # 失败（超时/空响应/JSON 解析失败）或结构不对：不缓存，留给下一轮重试。
        # 只有显式返回 {"note": <字符串>} 才算成功（note="" 表示模型判断无需续点）。
        return None
    note = " ".join(result["note"].split())

    async with get_db() as db:
        await db.execute(
            "UPDATE conversations SET handoff_note=?, handoff_note_sig=? WHERE id=?",
            (note, sig, conv_id),
        )
        await db.commit()
    return note  # 可能是 ""（合法的“无需续点”）


def _split_into_groups(msgs: list[dict], group_size: int = 20) -> list[list[dict]]:
    if len(msgs) <= group_size:
        return [msgs] if msgs else []
    groups = [msgs[i: i + group_size] for i in range(0, len(msgs), group_size)]
    if len(groups) >= 2 and len(groups[-1]) < 5:
        groups[-2].extend(groups[-1])
        groups.pop()
    return groups


def _messages_text(group: list[dict], *, user_name: str, ai_name: str) -> str:
    group_start = datetime.fromtimestamp(group[0]["created_at"]).strftime("%Y年%m月%d日 %H:%M")
    group_end = datetime.fromtimestamp(group[-1]["created_at"]).strftime("%Y年%m月%d日 %H:%M")
    lines = [f"[对话时间范围: {group_start} ~ {group_end}]"]
    for message in group:
        role_name = user_name if message["role"] == "user" else ai_name
        ts = datetime.fromtimestamp(message["created_at"]).strftime("%m-%d %H:%M")
        content = " ".join(str(message["content"] or "").split())[:500]
        lines.append(f"[id={message['id']}] [{ts}] {role_name}: {content}")
    return "\n".join(lines)


def _digest_prompt(group: list[dict], *, user_name: str, ai_name: str) -> str:
    messages_text = _messages_text(group, user_name=user_name, ai_name=ai_name)
    return (
        "你是长期陪伴 AI 的记忆整理器。请从一段对话里提取 0-N 条短 note，写入长期记忆。\n"
        "目标是保留可检索的小细节，而不是压成一条泛泛摘要。\n\n"
        "要求：\n"
        "1. 每条 note 只写一个具体事实、偏好、计划、情绪背景或双方约定；可以同时包含事件、偏好、情绪和计划，不要强行分类。\n"
        "2. 不要输出“聊了音乐相关话题”这类低信息量内容；没有值得记的内容就输出空数组。\n"
        "3. content 用自然中文，尽量包含可搜索实体，例如歌名、店名、地点、菜名、对象名、时间背景。\n"
        f"4. 指代双方时使用“{user_name}”和“{ai_name}”，不要把人名写错。\n"
        "5. 每条 note 必须带 source_message_ids，使用输入里的 id。\n"
        "6. keywords 提取 2-6 个适合检索的关键词，过滤高频称呼和无意义词。\n"
        "7. importance 取 0.1-0.8；普通日常细节多为 0.3-0.5，长期偏好或未完成计划可以略高。\n\n"
        "严格只输出 JSON 对象，格式：\n"
        "{\n"
        '  "notes": [\n'
        '    {"content": "...", "source_message_ids": ["msg_..."], "keywords": ["..."], "importance": 0.4}\n'
        "  ]\n"
        "}\n\n"
        f"【对话记录】\n{messages_text}"
    )


def _valid_source_ids(note: dict, group_by_id: dict[str, dict]) -> list[str]:
    raw_ids = note.get("source_message_ids") or note.get("message_ids") or []
    if isinstance(raw_ids, str):
        raw_ids = [part.strip() for part in raw_ids.replace("、", ",").split(",") if part.strip()]
    if not isinstance(raw_ids, list):
        raw_ids = []
    valid = [str(item) for item in raw_ids if str(item) in group_by_id]
    return list(dict.fromkeys(valid))


def _note_keywords(note: dict) -> list[str]:
    keywords = normalize_keywords(note.get("keywords"))
    content_keywords = extract_keywords(note.get("content") or "", limit=8)
    merged = []
    seen = set()
    for keyword in [*keywords, *content_keywords]:
        key = str(keyword).strip().lower()
        if len(key) < 2 or key in seen:
            continue
        seen.add(key)
        merged.append(str(keyword).strip())
        if len(merged) >= 8:
            break
    return merged


def _importance(value) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        parsed = 0.4
    return max(0.0, min(parsed, 1.0))


async def _note_already_exists(content: str, source_start_ts: float, source_end_ts: float) -> bool:
    async with get_db() as db:
        cur = await db.execute(
            "SELECT 1 FROM memory_items "
            "WHERE content=? "
            "AND source_start_ts IS NOT NULL AND ABS(source_start_ts - ?) < 0.001 "
            "AND source_end_ts IS NOT NULL AND ABS(source_end_ts - ?) < 0.001 "
            "LIMIT 1",
            (content, float(source_start_ts), float(source_end_ts)),
        )
        row = await cur.fetchone()
    return row is not None


async def _insert_note(
    note: dict,
    group: list[dict],
    *,
    group_by_id: dict[str, dict],
    broadcast: bool = True,
) -> dict | None:
    content = " ".join(str(note.get("content") or "").split())
    if len(content) < 4:
        return None
    source_ids = _valid_source_ids(note, group_by_id)
    source_rows = [group_by_id[msg_id] for msg_id in source_ids] if source_ids else group
    source_start_ts = min(float(row["created_at"]) for row in source_rows)
    source_end_ts = max(float(row["created_at"]) for row in source_rows)
    if await _note_already_exists(content, source_start_ts, source_end_ts):
        return None
    source_convs = {row.get("conv_id") for row in source_rows if row.get("conv_id")}
    source_conv = next(iter(source_convs)) if len(source_convs) == 1 else None
    keywords = _note_keywords({**note, "content": content})
    namespace = infer_namespace(content, keywords)
    item = {
        "id": new_id("memv2_note"),
        "legacy_memory_id": None,
        "origin_type": "auto_digest",
        "kind": infer_kind("digest", False, content, keywords),
        "namespace": namespace,
        "content": content,
        "emotion": detect_emotion(content, keywords),
        "importance": _importance(note.get("importance")),
        "confidence": 0.72,
        "status": "active",
        "visibility": "prompt",
        "embedding": None,
        "keywords_json": json.dumps(keywords, ensure_ascii=False),
        "source_conv": source_conv,
        "source_start_ts": source_start_ts,
        "source_end_ts": source_end_ts,
        "created_at": time.time(),
        "updated_at": time.time(),
        "metadata_json": json.dumps({
            "source": "digest.multi_note",
            "source_message_ids": source_ids,
            "source_time_range": [source_start_ts, source_end_ts],
            "secondary_namespaces": infer_secondary_namespaces(content, keywords, namespace),
        }, ensure_ascii=False),
    }
    vec = await embedding.get_document_embedding(content)
    if vec:
        item["embedding"] = embedding.pack_embedding(vec)
    inserted = await MemoryRepository().insert_memory_item(item, ignore_existing=True)
    if not inserted:
        return None
    payload = {
        "id": item["id"],
        "content": item["content"],
        "type": "digest_note",
        "created_at": item["created_at"],
        "keywords": item["keywords_json"],
        "importance": item["importance"],
        "source_start_ts": source_start_ts,
        "source_end_ts": source_end_ts,
    }
    if broadcast:
        await manager.broadcast({"type": "memory_added", "data": payload})
    return payload


async def _fetch_new_messages(anchor_ts: float) -> list[dict]:
    async with get_db() as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT id, conv_id, role, content, created_at FROM messages "
            "WHERE role IN ('user','assistant') AND created_at > ? "
            "ORDER BY created_at ASC",
            (anchor_ts,),
        )
        rows = await cur.fetchall()
    return [dict(row) for row in rows]


async def manual_digest() -> dict:
    anchor_ts = load_digest_anchor()
    new_msgs = await _fetch_new_messages(anchor_ts)
    if not new_msgs:
        return {"ok": True, "message": "当前没有新增内容需要总结", "new_memories_count": 0, "processed_messages": 0}

    wb = load_worldbook()
    user_name, ai_name = resolve_worldbook_names(wb)
    groups = _split_into_groups(new_msgs, 20)
    total_new = 0
    failed_groups = 0

    for group in groups:
        prompt = _digest_prompt(group, user_name=user_name, ai_name=ai_name)
        result = await _call_flash_lite(prompt, scope="memory:multi_note_digest")
        if not isinstance(result, dict):
            failed_groups += 1
            continue
        notes = result.get("notes") or []
        if not isinstance(notes, list):
            notes = []
        group_by_id = {row["id"]: row for row in group}
        for raw_note in notes:
            if not isinstance(raw_note, dict):
                continue
            try:
                inserted = await _insert_note(raw_note, group, group_by_id=group_by_id)
            except Exception as exc:
                print(f"[MemoryV2Digest] note insert skipped: {exc}")
                inserted = None
            if inserted:
                total_new += 1
        save_digest_anchor(float(group[-1]["created_at"]))

    return {
        "ok": failed_groups == 0,
        "message": f"总结完成：处理了 {len(new_msgs)} 条消息（{len(groups)} 组），生成了 {total_new} 条新 note",
        "new_memories_count": total_new,
        "processed_messages": len(new_msgs),
        "failed_groups": failed_groups,
    }


__all__ = [
    "instant_digest",
    "local_instant_digest",
    "manual_digest",
    "write_handoff_note",
    "load_digest_anchor",
    "save_digest_anchor",
]


async def instant_digest(recent_messages: list[dict]) -> dict:
    from memory import instant_digest as _legacy_instant_digest

    return await _legacy_instant_digest(recent_messages)
