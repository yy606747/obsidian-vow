"""
记忆库持久化边界。

把 routes/chat 中散落的 memories 表写法收敛到这里。Batch 2.0 不改 schema。
"""

from __future__ import annotations

import json
import time

import aiosqlite

from config import load_worldbook
from database import get_db
from app.chat.worldbook import resolve_worldbook_names

from .embedding import get_document_embedding, pack_embedding
from .shadow_write import shadow_legacy_memory
from .v2_repository import MemoryRepository


MEMORY_LIST_DEFAULT_LIMIT = 100
MEMORY_LIST_MAX_LIMIT = 200


async def list_memories() -> list[dict]:
    async with get_db() as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT id, content, type, created_at, source_conv, keywords, importance, source_start_ts, source_end_ts, unresolved "
            "FROM memories ORDER BY created_at DESC"
        )
        rows = await cur.fetchall()
        cur = await db.execute(
            "SELECT id, kind, namespace, content, importance, keywords_json, source_conv, "
            "source_start_ts, source_end_ts, created_at, metadata_json "
            "FROM memory_items "
            "WHERE legacy_memory_id IS NULL AND status='active' "
            "ORDER BY created_at DESC"
        )
        v2_rows = await cur.fetchall()
    memories = [dict(r) for r in rows]
    memories.extend(_public_v2_memory(dict(r)) for r in v2_rows)
    memories.sort(key=lambda item: float(item.get("created_at") or 0), reverse=True)
    return memories


async def list_memories_page(
    *,
    limit: int = MEMORY_LIST_DEFAULT_LIMIT,
    offset: int = 0,
    query: str = "",
    memory_type: str = "",
    unresolved: int | None = None,
) -> dict:
    limit = max(1, min(int(limit or MEMORY_LIST_DEFAULT_LIMIT), MEMORY_LIST_MAX_LIMIT))
    offset = max(0, int(offset or 0))
    query = str(query or "").strip()
    memory_type = str(memory_type or "").strip()

    where = []
    params: list = []
    if query:
        like = f"%{query}%"
        where.append("(content LIKE ? OR keywords LIKE ? OR type LIKE ?)")
        params.extend([like, like, like])
    if memory_type:
        where.append("type=?")
        params.append(memory_type)
    if unresolved is not None:
        where.append("unresolved=?")
        params.append(1 if unresolved else 0)

    where_sql = f"WHERE {' AND '.join(where)}" if where else ""
    sql = (
        f"SELECT * FROM ({_memory_list_union_sql()}) "
        f"{where_sql} "
        "ORDER BY created_at DESC, id DESC "
        "LIMIT ? OFFSET ?"
    )
    params.extend([limit + 1, offset])

    async with get_db() as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(sql, params)
        rows = await cur.fetchall()

    items = [dict(row) for row in rows[:limit]]
    has_more = len(rows) > limit
    return {
        "items": items,
        "limit": limit,
        "offset": offset,
        "next_offset": offset + len(items) if has_more else None,
        "has_more": has_more,
        "query": query,
        "type": memory_type,
        "unresolved": unresolved,
    }


async def create_memory(content: str, memory_type: str = "event", *,
                        source_conv: str | None = None,
                        keywords: str = "",
                        importance: float = 0.5,
                        source_start_ts=None,
                        source_end_ts=None) -> dict:
    vec = await get_document_embedding(content)
    mem_id = f"mem_{int(time.time()*1000)}"
    now = time.time()
    embedding_blob = pack_embedding(vec) if vec else None
    async with get_db() as db:
        await db.execute(
            "INSERT INTO memories (id, content, type, created_at, source_conv, embedding, keywords, importance, source_start_ts, source_end_ts) "
            "VALUES (?,?,?,?,?,?,?,?,?,?)",
            (
                mem_id, content, memory_type, now, source_conv, embedding_blob,
                keywords, importance, source_start_ts, source_end_ts,
            ),
        )
        await db.commit()
    mem = {
        "id": mem_id,
        "content": content,
        "type": memory_type,
        "created_at": now,
        "source_conv": source_conv,
        "keywords": keywords,
        "importance": importance,
        "source_start_ts": source_start_ts,
        "source_end_ts": source_end_ts,
    }
    shadow_source = "remember_cmd" if memory_type == "ai_note" else "manual_memory"
    try:
        await shadow_legacy_memory(
            mem_id,
            write_path="repository.create_memory",
            source=shadow_source,
            extra_metadata={"memory_type": memory_type},
        )
    except Exception as exc:
        print(f"[MemoryV2Shadow] create_memory shadow skipped: {exc}")
    return mem


