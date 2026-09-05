"""Conversation and message CRUD routes."""

from __future__ import annotations

import json
import time
from typing import Optional

from fastapi import APIRouter, Query

from config import DEFAULT_MODEL
from database import get_db
from routes.files import export_conversation
from ws import manager

from app.vows.service import vow_service
from app.memory_v2.service import memory_service
from app.memory_v3.repository import PendingRecallRepository
from app.web_search.repository import WebSearchRepository

from .models import ConvCreate, ConvUpdate, MsgUpdate

router = APIRouter()

@router.get("/api/conversations")
async def list_conversations():
    async with get_db() as db:
        db.row_factory = __import__('aiosqlite').Row
        cur = await db.execute(
            "SELECT c.*, (SELECT COUNT(*) FROM messages m WHERE m.conv_id = c.id AND m.role IN ('user','assistant')) AS message_count "
            "FROM conversations c ORDER BY c.updated_at DESC"
        )
        rows = await cur.fetchall()
        return [dict(r) for r in rows]

@router.post("/api/conversations")
async def create_conversation(body: ConvCreate):
    now = time.time()
    conv_id = f"conv_{int(now*1000)}"
    async with get_db() as db:
        await db.execute(
            "INSERT INTO conversations (id, title, model, created_at, updated_at) VALUES (?,?,?,?,?)",
            (conv_id, body.title, body.model, now, now)
        )
        await db.commit()
    conv = {"id": conv_id, "title": body.title, "model": body.model, "created_at": now, "updated_at": now}
    await manager.broadcast({"type": "conv_created", "data": conv})
    await export_conversation(conv_id)
    return conv

@router.put("/api/conversations/{conv_id}")
async def update_conversation(conv_id: str, body: ConvUpdate):
    async with get_db() as db:
        if body.title is not None:
            await db.execute("UPDATE conversations SET title=?, updated_at=? WHERE id=?",
                             (body.title, time.time(), conv_id))
        if body.model is not None:
            await db.execute("UPDATE conversations SET model=?, updated_at=? WHERE id=?",
                             (body.model, time.time(), conv_id))
        await db.commit()
    await manager.broadcast({"type": "conv_updated", "data": {"id": conv_id, **(body.dict(exclude_none=True))}})
    await export_conversation(conv_id)
    return {"ok": True}

@router.delete("/api/conversations/{conv_id}")
async def delete_conversation(conv_id: str):
    from routes.files import delete_exported_file
    async with get_db() as db:
        await db.execute("PRAGMA foreign_keys = ON")
        await db.execute("BEGIN IMMEDIATE")
        try:
            # 批量撤约（誓约设计 §4.5）：来源消息将随会话级联删除，
            # 撤约必须先于删除（子查询依赖 messages 行）且同一事务
            revoked = await vow_service.revoke_for_origin_conversation_in_tx(db, conv_id=conv_id)
            await PendingRecallRepository.delete_for_conversation_in_tx(
                db,
                conv_id=conv_id,
            )
            await WebSearchRepository.delete_for_conversation_in_tx(
                db,
                conv_id=conv_id,
            )
            from app.self_wake.repository import invalidate_origin_in_tx

            await invalidate_origin_in_tx(
                db,
                origin="relationship",
                origin_ref=conv_id,
            )
            await db.execute("DELETE FROM conversations WHERE id=?", (conv_id,))
            await memory_service.reconcile_conversation_chunks_in_tx(db, conv_id)
            await db.commit()
            memory_service.invalidate_conversation_cache(conv_id)
        except BaseException:
            await db.rollback()
            return {"ok": False, "error": "delete_failed"}
    await manager.broadcast({"type": "conv_deleted", "data": {"id": conv_id}})
    if revoked:
        await manager.broadcast({"type": "vow_changed", "data": {"action": "origin_deleted"}})
    delete_exported_file(conv_id)
    return {"ok": True}

# ── 消息 CRUD ─────────────────────────────────────
@router.get("/api/conversations/{conv_id}/messages")
async def list_messages(conv_id: str, limit: int = Query(50, ge=1, le=500), before: Optional[float] = Query(None)):
    """获取消息，支持分页。limit=条数，before=时间戳(加载更早的消息)"""
    async with get_db() as db:
        db.row_factory = __import__('aiosqlite').Row
        if before:
            cur = await db.execute(
                "SELECT * FROM messages WHERE conv_id=? AND created_at<? ORDER BY created_at DESC LIMIT ?",
                (conv_id, before, limit)
            )
        else:
            cur = await db.execute(
                "SELECT * FROM messages WHERE conv_id=? ORDER BY created_at DESC LIMIT ?",
                (conv_id, limit)
            )
        rows = await cur.fetchall()
        rows = list(reversed(rows))  # 按时间正序返回
        result = []
        for r in rows:
            d = dict(r)
            d["attachments"] = json.loads(d.get("attachments") or "[]") if d.get("attachments") else []
            result.append(d)
        return result

