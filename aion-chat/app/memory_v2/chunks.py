"""Raw chat chunk indexing for Memory V2."""

from __future__ import annotations

import asyncio
from datetime import datetime
import hashlib
import json
import re
import time
from typing import Iterable

import aiosqlite

from config import load_worldbook
from database import get_db
from app.chat.worldbook import resolve_worldbook_names
from app.memory_v3.provenance import source_hash_for_messages

from . import embedding


MAX_MESSAGES_PER_CHUNK = 6
CHUNK_GAP_SECONDS = 15 * 60
MAX_MESSAGE_CHARS = 1200
MAX_CHUNK_CHARS = 2600
DEFAULT_EMBED_BATCH_SIZE = 8

_WORD_RE = re.compile(r"[A-Za-z0-9_+#./-]{2,}|[\u4e00-\u9fff]{2,12}")
_STOPWORDS = {
    "user",
    "assistant",
    "system",
    "ai",
    "好的",
    "收到",
    "知道",
    "可以",
    "用户",
    "助手",
    "我们",
    "你们",
    "他们",
}


def _json_dumps(value) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def extract_keywords(text: str, *, limit: int = 16) -> list[str]:
    """Cheap substring keywords for ranking only; model extraction stays in instant_digest."""
    seen: set[str] = set()
    keywords: list[str] = []
    for match in _WORD_RE.finditer(text or ""):
        token = match.group(0).strip().lower()
        if len(token) < 2 or token in _STOPWORDS or token in seen:
            continue
        seen.add(token)
        keywords.append(match.group(0).strip())
        if len(keywords) >= limit:
            break
    return keywords


def _message_id(row: dict) -> str:
    value = row.get("id")
    if value:
        return str(value)
    seed = f"{row.get('conv_id','')}:{row.get('role','')}:{row.get('created_at','')}:{row.get('content','')[:80]}"
    return hashlib.sha1(seed.encode("utf-8", errors="ignore")).hexdigest()[:16]


def _chunk_id(conv_id: str, message_ids: list[str], part_index: int) -> str:
    seed = f"{conv_id}\0{_json_dumps(message_ids)}\0{part_index}"
    return "mchunk_" + hashlib.sha1(seed.encode("utf-8", errors="ignore")).hexdigest()[:24]


def _role_name(role: str, *, user_name: str, ai_name: str) -> str:
    return user_name if role == "user" else ai_name


def _clip_message(content: str) -> str:
    text = " ".join(str(content or "").split())
    if len(text) <= MAX_MESSAGE_CHARS:
        return text
    return text[: MAX_MESSAGE_CHARS - 3] + "..."


def _format_line(row: dict, *, user_name: str, ai_name: str) -> str:
    try:
        ts = float(row.get("created_at") or 0)
        prefix = datetime.fromtimestamp(ts).strftime("%m-%d %H:%M")
    except Exception:
        prefix = "??-?? ??:??"
    name = _role_name(str(row.get("role") or ""), user_name=user_name, ai_name=ai_name)
    return f"[{prefix}] {name}: {_clip_message(str(row.get('content') or ''))}"


def _split_text(lines: list[str], *, max_chars: int = MAX_CHUNK_CHARS) -> list[str]:
    parts: list[str] = []
    current: list[str] = []
    current_len = 0
    for line in lines:
        if len(line) > max_chars:
            if current:
                parts.append("\n".join(current))
                current = []
                current_len = 0
            for start in range(0, len(line), max_chars):
                parts.append(line[start: start + max_chars])
            continue
        projected = current_len + len(line) + (1 if current else 0)
        if current and projected > max_chars:
            parts.append("\n".join(current))
            current = [line]
            current_len = len(line)
        else:
            current.append(line)
            current_len = projected
    if current:
        parts.append("\n".join(current))
    return [part for part in parts if part.strip()]


