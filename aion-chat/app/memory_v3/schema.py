"""SQLite schema for Memory V3.

Schema installation is behaviour-neutral: all feature flags remain disabled by
default and existing recall queries keep their current output until later
phases explicitly opt in.
"""

from __future__ import annotations

from collections.abc import Iterable


MEMORY_CHUNK_COLUMNS = {
    "source_hash": "TEXT NOT NULL DEFAULT ''",
    "status": "TEXT NOT NULL DEFAULT 'active'",
    "retired_at": "REAL",
    "card_generation_hash": "TEXT NOT NULL DEFAULT ''",
    "card_generation_status": "TEXT NOT NULL DEFAULT ''",
    "card_generation_reason": "TEXT NOT NULL DEFAULT ''",
    "card_generation_prompt_version": "TEXT NOT NULL DEFAULT ''",
    "card_generation_attempted_at": "REAL",
}

MEMORY_ITEM_COLUMNS = {
    "origin_type": "TEXT NOT NULL DEFAULT 'legacy'",
}

TIMELINE_COLUMNS = {
    "metadata_json": "TEXT NOT NULL DEFAULT '{}'",
    "failure_reason": "TEXT NOT NULL DEFAULT ''",
}

NEW_TABLES = (
    "memory_relational_cards",
    "memory_pending_recalls",
    "memory_timeline_versions",
    "memory_injection_events",
)


async def _column_names(db, table: str) -> set[str]:
    cur = await db.execute(f"PRAGMA table_info({table})")
    return {str(row[1]) for row in await cur.fetchall()}


async def _table_names(db) -> set[str]:
    cur = await db.execute("SELECT name FROM sqlite_master WHERE type='table'")
    return {str(row[0]) for row in await cur.fetchall()}


async def _add_columns(db, table: str, columns: dict[str, str]) -> list[str]:
    existing = await _column_names(db, table)
    added: list[str] = []
    for name, definition in columns.items():
        if name in existing:
            continue
        await db.execute(f"ALTER TABLE {table} ADD COLUMN {name} {definition}")
        added.append(name)
    return added


async def inspect_memory_v3_schema(db) -> dict:
    tables = await _table_names(db)
    chunk_columns = await _column_names(db, "memory_chunks") if "memory_chunks" in tables else set()
    item_columns = await _column_names(db, "memory_items") if "memory_items" in tables else set()
    timeline_columns = (
        await _column_names(db, "memory_timeline_versions")
        if "memory_timeline_versions" in tables
        else set()
    )
    return {
        "missing_base_tables": [
            table for table in ("memory_chunks", "memory_items") if table not in tables
        ],
        "missing_memory_chunk_columns": sorted(set(MEMORY_CHUNK_COLUMNS) - chunk_columns),
        "missing_memory_item_columns": sorted(set(MEMORY_ITEM_COLUMNS) - item_columns),
        "missing_timeline_columns": (
            sorted(set(TIMELINE_COLUMNS) - timeline_columns)
            if "memory_timeline_versions" in tables
            else []
        ),
        "missing_new_tables": sorted(set(NEW_TABLES) - tables),
    }


