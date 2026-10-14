"""An overlay over original rows: never overwrite source text or embeddings."""
from __future__ import annotations


async def init_tables(db) -> None:
    await db.execute("""CREATE TABLE IF NOT EXISTS memory_compaction_batches (
        id TEXT PRIMARY KEY, status TEXT NOT NULL, created_at REAL NOT NULL,
        source_start_ts REAL NOT NULL, source_end_ts REAL NOT NULL,
        source_count INTEGER NOT NULL, note_count INTEGER NOT NULL,
        input_chars INTEGER NOT NULL, output_chars INTEGER NOT NULL
    )""")
    await db.execute("""CREATE TABLE IF NOT EXISTS memory_compaction_sources (
        batch_id TEXT NOT NULL, lane TEXT NOT NULL, source_id TEXT NOT NULL,
        content TEXT NOT NULL, source_hash TEXT NOT NULL DEFAULT '',
        PRIMARY KEY(batch_id,lane,source_id)
    )""")
    await db.execute("CREATE INDEX IF NOT EXISTS idx_compaction_source ON memory_compaction_sources(lane,source_id)")
    await db.execute("""CREATE TABLE IF NOT EXISTS memory_compaction_outputs (
        batch_id TEXT NOT NULL, memory_id TEXT PRIMARY KEY
    )""")
    await db.execute("""CREATE TABLE IF NOT EXISTS memory_compaction_work (
        day_key TEXT PRIMARY KEY, attempts INTEGER NOT NULL DEFAULT 0
    )""")


# A later edit/deletion retires the derived summary at read time. This also
# handles edits outside the application without changing any source row.
VALID_BATCH = """b.status='applied' AND NOT EXISTS (
    SELECT 1 FROM memory_compaction_sources s
    LEFT JOIN memory_chunks c ON s.lane='chunk' AND c.id=s.source_id
    LEFT JOIN memory_items n ON s.lane='note' AND n.id=s.source_id
    WHERE s.batch_id=b.id AND (
        (s.lane='chunk' AND (c.id IS NULL OR c.status NOT IN ('active','cold')
            OR c.content!=s.content OR COALESCE(c.source_hash,'')!=s.source_hash))
        OR (s.lane='note' AND (n.id IS NULL OR n.status!='active'
            OR n.visibility!='prompt' OR n.content!=s.content))
    )
)"""


def covered_sql(lane: str, id_expression: str) -> str:
    assert lane in {"chunk", "note"}
    return ("EXISTS (SELECT 1 FROM memory_compaction_sources cs "
            "JOIN memory_compaction_batches b ON b.id=cs.batch_id "
            f"WHERE cs.lane='{lane}' AND cs.source_id={id_expression} AND {VALID_BATCH})")


def valid_output_sql(id_expression: str) -> str:
    return ("EXISTS (SELECT 1 FROM memory_compaction_outputs co "
            "JOIN memory_compaction_batches b ON b.id=co.batch_id "
            f"WHERE co.memory_id={id_expression} AND {VALID_BATCH})")