def _split_rows(
    rows: list[dict],
    *,
    user_name: str,
    ai_name: str,
    max_chars: int = MAX_CHUNK_CHARS,
) -> list[dict]:
    """Split formatted text while retaining the exact rows in each part.

    The previous implementation split only strings and then attached the full
    group's message IDs to every part.  Derived cards could therefore cite a
    message that was not present in their chunk text.
    """
    parts: list[dict] = []
    current_lines: list[str] = []
    current_rows: list[dict] = []
    current_len = 0

    def flush() -> None:
        nonlocal current_lines, current_rows, current_len
        if current_lines:
            parts.append({"content": "\n".join(current_lines), "rows": list(current_rows)})
        current_lines = []
        current_rows = []
        current_len = 0

    for row in rows:
        line = _format_line(row, user_name=user_name, ai_name=ai_name)
        if len(line) > max_chars:
            flush()
            for start in range(0, len(line), max_chars):
                content = line[start: start + max_chars]
                if content.strip():
                    parts.append({"content": content, "rows": [row]})
            continue
        projected = current_len + len(line) + (1 if current_lines else 0)
        if current_lines and projected > max_chars:
            flush()
        current_lines.append(line)
        current_rows.append(row)
        current_len = len(line) if len(current_lines) == 1 else projected
    flush()
    return parts


def _finalize_group(group: list[dict], *, user_name: str, ai_name: str) -> list[dict]:
    if not group:
        return []
    conv_id = str(group[0].get("conv_id") or "")
    parts = _split_rows(group, user_name=user_name, ai_name=ai_name)
    chunks = []
    for part_index, part in enumerate(parts):
        part_rows = list(part["rows"])
        message_ids = [_message_id(row) for row in part_rows]
        content = str(part["content"])
        source_start_ts = float(part_rows[0].get("created_at") or time.time())
        source_end_ts = float(part_rows[-1].get("created_at") or source_start_ts)
        metadata = {
            "source": "messages",
            "source_start_ts": source_start_ts,
            "source_end_ts": source_end_ts,
            "message_count": len(part_rows),
            "group_message_count": len(group),
            "part_index": part_index,
            "part_count": len(parts),
        }
        chunks.append({
            "id": _chunk_id(conv_id, message_ids, part_index),
            "conv_id": conv_id,
            "message_ids": message_ids,
            "message_ids_json": _json_dumps(message_ids),
            "content": content,
            "source_hash": source_hash_for_messages(part_rows),
            "created_at": source_start_ts,
            "updated_at": source_end_ts,
            "keywords": extract_keywords(content),
            "keywords_json": _json_dumps(extract_keywords(content)),
            "metadata_json": _json_dumps(metadata),
        })
    return chunks


def build_chunks_from_messages(messages: Iterable[dict]) -> list[dict]:
    """Build deterministic raw chunks from user/assistant messages."""
    wb = load_worldbook()
    user_name, ai_name = resolve_worldbook_names(wb)
    rows = [
        dict(message)
        for message in messages
        if message.get("role") in {"user", "assistant"} and str(message.get("content") or "").strip()
    ]
    rows.sort(key=lambda row: float(row.get("created_at") or 0))
    groups: list[list[dict]] = []
    current: list[dict] = []
    last_ts: float | None = None
    for row in rows:
        ts = float(row.get("created_at") or 0)
        should_break = False
        if current and last_ts is not None and ts - last_ts > CHUNK_GAP_SECONDS:
            should_break = True
        if current and len(current) >= MAX_MESSAGES_PER_CHUNK:
            should_break = True
        if should_break:
            groups.append(current)
            current = []
        current.append(row)
        last_ts = ts
    if current:
        groups.append(current)
    chunks: list[dict] = []
    for group in groups:
        chunks.extend(_finalize_group(group, user_name=user_name, ai_name=ai_name))
    return chunks


async def fetch_conversation_messages(conv_id: str, *, limit: int | None = None) -> list[dict]:
    sql = (
        "SELECT id, conv_id, role, content, created_at FROM messages "
        "WHERE conv_id=? AND role IN ('user','assistant') "
        "ORDER BY created_at ASC"
    )
    params: list = [conv_id]
    if limit is not None:
        sql += " LIMIT ?"
        params.append(int(limit))
    async with get_db() as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(sql, params)
        rows = await cur.fetchall()
    return [dict(row) for row in rows]