async def init_memory_v3_tables(db) -> dict:
    tables = await _table_names(db)
    missing_base = {"memory_chunks", "memory_items"} - tables
    if missing_base:
        raise RuntimeError(f"Memory V3 requires base tables: {sorted(missing_base)}")

    added_chunk_columns = await _add_columns(db, "memory_chunks", MEMORY_CHUNK_COLUMNS)
    added_item_columns = await _add_columns(db, "memory_items", MEMORY_ITEM_COLUMNS)

    await db.execute("""
        CREATE TABLE IF NOT EXISTS memory_relational_cards (
            id TEXT PRIMARY KEY,
            source_chunk_id TEXT NOT NULL,
            version INTEGER NOT NULL,
            content TEXT NOT NULL,
            source_message_ids_json TEXT NOT NULL DEFAULT '[]',
            evidence_json TEXT NOT NULL DEFAULT '[]',
            source_hash TEXT NOT NULL,
            status TEXT NOT NULL CHECK (status IN ('active','superseded','invalid')),
            supersedes_card_id TEXT,
            generator_model TEXT,
            prompt_version TEXT NOT NULL,
            embedding BLOB,
            embedding_model TEXT,
            metadata_json TEXT NOT NULL DEFAULT '{}',
            created_at REAL NOT NULL,
            updated_at REAL NOT NULL,
            UNIQUE(source_chunk_id, version),
            FOREIGN KEY (source_chunk_id) REFERENCES memory_chunks(id) ON DELETE RESTRICT
        )
    """)
    await db.execute("""
        CREATE UNIQUE INDEX IF NOT EXISTS idx_rel_cards_one_active_chunk
        ON memory_relational_cards(source_chunk_id) WHERE status='active'
    """)
    await db.execute(
        "CREATE INDEX IF NOT EXISTS idx_rel_cards_status_updated "
        "ON memory_relational_cards(status, updated_at DESC)"
    )

    await db.execute("""
        CREATE TABLE IF NOT EXISTS memory_pending_recalls (
            id TEXT PRIMARY KEY,
            conv_id TEXT NOT NULL,
            origin_assistant_message_id TEXT NOT NULL UNIQUE,
            target_user_message_id TEXT,
            intent_text TEXT NOT NULL,
            status TEXT NOT NULL CHECK (
                status IN ('queued','ready','selected','consumed','superseded','cancelled','failed')
            ),
            candidate_json TEXT NOT NULL DEFAULT '[]',
            selected_json TEXT NOT NULL DEFAULT '[]',
            config_json TEXT NOT NULL DEFAULT '{}',
            attempt_count INTEGER NOT NULL DEFAULT 0,
            deferred_count INTEGER NOT NULL DEFAULT 0,
            last_deferred_user_message_id TEXT,
            failure_reason TEXT,
            created_at REAL NOT NULL,
            retrieval_started_at REAL,
            retrieval_completed_at REAL,
            selected_at REAL,
            consumed_by_assistant_message_id TEXT,
            consumed_at REAL,
            retrieval_deadline_at REAL NOT NULL,
            updated_at REAL NOT NULL
        )
    """)
    await db.execute("""
        CREATE UNIQUE INDEX IF NOT EXISTS idx_pending_one_active_conv
        ON memory_pending_recalls(conv_id)
        WHERE status IN ('queued','ready','selected')
    """)
    await db.execute(
        "CREATE INDEX IF NOT EXISTS idx_pending_status_updated "
        "ON memory_pending_recalls(status, updated_at DESC)"
    )
    await db.execute(
        "CREATE INDEX IF NOT EXISTS idx_pending_target_user "
        "ON memory_pending_recalls(target_user_message_id)"
    )

    await db.execute("""
        CREATE TABLE IF NOT EXISTS memory_timeline_versions (
            id TEXT PRIMARY KEY,
            version INTEGER NOT NULL UNIQUE,
            window_start_ts REAL NOT NULL,
            window_end_ts REAL NOT NULL,
            entries_json TEXT NOT NULL DEFAULT '[]',
            source_message_ids_json TEXT NOT NULL DEFAULT '[]',
            source_hash TEXT NOT NULL,
            status TEXT NOT NULL CHECK (status IN ('active','superseded','invalid')),
            generator_model TEXT,
            prompt_version TEXT NOT NULL,
            metadata_json TEXT NOT NULL DEFAULT '{}',
            failure_reason TEXT NOT NULL DEFAULT '',
            created_at REAL NOT NULL,
            updated_at REAL NOT NULL
        )
    """)
    added_timeline_columns = await _add_columns(
        db,
        "memory_timeline_versions",
        TIMELINE_COLUMNS,
    )
    await db.execute("""
        CREATE UNIQUE INDEX IF NOT EXISTS idx_timeline_one_active
        ON memory_timeline_versions(status) WHERE status='active'
    """)
    await db.execute(
        "CREATE INDEX IF NOT EXISTS idx_timeline_window "
        "ON memory_timeline_versions(window_end_ts DESC)"
    )

    await db.execute("""
        CREATE TABLE IF NOT EXISTS memory_injection_events (
            id TEXT PRIMARY KEY,
            request_id TEXT,
            conv_id TEXT NOT NULL,
            user_message_id TEXT,
            assistant_message_id TEXT,
            route TEXT NOT NULL,
            candidate_id TEXT,
            source_chunk_id TEXT,
            memory_item_id TEXT,
            card_id TEXT,
            card_version INTEGER,
            score REAL,
            rank INTEGER,
            cooldown_penalty REAL,
            outcome TEXT NOT NULL,
            reason TEXT NOT NULL DEFAULT '',
            rendered_chars INTEGER NOT NULL DEFAULT 0,
            selector_model TEXT,
            selector_input_tokens INTEGER,
            selector_output_tokens INTEGER,
            selector_latency_ms INTEGER,
            response_overlap_proxy REAL,
            past_reference_proxy INTEGER,
            metadata_json TEXT NOT NULL DEFAULT '{}',
            created_at REAL NOT NULL
        )
    """)
    await db.execute(
        "CREATE INDEX IF NOT EXISTS idx_injection_request "
        "ON memory_injection_events(request_id, created_at)"
    )
    await db.execute(
        "CREATE INDEX IF NOT EXISTS idx_injection_candidate_time "
        "ON memory_injection_events(candidate_id, created_at DESC)"
    )

    # Existing metadata provides a deterministic origin for the two automated
    # paths.  Other rows retain the conservative legacy/manual default.
    await db.execute("""
        UPDATE memory_items
        SET origin_type = CASE
            WHEN json_valid(metadata_json)
             AND (
                json_extract(metadata_json, '$.source') = 'remember_cmd'
                OR json_extract(metadata_json, '$.legacy_type') = 'ai_note'
             ) THEN 'ai_note'
            WHEN json_valid(metadata_json)
             AND (
                json_extract(metadata_json, '$.source') = 'digest.multi_note'
                OR json_extract(metadata_json, '$.legacy_type') IN ('digest','digest_note')
             ) THEN 'auto_digest'
            WHEN origin_type IS NOT NULL AND TRIM(origin_type) NOT IN ('', 'legacy')
             THEN origin_type
            WHEN legacy_memory_id IS NULL THEN 'manual'
            ELSE 'legacy'
        END
    """)

    return {
        "added_memory_chunk_columns": added_chunk_columns,
        "added_memory_item_columns": added_item_columns,
        "added_timeline_columns": added_timeline_columns,
        "tables": list(NEW_TABLES),
    }
