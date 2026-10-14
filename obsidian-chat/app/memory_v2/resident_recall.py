"""Bounded ordinary recall and paged, disk-backed deliberate recollection."""
from __future__ import annotations

import asyncio
import heapq
import sys
import time

import aiosqlite

from .compaction_store import covered_sql, init_tables, valid_output_sql


PAGE_ROWS = 64
CHUNK_TIME = "COALESCE(CASE WHEN json_valid(c.metadata_json) THEN json_extract(c.metadata_json,'$.source_end_ts') END,c.updated_at,c.created_at)"
CHUNK_START = "COALESCE(CASE WHEN json_valid(c.metadata_json) THEN json_extract(c.metadata_json,'$.source_start_ts') END,c.created_at)"
NOTE_TIME = "COALESCE(n.source_end_ts,n.source_start_ts,n.created_at)"
PINNED_NOTE = "(COALESCE(n.importance,0)>=0.9 OR CASE WHEN json_valid(n.metadata_json) THEN COALESCE(json_extract(n.metadata_json,'$.ui_unresolved'),0)=1 ELSE 0 END)"


def chunk_query(*, include_cards: bool, recent_after: float | None = None) -> tuple[str, list]:
    from app.memory_v3.card_versions import READABLE_RELATIONAL_CARD_PROMPT_VERSIONS
    columns, join, params = "", "", []
    if include_cards:
        versions = sorted(READABLE_RELATIONAL_CARD_PROMPT_VERSIONS)
        placeholders = ",".join("?" for _ in versions)
        columns = ",rc.id AS card_id,rc.version AS card_version,rc.content AS card_content,rc.prompt_version AS card_prompt_version"
        join = (" LEFT JOIN memory_relational_cards rc ON rc.source_chunk_id=c.id "
                f"AND rc.status='active' AND rc.prompt_version IN ({placeholders})")
        params.extend(versions)
    sql = ("SELECT c.id,c.conv_id,c.message_ids_json,c.content,c.created_at,c.updated_at,"
           "c.source_hash,c.embedding,c.keywords_json,c.metadata_json" + columns +
           " FROM memory_chunks c" + join + " WHERE c.status IN ('active','cold') AND TRIM(c.content)!=''")
    if recent_after is not None:
        sql += f" AND {CHUNK_TIME}>=? AND NOT {covered_sql('chunk', 'c.id')}"
        params.append(recent_after)
    # The cold scan follows the primary-key index: no all-corpus BLOB sort.
    order = f"{CHUNK_TIME} DESC,c.id" if recent_after is not None else "c.id"
    return sql + " ORDER BY " + order, params


async def pages(db, sql: str, params=()):
    # SQLite retains the result cursor, not a Python copy of the entire corpus.
    async with db.execute(sql, params) as cursor:
        while raw := await cursor.fetchmany(PAGE_ROWS):
            yield [dict(row) for row in raw]
            await asyncio.sleep(0)


def row_bytes(row: dict) -> int:
    def size(value):
        own = sys.getsizeof(value)
        if isinstance(value, dict):
            return own + sum(size(k) + size(v) for k, v in value.items())
        if isinstance(value, (list, tuple)):
            return own + sum(size(v) for v in value)
        return own
    # The row retains its BLOB; the matrix is a second float32 copy. Include
    # array/id/list overhead and slack instead of counting only vector bytes.
    return size(row) + len(row.get("embedding") or b"") + 512


async def build_entry(hybrid, policy: dict, *, include_cards: bool, ai_note_lane_enabled: bool):
    budget = int(policy["cache_mib"]) * 1024 * 1024
    cutoff = time.time() - int(policy["recent_days"]) * 86400
    lanes = {"chunks": [], "notes": [], "ai_notes": []}
    used = 4096
    skipped = 0
    skipped_notes = 0

    def accept(row, lane):
        nonlocal used, skipped, skipped_notes
        hybrid._prepare_cached_rows([row])
        charge = row_bytes(row)
        if used + charge > budget:
            skipped += 1
            if lane != "chunks":
                skipped_notes += 1
            return
        lanes[lane].append(row)
        used += charge

    async with hybrid.get_db() as db:
        db.row_factory = aiosqlite.Row
        await init_tables(db)
        await db.commit()
        # Source-specific notes and consolidated long-term notes have priority.
        # Neither being important nor being recent bypasses the byte budget.
        sql = ("SELECT n.* FROM memory_items n WHERE n.status='active' AND n.visibility='prompt' "
               "AND TRIM(n.content)!='' AND (n.origin_type!='compacted' OR " + valid_output_sql("n.id") + ") "
               f"AND NOT {covered_sql('note', 'n.id')} "
               f"AND (n.origin_type!='auto_digest' OR {PINNED_NOTE} OR {NOTE_TIME}>=?) "
               "ORDER BY CASE WHEN n.origin_type IN ('ai_note','manual','legacy') THEN 0 "
               "WHEN n.kind IN ('open_loop','rule','preference') THEN 1 "
               f"WHEN n.origin_type='compacted' THEN 2 ELSE 3 END,{NOTE_TIME} DESC,n.id")
        async for batch in pages(db, sql, (cutoff,)):
            for row in batch:
                accept(row, "ai_notes" if ai_note_lane_enabled and row["origin_type"] == "ai_note" else "notes")
        sql, params = chunk_query(include_cards=include_cards, recent_after=cutoff)
        async for batch in pages(db, sql, params):
            for row in batch:
                accept(row, "chunks")
        from app.image_memory.repository import iter_recall_rows
        async for row in iter_recall_rows(db, recent_after=cutoff):
            accept(row, "chunks")
        from app.heart_works.memory import iter_recall_rows as heart_event_rows
        async for row in heart_event_rows(db, recent_after=cutoff):
            accept(row, "chunks")
    entry = hybrid._FullCorpusCacheEntry(
        **lanes, chunk_matrix=hybrid._CachedEmbeddingMatrix({}, {}),
        note_matrix=hybrid._CachedEmbeddingMatrix({}, {}),
        ai_note_matrix=hybrid._CachedEmbeddingMatrix({}, {}),
        include_cards=include_cards, ai_note_lane_enabled=ai_note_lane_enabled,
        built_at=time.monotonic(),
    )
    hybrid._refresh_entry_matrices(entry)
    entry.residency = {"budget_bytes": budget, "estimated_bytes": used,
                       "recent_days": policy["recent_days"], "budget_excluded_rows": skipped,
                       "note_budget_excluded_rows": skipped_notes}
    return entry


