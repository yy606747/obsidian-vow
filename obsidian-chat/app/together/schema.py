"""Separate transcript tables: no messages/chunks/image-memory production hooks."""


async def init_together_tables(db):
    await db.executescript("""
        CREATE TABLE IF NOT EXISTS together_sessions (
            id TEXT PRIMARY KEY, request_id TEXT NOT NULL UNIQUE,
            conv_id TEXT NOT NULL, device_id TEXT NOT NULL, model_key TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'active', started_at REAL NOT NULL, ended_at REAL,
            heartbeat_at REAL NOT NULL, accounted_at REAL NOT NULL, recovery_heartbeat_at REAL,
            active_seconds REAL NOT NULL DEFAULT 0, generation INTEGER NOT NULL DEFAULT 0,
            source TEXT NOT NULL, source_version INTEGER NOT NULL DEFAULT 1,
            capture_state TEXT NOT NULL DEFAULT 'paused', input_busy INTEGER NOT NULL DEFAULT 0,
            entry_history TEXT NOT NULL DEFAULT '[]', opening_pending INTEGER NOT NULL DEFAULT 1,
            wake_at REAL, wake_intent TEXT NOT NULL DEFAULT '', wake_image INTEGER NOT NULL DEFAULT 0,
            event_message_id TEXT, cleaned_at REAL
        );
        CREATE UNIQUE INDEX IF NOT EXISTS idx_together_single_active
            ON together_sessions(status) WHERE status='active';
        CREATE INDEX IF NOT EXISTS idx_together_recent ON together_sessions(status,ended_at);
        CREATE TABLE IF NOT EXISTS together_messages (
            seq INTEGER PRIMARY KEY AUTOINCREMENT, id TEXT NOT NULL UNIQUE,
            session_id TEXT NOT NULL, client_id TEXT,
            role TEXT NOT NULL, content TEXT NOT NULL, status TEXT NOT NULL,
            capture_id TEXT, created_at REAL NOT NULL, turn_id TEXT,
            UNIQUE(session_id, client_id)
        );
        CREATE INDEX IF NOT EXISTS idx_together_messages_session ON together_messages(session_id,seq);
        CREATE TABLE IF NOT EXISTS together_context_messages (
            seq INTEGER PRIMARY KEY, id TEXT NOT NULL UNIQUE,
            session_id TEXT NOT NULL, conv_id TEXT NOT NULL,
            role TEXT NOT NULL, content TEXT NOT NULL, created_at REAL NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_together_context_conv
            ON together_context_messages(conv_id,created_at);
        CREATE TABLE IF NOT EXISTS together_turns (
            id TEXT PRIMARY KEY, session_id TEXT NOT NULL, kind TEXT NOT NULL,
            generation INTEGER NOT NULL, message_id TEXT, status TEXT NOT NULL,
            created_at REAL NOT NULL, finished_at REAL, error TEXT
        );
        CREATE UNIQUE INDEX IF NOT EXISTS idx_together_running
            ON together_turns(session_id) WHERE status='running';
        CREATE TABLE IF NOT EXISTS together_events (
            seq INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT NOT NULL,
            type TEXT NOT NULL, payload TEXT NOT NULL, created_at REAL NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_together_events_session ON together_events(session_id,seq);
        CREATE TABLE IF NOT EXISTS together_captures (
            id TEXT PRIMARY KEY, session_id TEXT NOT NULL, source TEXT NOT NULL,
            source_version INTEGER NOT NULL, purpose TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'requested', requested_at REAL NOT NULL,
            deadline REAL NOT NULL, captured_at REAL, received_at REAL, expires_at REAL,
            filename TEXT, sha256 TEXT, error TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_together_captures_session ON together_captures(session_id,status);
        CREATE TABLE IF NOT EXISTS together_writebacks (
            id TEXT PRIMARY KEY, session_id TEXT NOT NULL, turn_id TEXT,
            kind TEXT NOT NULL, payload TEXT NOT NULL, evidence TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending', created_at REAL NOT NULL,
            finished_at REAL, result TEXT, error TEXT, attempts INTEGER NOT NULL DEFAULT 0
        );
        CREATE INDEX IF NOT EXISTS idx_together_writebacks_status ON together_writebacks(status,created_at);
        CREATE TABLE IF NOT EXISTS together_usage (
            id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT NOT NULL,
            turn_id TEXT, kind TEXT NOT NULL, model_key TEXT NOT NULL DEFAULT '',
            started_at REAL NOT NULL, elapsed_ms INTEGER NOT NULL, status TEXT NOT NULL,
            input_tokens INTEGER, output_tokens INTEGER, reasoning_tokens INTEGER,
            image_count INTEGER NOT NULL DEFAULT 0, error TEXT
        );
        CREATE TABLE IF NOT EXISTS together_holds (
            kind TEXT NOT NULL, item_id TEXT NOT NULL, started_at REAL NOT NULL,
            deadlines TEXT NOT NULL, PRIMARY KEY(kind,item_id)
        );
        CREATE TABLE IF NOT EXISTS together_deliveries (
            id TEXT PRIMARY KEY, payload TEXT NOT NULL, created_at REAL NOT NULL,
            delivered_at REAL
        );
    """)
    columns = {row[1] for row in await (await db.execute("PRAGMA table_info(together_sessions)")).fetchall()}
    if "recovery_heartbeat_at" not in columns:
        await db.execute("ALTER TABLE together_sessions ADD COLUMN recovery_heartbeat_at REAL")