async def create_working_model_ai_note_in_tx(
    db,
    *,
    memory_id: str,
    content: str,
    source_conv: str | None,
    origin_request_id: str,
    embedding_blob: bytes | None,
    created_at: float,
    importance: float = 0.6,
) -> dict:
    """Atomically persist a routed V2 request through the existing AI-note lane.

    Unlike ``create_memory``, this helper never owns the transaction.  The
    caller commits the legacy row, its V2 metadata mirror, the request link,
    and the working-model request terminal state together.
    """

    await db.execute(
        "INSERT INTO memories "
        "(id, content, type, created_at, source_conv, embedding, keywords, "
        "importance, source_start_ts, source_end_ts) "
        "VALUES (?,?,?,?,?,?,?,?,?,?)",
        (
            memory_id,
            content,
            "ai_note",
            created_at,
            source_conv,
            embedding_blob,
            "",
            importance,
            None,
            None,
        ),
    )

    # Keep the same deterministic mirror shape as the normal remember shadow,
    # but write it in the caller's transaction so provenance cannot lag behind.
    from .migrations import legacy_memory_to_item

    legacy = {
        "id": memory_id,
        "content": content,
        "type": "ai_note",
        "created_at": created_at,
        "source_conv": source_conv,
        "embedding": embedding_blob,
        "keywords": "",
        "importance": importance,
        "source_start_ts": None,
        "source_end_ts": None,
        "unresolved": 0,
    }
    # ``legacy_memory_to_item`` intentionally returns only inferred values.
    # The normal repository lane overlays those values on ITEM_DEFAULTS before
    # inserting.  This transaction-neutral lane must do the same: passing
    # ``None`` explicitly for an omitted NOT NULL column bypasses SQLite's
    # column default (production first failed on ``subject``, then would have
    # failed on ``entities_json``).
    item = dict(MemoryRepository.ITEM_DEFAULTS)
    item.update(legacy_memory_to_item(legacy))
    metadata = _safe_json_object(item.get("metadata_json"))
    metadata.update({
        "source": "working_model_request",
        "legacy_type": "ai_note",
        "origin_request_id": origin_request_id,
        "authored_by": "assistant",
        "source_identity": "assistant_interpretation",
        "write_path": "working_model.runtime",
    })
    item["metadata_json"] = json.dumps(metadata, ensure_ascii=False)
    item["origin_type"] = "ai_note"
    columns = MemoryRepository.ITEM_COLUMNS
    await db.execute(
        f"INSERT INTO memory_items ({', '.join(columns)}) "
        f"VALUES ({','.join('?' for _ in columns)})",
        [item.get(column) for column in columns],
    )
    await db.execute(
        "INSERT INTO memory_links "
        "(memory_id, target_id, target_type, relation, created_at) "
        "VALUES (?,?,?,?,?)",
        (
            item["id"],
            origin_request_id,
            "working_model_request",
            "generated_from",
            created_at,
        ),
    )
    return {
        "id": memory_id,
        "content": content,
        "type": "ai_note",
        "created_at": created_at,
        "source_conv": source_conv,
        "keywords": "",
        "importance": importance,
        "source_start_ts": None,
        "source_end_ts": None,
        "memory_item_id": item["id"],
        "metadata": metadata,
    }


