"""Hybrid chunk + note recall for the primary chat memory path."""

from __future__ import annotations

import asyncio
from app.background_tasks import create_tracked_task
from dataclasses import dataclass, field
import json
import logging
import math
import time
from typing import Any
import weakref

import aiosqlite
import numpy as np

from database import get_db
from app.memory_v3.card_versions import READABLE_RELATIONAL_CARD_PROMPT_VERSIONS

from . import embedding
from .chunks import extract_keywords
from .prompt_eligibility import prompt_eligible


DEFAULT_CHUNK_TOP_K = 8
DEFAULT_NOTE_TOP_K = 6
DEFAULT_TOP_K = 8
DEFAULT_CANDIDATE_LIMIT = 1000
DEFAULT_MIN_SCORE = 0.18
CANDIDATE_WINDOW_LOG_INTERVAL_SECONDS = 300.0
SLOW_RECALL_WARNING_SECONDS = 0.300
SLOW_RECALL_LOG_INTERVAL_SECONDS = 300.0
FULL_CORPUS_CACHE_TTL_SECONDS = 1800.0
FULL_CORPUS_CACHE_RETRY_SECONDS = 60.0


logger = logging.getLogger(__name__)
_candidate_window_last_logged: dict[str, float] = {}
_slow_recall_last_logged_at: float | None = None


@dataclass
class _CachedEmbeddingMatrix:
    matrices: dict[int, np.ndarray]
    row_ids_by_dimension: dict[int, list[str]]

    def similarities(
        self,
        rows: list[dict],
        query_embedding: list[float] | None,
    ) -> list[float]:
        if not rows or query_embedding is None:
            return [0.0 for _row in rows]
        try:
            query = np.asarray(query_embedding, dtype=np.float32)
        except (TypeError, ValueError):
            return [0.0 for _row in rows]
        query_norm = np.linalg.norm(query)
        matrix = self.matrices.get(int(query.size))
        row_ids = self.row_ids_by_dimension.get(int(query.size), [])
        if query.size == 0 or query_norm == 0 or matrix is None or matrix.size == 0:
            return [0.0 for _row in rows]
        values = np.clip(matrix @ (query / query_norm), -1.0, 1.0)
        by_id = {
            row_id: float(value)
            for row_id, value in zip(row_ids, values)
        }
        return [
            by_id.get(str(row.get("id") or ""), 0.0)
            for row in rows
        ]


@dataclass
class _FullCorpusCacheEntry:
    chunks: list[dict]
    notes: list[dict]
    ai_notes: list[dict]
    chunk_matrix: _CachedEmbeddingMatrix
    note_matrix: _CachedEmbeddingMatrix
    ai_note_matrix: _CachedEmbeddingMatrix
    include_cards: bool
    ai_note_lane_enabled: bool
    built_at: float
    dirty_chunk_conversations: set[str] = field(default_factory=set)
    chunks_dirty: bool = False
    notes_dirty: bool = False
    dirty_revision: int = 0
    background_refresh_task: asyncio.Task | None = field(default=None, repr=False)
    background_refresh_not_before: float = 0.0


_full_corpus_cache: dict[tuple[bool, bool, str], _FullCorpusCacheEntry] = {}
_full_corpus_cache_locks: "weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, asyncio.Lock]" = (
    weakref.WeakKeyDictionary()
)


def _full_corpus_cache_lock() -> asyncio.Lock:
    loop = asyncio.get_running_loop()
    lock = _full_corpus_cache_locks.get(loop)
    if lock is None:
        lock = asyncio.Lock()
        _full_corpus_cache_locks[loop] = lock
    return lock


def invalidate_full_corpus_cache(
    *,
    chunk_conv_id: str | None = None,
    chunks: bool = False,
    notes: bool = False,
) -> None:
    """Mark cached lanes dirty after an in-process memory write.

    Conversation chunk changes are refreshed incrementally.  Card-wide or
    note-wide changes rebuild only their affected lane on the next recall.
    A TTL remains as a fallback for writes that bypass MemoryService.
    """

    normalized_conv = str(chunk_conv_id or "").strip()
    for entry in _full_corpus_cache.values():
        changed = False
        if normalized_conv:
            entry.dirty_chunk_conversations.add(normalized_conv)
            changed = True
        if chunks:
            entry.chunks_dirty = True
            changed = True
        if notes:
            entry.notes_dirty = True
            changed = True
        if changed:
            entry.dirty_revision += 1


def clear_full_corpus_cache() -> None:
    """Clear process-local recall rows/matrices (tests and explicit rollback)."""

    for entry in _full_corpus_cache.values():
        task = entry.background_refresh_task
        if task is not None and not task.done():
            task.cancel()
    _full_corpus_cache.clear()


def _candidate_window_state(lane: str, returned: int, limit: int) -> dict:
    """Expose bounded-fetch saturation without claiming definite truncation."""
    normalized_limit = max(int(limit), 0)
    normalized_returned = max(int(returned), 0)
    saturated = normalized_limit > 0 and normalized_returned >= normalized_limit
    if saturated:
        now = time.monotonic()
        last_logged = _candidate_window_last_logged.get(lane)
        if (
            last_logged is None
            or now - last_logged >= CANDIDATE_WINDOW_LOG_INTERVAL_SECONDS
        ):
            logger.warning(
                "memory candidate window saturated lane=%s returned=%d limit=%d; "
                "older rows may be outside the scoring pool",
                lane,
                normalized_returned,
                normalized_limit,
            )
            _candidate_window_last_logged[lane] = now
    return {
        "returned": normalized_returned,
        "limit": normalized_limit,
        "saturated": saturated,
    }


def _maybe_warn_slow_recall(
    elapsed_seconds: float,
    *,
    chunks: int,
    notes: int,
    ai_notes: int,
    scope: str,
) -> bool:
    """Rate-limit the exact-scan cache trigger without adding a DB metric."""

    global _slow_recall_last_logged_at
    elapsed = max(float(elapsed_seconds), 0.0)
    if elapsed < SLOW_RECALL_WARNING_SECONDS:
        return False
    now = time.monotonic()
    if (
        _slow_recall_last_logged_at is not None
        and now - _slow_recall_last_logged_at < SLOW_RECALL_LOG_INTERVAL_SECONDS
    ):
        return False
    logger.warning(
        "memory recall slow elapsed_ms=%.1f chunks=%d notes=%d ai_notes=%d scope=%s; "
        "matrix cache trigger is 300ms",
        elapsed * 1000,
        max(int(chunks), 0),
        max(int(notes), 0),
        max(int(ai_notes), 0),
        scope,
    )
    _slow_recall_last_logged_at = now
    return True