async def _fetch_conversation_messages_in_db(
    db,
    conv_id: str,
    *,
    limit: int | None = None,
) -> list[dict]:
    sql = (
        "SELECT id, conv_id, role, content, created_at FROM messages "
        "WHERE conv_id=? AND role IN ('user','assistant') "
        "ORDER BY created_at ASC"
    )
    params: list = [conv_id]
    if limit is not None:
        sql += " LIMIT ?"
        params.append(int(limit))
    db.row_factory = aiosqlite.Row
    cur = await db.execute(sql, params)
    return [dict(row) for row in await cur.fetchall()]


async def _invalidate_cards_in_tx(db, chunk_ids: list[str], *, now: float) -> int:
    if not chunk_ids:
        return 0
    placeholders = ",".join("?" for _ in chunk_ids)
    cur = await db.execute(
        "UPDATE memory_relational_cards SET status='invalid', updated_at=? "
        f"WHERE status='active' AND source_chunk_id IN ({placeholders})",
        [now, *chunk_ids],
    )
    return max(int(cur.rowcount or 0), 0)


async def _count_active_cards_in_db(db, chunk_ids: list[str]) -> int:
    if not chunk_ids:
        return 0
    placeholders = ",".join("?" for _ in chunk_ids)
    cur = await db.execute(
        "SELECT COUNT(*) FROM memory_relational_cards "
        f"WHERE status='active' AND source_chunk_id IN ({placeholders})",
        chunk_ids,
    )
    row = await cur.fetchone()
    return int(row[0] or 0)


async def _fetch_existing_chunks_in_db(db, conv_id: str) -> dict[str, dict]:
    db.row_factory = aiosqlite.Row
    cur = await db.execute(
        "SELECT id, message_ids_json, content, source_hash, status, embedding "
        "FROM memory_chunks WHERE conv_id=?",
        (conv_id,),
    )
    return {row["id"]: dict(row) for row in await cur.fetchall()}


def _plan_chunk_reconciliation(
    existing: dict[str, dict],
    desired: list[dict],
    *,
    retire_missing: bool,
) -> dict:
    """Build the exact mutation plan without touching SQLite.

    The same plan is consumed by the apply path and exposed by the read-only
    preview.  This keeps dry-run counts from drifting away from real writes.
    """
    desired_ids = {str(chunk["id"]) for chunk in desired}
    insert: list[dict] = []
    update: list[dict] = []
    reactivate: list[dict] = []
    unchanged: list[str] = []
    invalidate_ids: set[str] = set()

    for chunk in desired:
        chunk_id = str(chunk["id"])
        old = existing.get(chunk_id)
        if old is None:
            insert.append(chunk)
            continue
        content_changed = str(old.get("content") or "") != chunk["content"]
        provenance_changed = (
            str(old.get("message_ids_json") or "") != chunk["message_ids_json"]
            or str(old.get("source_hash") or "") != chunk["source_hash"]
        )
        was_retired = old.get("status") == "retired"
        if not content_changed and not provenance_changed and not was_retired:
            unchanged.append(chunk_id)
            continue
        action = {
            "chunk": chunk,
            "old_embedding": old.get("embedding"),
            "content_changed": content_changed,
            "provenance_changed": provenance_changed,
        }
        (reactivate if was_retired else update).append(action)
        if content_changed or provenance_changed:
            invalidate_ids.add(chunk_id)

    retire = []
    if retire_missing:
        retire = sorted(
            chunk_id
            for chunk_id, row in existing.items()
            if chunk_id not in desired_ids and row.get("status") != "retired"
        )
        invalidate_ids.update(retire)

    return {
        "insert": insert,
        "update": update,
        "reactivate": reactivate,
        "retire": retire,
        "unchanged": unchanged,
        "invalidate": sorted(invalidate_ids),
    }


