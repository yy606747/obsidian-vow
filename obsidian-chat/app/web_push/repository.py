from __future__ import annotations

import time

import aiosqlite

from database import get_db


async def upsert_subscription(*, endpoint: str, p256dh: str, auth: str) -> None:
    async with get_db() as db:
        await db.execute(
            """
            INSERT INTO web_push_subscriptions (
                endpoint, p256dh, auth, created_at, last_ok_at, failure_count
            ) VALUES (?, ?, ?, ?, NULL, 0)
            ON CONFLICT(endpoint) DO UPDATE SET
                p256dh = excluded.p256dh,
                auth = excluded.auth,
                failure_count = 0
            """,
            (endpoint, p256dh, auth, time.time()),
        )
        await db.commit()


async def delete_subscription(endpoint: str) -> None:
    async with get_db() as db:
        await db.execute(
            "DELETE FROM web_push_subscriptions WHERE endpoint = ?", (endpoint,)
        )
        await db.commit()


async def list_subscriptions() -> list[dict]:
    async with get_db() as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            """
            SELECT endpoint, p256dh, auth, created_at, last_ok_at, failure_count
            FROM web_push_subscriptions
            ORDER BY created_at
            """
        )
        return [dict(row) for row in await cursor.fetchall()]


async def mark_success(endpoint: str) -> None:
    async with get_db() as db:
        await db.execute(
            """
            UPDATE web_push_subscriptions
            SET last_ok_at = ?, failure_count = 0
            WHERE endpoint = ?
            """,
            (time.time(), endpoint),
        )
        await db.commit()


async def mark_failure(endpoint: str) -> None:
    async with get_db() as db:
        await db.execute(
            """
            UPDATE web_push_subscriptions
            SET failure_count = failure_count + 1
            WHERE endpoint = ?
            """,
            (endpoint,),
        )
        await db.commit()