def _safe_json(value: str | None, default):
    if not value:
        return default
    try:
        parsed = json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return default
    return parsed


def _one_line(value: Any) -> str:
    return " ".join(str(value or "").split())


def _terms(query_text: str, keywords: list[str] | None) -> list[str]:
    seen: set[str] = set()
    terms: list[str] = []
    for raw in [*(keywords or []), *extract_keywords(query_text, limit=12)]:
        term = str(raw or "").strip()
        key = term.lower()
        if len(key) < 2 or key in seen:
            continue
        seen.add(key)
        terms.append(term)
        if len(terms) >= 16:
            break
    return terms


def _keyword_score_prepared(
    terms: list[str],
    keyword_text: str,
    content_lower: str,
) -> tuple[float, list[str]]:
    if not terms:
        return 0.0, []
    hits: list[str] = []
    for term in terms:
        lowered = term.lower()
        if not lowered:
            continue
        if lowered in keyword_text or lowered in content_lower:
            hits.append(term)
            continue
        if any(part and part in content_lower for part in lowered.split()):
            hits.append(term)
    unique_hits = list(dict.fromkeys(hits))
    return min(len(unique_hits) / max(len(terms), 1), 1.0), unique_hits


def _keyword_score(terms: list[str], item_keywords: list[str], content: str) -> tuple[float, list[str]]:
    return _keyword_score_prepared(
        terms,
        " ".join(str(item).lower() for item in item_keywords),
        (content or "").lower(),
    )


def _recency_score(ts: float | None) -> float:
    if not ts:
        return 0.0
    try:
        days = max((time.time() - float(ts)) / 86400, 0.0)
    except (TypeError, ValueError):
        return 0.0
    return 1 / (1 + math.log1p(days))


def _recent_used_penalty(last_used_at: float | None) -> float:
    if not last_used_at:
        return 0.0
    try:
        age = time.time() - float(last_used_at)
    except (TypeError, ValueError):
        return 0.0
    if age < 30 * 60:
        return 0.18
    if age < 2 * 60 * 60:
        return 0.08
    return 0.0


def _embedding_array(blob):
    if not blob:
        return None
    try:
        if isinstance(blob, memoryview):
            blob = blob.tobytes()
        if not isinstance(blob, (bytes, bytearray)):
            return None
        vector = np.frombuffer(blob, dtype=np.float32)
    except (TypeError, ValueError):
        return None
    return vector if vector.size else None


def _batch_similarities(rows: list[dict], query_embedding: list[float] | None) -> list[float]:
    if not rows or not query_embedding:
        return [0.0 for _row in rows]
    query = np.asarray(query_embedding, dtype=np.float32)
    query_norm = np.linalg.norm(query)
    if query.size == 0 or query_norm == 0:
        return [0.0 for _row in rows]
    indexes: list[int] = []
    vectors = []
    for index, row in enumerate(rows):
        vector = _embedding_array(row.get("embedding"))
        if vector is None or vector.size != query.size:
            continue
        indexes.append(index)
        vectors.append(vector)
    scores = [0.0 for _row in rows]
    if not vectors:
        return scores
    matrix = np.vstack(vectors)
    norms = np.linalg.norm(matrix, axis=1)
    valid = norms > 0
    values = np.zeros(len(vectors), dtype=np.float32)
    values[valid] = (matrix[valid] @ query) / (norms[valid] * query_norm)
    values = np.clip(values, -1.0, 1.0)
    for index, value in zip(indexes, values):
        scores[index] = float(value)
    return scores


def _source_timestamp(row: dict) -> float:
    metadata = (
        row.get("_recall_metadata")
        if "_recall_metadata" in row
        else _safe_json(row.get("metadata_json"), {})
    )
    if not isinstance(metadata, dict):
        metadata = {}
    for value in (
        row.get("source_end_ts"),
        metadata.get("source_end_ts"),
        row.get("source_start_ts"),
        metadata.get("source_start_ts"),
        row.get("created_at"),
    ):
        try:
            timestamp = float(value or 0)
        except (TypeError, ValueError):
            continue
        if timestamp:
            return timestamp
    return 0.0


def _sort_rows_by_recency(rows: list[dict]) -> list[dict]:
    """Keep source-recency tie behavior without treating usage as event time."""

    def key(row: dict) -> tuple[float, str]:
        return _source_timestamp(row), str(row.get("id") or "")

    rows.sort(key=key, reverse=True)
    return rows


async def _fetch_chunks(limit: int | None, *, include_cards: bool = False) -> list[dict]:
    card_columns = (
        ", rc.id AS card_id, rc.version AS card_version, "
        "rc.content AS card_content, rc.prompt_version AS card_prompt_version"
        if include_cards
        else ""
    )
    readable_versions = tuple(sorted(READABLE_RELATIONAL_CARD_PROMPT_VERSIONS))
    version_placeholders = ",".join("?" for _ in readable_versions)
    card_join = (
        "LEFT JOIN memory_relational_cards rc "
        "ON rc.source_chunk_id=c.id AND rc.status='active' "
        f"AND rc.prompt_version IN ({version_placeholders}) "
        if include_cards
        else ""
    )
    params: list[Any] = list(readable_versions) if include_cards else []
    tail = ""
    if limit is not None:
        tail = "ORDER BY c.updated_at DESC LIMIT ?"
        params.append(max(int(limit), 0))
    async with get_db() as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT c.id, c.conv_id, c.message_ids_json, c.content, c.created_at, "
            "c.updated_at, c.source_hash, c.embedding, c.keywords_json, c.metadata_json "
            f"{card_columns} FROM memory_chunks c {card_join}"
            "WHERE TRIM(c.content) != '' AND c.status IN ('active','cold') "
            f"{tail}",
            params,
        )
        rows = await cur.fetchall()
        from app.image_memory.repository import recall_rows as image_recall_rows
        image_rows = await image_recall_rows(db)
    result = [dict(row) for row in rows] + image_rows
    if limit is None:
        return _sort_rows_by_recency(result)
    return _sort_rows_by_recency(result)[:limit] if image_rows else result


