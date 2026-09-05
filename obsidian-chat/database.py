"""
数据库初始化与连接
"""

from contextlib import asynccontextmanager
from pathlib import Path

import aiosqlite
from config import (
    DB_PATH,
    WORKING_MODEL_MIGRATION_REPORT_PATH,
    WORKING_MODEL_PATH,
    load_ai_behavior,
)


async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
        # WAL 是持久化的（写进 DB 文件），只需设置一次；对比 DELETE 模式写性能 +2-3x
        await db.execute("PRAGMA journal_mode = WAL")
        # NORMAL 在 WAL 下是安全的（checkpoint 时 fsync），比 FULL 快，只在断电时可能丢最后一次事务
        await db.execute("PRAGMA synchronous = NORMAL")
        await db.execute("""
            CREATE TABLE IF NOT EXISTS conversations (
                id TEXT PRIMARY KEY,
                title TEXT NOT NULL,
                model TEXT NOT NULL DEFAULT 'gemini-3-flash',
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS messages (
                id TEXT PRIMARY KEY,
                conv_id TEXT NOT NULL,
                role TEXT NOT NULL,
                content TEXT NOT NULL,
                created_at REAL NOT NULL,
                FOREIGN KEY (conv_id) REFERENCES conversations(id) ON DELETE CASCADE
            )
        """)
        await db.execute("PRAGMA foreign_keys = ON")
        try:
            await db.execute("ALTER TABLE messages ADD COLUMN attachments TEXT DEFAULT ''")
        except:
            pass
        # 昨日续点缓存：纸条文本 + 窗口内容签名（内容不变就不重算）
        for _col in ("handoff_note TEXT", "handoff_note_sig TEXT"):
            try:
                await db.execute(f"ALTER TABLE conversations ADD COLUMN {_col}")
            except:
                pass
        # 性能索引
        await db.execute("CREATE INDEX IF NOT EXISTS idx_messages_conv_id ON messages(conv_id, created_at)")
        await db.execute("CREATE INDEX IF NOT EXISTS idx_conversations_updated ON conversations(updated_at DESC)")
        await db.execute("""
            CREATE TABLE IF NOT EXISTS memories (
                id TEXT PRIMARY KEY,
                content TEXT NOT NULL,
                type TEXT DEFAULT 'event',
                created_at REAL NOT NULL,
                source_conv TEXT,
                embedding BLOB
            )
        """)
        # memories 表新增字段（向后兼容迁移）
        for col, defn in [
            ("keywords", "TEXT DEFAULT ''"),
            ("importance", "REAL DEFAULT 0.5"),
            ("source_start_ts", "REAL"),
            ("source_end_ts", "REAL"),
            ("unresolved", "INTEGER DEFAULT 0"),
        ]:
            try:
                await db.execute(f"ALTER TABLE memories ADD COLUMN {col} {defn}")
            except:
                pass
        await db.execute("CREATE INDEX IF NOT EXISTS idx_memories_created ON memories(created_at DESC)")
        # ── Memory V2 tables ──
        # Batch 2.1 只落结构。旧 memories 表仍是正式读写路径。
        await db.execute("""
            CREATE TABLE IF NOT EXISTS memory_events (
                id TEXT PRIMARY KEY,
                source TEXT NOT NULL,
                namespace TEXT NOT NULL DEFAULT 'normal',
                conv_id TEXT,
                role TEXT,
                content TEXT NOT NULL,
                metadata_json TEXT NOT NULL DEFAULT '{}',
                created_at REAL NOT NULL
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS memory_items (
                id TEXT PRIMARY KEY,
                legacy_memory_id TEXT UNIQUE,
                kind TEXT NOT NULL DEFAULT 'episode',
                namespace TEXT NOT NULL DEFAULT 'normal',
                content TEXT NOT NULL,
                subject TEXT NOT NULL DEFAULT '',
                entities_json TEXT NOT NULL DEFAULT '[]',
                emotion TEXT NOT NULL DEFAULT '',
                importance REAL NOT NULL DEFAULT 0.5,
                confidence REAL NOT NULL DEFAULT 0.7,
                status TEXT NOT NULL DEFAULT 'active',
                visibility TEXT NOT NULL DEFAULT 'prompt',
                embedding BLOB,
                keywords_json TEXT NOT NULL DEFAULT '[]',
                source_conv TEXT,
                source_start_ts REAL,
                source_end_ts REAL,
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL,
                last_seen_at REAL,
                last_used_at REAL,
                expires_at REAL,
                metadata_json TEXT NOT NULL DEFAULT '{}'
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS memory_chunks (
                id TEXT PRIMARY KEY,
                conv_id TEXT NOT NULL,
                message_ids_json TEXT NOT NULL DEFAULT '[]',
                content TEXT NOT NULL,
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL,
                embedding BLOB,
                keywords_json TEXT NOT NULL DEFAULT '[]',
                metadata_json TEXT NOT NULL DEFAULT '{}'
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS memory_links (
                memory_id TEXT NOT NULL,
                target_id TEXT NOT NULL,
                target_type TEXT NOT NULL,
                relation TEXT NOT NULL,
                created_at REAL NOT NULL,
                PRIMARY KEY (memory_id, target_id, target_type, relation)
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS memory_usage (
                id TEXT PRIMARY KEY,
                memory_id TEXT NOT NULL,
                conv_id TEXT,
                request_id TEXT,
                used_at REAL NOT NULL,
                reason TEXT NOT NULL DEFAULT '',
                score REAL,
                rank INTEGER
            )
        """)
        await db.execute("CREATE INDEX IF NOT EXISTS idx_memory_events_ns_source_time ON memory_events(namespace, source, created_at DESC)")
        await db.execute("CREATE INDEX IF NOT EXISTS idx_memory_items_ns_kind_status ON memory_items(namespace, kind, status)")
        await db.execute("CREATE INDEX IF NOT EXISTS idx_memory_items_updated ON memory_items(updated_at DESC)")
        await db.execute("CREATE INDEX IF NOT EXISTS idx_memory_items_created ON memory_items(created_at DESC)")
        await db.execute("CREATE INDEX IF NOT EXISTS idx_memory_items_legacy ON memory_items(legacy_memory_id)")
        await db.execute("CREATE INDEX IF NOT EXISTS idx_memory_chunks_conv_updated ON memory_chunks(conv_id, updated_at DESC)")
        await db.execute("CREATE INDEX IF NOT EXISTS idx_memory_chunks_updated ON memory_chunks(updated_at DESC)")
        await db.execute("CREATE INDEX IF NOT EXISTS idx_memory_usage_memory_time ON memory_usage(memory_id, used_at DESC)")
        from app.control.service import init_control_tables
        from app.control.outcome import init_control_outcome_tables
        from app.control.agenda import init_control_agenda_tables
        from app.tide.intent import init_tide_tables
        from app.vows.schema import init_vow_tables
        from app.memory_v3.schema import init_memory_v3_tables
        from app.desire.schema import init_desire_tables
        from app.reflection.schema import init_reflection_tables
        from app.tools.ledger_schema import init_tool_invocation_ledger_tables
        from app.presence.db import init_presence_tables
        from app.self_wake.schema import init_self_wake_tables
        from app.web_search.schema import init_web_search_tables
        from app.web_push.schema import init_web_push_tables
        from app.working_model.schema import init_working_model_tables
        from app.working_model.service import (
            migrate_legacy_roots_in_tx,
            write_migration_report,
        )

        await init_control_tables(db)
        await init_control_outcome_tables(db)
        await init_control_agenda_tables(db)
        await init_tide_tables(db)
        await init_vow_tables(db)
        await init_memory_v3_tables(db)
        await init_working_model_tables(db)
        await init_desire_tables(db)
        await init_reflection_tables(db)
        await init_tool_invocation_ledger_tables(db)
        await init_presence_tables(db)
        await init_self_wake_tables(db)
        await init_web_search_tables(db)
        await init_web_push_tables(db)
        working_model_migration_report = await migrate_legacy_roots_in_tx(
            db,
            source_path=WORKING_MODEL_PATH,
        )
        if load_ai_behavior().get("working_model_v2_write_enabled", False):
            from app.working_model.service import assert_v2_write_path_ready_in_tx

            await assert_v2_write_path_ready_in_tx(db)
        # ── 日程/闹铃表 ──
        await db.execute("""
            CREATE TABLE IF NOT EXISTS schedules (
                id TEXT PRIMARY KEY,
                type TEXT NOT NULL,
                trigger_at TEXT NOT NULL,
                content TEXT NOT NULL,
                created_at REAL NOT NULL,
                status TEXT NOT NULL DEFAULT 'active'
            )
        """)
        await db.execute("CREATE INDEX IF NOT EXISTS idx_schedules_status ON schedules(status, trigger_at)")
        # 创建时上下文走 schedule 自有旁表，不改变既有 schedules 契约。
        from app.schedule.alarm_context import init_alarm_context_tables

        await init_alarm_context_tables(db)
        from app.image_memory.repository import init_tables as init_image_memory_tables

        await init_image_memory_tables(db)
        # ── 心语表 ──
        await db.execute("""
            CREATE TABLE IF NOT EXISTS heart_whispers (
                id TEXT PRIMARY KEY,
                conv_id TEXT,
                msg_id TEXT,
                content TEXT NOT NULL,
                created_at REAL NOT NULL
            )
        """)
        await db.execute("CREATE INDEX IF NOT EXISTS idx_heart_whispers_created ON heart_whispers(created_at DESC)")
        await db.commit()
        write_migration_report(
            working_model_migration_report,
            WORKING_MODEL_MIGRATION_REPORT_PATH,
        )


@asynccontextmanager
async def get_db(*, timeout: float | None = None, read_only: bool = False):
    """每次打开连接都设 synchronous=NORMAL（此 pragma 是 per-connection 的）。
    journal_mode=WAL 已在 init_db 持久化到 DB 文件，不用每次重设。"""
    connect_kwargs = {} if timeout is None else {"timeout": timeout}
    target = DB_PATH
    if read_only:
        target = Path(DB_PATH).resolve().as_uri() + "?mode=ro"
        connect_kwargs["uri"] = True
    async with aiosqlite.connect(target, **connect_kwargs) as db:
        await db.execute("PRAGMA synchronous = NORMAL")
        yield db