async def _reconcile_chunks_in_tx(
    db,
    conv_id: str,
    desired: list[dict],
    *,
    retire_missing: bool,
    now: float,
) -> dict:
    existing = await _fetch_existing_chunks_in_db(db, conv_id)
    plan = _plan_chunk_reconciliation(
        existing,
        desired,
        retire_missing=retire_missing,
    )
    stats = {
        "inserted": len(plan["insert"]),
        "updated": len(plan["update"]),
        "reactivated": len(plan["reactivate"]),
        "retired": len(plan["retire"]),
        "unchanged": len(plan["unchanged"]),
        "cards_invalidated": 0,
    }

    for chunk in plan["insert"]:
        await db.execute(
            "INSERT INTO memory_chunks "
            "(id, conv_id, message_ids_json, content, source_hash, status, retired_at, "
            "created_at, updated_at, embedding, keywords_json, metadata_json) "
            "VALUES (?,?,?,?,?,'active',NULL,?,?,?,?,?)",
            (
                chunk["id"],
                chunk["conv_id"],
                chunk["message_ids_json"],
                chunk["content"],
                chunk["source_hash"],
                chunk["created_at"],
                chunk["updated_at"],
                None,
                chunk["keywords_json"],
                chunk["metadata_json"],
            ),
        )

    for action in [*plan["update"], *plan["reactivate"]]:
        chunk = action["chunk"]
        embedding_value = None if action["content_changed"] else action["old_embedding"]
        await db.execute(
            "UPDATE memory_chunks SET message_ids_json=?, content=?, source_hash=?, "
            "status='active', retired_at=NULL, created_at=?, updated_at=?, embedding=?, "
            "keywords_json=?, metadata_json=? WHERE id=?",
            (
                chunk["message_ids_json"],
                chunk["content"],
                chunk["source_hash"],
                chunk["created_at"],
                chunk["updated_at"],
                embedding_value,
                chunk["keywords_json"],
                chunk["metadata_json"],
                chunk["id"],
            ),
        )

    if plan["retire"]:
        placeholders = ",".join("?" for _ in plan["retire"])
        await db.execute(
            f"UPDATE memory_chunks SET status='retired', retired_at=? "
            f"WHERE id IN ({placeholders})",
            [now, *plan["retire"]],
        )

    stats["cards_invalidated"] = await _invalidate_cards_in_tx(
        db, plan["invalidate"], now=now
    )
    return stats


async def preview_conversation_chunk_reconciliation(
    conv_id: str,
    *,
    message_limit: int | None = None,
) -> dict:
    """Return the exact apply plan while making the connection query-only."""
    async with get_db() as db:
        await db.execute("PRAGMA query_only=ON")
        await db.execute("BEGIN")
        messages = await _fetch_conversation_messages_in_db(
            db, conv_id, limit=message_limit
        )
        desired = build_chunks_from_messages(messages)
        existing = await _fetch_existing_chunks_in_db(db, conv_id)
        plan = _plan_chunk_reconciliation(
            existing,
            desired,
            retire_missing=message_limit is None,
        )
        active_cards_to_invalidate = await _count_active_cards_in_db(
            db, plan["invalidate"]
        )
        await db.rollback()
    return {
        "ok": True,
        "apply": False,
        "conv_id": conv_id,
        "scanned_messages": len(messages),
        "generated_chunks": len(desired),
        "would_insert": len(plan["insert"]),
        "would_update": len(plan["update"]),
        "would_reactivate": len(plan["reactivate"]),
        "would_retire": len(plan["retire"]),
        "would_invalidate_cards": active_cards_to_invalidate,
        "unchanged": len(plan["unchanged"]),
        "insert_ids": [str(chunk["id"]) for chunk in plan["insert"]],
        "update_ids": [str(item["chunk"]["id"]) for item in plan["update"]],
        "reactivate_ids": [str(item["chunk"]["id"]) for item in plan["reactivate"]],
        "retire_ids": list(plan["retire"]),
        "invalidate_card_source_ids": list(plan["invalidate"]),
    }


async def _fetch_pending_chunks(ids: list[str] | None = None, *, limit: int | None = None) -> list[dict]:
    clauses = ["embedding IS NULL", "TRIM(content) != ''", "status IN ('active','cold')"]
    params: list = []
    if ids:
        placeholders = ",".join("?" for _ in ids)
        clauses.append(f"id IN ({placeholders})")
        params.extend(ids)
    sql = (
        "SELECT id, content, source_hash FROM memory_chunks "
        f"WHERE {' AND '.join(clauses)} "
        "ORDER BY updated_at ASC"
    )
    if limit is not None:
        sql += " LIMIT ?"
        params.append(int(limit))
    async with get_db() as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(sql, params)
        rows = await cur.fetchall()
    return [dict(row) for row in rows]