async def _fetch_notes(
    limit: int | None,
    *,
    origin_type: str | None = None,
    exclude_origin_types: set[str] | None = None,
) -> list[dict]:
    clauses = ["status='active'", "visibility='prompt'", "TRIM(content) != ''"]
    params: list = []
    if origin_type:
        clauses.append("origin_type=?")
        params.append(origin_type)
    excluded = sorted(str(value) for value in (exclude_origin_types or set()) if str(value))
    if excluded:
        placeholders = ",".join("?" for _ in excluded)
        clauses.append(f"origin_type NOT IN ({placeholders})")
        params.extend(excluded)
    tail = ""
    if limit is not None:
        tail = "ORDER BY COALESCE(source_end_ts, source_start_ts, created_at) DESC LIMIT ?"
        params.append(max(int(limit), 0))
    async with get_db() as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT * FROM memory_items "
            f"WHERE {' AND '.join(clauses)} "
            f"{tail}",
            params,
        )
        rows = await cur.fetchall()
    result = [dict(row) for row in rows]
    return _sort_rows_by_recency(result) if limit is None else result


async def _fetch_chunks_for_conversations(
    conv_ids: set[str],
    *,
    include_cards: bool,
) -> list[dict]:
    normalized = sorted(str(value) for value in conv_ids if str(value))
    if not normalized:
        return []
    conv_placeholders = ",".join("?" for _ in normalized)
    card_columns = (
        ", rc.id AS card_id, rc.version AS card_version, "
        "rc.content AS card_content, rc.prompt_version AS card_prompt_version"
        if include_cards
        else ""
    )
    readable_versions = tuple(sorted(READABLE_RELATIONAL_CARD_PROMPT_VERSIONS))
    version_placeholders = ",".join("?" for _ in readable_versions)
    card_join = (
        "LEFT JOIN memory_relational_cards rc "
        "ON rc.source_chunk_id=c.id AND rc.status='active' "
        f"AND rc.prompt_version IN ({version_placeholders}) "
        if include_cards
        else ""
    )
    params: list[Any] = [*(readable_versions if include_cards else ()), *normalized]
    async with get_db() as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT c.id, c.conv_id, c.message_ids_json, c.content, c.created_at, "
            "c.updated_at, c.source_hash, c.embedding, c.keywords_json, c.metadata_json "
            f"{card_columns} FROM memory_chunks c {card_join}"
            "WHERE TRIM(c.content) != '' AND c.status IN ('active','cold') "
            f"AND c.conv_id IN ({conv_placeholders})",
            params,
        )
        rows = await cur.fetchall()
        from app.image_memory.repository import recall_rows as image_recall_rows
        image_rows = await image_recall_rows(db, normalized)
    return _sort_rows_by_recency([dict(row) for row in rows] + image_rows)


def _prepare_cached_rows(rows: list[dict]) -> list[dict]:
    """Precompute immutable lexical/provenance fields once per cache generation."""

    for row in rows:
        keywords = _safe_json(row.get("keywords_json"), [])
        if not isinstance(keywords, list):
            keywords = []
        metadata = _safe_json(row.get("metadata_json"), {})
        source_ids = metadata.get("source_message_ids") if isinstance(metadata, dict) else None
        if isinstance(source_ids, list):
            normalized_source_ids = [str(item) for item in source_ids if str(item)]
        else:
            message_ids = _safe_json(row.get("message_ids_json"), [])
            normalized_source_ids = (
                [str(item) for item in message_ids]
                if isinstance(message_ids, list)
                else []
            )
        row["_recall_keywords"] = keywords
        row["_recall_keyword_text"] = " ".join(
            str(item).lower() for item in keywords
        )
        row["_recall_content_lower"] = str(row.get("content") or "").lower()
        row["_recall_metadata"] = metadata
        row["_recall_source_message_ids"] = normalized_source_ids
    return rows


def _cached_embedding_matrix(rows: list[dict]) -> _CachedEmbeddingMatrix:
    vectors_by_dimension: dict[int, list[np.ndarray]] = {}
    row_ids_by_dimension: dict[int, list[str]] = {}
    for row in rows:
        vector = _embedding_array(row.get("embedding"))
        if vector is None:
            continue
        norm = np.linalg.norm(vector)
        if norm <= 0:
            continue
        dimensions = int(vector.size)
        vectors_by_dimension.setdefault(dimensions, []).append(
            np.asarray(vector / norm, dtype=np.float32)
        )
        row_ids_by_dimension.setdefault(dimensions, []).append(
            str(row.get("id") or "")
        )
    return _CachedEmbeddingMatrix(
        matrices={
            dimensions: np.vstack(vectors)
            for dimensions, vectors in vectors_by_dimension.items()
        },
        row_ids_by_dimension=row_ids_by_dimension,
    )


def _refresh_entry_matrices(entry: _FullCorpusCacheEntry) -> None:
    entry.chunk_matrix = _cached_embedding_matrix(entry.chunks)
    entry.note_matrix = _cached_embedding_matrix(entry.notes)
    entry.ai_note_matrix = _cached_embedding_matrix(entry.ai_notes)


async def _build_full_corpus_cache_entry(
    *,
    include_cards: bool,
    ai_note_lane_enabled: bool,
) -> _FullCorpusCacheEntry:
    chunks = await _fetch_chunks(None, include_cards=True) if include_cards else await _fetch_chunks(None)
    excluded_origins = {"ai_note"} if ai_note_lane_enabled else set()
    notes = (
        await _fetch_notes(None, exclude_origin_types=excluded_origins)
        if excluded_origins
        else await _fetch_notes(None)
    )
    ai_notes = (
        await _fetch_notes(None, origin_type="ai_note")
        if ai_note_lane_enabled
        else []
    )
    _prepare_cached_rows(chunks)
    _prepare_cached_rows(notes)
    _prepare_cached_rows(ai_notes)
    entry = _FullCorpusCacheEntry(
        chunks=chunks,
        notes=notes,
        ai_notes=ai_notes,
        chunk_matrix=_CachedEmbeddingMatrix({}, {}),
        note_matrix=_CachedEmbeddingMatrix({}, {}),
        ai_note_matrix=_CachedEmbeddingMatrix({}, {}),
        include_cards=include_cards,
        ai_note_lane_enabled=ai_note_lane_enabled,
        built_at=time.monotonic(),
    )
    _refresh_entry_matrices(entry)
    return entry


