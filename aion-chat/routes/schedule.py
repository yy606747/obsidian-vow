"""
日程路由：列表 / 手动添加 / 删除
"""

import time
from fastapi import APIRouter, Query
from pydantic import BaseModel
from typing import Optional

import aiosqlite
from database import get_db
from ws import manager

router = APIRouter()


@router.get("/api/schedule/missed-recent")
async def missed_recent():
    """前端在初始化时拉一次；若服务器启动时检测到错过的闹铃，会返回一次汇总后清空。"""
    from schedule import get_last_missed_summary
    return get_last_missed_summary() or {"count": 0, "items": []}


class ScheduleCreate(BaseModel):
    type: str = "alarm"          # alarm / reminder
    trigger_at: str              # ISO: 2026-03-25T10:00
    content: str


@router.get("/api/schedules")
async def list_schedules(status: Optional[str] = Query(None)):
    async with get_db() as db:
        db.row_factory = aiosqlite.Row
        if status:
            cur = await db.execute(
                "SELECT * FROM schedules WHERE status=? ORDER BY trigger_at", (status,)
            )
        else:
            # 默认不返回已取消的条目
            cur = await db.execute(
                "SELECT * FROM schedules WHERE status != 'cancelled' ORDER BY trigger_at"
            )
        return [dict(r) for r in await cur.fetchall()]


@router.post("/api/schedules")
async def create_schedule(body: ScheduleCreate):
    sid = f"sch_{int(time.time()*1000)}"
    now = time.time()
    trigger_at = body.trigger_at.replace("T", " ")
    async with get_db() as db:
        await db.execute(
            "INSERT INTO schedules (id, type, trigger_at, content, created_at, status) VALUES (?,?,?,?,?,?)",
            (sid, body.type, trigger_at, body.content, now, "active"),
        )
        await db.commit()
    item = {"id": sid, "type": body.type, "trigger_at": trigger_at,
            "content": body.content, "created_at": now, "status": "active"}
    await manager.broadcast({"type": "schedule_changed"})
    if body.type == "alarm":
        await manager.broadcast({
            "type": "android_alarm_set",
            "data": {"id": sid, "trigger_at": trigger_at, "content": body.content},
        })
    return item


@router.delete("/api/schedules/{schedule_id}")
async def delete_schedule(schedule_id: str):
    async with get_db() as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT id, type, trigger_at, content, status FROM schedules WHERE id=?",
            (schedule_id,),
        )
        row = await cur.fetchone()
        await db.execute("UPDATE schedules SET status='cancelled' WHERE id=?", (schedule_id,))
        await db.commit()
    await manager.broadcast({"type": "schedule_changed"})
    item = dict(row) if row else None
    if item and item["type"] == "alarm" and item["status"] == "active":
        await manager.broadcast({
            "type": "android_alarm_cancel",
            "data": {
                "id": item["id"],
                "trigger_at": item["trigger_at"],
                "content": item["content"],
            },
        })
    return {"ok": True}