async def embed_pending_chunks(
    ids: list[str] | None = None,
    *,
    limit: int | None = None,
    batch_size: int = DEFAULT_EMBED_BATCH_SIZE,
    sleep_seconds: float = 0.0,
) -> dict:
    pending = await _fetch_pending_chunks(ids, limit=limit)
    stats = {
        "selected": len(pending),
        "embedded": 0,
        "embedding_failed": 0,
        "skipped": 0,
    }
    batch_size = max(int(batch_size or DEFAULT_EMBED_BATCH_SIZE), 1)
    for start in range(0, len(pending), batch_size):
        batch = pending[start: start + batch_size]
        vectors = await embedding.get_embeddings_batch([row["content"] for row in batch])
        async with get_db() as db:
            for row, vector in zip(batch, vectors):
                if not vector:
                    stats["embedding_failed"] += 1
                    continue
                cur = await db.execute(
                    "UPDATE memory_chunks SET embedding=?, updated_at=updated_at "
                    "WHERE id=? AND embedding IS NULL AND source_hash IS ? AND content=? "
                    "AND status IN ('active','cold')",
                    (embedding.pack_embedding(vector), row["id"], row["source_hash"], row["content"]),
                )
                stats["embedded" if cur.rowcount else "skipped"] += 1
            await db.commit()
        if sleep_seconds > 0 and start + batch_size < len(pending):
            await asyncio.sleep(float(sleep_seconds))
    return stats


async def reconcile_conversation_chunks_in_tx(db, conv_id: str) -> dict:
    """与消息修改共用事务对账；不提交事务，也不触发向量化或卡片生成。"""
    messages = await _fetch_conversation_messages_in_db(db, conv_id)
    desired = build_chunks_from_messages(messages)
    return await _reconcile_chunks_in_tx(
        db, conv_id, desired, retire_missing=True, now=time.time(),
    )


async def ensure_conversation_chunks(
    conv_id: str,
    *,
    embed: bool = True,
    batch_size: int = DEFAULT_EMBED_BATCH_SIZE,
    sleep_seconds: float = 0.0,
    message_limit: int | None = None,
) -> dict:
    # Fetch and reconcile under one write transaction.  Taking the snapshot
    # before acquiring the transaction allowed an older concurrent task to run
    # last and retire chunks produced from newer messages.
    async with get_db() as db:
        await db.execute("BEGIN IMMEDIATE")
        try:
            messages = await _fetch_conversation_messages_in_db(
                db, conv_id, limit=message_limit
            )
            chunks = build_chunks_from_messages(messages)
            reconcile = await _reconcile_chunks_in_tx(
                db,
                conv_id,
                chunks,
                retire_missing=message_limit is None,
                now=time.time(),
            )
            await db.commit()
        except BaseException:
            await db.rollback()
            raise
    embed_stats = {"selected": 0, "embedded": 0, "embedding_failed": 0, "skipped": 0}
    if embed and chunks:
        embed_stats = await embed_pending_chunks(
            [chunk["id"] for chunk in chunks],
            batch_size=batch_size,
            sleep_seconds=sleep_seconds,
        )
    return {
        "ok": True,
        "conv_id": conv_id,
        "scanned_messages": len(messages),
        "generated_chunks": len(chunks),
        "inserted_chunks": reconcile["inserted"],
        "updated_chunks": reconcile["updated"],
        "reactivated_chunks": reconcile["reactivated"],
        "retired_chunks": reconcile["retired"],
        "unchanged_chunks": reconcile["unchanged"],
        "cards_invalidated": reconcile["cards_invalidated"],
        "existing_chunks": len(chunks) - reconcile["inserted"],
        "embedding_selected": embed_stats["selected"],
        "embedding_success": embed_stats["embedded"],
        "embedding_failed": embed_stats["embedding_failed"],
    }