async def _refresh_full_corpus_cache_entry(entry: _FullCorpusCacheEntry) -> None:
    revision = entry.dirty_revision
    dirty_conversations = set(entry.dirty_chunk_conversations)
    chunks_dirty = bool(entry.chunks_dirty)
    notes_dirty = bool(entry.notes_dirty)
    if chunks_dirty:
        entry.chunks = (
            await _fetch_chunks(None, include_cards=True)
            if entry.include_cards
            else await _fetch_chunks(None)
        )
        _prepare_cached_rows(entry.chunks)
    elif dirty_conversations:
        fresh = await _fetch_chunks_for_conversations(
            dirty_conversations,
            include_cards=entry.include_cards,
        )
        _prepare_cached_rows(fresh)
        entry.chunks = [
            row
            for row in entry.chunks
            if str(row.get("conv_id") or "") not in dirty_conversations
        ] + fresh
    if notes_dirty:
        excluded_origins = {"ai_note"} if entry.ai_note_lane_enabled else set()
        entry.notes = (
            await _fetch_notes(None, exclude_origin_types=excluded_origins)
            if excluded_origins
            else await _fetch_notes(None)
        )
        entry.ai_notes = (
            await _fetch_notes(None, origin_type="ai_note")
            if entry.ai_note_lane_enabled
            else []
        )
        _prepare_cached_rows(entry.notes)
        _prepare_cached_rows(entry.ai_notes)
    if chunks_dirty or dirty_conversations:
        entry.chunk_matrix = _cached_embedding_matrix(entry.chunks)
    if notes_dirty:
        entry.note_matrix = _cached_embedding_matrix(entry.notes)
        entry.ai_note_matrix = _cached_embedding_matrix(entry.ai_notes)
    # Keep ``built_at`` anchored to the last complete rebuild.  Moving it on
    # every known in-process write could postpone the TTL forever and leave
    # direct/external writes stale indefinitely.
    if entry.dirty_revision == revision:
        entry.dirty_chunk_conversations.difference_update(dirty_conversations)
        if chunks_dirty:
            entry.chunks_dirty = False
        if notes_dirty:
            entry.notes_dirty = False


async def _rebuild_full_corpus_cache_in_background(
    key: tuple[bool, bool, str],
    expected_entry: _FullCorpusCacheEntry,
    *,
    include_cards: bool,
    ai_note_lane_enabled: bool,
    starting_revision: int,
) -> None:
    """Rebuild an expired entry without charging the triggering user turn."""

    fresh: _FullCorpusCacheEntry | None = None
    try:
        fresh = await _build_full_corpus_cache_entry(
            include_cards=include_cards,
            ai_note_lane_enabled=ai_note_lane_enabled,
        )
    except asyncio.CancelledError:
        return
    except Exception:
        logger.exception("memory recall background cache rebuild failed")

    async with _full_corpus_cache_lock():
        current = _full_corpus_cache.get(key)
        if current is not expected_entry:
            return
        current.background_refresh_task = None
        if fresh is None:
            current.background_refresh_not_before = (
                time.monotonic() + FULL_CORPUS_CACHE_RETRY_SECONDS
            )
            return
        # A known write raced the snapshot.  Keep the current entry (which the
        # normal dirty path may already have refreshed) instead of replacing it
        # with a possibly stale generation; retry on a later turn.
        if current.dirty_revision != starting_revision:
            current.background_refresh_not_before = time.monotonic() + 1.0
            return
        _full_corpus_cache[key] = fresh


async def _full_corpus_candidates(
    *,
    include_cards: bool,
    ai_note_lane_enabled: bool,
) -> _FullCorpusCacheEntry:
    key = (
        bool(include_cards),
        bool(ai_note_lane_enabled),
        embedding.embedding_signature(),
    )
    async with _full_corpus_cache_lock():
        entry = _full_corpus_cache.get(key)
        if entry is None:
            entry = await _build_full_corpus_cache_entry(
                include_cards=include_cards,
                ai_note_lane_enabled=ai_note_lane_enabled,
            )
            _full_corpus_cache[key] = entry
            return entry
        if entry.chunks_dirty or entry.notes_dirty or entry.dirty_chunk_conversations:
            await _refresh_full_corpus_cache_entry(entry)
        now = time.monotonic()
        expired = now - entry.built_at >= FULL_CORPUS_CACHE_TTL_SECONDS
        refresh_task = entry.background_refresh_task
        if (
            expired
            and now >= entry.background_refresh_not_before
            and (refresh_task is None or refresh_task.done())
        ):
            entry.background_refresh_task = create_tracked_task(
                _rebuild_full_corpus_cache_in_background(
                    key,
                    entry,
                    include_cards=include_cards,
                    ai_note_lane_enabled=ai_note_lane_enabled,
                    starting_revision=entry.dirty_revision,
                ),
                name="memory-full-corpus-cache-refresh",
            )
        return entry


async def _recent_usage(ids: list[str]) -> dict[str, float]:
    if not ids:
        return {}
    usage: dict[str, float] = {}
    async with get_db() as db:
        db.row_factory = aiosqlite.Row
        for start in range(0, len(ids), 500):
            batch = ids[start: start + 500]
            placeholders = ",".join("?" for _id in batch)
            db.row_factory = aiosqlite.Row
            cur = await db.execute(
                "SELECT memory_id, MAX(used_at) AS used_at FROM memory_usage "
                f"WHERE memory_id IN ({placeholders}) GROUP BY memory_id",
                batch,
            )
            rows = await cur.fetchall()
            usage.update({row["memory_id"]: float(row["used_at"] or 0) for row in rows})
    return usage


def _source_message_ids(row: dict) -> list[str]:
    if "_recall_source_message_ids" in row:
        return list(row.get("_recall_source_message_ids") or [])
    metadata = _safe_json(row.get("metadata_json"), {})
    ids = metadata.get("source_message_ids") if isinstance(metadata, dict) else None
    if isinstance(ids, list):
        return [str(item) for item in ids if str(item)]
    parsed = _safe_json(row.get("message_ids_json"), [])
    return [str(item) for item in parsed] if isinstance(parsed, list) else []