@router.get("/api/messages/{msg_id}")
async def get_message(msg_id: str):
    """拉单条完整消息；流式断线后前端用它覆盖半截内容。"""
    async with get_db() as db:
        db.row_factory = __import__('aiosqlite').Row
        cur = await db.execute("SELECT * FROM messages WHERE id=?", (msg_id,))
        msg = await cur.fetchone()
    if not msg:
        return {"error": "not_found"}
    d = dict(msg)
    if d.get("attachments"):
        import json as _json
        try:
            d["attachments"] = _json.loads(d["attachments"])
        except Exception:
            d["attachments"] = []
    return d

@router.delete("/api/messages/{msg_id}")
async def delete_message(msg_id: str):
    # 原话撤回则约随之退役（誓约设计 §4.5）：撤约与删消息同一事务，
    # 任一步失败整体回滚，消息与 vow 都保留。
    conv_id = None
    async with get_db() as db:
        db.row_factory = __import__('aiosqlite').Row
        await db.execute("BEGIN IMMEDIATE")
        try:
            cur = await db.execute("SELECT * FROM messages WHERE id=?", (msg_id,))
            msg = await cur.fetchone()
            if msg:
                conv_id = msg["conv_id"]
                revoked = await vow_service.revoke_for_origin_message_in_tx(
                    db, message_id=msg_id, close_action="origin_deleted"
                )
                await PendingRecallRepository.cancel_for_message_in_tx(
                    db,
                    message_id=msg_id,
                    now=time.time(),
                )
                await WebSearchRepository.cancel_for_message_in_tx(
                    db,
                    message_id=msg_id,
                )
                await db.execute("DELETE FROM messages WHERE id=?", (msg_id,))
                await memory_service.reconcile_conversation_chunks_in_tx(db, conv_id)
            await db.commit()
            if conv_id:
                memory_service.invalidate_conversation_cache(conv_id)
        except BaseException:
            await db.rollback()
            return {"ok": False, "error": "delete_failed"}
    if conv_id:
        await manager.broadcast({"type": "msg_deleted", "data": {"id": msg_id, "conv_id": conv_id}})
        if revoked is not None:
            await manager.broadcast({"type": "vow_changed", "data": {"action": "origin_deleted"}})
        await export_conversation(conv_id)
    return {"ok": True}

@router.put("/api/messages/{msg_id}")
async def update_message(msg_id: str, body: MsgUpdate):
    conv_id = None
    async with get_db() as db:
        db.row_factory = __import__('aiosqlite').Row
        await db.execute("BEGIN IMMEDIATE")
        cur = await db.execute("SELECT role, conv_id FROM messages WHERE id=?", (msg_id,))
        row = await cur.fetchone()
        if row is None:
            await db.rollback()
            return {"ok": False, "error": "not_found"}
        if row["role"] != "user":
            # 只允许编辑 user 消息（誓约设计 §4.5）：assistant/system 一律拒绝——
            # 否则可删掉正文里的立约确认语而 vow 仍 active，破坏可见性不变量。
            await db.rollback()
            return {"ok": False, "error": "only_user_messages_editable"}
        await PendingRecallRepository.cancel_for_message_in_tx(
            db,
            message_id=msg_id,
            now=time.time(),
        )
        await WebSearchRepository.cancel_for_message_in_tx(
            db,
            message_id=msg_id,
        )
        await db.execute("UPDATE messages SET content=? WHERE id=?", (body.content, msg_id))
        await memory_service.reconcile_conversation_chunks_in_tx(db, row["conv_id"])
        await db.commit()
        memory_service.invalidate_conversation_cache(row["conv_id"])
        cur = await db.execute("SELECT * FROM messages WHERE id=?", (msg_id,))
        msg = await cur.fetchone()
        if msg:
            d = dict(msg)
            try: d["attachments"] = json.loads(d.get("attachments") or "[]") if d.get("attachments") else []
            except: d["attachments"] = []
            conv_id = d["conv_id"]
            await manager.broadcast({"type": "msg_updated", "data": d})
    if conv_id:
        await export_conversation(conv_id)
    return {"ok": True}
