"""Daily windows reference original messages; they never copy chat records."""


async def init_daily_chat_tables(db):
    await db.executescript("""
        CREATE TABLE IF NOT EXISTS daily_chat_state (
            id INTEGER PRIMARY KEY CHECK (id=1),
            enabled INTEGER NOT NULL DEFAULT 1,
            timezone TEXT NOT NULL,
            inherit_count INTEGER NOT NULL DEFAULT 30,
            count_initialized INTEGER NOT NULL DEFAULT 0,
            day_key TEXT NOT NULL,
            active_conv_id TEXT,
            revision INTEGER NOT NULL DEFAULT 0,
            next_rollover_at REAL NOT NULL
        );
        CREATE TABLE IF NOT EXISTS daily_chat_windows (
            conv_id TEXT PRIMARY KEY,
            day_key TEXT NOT NULL UNIQUE,
            previous_conv_id TEXT,
            inherit_count INTEGER NOT NULL,
            switched_at REAL NOT NULL
        );
        CREATE TABLE IF NOT EXISTS daily_chat_inheritance (
            conv_id TEXT NOT NULL,
            message_id TEXT NOT NULL,
            position INTEGER NOT NULL,
            PRIMARY KEY (conv_id, message_id)
        );
        CREATE TABLE IF NOT EXISTS daily_chat_successors (
            source_conv_id TEXT PRIMARY KEY,
            target_conv_id TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS daily_chat_deliveries (
            message_id TEXT PRIMARY KEY,
            origin_conv_id TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_daily_chat_inheritance_order
            ON daily_chat_inheritance(conv_id, position);
        CREATE INDEX IF NOT EXISTS idx_daily_chat_successor_target
            ON daily_chat_successors(target_conv_id);

        -- The rollover and message commit share SQLite's writer serialization.
        -- A turn that started yesterday can finish today without restarting its
        -- provider request or replaying tools. Its original source remains recorded.
        CREATE TRIGGER IF NOT EXISTS daily_chat_route_insert
        AFTER INSERT ON messages
        WHEN EXISTS (
            SELECT 1 FROM daily_chat_successors r JOIN daily_chat_state s ON s.id=1
            WHERE r.source_conv_id=NEW.conv_id AND s.enabled=1
        )
        BEGIN
            INSERT OR IGNORE INTO daily_chat_deliveries(message_id, origin_conv_id)
                VALUES(NEW.id, NEW.conv_id);
            UPDATE messages SET conv_id=(
                SELECT target_conv_id FROM daily_chat_successors WHERE source_conv_id=NEW.conv_id
            ) WHERE id=NEW.id;
            UPDATE conversations SET updated_at=MAX(updated_at, NEW.created_at)
                WHERE id=(SELECT conv_id FROM messages WHERE id=NEW.id);
        END;
    """)
    cursor = await db.execute("SELECT name FROM sqlite_master WHERE type='table'")
    tables = {row[0] for row in await cursor.fetchall()}
    # A queued tool can be created after the rollover by a request already in flight.
    for table, key in (('web_search_pending', 'id'), ('memory_pending_recalls', 'id'),
                       ('self_wakes', 'id'), ('control_sessions', 'session_id')):
        if table not in tables:
            continue
        await db.executescript(f"""
            CREATE TRIGGER IF NOT EXISTS daily_chat_route_{table}
            AFTER INSERT ON {table}
            WHEN EXISTS (
                SELECT 1 FROM daily_chat_successors r JOIN daily_chat_state s ON s.id=1
                WHERE r.source_conv_id=NEW.conv_id AND s.enabled=1
            )
            BEGIN
                UPDATE {table} SET conv_id=(
                    SELECT target_conv_id FROM daily_chat_successors WHERE source_conv_id=NEW.conv_id
                ) WHERE {key}=NEW.{key};
            END;
        """)
