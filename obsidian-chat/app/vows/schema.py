"""vows 表结构。独立新表，不 ALTER 任何现有表。"""


async def init_vow_tables(db) -> None:
    await db.execute("""
        CREATE TABLE IF NOT EXISTS vows (
            id                  TEXT PRIMARY KEY,
            root_id             TEXT NOT NULL,
            previous_version_id TEXT,
            content             TEXT NOT NULL,
            status              TEXT NOT NULL CHECK (status IN ('active','superseded','retired','fulfilled')),
            origin_type         TEXT NOT NULL CHECK (origin_type IN ('ai_marker','user_ui')),
            origin_conv_id      TEXT,
            origin_message_id   TEXT,
            created_at          REAL NOT NULL,
            status_changed_at   REAL NOT NULL,
            close_action        TEXT CHECK (close_action IN ('revised','retired','fulfilled','origin_regenerated','origin_deleted')),
            closed_reason       TEXT
        )
    """)
    await db.execute("""
        CREATE UNIQUE INDEX IF NOT EXISTS idx_vows_one_active
            ON vows(root_id) WHERE status = 'active'
    """)
    await db.execute("""
        CREATE UNIQUE INDEX IF NOT EXISTS idx_vows_origin_msg
            ON vows(origin_message_id) WHERE origin_message_id IS NOT NULL
    """)
    await db.execute("CREATE INDEX IF NOT EXISTS idx_vows_status ON vows(status, status_changed_at DESC)")
    await db.execute(
        "CREATE INDEX IF NOT EXISTS idx_vows_root_created "
        "ON vows(root_id, created_at DESC)"
    )
