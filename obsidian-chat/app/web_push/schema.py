from __future__ import annotations


async def init_web_push_tables(db) -> None:
    await db.execute(
        """
        CREATE TABLE IF NOT EXISTS web_push_subscriptions (
            endpoint TEXT PRIMARY KEY,
            p256dh TEXT NOT NULL,
            auth TEXT NOT NULL,
            created_at REAL NOT NULL,
            last_ok_at REAL,
            failure_count INTEGER NOT NULL DEFAULT 0
        )
        """
    )
