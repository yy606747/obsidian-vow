"""显式补做与状态查询，不在启用时自动回填历史图片。"""

import aiosqlite
from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from database import get_db
from app.image_memory.service import freeze_slot, schedule_message

router = APIRouter()


class BackfillRequest(BaseModel):
    message_ids: list[str] = Field(min_length=1, max_length=100)


@router.post("/api/image-memory/backfill")
async def backfill(body: BackfillRequest):
    if not freeze_slot():
        raise HTTPException(409, "请先启用图片记忆，并配置、启用兼容识图接口的视觉摘要槽位")
    scheduled = []
    async with get_db() as db:
        for message_id in dict.fromkeys(body.message_ids):
            row = await (await db.execute("SELECT 1 FROM messages WHERE id=? AND role='user'", (message_id,))).fetchone()
            if row:
                schedule_message(message_id)
                scheduled.append(message_id)
    return {"scheduled_message_ids": scheduled}


@router.get("/api/image-memory")
async def list_observations(limit: int = Query(50, ge=1, le=200)):
    async with get_db() as db:
        db.row_factory = aiosqlite.Row
        rows = await (await db.execute(
            "SELECT id,message_id,attachment_url,description_version,source_time,status,"
            "endpoint_id,model,attempts,error_type,updated_at FROM image_observations ORDER BY updated_at DESC LIMIT ?",
            (limit,),
        )).fetchall()
    return {"items": [dict(row) for row in rows]}
