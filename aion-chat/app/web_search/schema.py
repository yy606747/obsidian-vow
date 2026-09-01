"""SQLite schema for the asynchronous web-search inbox."""

from __future__ import annotations


async def init_web_search_tables(db) -> None:
    await db.execute(
        """
        CREATE TABLE IF NOT EXISTS web_search_pending (
            id TEXT PRIMARY KEY,
            conv_id TEXT NOT NULL,
            origin_source TEXT NOT NULL,
            origin_turn_id TEXT NOT NULL,
            intent_text TEXT NOT NULL,
            status TEXT NOT NULL CHECK(status IN ('queued','ready','consumed','failed')),
            result_json TEXT NOT NULL DEFAULT '{}',
            failure_reason TEXT NOT NULL DEFAULT '',
            created_at REAL NOT NULL,
            ready_at REAL,
            expires_at REAL,
            bound_turn_id TEXT,
            bound_at REAL,
            consumed_by_message_id TEXT,
            consumed_at REAL,
            FOREIGN KEY(conv_id) REFERENCES conversations(id) ON DELETE CASCADE
        )
        """
    )
    await db.execute(
        "CREATE INDEX IF NOT EXISTS idx_web_search_conv_status_ready "
        "ON web_search_pending(conv_id, status, ready_at)"
    )
    await db.execute(
        "CREATE INDEX IF NOT EXISTS idx_web_search_status_created "
        "ON web_search_pending(status, created_at)"
    )
    await db.execute(
        "CREATE INDEX IF NOT EXISTS idx_web_search_bound_turn "
        "ON web_search_pending(bound_turn_id)"
    )


__all__ = ["init_web_search_tables"]