async def update_memory(mem_id: str, content: str, *,
                        memory_type: str | None = None,
                        keywords: str | None = None,
                        importance: float | None = None,
                        unresolved: int | None = None) -> dict:
    vec = await get_document_embedding(content)
    async with get_db() as db:
        fields = ["content=?", "embedding=?"]
        params = [content, pack_embedding(vec) if vec else None]
        if memory_type is not None:
            fields.append("type=?")
            params.append(memory_type)
        if keywords is not None:
            fields.append("keywords=?")
            params.append(keywords)
        if importance is not None:
            fields.append("importance=?")
            params.append(importance)
        if unresolved is not None:
            fields.append("unresolved=?")
            params.append(1 if unresolved else 0)
        params.append(mem_id)
        cur = await db.execute(f"UPDATE memories SET {', '.join(fields)} WHERE id=?", params)
        if cur.rowcount == 0:
            v2_fields = ["content=?", "embedding=?", "updated_at=?"]
            v2_params = [content, pack_embedding(vec) if vec else None, time.time()]
            if keywords is not None:
                v2_fields.append("keywords_json=?")
                v2_params.append(keywords)
            if importance is not None:
                v2_fields.append("importance=?")
                v2_params.append(importance)
            if unresolved is not None:
                row_cur = await db.execute("SELECT metadata_json FROM memory_items WHERE id=?", (mem_id,))
                row = await row_cur.fetchone()
                if row:
                    metadata = _safe_json_object(row["metadata_json"] if hasattr(row, "keys") else row[0])
                    metadata["ui_unresolved"] = bool(unresolved)
                    v2_fields.append("metadata_json=?")
                    v2_params.append(json.dumps(metadata, ensure_ascii=False))
            v2_params.append(mem_id)
            await db.execute(
                f"UPDATE memory_items SET {', '.join(v2_fields)} WHERE id=?",
                v2_params,
            )
        await db.commit()
    return {"ok": True, "id": mem_id}


async def delete_memory(mem_id: str) -> dict:
    async with get_db() as db:
        cur = await db.execute("DELETE FROM memories WHERE id=?", (mem_id,))
        if cur.rowcount == 0:
            await db.execute(
                "UPDATE memory_items SET status='archived', visibility='hidden', updated_at=? WHERE id=?",
                (time.time(), mem_id),
            )
        await db.commit()
    return {"ok": True}


async def toggle_unresolved(mem_id: str) -> dict:
    async with get_db() as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("SELECT unresolved FROM memories WHERE id=?", (mem_id,))
        row = await cur.fetchone()
        if row:
            new_val = 0 if row["unresolved"] else 1
            await db.execute("UPDATE memories SET unresolved=? WHERE id=?", (new_val, mem_id))
        else:
            cur = await db.execute("SELECT kind, metadata_json FROM memory_items WHERE id=? AND status='active'", (mem_id,))
            item = await cur.fetchone()
            if not item:
                return {"ok": False, "message": "记忆不存在"}
            metadata = _safe_json_object(item["metadata_json"])
            current = bool(metadata["ui_unresolved"]) if "ui_unresolved" in metadata else item["kind"] == "open_loop"
            new_val = 0 if current else 1
            metadata["ui_unresolved"] = bool(new_val)
            await db.execute(
                "UPDATE memory_items SET metadata_json=?, updated_at=? WHERE id=?",
                (json.dumps(metadata, ensure_ascii=False), time.time(), mem_id),
            )
        await db.commit()
    return {"ok": True, "unresolved": new_val}


async def get_memory_source(mem_id: str) -> dict:
    async with get_db() as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("SELECT source_start_ts, source_end_ts FROM memories WHERE id=?", (mem_id,))
        mem = await cur.fetchone()
        if not mem:
            cur = await db.execute(
                "SELECT source_start_ts, source_end_ts FROM memory_items WHERE id=? AND status='active'",
                (mem_id,),
            )
            mem = await cur.fetchone()
    if not mem or not mem["source_start_ts"] or not mem["source_end_ts"]:
        return {"ok": False, "message": "该记忆没有可追溯的原文"}

    wb = load_worldbook()
    user_name, ai_name = resolve_worldbook_names(wb)

    async with get_db() as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT role, content, created_at FROM messages "
            "WHERE role IN ('user','assistant') AND created_at >= ? AND created_at <= ? "
            "ORDER BY created_at ASC",
            (mem["source_start_ts"], mem["source_end_ts"]),
        )
        rows = await cur.fetchall()

    messages = []
    for r in rows:
        messages.append({
            "role": r["role"],
            "name": user_name if r["role"] == "user" else ai_name,
            "content": r["content"],
            "created_at": r["created_at"],
        })
    return {"ok": True, "messages": messages}