def _exclude_visible_sources(
    rows: list[dict],
    visible_message_ids: set[str],
) -> tuple[list[dict], int]:
    """Remove candidates backed by any message already visible to the model."""

    if not rows or not visible_message_ids:
        return rows, 0
    eligible: list[dict] = []
    excluded = 0
    for row in rows:
        source_ids = set(_source_message_ids(row))
        if source_ids & visible_message_ids and row.get("source_type") != "image":
            excluded += 1
            continue
        eligible.append(row)
    return eligible, excluded


def _source_key(item: dict) -> str:
    if item.get("source_type") == "image":
        return "image:" + str(item.get("source_message_ids")) + ":" + str(item.get("attachment_url"))
    ids = item.get("source_message_ids") or []
    if ids:
        return "messages:" + ",".join(ids)
    start = item.get("source_start_ts")
    end = item.get("source_end_ts")
    conv_id = item.get("source_conv") or item.get("conv_id") or ""
    if start and end:
        return f"range:{conv_id}:{round(float(start), 3)}:{round(float(end), 3)}"
    return f"id:{item.get('id')}"


def _content_similarity(a: str, b: str) -> float:
    a_text = _one_line(a).lower()
    b_text = _one_line(b).lower()
    if not a_text or not b_text:
        return 0.0
    if a_text in b_text or b_text in a_text:
        return 1.0
    a_tokens = set(extract_keywords(a_text, limit=64))
    b_tokens = set(extract_keywords(b_text, limit=64))
    if not a_tokens or not b_tokens:
        return 0.0
    return len(a_tokens & b_tokens) / len(a_tokens | b_tokens)


def _specificity(item: dict) -> float:
    content_len = min(len(item.get("content") or "") / 800, 1.0)
    source_bonus = 0.15 if item.get("source_type") == "chunk" else 0.0
    ids_bonus = min(len(item.get("source_message_ids") or []) / 6, 1.0) * 0.1
    return content_len + source_bonus + ids_bonus


def _better_item(new_item: dict, old_item: dict) -> dict:
    new_value = float(new_item.get("score") or 0) + _specificity(new_item) * 0.08
    old_value = float(old_item.get("score") or 0) + _specificity(old_item) * 0.08
    return new_item if new_value > old_value else old_item


def _dedupe(items: list[dict]) -> list[dict]:
    by_source: dict[str, dict] = {}
    for item in items:
        key = _source_key(item)
        if key in by_source:
            by_source[key] = _better_item(item, by_source[key])
        else:
            by_source[key] = item
    result: list[dict] = []
    for item in sorted(by_source.values(), key=lambda value: value["score"], reverse=True):
        duplicate_index = None
        for index, existing in enumerate(result):
            if item.get("source_type") == "image" or existing.get("source_type") == "image":
                continue
            if _content_similarity(item.get("content") or "", existing.get("content") or "") >= 0.82:
                duplicate_index = index
                break
        if duplicate_index is None:
            result.append(item)
        else:
            result[duplicate_index] = _better_item(item, result[duplicate_index])
            result.sort(key=lambda value: value["score"], reverse=True)
    return result


def _score_chunk(
    row: dict,
    terms: list[str],
    semantic_similarity: float,
    last_used_at: float | None,
    *,
    card_readout_mode: str = "current_raw",
) -> dict:
    if "_recall_keywords" in row:
        keywords = row.get("_recall_keywords") or []
        kw_score, hits = _keyword_score_prepared(
            terms,
            str(row.get("_recall_keyword_text") or ""),
            str(row.get("_recall_content_lower") or ""),
        )
    else:
        keywords = _safe_json(row.get("keywords_json"), [])
        if not isinstance(keywords, list):
            keywords = []
        kw_score, hits = _keyword_score(terms, keywords, row.get("content") or "")
    semantic_positive = max(semantic_similarity, 0.0)
    recency = _recency_score(_source_timestamp(row))
    penalty = _recent_used_penalty(last_used_at)
    score = semantic_positive * 0.78 + kw_score * 0.14 + recency * 0.05 + 0.02 - penalty
    metadata = (
        row.get("_recall_metadata")
        if "_recall_metadata" in row
        else _safe_json(row.get("metadata_json"), {})
    )
    card_content = str(row.get("card_content") or "").strip()
    use_card = card_readout_mode == "card_preferred" and bool(card_content)
    readout_type = (
        "relational_card"
        if use_card
        else "raw_full"
        if card_readout_mode == "raw_full_fallback"
        else "raw"
    )
    item = {
        "id": row["id"],
        "candidate_id": row["id"],
        "source_type": "chunk",
        "content": row.get("content") or "",
        "raw_content": row.get("content") or "",
        "kind": "raw_chunk",
        "lane": "ordinary",
        "readout_type": readout_type,
        "card_id": row.get("card_id"),
        "card_version": row.get("card_version"),
        "namespace": "normal",
        "conv_id": row.get("conv_id"),
        "source_conv": row.get("conv_id"),
        "source_message_ids": _source_message_ids(row),
        "source_start_ts": metadata.get("source_start_ts") if isinstance(metadata, dict) else row.get("created_at"),
        "source_end_ts": metadata.get("source_end_ts") if isinstance(metadata, dict) else row.get("updated_at"),
        "created_at": row.get("created_at"),
        "updated_at": row.get("updated_at"),
        "score": round(max(score, 0.0), 4),
        "relevance": round(max(semantic_positive, kw_score), 4),
        "semantic_similarity": round(semantic_similarity, 4),
        "keyword_relevance": round(kw_score, 4),
        "cooldown_penalty": round(penalty, 4),
        "importance": 0.0,
        "confidence": 1.0,
        "reason": "; ".join(["chunk", *(f"keyword:{hit}" for hit in hits[:4]), "cooldown" if penalty else ""]).strip("; "),
    }
    if row.get("source_type") == "image":
        item.update(source_type="image", kind="image_observation", readout_type="image_observation",
                    attachment_url=row["attachment_url"])
    if use_card:
        item["preview"] = card_content
        item["reason"] = "; ".join(
            part for part in (item["reason"], "card_readout") if part
        )
    return item


