"""Scene storage: cards, feedback, optional notes and expiring recent evidence."""


async def init_scene_tables(db) -> None:
    await db.executescript("""
        CREATE TABLE IF NOT EXISTS scene_cards (
            id TEXT PRIMARY KEY, raw_text TEXT NOT NULL, required_devices TEXT NOT NULL,
            source TEXT NOT NULL, conv_id TEXT NOT NULL, model_key TEXT NOT NULL,
            created_at REAL NOT NULL, used_at REAL
        );
        CREATE TABLE IF NOT EXISTS scene_sessions (
            id TEXT PRIMARY KEY, request_id TEXT NOT NULL UNIQUE, card_id TEXT,
            conv_id TEXT NOT NULL, model_key TEXT NOT NULL, owner_id TEXT NOT NULL,
            authorized_devices TEXT NOT NULL, bindings TEXT NOT NULL DEFAULT '{}',
            status TEXT NOT NULL, generation INTEGER NOT NULL DEFAULT 0,
            created_at REAL NOT NULL, started_at REAL, ended_at REAL, reason TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_scene_cards_unused ON scene_cards(used_at, created_at);
        CREATE INDEX IF NOT EXISTS idx_scene_sessions_status ON scene_sessions(status, created_at);
        CREATE TABLE IF NOT EXISTS scene_feedback (
            session_id TEXT PRIMARY KEY, rating TEXT, reason TEXT NOT NULL DEFAULT '',
            version INTEGER NOT NULL, processed_version INTEGER NOT NULL DEFAULT 0,
            note_status TEXT NOT NULL, error_type TEXT, updated_at REAL NOT NULL
        );
        CREATE TABLE IF NOT EXISTS scene_notes (
            id INTEGER PRIMARY KEY CHECK (id = 1), content TEXT NOT NULL DEFAULT '',
            version INTEGER NOT NULL DEFAULT 0, updated_at REAL NOT NULL DEFAULT 0
        );
        INSERT OR IGNORE INTO scene_notes(id) VALUES(1);
        CREATE TABLE IF NOT EXISTS scene_recent (
            session_id TEXT PRIMARY KEY, evidence TEXT NOT NULL,
            summary TEXT NOT NULL DEFAULT '', summary_status TEXT NOT NULL DEFAULT 'pending',
            expires_at REAL NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_scene_recent_expiry ON scene_recent(expires_at);
        CREATE TABLE IF NOT EXISTS scene_memory_writes (
            id TEXT PRIMARY KEY, session_id TEXT NOT NULL, content TEXT NOT NULL,
            created_at REAL NOT NULL, status TEXT NOT NULL DEFAULT 'pending',
            memory_id TEXT, error_type TEXT
        );
    """)
    # Keep already-revealed cards available when upgrading existing installs.
    columns = {row[1] for row in await (await db.execute("PRAGMA table_info(scene_sessions)")).fetchall()}
    if "phase" not in columns:
        await db.execute("ALTER TABLE scene_sessions ADD COLUMN phase TEXT NOT NULL DEFAULT 'play'")
    if "revealed_at" not in columns:
        await db.execute("ALTER TABLE scene_sessions ADD COLUMN revealed_at REAL")
        await db.execute("UPDATE scene_sessions SET revealed_at=ended_at WHERE status='ended'")
