"""
心语 API：列表查询 + 删除
"""

import time
from fastapi import APIRouter, Query
from database import get_db

router = APIRouter()


@router.get("/api/heart-whispers")
async def list_heart_whispers(page: int = Query(1, ge=1), page_size: int = Query(20, ge=1, le=100)):
    """分页获取心语列表（按时间倒序）"""
    offset = (page - 1) * page_size
    async with get_db() as db:
        db.row_factory = __import__('aiosqlite').Row
        cur = await db.execute("SELECT COUNT(*) as cnt FROM heart_whispers")
        total = (await cur.fetchone())["cnt"]
        cur = await db.execute(
            "SELECT id, conv_id, msg_id, content, created_at FROM heart_whispers ORDER BY created_at DESC LIMIT ? OFFSET ?",
            (page_size, offset)
        )
        rows = await cur.fetchall()
    items = [dict(r) for r in rows]
    return {"items": items, "total": total, "page": page, "page_size": page_size}


@router.delete("/api/heart-whispers/{hw_id}")
async def delete_heart_whisper(hw_id: str):
    """删除单条心语"""
    async with get_db() as db:
        await db.execute("DELETE FROM heart_whispers WHERE id=?", (hw_id,))
        await db.commit()
    return {"ok": True}