def _public_v2_memory(row: dict) -> dict:
    metadata = _safe_json_object(row.get("metadata_json"))
    keywords = row.get("keywords_json") or "[]"
    source = str(metadata.get("source") or "")
    memory_type = "digest_note" if source == "digest.multi_note" else "v2_note"
    if source == "remember_cmd":
        memory_type = "ai_note"
    if "legacy_type" in metadata:
        memory_type = str(metadata.get("legacy_type") or memory_type)
    if "ui_unresolved" in metadata:
        unresolved = 1 if metadata.get("ui_unresolved") else 0
    else:
        unresolved = 1 if row.get("kind") == "open_loop" else 0
    return {
        "id": row["id"],
        "content": row["content"],
        "type": memory_type,
        "created_at": row.get("created_at"),
        "source_conv": row.get("source_conv"),
        "keywords": keywords,
        "importance": row.get("importance"),
        "source_start_ts": row.get("source_start_ts"),
        "source_end_ts": row.get("source_end_ts"),
        "unresolved": unresolved,
        "source_table": "memory_items",
        "kind": row.get("kind"),
        "namespace": row.get("namespace"),
    }


def _safe_json_object(raw) -> dict:
    if isinstance(raw, dict):
        return dict(raw)
    try:
        parsed = json.loads(raw or "{}")
    except (TypeError, json.JSONDecodeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _memory_list_union_sql() -> str:
    source_expr = "CASE WHEN json_valid(metadata_json) THEN json_extract(metadata_json, '$.source') ELSE '' END"
    legacy_type_expr = "CASE WHEN json_valid(metadata_json) THEN json_extract(metadata_json, '$.legacy_type') ELSE '' END"
    ui_unresolved_expr = "CASE WHEN json_valid(metadata_json) THEN json_extract(metadata_json, '$.ui_unresolved') ELSE NULL END"
    v2_type_expr = (
        f"CASE "
        f"WHEN {legacy_type_expr} IS NOT NULL AND TRIM(CAST({legacy_type_expr} AS TEXT)) != '' "
        f"THEN CAST({legacy_type_expr} AS TEXT) "
        f"WHEN {source_expr}='digest.multi_note' THEN 'digest_note' "
        f"WHEN {source_expr}='remember_cmd' THEN 'ai_note' "
        f"ELSE 'v2_note' END"
    )
    v2_unresolved_expr = (
        f"CASE "
        f"WHEN {ui_unresolved_expr} IS NOT NULL THEN CASE WHEN {ui_unresolved_expr} THEN 1 ELSE 0 END "
        f"WHEN kind='open_loop' THEN 1 "
        f"ELSE 0 END"
    )
    return f"""
        SELECT
            id,
            content,
            COALESCE(type, 'event') AS type,
            created_at,
            source_conv,
            COALESCE(keywords, '') AS keywords,
            importance,
            source_start_ts,
            source_end_ts,
            COALESCE(unresolved, 0) AS unresolved,
            NULL AS kind,
            NULL AS namespace,
            'memories' AS source_table
        FROM memories
        UNION ALL
        SELECT
            id,
            content,
            {v2_type_expr} AS type,
            created_at,
            source_conv,
            COALESCE(keywords_json, '[]') AS keywords,
            importance,
            source_start_ts,
            source_end_ts,
            {v2_unresolved_expr} AS unresolved,
            kind,
            namespace,
            'memory_items' AS source_table
        FROM memory_items
        WHERE legacy_memory_id IS NULL AND status='active'
    """