def _score_note(
    row: dict,
    terms: list[str],
    semantic_similarity: float,
    last_used_at: float | None,
    *,
    as_ai_note: bool = False,
) -> dict:
    if "_recall_keywords" in row:
        keywords = row.get("_recall_keywords") or []
        kw_score, hits = _keyword_score_prepared(
            terms,
            str(row.get("_recall_keyword_text") or ""),
            str(row.get("_recall_content_lower") or ""),
        )
    else:
        keywords = _safe_json(row.get("keywords_json"), [])
        if not isinstance(keywords, list):
            keywords = []
        kw_score, hits = _keyword_score(terms, keywords, row.get("content") or "")
    semantic_positive = max(semantic_similarity, 0.0)
    recency = _recency_score(_source_timestamp(row))
    penalty = _recent_used_penalty(last_used_at or row.get("last_used_at"))
    try:
        importance = float(row.get("importance") if row.get("importance") is not None else 0.5)
    except (TypeError, ValueError):
        importance = 0.5
    score = semantic_positive * 0.74 + kw_score * 0.14 + recency * 0.04 + importance * 0.03 - penalty
    origin_type = str(row.get("origin_type") or "legacy")
    is_ai_note = bool(as_ai_note)
    return {
        "id": row["id"],
        "candidate_id": row["id"],
        "legacy_memory_id": row.get("legacy_memory_id"),
        "source_type": "ai_note" if is_ai_note else "note",
        "origin_type": origin_type,
        "lane": "ai_note" if is_ai_note else "ordinary",
        "content": row.get("content") or "",
        "kind": row.get("kind") or "episode",
        "namespace": row.get("namespace") or "normal",
        "source_conv": row.get("source_conv"),
        "source_message_ids": _source_message_ids(row),
        "source_start_ts": row.get("source_start_ts"),
        "source_end_ts": row.get("source_end_ts"),
        "created_at": row.get("created_at"),
        "updated_at": row.get("updated_at"),
        "score": round(max(score, 0.0), 4),
        "relevance": round(max(semantic_positive, kw_score), 4),
        "semantic_similarity": round(semantic_similarity, 4),
        "keyword_relevance": round(kw_score, 4),
        "cooldown_penalty": round(penalty, 4),
        "importance": round(importance, 2),
        "confidence": row.get("confidence"),
        "reason": "; ".join(["note", *(f"keyword:{hit}" for hit in hits[:4]), "cooldown" if penalty else ""]).strip("; "),
    }


