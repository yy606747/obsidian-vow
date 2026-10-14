"""
心语 API：列表查询 + 删除
"""

import json
from pathlib import Path
from typing import Literal
from fastapi import APIRouter, Query, HTTPException
from fastapi.responses import FileResponse
from database import get_db
from app.heart_works.repository import Repository

router = APIRouter()


@router.get("/api/heart-whispers")
async def list_heart_whispers(
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    include_works: bool = False,
    kind: Literal['work', 'legacy'] | None = None,
):
    """按类型独立分页；未指定类型时保留原有列表行为。"""
    offset = (page - 1) * page_size
    if kind == 'work':
        repo = Repository(get_db)
        count = await repo.read("SELECT COUNT(*) AS total FROM heart_works WHERE status='published'")
        rows = await repo.read('''SELECT id,published_at AS created_at,cover_json
            FROM heart_works WHERE status='published'
            ORDER BY published_at DESC,id DESC LIMIT ? OFFSET ?''', (page_size, offset), many=True)
        items = [{'id': row['id'], 'kind': 'work', 'created_at': row['created_at'], 'cover': row['cover']}
                 for row in rows]
        return {'items': items, 'total': count['total'], 'page': page, 'page_size': page_size}
    if include_works and kind is None:
        repo = Repository(get_db)
        count = await repo.read("SELECT (SELECT COUNT(*) FROM heart_whispers) + (SELECT COUNT(*) FROM heart_works WHERE status='published') AS total")
        rows = await repo.read('''SELECT id,conv_id,msg_id,content,created_at,'legacy' AS kind,NULL AS cover
            FROM heart_whispers UNION ALL
            SELECT id,conv_id,NULL,NULL,published_at,'work',cover_json FROM heart_works WHERE status='published'
            ORDER BY created_at DESC,id DESC LIMIT ? OFFSET ?''', (page_size, offset), many=True)
        items = [({'id': r['id'], 'kind': 'work', 'created_at': r['created_at'], 'cover': json.loads(r['cover'])}
                  if r['kind'] == 'work' else {k: v for k, v in r.items() if k != 'cover'}) for r in rows]
        return {'items': items, 'total': count['total'], 'page': page, 'page_size': page_size}
    async with get_db() as db:
        db.row_factory = __import__('aiosqlite').Row
        cur = await db.execute("SELECT COUNT(*) as cnt FROM heart_whispers")
        total = (await cur.fetchone())["cnt"]
        cur = await db.execute(
            "SELECT id, conv_id, msg_id, content, created_at FROM heart_whispers ORDER BY created_at DESC, id DESC LIMIT ? OFFSET ?",
            (page_size, offset)
        )
        rows = await cur.fetchall()
    items = [dict(r) for r in rows]
    if kind == 'legacy':
        for item in items:
            item['kind'] = 'legacy'
    return {"items": items, "total": total, "page": page, "page_size": page_size}


@router.delete("/api/heart-whispers/{hw_id}")
async def delete_heart_whisper(hw_id: str):
    """删除单条心语"""
    async with get_db() as db:
        await db.execute("DELETE FROM heart_whispers WHERE id=?", (hw_id,))
        await db.commit()
    return {"ok": True}


@router.get('/api/heart-whispers/works-status')
async def heart_work_status():
    return await Repository(get_db).read("SELECT COALESCE(MAX(published_at),0) AS latest FROM heart_works WHERE status='published'")


@router.get('/api/heart-whispers/works/{work_id}')
async def read_heart_work(work_id: str):
    row = await Repository(get_db).get(work_id)
    if not row or row['status'] != 'published':
        raise HTTPException(404, '心语不存在')
    return {'id': row['id'], 'cover': row['cover'], 'body': row['body'], 'published_at': row['published_at']}


@router.get('/api/heart-whispers/works/{work_id}/assets/{asset_id}')
async def heart_work_asset(work_id: str, asset_id: str):
    from app.heart_works.tools import material_root
    repo = Repository(get_db)
    work = await repo.get(work_id)
    if not work or work['status'] != 'published' or not any(
        block.get('asset_id') == asset_id for block in [*work['cover'], *work['body']]):
        raise HTTPException(404)
    asset = await repo.read('SELECT * FROM heart_work_assets WHERE id=? AND work_id=?', (asset_id, work_id))
    if not asset:
        raise HTTPException(404)
    path = Path(asset['path']).resolve()
    if not path.is_relative_to(material_root().resolve()) or not path.is_file():
        raise HTTPException(404)
    return FileResponse(path, media_type='image/png')


@router.delete('/api/heart-whispers/works/{work_id}')
async def delete_heart_work(work_id: str):
    from app.heart_works.service import heart_work_service
    repo = Repository(get_db)
    await repo.delete(work_id)
    heart_work_service.invalidate()
    return {'ok': True}


@router.post('/api/heart-whispers/works/{work_id}/resume')
async def resume_heart_work(work_id: str):
    from app.heart_works.service import heart_work_service
    row = await heart_work_service.resume(work_id)
    if not row:
        raise HTTPException(404, '心语不存在')
    return {'ok': True, 'id': row['id'], 'status': row['status']}
