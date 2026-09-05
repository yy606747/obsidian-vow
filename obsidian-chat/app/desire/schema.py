"""Desire-layer version table."""


async def init_desire_tables(db) -> None:
    await db.execute("""
        CREATE TABLE IF NOT EXISTS desire_versions (
            id                  TEXT PRIMARY KEY,
            previous_version_id TEXT,
            content             TEXT NOT NULL,
            change_note         TEXT NOT NULL,
            origin_request_id   TEXT NOT NULL,
            working_model_id    TEXT NOT NULL,
            writer_model        TEXT,
            prompt_version      TEXT,
            created_at          REAL NOT NULL
        )
    """)
    await db.execute(
        "CREATE INDEX IF NOT EXISTS idx_desire_created "
        "ON desire_versions(created_at DESC)"
    )
    await db.execute("""
        CREATE UNIQUE INDEX IF NOT EXISTS idx_desire_successor
            ON desire_versions(previous_version_id)
            WHERE previous_version_id IS NOT NULL
    """)
    await db.execute("""
        CREATE UNIQUE INDEX IF NOT EXISTS idx_desire_request
            ON desire_versions(origin_request_id)
    """)