async def hybrid_recall(
    query_text: str,
    keywords: list[str] | None = None,
    *,
    top_k: int = DEFAULT_TOP_K,
    chunk_top_k: int = DEFAULT_CHUNK_TOP_K,
    note_top_k: int = DEFAULT_NOTE_TOP_K,
    candidate_limit: int = DEFAULT_CANDIDATE_LIMIT,
    min_score: float = DEFAULT_MIN_SCORE,
    slot_min_score: float | None = None,
    visible_message_ids: list[str] | None = None,
    full_corpus: bool = False,
    relational_cards_enabled: bool = False,
    card_readout_mode: str = "current_raw",
    ai_note_lane_enabled: bool = False,
    ai_note_top_k: int = 3,
    ai_note_max_items: int = 2,
    **_ignored,
) -> dict:
    query_text = _one_line(query_text)
    terms = _terms(query_text, keywords)
    recall_work_started = time.perf_counter()
    embedding_elapsed = 0.0
    scan_limit = None if full_corpus else max(int(candidate_limit), 0)
    diagnostic_limit = 0 if scan_limit is None else scan_limit
    effective_card_mode = card_readout_mode if relational_cards_enabled else "current_raw"
    cache_entry: _FullCorpusCacheEntry | None = None
    ai_note_lane_active = bool(
        ai_note_lane_enabled and ai_note_top_k > 0 and ai_note_max_items > 0
    )
    if full_corpus:
        cache_entry = await _full_corpus_candidates(
            include_cards=effective_card_mode == "card_preferred",
            # Keep AI notes out of ordinary recall whenever the lane is
            # enabled, even if its configured quota is currently zero.  This
            # matches the bounded path's lane partitioning.
            ai_note_lane_enabled=bool(ai_note_lane_enabled),
        )
        chunks = list(cache_entry.chunks)
        notes = list(cache_entry.notes)
        ai_notes = list(cache_entry.ai_notes) if ai_note_lane_active else []
        # Hold one immutable matrix generation for this recall.  Another chat
        # may invalidate and refresh the shared entry while this coroutine is
        # awaiting usage or query-embedding I/O.
        chunk_matrix = cache_entry.chunk_matrix
        note_matrix = cache_entry.note_matrix
        ai_note_matrix = cache_entry.ai_note_matrix
    else:
        chunk_matrix = None
        note_matrix = None
        ai_note_matrix = None
        chunks = (
            await _fetch_chunks(scan_limit, include_cards=True)
            if effective_card_mode == "card_preferred"
            else await _fetch_chunks(scan_limit)
        )
        excluded_origins: set[str] = set()
        if ai_note_lane_enabled:
            excluded_origins.add("ai_note")
        notes = (
            await _fetch_notes(scan_limit, exclude_origin_types=excluded_origins)
            if excluded_origins
            else await _fetch_notes(scan_limit)
        )
        ai_notes = (
            await _fetch_notes(scan_limit, origin_type="ai_note")
            if ai_note_lane_active
            else []
        )
    candidate_windows = {
        "chunks": _candidate_window_state(
            "ordinary_chunks", len(chunks), diagnostic_limit
        ),
        "notes": _candidate_window_state(
            "ordinary_notes", len(notes), diagnostic_limit
        ),
        "ai_notes": _candidate_window_state(
            "ai_notes", len(ai_notes), diagnostic_limit
        ),
    }
    visible_ids = {
        str(value)
        for value in (visible_message_ids or [])
        if str(value)
    }
    chunks, excluded_chunks = _exclude_visible_sources(chunks, visible_ids)
    notes, excluded_notes = _exclude_visible_sources(notes, visible_ids)
    ai_notes, excluded_ai_notes = _exclude_visible_sources(ai_notes, visible_ids)
    visible_source_excluded = {
        "chunks": excluded_chunks,
        "notes": excluded_notes,
        "ai_notes": excluded_ai_notes,
        "total": excluded_chunks + excluded_notes + excluded_ai_notes,
    }
    ids = (
        [row["id"] for row in chunks]
        + [row["id"] for row in notes]
        + [row["id"] for row in ai_notes]
    )
    recent_usage = await _recent_usage(ids)

    query_embedding = None
    if query_text and (chunks or notes or ai_notes):
        embedding_started = time.perf_counter()
        try:
            query_embedding = await embedding.get_embedding(query_text)
        except Exception:
            query_embedding = None
        finally:
            embedding_elapsed = time.perf_counter() - embedding_started

    if cache_entry is not None:
        chunk_sims = chunk_matrix.similarities(chunks, query_embedding)
        note_sims = note_matrix.similarities(notes, query_embedding)
        ai_note_sims = ai_note_matrix.similarities(ai_notes, query_embedding)
    else:
        chunk_sims = _batch_similarities(chunks, query_embedding)
        note_sims = _batch_similarities(notes, query_embedding)
        ai_note_sims = _batch_similarities(ai_notes, query_embedding)
    scored_chunks = [
        _score_chunk(
            row,
            terms,
            chunk_sims[index],
            recent_usage.get(row["id"]),
            card_readout_mode=effective_card_mode,
        )
        for index, row in enumerate(chunks)
    ]
    scored_notes = [
        _score_note(row, terms, note_sims[index], recent_usage.get(row["id"]))
        for index, row in enumerate(notes)
    ]
    scored_ai_notes = [
        _score_note(
            row,
            terms,
            ai_note_sims[index],
            recent_usage.get(row["id"]),
            as_ai_note=True,
        )
        for index, row in enumerate(ai_notes)
    ]
    scored_chunks.sort(key=lambda item: item["score"], reverse=True)
    scored_notes.sort(key=lambda item: item["score"], reverse=True)
    scored_ai_notes.sort(key=lambda item: item["score"], reverse=True)

    wide = [*scored_chunks[:chunk_top_k], *scored_notes[:note_top_k]]
    deduped = _dedupe(wide)
    threshold = float(min_score if query_embedding else min(min_score, 0.05))
    ordinary_candidates = [
        item for item in deduped
        if item["score"] >= threshold or item.get("keyword_relevance", 0) >= 0.35
    ]
    ai_candidates = [
        item for item in scored_ai_notes[: max(int(ai_note_top_k), 0)]
        if item["score"] >= threshold or item.get("keyword_relevance", 0) >= 0.35
    ]
    semantic_available = bool(query_embedding)
    final_min_score = float(min_score if slot_min_score is None else slot_min_score)
    ordinary_selected = [
        item
        for item in ordinary_candidates
        if prompt_eligible(
            item,
            min_score=final_min_score,
            semantic_available=semantic_available,
        )
    ]
    ai_selected = [
        item
        for item in ai_candidates
        if prompt_eligible(
            item,
            min_score=final_min_score,
            semantic_available=semantic_available,
        )
    ][: max(min(int(ai_note_max_items), int(top_k)), 0)]
    ordinary_limit = max(int(top_k) - len(ai_selected), 0)
    selected = [*ordinary_selected[:ordinary_limit], *ai_selected]
    selected.sort(key=lambda item: item["score"], reverse=True)
    ordinary_debug = _dedupe(
        [
            *scored_chunks[: max(chunk_top_k, 12)],
            *scored_notes[: max(note_top_k, 12)],
        ]
    )[: max(top_k, 12)]
    debug_top = [*ordinary_debug, *scored_ai_notes[: max(ai_note_top_k, 3)]]
    debug_top.sort(key=lambda item: item["score"], reverse=True)
    debug_top = debug_top[: max(top_k, 12)]
    abstain_reason = None
    if not selected:
        if not chunks and not notes and not ai_notes:
            abstain_reason = "no_candidates"
        elif not query_embedding and not terms:
            abstain_reason = "no_query_signal"
        else:
            abstain_reason = "below_min_score"

    non_embedding_elapsed = max(
        time.perf_counter() - recall_work_started - embedding_elapsed,
        0.0,
    )
    slow_warning_emitted = _maybe_warn_slow_recall(
        non_embedding_elapsed,
        chunks=len(chunks),
        notes=len(notes),
        ai_notes=len(ai_notes),
        scope="full_corpus" if full_corpus else "bounded_window",
    )

    return {
        "query": query_text,
        "keywords": keywords or [],
        "terms": terms,
        "retrieval_mode": (
            "hybrid_chunk_note_ai_note" if ai_note_lane_enabled else "hybrid_chunk_note"
        ),
        "card_readout_mode": effective_card_mode,
        "turn_plan": {
            "mode": "normal",
            "namespace": "all",
            "detected_namespaces": [],
            "preferred_kinds": [],
            "needs_memory": bool(query_text),
            "emotion": "",
            "terms": terms,
        },
        "allowed_namespaces": ["all"],
        "candidate_count": len(chunks) + len(notes) + len(ai_notes),
        "chunk_candidate_count": len(chunks),
        "note_candidate_count": len(notes),
        "ai_note_candidate_count": len(ai_notes),
        "ai_note_selected_count": len(ai_selected),
        "candidate_windows": candidate_windows,
        "candidate_scope": "full_corpus" if full_corpus else "bounded_window",
        "candidate_cache": {
            "enabled": cache_entry is not None,
            "ttl_seconds": FULL_CORPUS_CACHE_TTL_SECONDS if cache_entry is not None else 0,
        },
        "visible_message_id_count": len(visible_ids),
        "visible_source_excluded": visible_source_excluded,
        "slot_min_score": final_min_score,
        "recall_timing": {
            "non_embedding_ms": round(non_embedding_elapsed * 1000, 2),
            "query_embedding_ms": round(embedding_elapsed * 1000, 2),
            "slow_warning_emitted": slow_warning_emitted,
        },
        "semantic_query": bool(query_embedding),
        "selected": selected,
        "debug_top": debug_top,
        "abstain_reason": abstain_reason,
    }