async def protected_note_recall(hybrid, vector, terms, *, ai_note_lane_enabled: bool,
                                top_k: int, visible_ids: set[str]) -> tuple[list, list]:
    """Even when protected notes outgrow RAM, they remain searchable on demand."""
    heaps = {False: [], True: []}
    sequence = 0
    async with hybrid.get_db() as db:
        db.row_factory = aiosqlite.Row
        sql = ("SELECT n.* FROM memory_items n WHERE n.status='active' AND n.visibility='prompt' "
               f"AND (n.origin_type!='auto_digest' OR {PINNED_NOTE}) AND TRIM(n.content)!='' "
               "AND (n.origin_type!='compacted' OR " + valid_output_sql("n.id") + ") ORDER BY n.id")
        async for batch in pages(db, sql):
            batch = [row for row in batch if not visible_ids.intersection(hybrid._source_message_ids(row))]
            similarities = hybrid._batch_similarities(batch, vector)
            usage = await hybrid._recent_usage([r["id"] for r in batch])
            for row, similarity in zip(batch, similarities):
                is_ai = ai_note_lane_enabled and row["origin_type"] == "ai_note"
                item = hybrid._score_note(row, terms, similarity, usage.get(row["id"]), as_ai_note=is_ai)
                sequence += 1
                value = (item["score"], -sequence, item)
                heap = heaps[is_ai]
                if len(heap) < top_k:
                    heapq.heappush(heap, value)
                elif value[:2] > heap[0][:2]:
                    heapq.heapreplace(heap, value)
    return tuple([item for _, _, item in sorted(heaps[key], key=lambda c: c[:2], reverse=True)] for key in (False, True))


async def wide_recall(hybrid, query_text: str, *, top_k: int, as_of_ts: float,
                      exclude_message_id: str | None, relational_cards_enabled: bool) -> list[dict]:
    """Search all historical chunks with bounded working memory, without caching them."""
    vector = await hybrid.embedding.get_embedding(query_text)
    if not vector:
        raise RuntimeError("pending recall query embedding unavailable")
    terms = hybrid._terms(query_text, None)
    heap = []
    sequence = 0

    async def score_batch(batch):
        nonlocal sequence
        eligible = [r for r in batch if hybrid._source_timestamp(r) <= as_of_ts
                    and (not exclude_message_id or exclude_message_id not in hybrid._source_message_ids(r))]
        usage = await hybrid._recent_usage([r["id"] for r in eligible])
        similarities = hybrid._batch_similarities(eligible, vector)
        for row, similarity in zip(eligible, similarities):
            item = hybrid._score_chunk(row, terms, similarity, usage.get(row["id"]),
                card_readout_mode="card_preferred" if relational_cards_enabled else "current_raw")
            sequence += 1
            candidate = (item["score"], -sequence, item, row)
            if len(heap) < top_k:
                heapq.heappush(heap, candidate)
            elif candidate[:2] > heap[0][:2]:
                heapq.heapreplace(heap, candidate)

    async with hybrid.get_db() as db:
        db.row_factory = aiosqlite.Row
        sql, params = chunk_query(include_cards=relational_cards_enabled)
        async for batch in pages(db, sql, params):
            await score_batch(batch)
        from app.image_memory.repository import iter_recall_rows
        batch = []
        async for row in iter_recall_rows(db):
            batch.append(row)
            if len(batch) >= PAGE_ROWS:
                await score_batch(batch)
                batch = []
        if batch:
            await score_batch(batch)
        from app.heart_works.memory import iter_recall_rows as heart_event_rows
        batch = []
        async for row in heart_event_rows(db):
            batch.append(row)
            if len(batch) >= PAGE_ROWS:
                await score_batch(batch)
                batch = []
        if batch:
            await score_batch(batch)
    result = []
    for rank, (_, _, item, row) in enumerate(sorted(heap, key=lambda c: c[:2], reverse=True), 1):
        raw = str(item.get("raw_content") or item.get("content") or "")
        card = str(row.get("card_content") or "").strip()
        result.append({
            "candidate_id": item["id"], "source_chunk_id": item["id"],
            "source_type": item.get("source_type", "chunk"), "attachment_url": item.get("attachment_url"),
            "work_id": item.get("work_id"),
            "source_hash": row.get("source_hash", ""), "source_message_ids": item.get("source_message_ids", []),
            "source_start_ts": item.get("source_start_ts"), "source_end_ts": item.get("source_end_ts"),
            "raw_content": raw, "readout_text": card or hybrid._clip_for_selector(raw, 600),
            "readout_type": "event" if item.get("source_type") == "heart_work" else "image_observation" if item.get("source_type") == "image" else "relational_card" if card else "raw_excerpt",
            "card_id": row.get("card_id"), "card_version": row.get("card_version"),
            "score": item["score"], "semantic_similarity": item["semantic_similarity"],
            "keyword_relevance": item["keyword_relevance"], "cooldown_penalty": item["cooldown_penalty"],
            "rank": rank,
        })
    return result
