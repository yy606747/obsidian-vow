"""图片描述旁表及来源校验，不改写原始聊天。"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import aiosqlite

from config import SETTINGS, UPLOADS_DIR
from database import get_db
from app.chat.audio_input import attachment_mime_type, attachment_url

VERSION = "image_observation.v1"
MAX_IMAGE_BYTES = 20 * 1024 * 1024


def enabled() -> bool:
    return SETTINGS.get("image_memory_enabled") is True


def images(attachments) -> dict[str, object]:
    if isinstance(attachments, str):
        try:
            attachments = json.loads(attachments)
        except (ValueError, TypeError):
            return {}
    if not isinstance(attachments, (list, tuple)):
        return {}
    return {
        attachment_url(item): item for item in (attachments or [])
        if isinstance(item, (str, dict))
        and attachment_url(item).startswith("/uploads/")
        and attachment_url(item) == "/uploads/" + Path(attachment_url(item)).name
        and attachment_mime_type(item).startswith("image/")
    }


def file_hash(url: str) -> str | None:
    root = Path(UPLOADS_DIR).resolve()
    path = (root / Path(url).name).resolve()
    try:
        if path.parent != root or not path.is_file() or not 0 < path.stat().st_size <= MAX_IMAGE_BYTES:
            return None
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return None


async def init_tables(db) -> None:
    await db.execute("""CREATE TABLE IF NOT EXISTS image_observations (
        id TEXT PRIMARY KEY, message_id TEXT NOT NULL, conv_id TEXT NOT NULL,
        attachment_url TEXT NOT NULL, file_hash TEXT NOT NULL,
        description_version TEXT NOT NULL, source_time REAL NOT NULL,
        status TEXT NOT NULL DEFAULT 'pending', description TEXT NOT NULL DEFAULT '',
        embedding BLOB, embedding_signature TEXT NOT NULL DEFAULT '',
        endpoint_id TEXT NOT NULL DEFAULT '', model TEXT NOT NULL DEFAULT '',
        attempts INTEGER NOT NULL DEFAULT 0, error_type TEXT NOT NULL DEFAULT '',
        updated_at REAL NOT NULL,
        UNIQUE(message_id, attachment_url, file_hash, description_version)
    )""")
    await db.execute("CREATE INDEX IF NOT EXISTS idx_image_observations_source ON image_observations(conv_id, message_id)")
    await db.execute("CREATE INDEX IF NOT EXISTS idx_image_observations_status ON image_observations(status, source_time)")
    # 仅恢复状态，不自动重试、调用模型或回填历史。
    await db.execute("UPDATE image_observations SET status='deferred',error_type='process_interrupted' WHERE status='running'")


async def source(db, message_id: str, url: str) -> dict | None:
    cur = await db.execute(
        "SELECT id, conv_id, role, attachments, created_at FROM messages WHERE id=?", (message_id,),
    )
    row = await cur.fetchone()
    if not row or row[2] != "user" or url not in images(row[3]):
        return None
    return {"message_id": row[0], "conv_id": row[1], "source_time": row[4],
            "mime_type": attachment_mime_type(images(row[3])[url])}


async def reconcile_in_tx(db, conv_id: str) -> None:
    # 关闭功能后仍清理来源失效；兼容旧测试或尚未迁移的数据库。
    if not await (await db.execute("SELECT 1 FROM sqlite_master WHERE name='image_observations'")).fetchone():
        return
    rows = await (await db.execute(
        "SELECT id, message_id, attachment_url FROM image_observations WHERE conv_id=? AND status!='retired'", (conv_id,),
    )).fetchall()
    for row in rows:
        if not await source(db, row[1], row[2]):
            await db.execute(
                "UPDATE image_observations SET status='retired', embedding=NULL WHERE id=?", (row[0],),
            )


async def recall_rows(db, conv_ids: list[str] | None = None) -> list[dict]:
    if not enabled():
        return []
    from app.memory_v2.embedding import embedding_signature

    extra = ""
    params = []
    if conv_ids is not None:
        if not conv_ids:
            return []
        extra = " AND o.conv_id IN (" + ",".join("?" for _ in conv_ids) + ")"
        params = conv_ids
    cur = await db.execute(
        "SELECT o.*, m.attachments AS source_attachments FROM image_observations o "
        "JOIN messages m ON m.id=o.message_id AND m.conv_id=o.conv_id AND m.role='user' "
        "WHERE o.status='ready'" + extra, params,
    )
    result = []
    signature = embedding_signature()
    for raw in await cur.fetchall():
        row = dict(raw)
        if row["attachment_url"] not in images(row["source_attachments"]):
            continue
        result.append({
            "id": row["id"], "conv_id": row["conv_id"],
            "source_type": "image", "attachment_url": row["attachment_url"],
            "message_ids_json": json.dumps([row["message_id"]]),
            "content": row["description"], "source_hash": row["file_hash"],
            "created_at": row["source_time"], "updated_at": row["source_time"],
            "embedding": row["embedding"] if row["embedding_signature"] == signature else None,
            "keywords_json": "[]", "metadata_json": json.dumps({
                "source_start_ts": row["source_time"], "source_end_ts": row["source_time"],
                "source_message_ids": [row["message_id"]],
            }),
        })
    return result


async def valid_items(items: list[dict]) -> list[dict]:
    """注入前校验少量图片候选，包括已经冻结的 pending 选择。"""
    if not any(item.get("source_type") == "image" for item in items):
        return items
    valid = []
    async with get_db() as db:
        for item in items:
            if item.get("source_type") != "image":
                valid.append(item)
                continue
            if not enabled():
                continue
            row = await (await db.execute(
                "SELECT message_id, attachment_url, file_hash FROM image_observations WHERE id=? AND status='ready'",
                (item.get("id") or item.get("candidate_id"),),
            )).fetchone()
            if row and await source(db, row[0], row[1]) and file_hash(row[1]) == row[2]:
                valid.append(item)
    return valid