async def wide_chunk_recall(
    query_text: str,
    *,
    top_k: int,
    candidate_limit: int,
    as_of_ts: float,
    exclude_message_id: str | None = None,
    relational_cards_enabled: bool = False,
    full_corpus_enabled: bool = True,
    ai_note_lane_enabled: bool = False,
) -> list[dict]:
    """Return a raw-embedding candidate snapshot for pending recall.

    This deliberately does not apply the ordinary prompt threshold.  The
    downstream selector is allowed to return none.  Cards are presentation
    readouts only; ranking always uses the source chunk embedding/content.
    """
    normalized_query = _one_line(query_text)
    if not normalized_query or int(top_k) <= 0:
        return []
    chunk_matrix = None
    if full_corpus_enabled:
        entry = await _full_corpus_candidates(
            include_cards=bool(relational_cards_enabled),
            ai_note_lane_enabled=bool(ai_note_lane_enabled),
        )
        # 在下一次等待之前固定同一代记录与矩阵；并发更新不会混用两代。
        rows = list(entry.chunks)
        chunk_matrix = entry.chunk_matrix
    else:
        limit = max(int(candidate_limit), int(top_k))
        rows = await _fetch_chunks(limit, include_cards=bool(relational_cards_enabled))
        _candidate_window_state("pending_chunks", len(rows), limit)
    eligible: list[dict] = []
    for row in rows:
        metadata = _safe_json(row.get("metadata_json"), {})
        source_end = (
            metadata.get("source_end_ts")
            if isinstance(metadata, dict)
            else None
        )
        try:
            source_end_ts = float(source_end or row.get("updated_at") or 0)
        except (TypeError, ValueError):
            source_end_ts = 0.0
        if source_end_ts > float(as_of_ts):
            continue
        message_ids = _source_message_ids(row)
        if exclude_message_id and exclude_message_id in message_ids:
            continue
        eligible.append(row)
    if not eligible:
        return []

    query_embedding = await embedding.get_embedding(normalized_query)
    if not query_embedding:
        raise RuntimeError("pending recall query embedding unavailable")
    terms = _terms(normalized_query, None)
    similarities = (
        chunk_matrix.similarities(eligible, query_embedding)
        if chunk_matrix is not None
        else _batch_similarities(eligible, query_embedding)
    )
    recent_usage = await _recent_usage([str(row["id"]) for row in eligible])
    scored = [
        _score_chunk(
            row,
            terms,
            similarities[index],
            recent_usage.get(str(row["id"])),
            card_readout_mode=(
                "card_preferred" if relational_cards_enabled else "current_raw"
            ),
        )
        for index, row in enumerate(eligible)
    ]
    scored.sort(key=lambda item: item["score"], reverse=True)
    source_by_id = {str(row["id"]): row for row in eligible}
    result: list[dict] = []
    for rank, item in enumerate(scored[: max(int(top_k), 0)], 1):
        row = source_by_id[str(item["id"])]
        raw_content = str(item.get("raw_content") or item.get("content") or "")
        card_content = str(row.get("card_content") or "").strip()
        readout_text = card_content if card_content else _clip_for_selector(raw_content, 600)
        result.append(
            {
                "candidate_id": str(item["id"]),
                "source_chunk_id": str(item["id"]),
                "source_type": item.get("source_type", "chunk"),
                "attachment_url": item.get("attachment_url"),
                "source_hash": str(row.get("source_hash") or ""),
                "source_message_ids": list(item.get("source_message_ids") or []),
                "source_start_ts": item.get("source_start_ts"),
                "source_end_ts": item.get("source_end_ts"),
                "raw_content": raw_content,
                "readout_text": readout_text,
                "readout_type": "image_observation" if item.get("source_type") == "image" else "relational_card" if card_content else "raw_excerpt",
                "card_id": row.get("card_id"),
                "card_version": row.get("card_version"),
                "score": item.get("score"),
                "semantic_similarity": item.get("semantic_similarity"),
                "keyword_relevance": item.get("keyword_relevance"),
                "cooldown_penalty": item.get("cooldown_penalty"),
                "rank": rank,
            }
        )
    return result


def _clip_for_selector(text: str, limit: int) -> str:
    normalized = _one_line(text)
    if len(normalized) <= limit:
        return normalized
    return normalized[: max(limit - 3, 0)] + "..."


def merge_pending_items(
    plan: dict,
    pending_items: list[dict] | None,
    *,
    top_k: int,
) -> dict:
    """Merge selector-approved pending items ahead of ordinary recall by chunk id."""
    pending = [item for item in (pending_items or []) if isinstance(item, dict)]
    if not pending:
        return plan
    result = dict(plan)
    ordinary = [item for item in (plan.get("selected") or []) if isinstance(item, dict)]
    ordinary_by_id = {
        str(item.get("candidate_id") or item.get("id") or ""): item
        for item in ordinary
    }
    merged_pending: list[dict] = []
    pending_ids: set[str] = set()
    for item in pending:
        candidate_id = str(item.get("candidate_id") or item.get("id") or "")
        if not candidate_id or candidate_id in pending_ids:
            continue
        pending_ids.add(candidate_id)
        existing = ordinary_by_id.get(candidate_id)
        merged = dict(item)
        if existing is not None:
            merged["score"] = max(
                float(existing.get("score") or 0),
                float(item.get("score") or 0),
            )
            merged["reason"] = "; ".join(
                value
                for value in (str(existing.get("reason") or ""), "pending_selector")
                if value
            )
            merged["retrieval_routes"] = ["ordinary", "pending"]
        else:
            merged["retrieval_routes"] = ["pending"]
        merged["lane"] = "pending"
        merged["prompt_priority"] = 1
        merged_pending.append(merged)

    remaining = [
        item
        for item in ordinary
        if str(item.get("candidate_id") or item.get("id") or "") not in pending_ids
    ]
    result["selected"] = [*merged_pending, *remaining][: max(int(top_k), 0)]
    result["pending_selected_count"] = len(merged_pending)
    result["retrieval_mode"] = f"{plan.get('retrieval_mode') or 'hybrid'}+pending"
    debug = [*merged_pending, *(plan.get("debug_top") or [])]
    seen: set[str] = set()
    deduped_debug: list[dict] = []
    for item in debug:
        key = str(item.get("candidate_id") or item.get("id") or "")
        if key and key in seen:
            continue
        if key:
            seen.add(key)
        deduped_debug.append(item)
    result["debug_top"] = deduped_debug[: max(int(top_k), 12)]
    return result
