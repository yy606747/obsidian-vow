"""SQLite operations for the web-search inbox."""

from __future__ import annotations

import json
import time
import uuid

import aiosqlite

from database import get_db


BUFFER_CAPACITY = 3
READY_TTL_SEC = 24 * 60 * 60
WORKER_DEADLINE_SEC = 120
TURN_LEASE_SEC = 30 * 60


def _row(row) -> dict | None:
    return dict(row) if row is not None else None


class WebSearchRepository:
    async def get(self, search_id: str) -> dict | None:
        async with get_db() as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute("SELECT * FROM web_search_pending WHERE id=?", (search_id,))
            return _row(await cur.fetchone())

    async def capacity_snapshot(self, conv_id: str, *, now: float | None = None) -> dict:
        now = time.time() if now is None else now
        async with get_db() as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute(
                "SELECT status, origin_source, intent_text, ready_at, created_at "
                "FROM web_search_pending WHERE conv_id=? AND "
                "((status='queued' AND expires_at>?) OR "
                "(status='ready' AND expires_at>?)) "
                "ORDER BY COALESCE(ready_at, created_at) DESC",
                (conv_id, now, now),
            )
            rows = [dict(item) for item in await cur.fetchall()]
        return {
            "count": len(rows),
            "full": len(rows) >= BUFFER_CAPACITY,
            "recent_intent": str(rows[0]["intent_text"] or "") if rows else "",
        }

    @staticmethod
    async def enqueue_in_tx(
        db,
        *,
        conv_id: str,
        origin_source: str,
        origin_turn_id: str,
        intent_text: str,
        now: float,
    ) -> dict:
        cur = await db.execute(
            "SELECT COUNT(*) FROM web_search_pending WHERE conv_id=? AND "
            "((status='queued' AND expires_at>?) OR "
            "(status='ready' AND expires_at>?))",
            (conv_id, now, now),
        )
        count = int((await cur.fetchone())[0])
        while count >= BUFFER_CAPACITY:
            cur = await db.execute(
                "SELECT id FROM web_search_pending WHERE conv_id=? AND status='ready' "
                "AND expires_at>? AND (bound_turn_id IS NULL OR bound_at<=?) "
                "ORDER BY CASE WHEN origin_source='opportunity' THEN 0 ELSE 1 END, "
                "ready_at ASC LIMIT 1",
                (conv_id, now, now - TURN_LEASE_SEC),
            )
            victim = await cur.fetchone()
            if victim is None:
                return {"status": "buffer_full", "search_id": None}
            await db.execute(
                "UPDATE web_search_pending SET status='failed', "
                "failure_reason='evicted_buffer_capacity', bound_turn_id=NULL, bound_at=NULL "
                "WHERE id=? AND status='ready'",
                (victim[0],),
            )
            count -= 1

        search_id = f"web_{uuid.uuid4().hex}"
        await db.execute(
            "INSERT INTO web_search_pending "
            "(id,conv_id,origin_source,origin_turn_id,intent_text,status,created_at,expires_at) "
            "VALUES (?,?,?,?,?,'queued',?,?)",
            (
                search_id,
                conv_id,
                origin_source,
                origin_turn_id,
                intent_text,
                now,
                now + WORKER_DEADLINE_SEC,
            ),
        )
        return {"status": "queued", "search_id": search_id}

    async def enqueue(self, **kwargs) -> dict:
        async with get_db() as db:
            await db.execute("BEGIN IMMEDIATE")
            try:
                result = await self.enqueue_in_tx(db, **kwargs)
                await db.commit()
                return result
            except BaseException:
                await db.rollback()
                raise

    async def mark_ready(self, search_id: str, *, result: dict, now: float) -> bool:
        async with get_db() as db:
            cur = await db.execute(
                "UPDATE web_search_pending SET status='ready', result_json=?, failure_reason='', "
                "ready_at=?, expires_at=? WHERE id=? AND status='queued'",
                (json.dumps(result, ensure_ascii=False), now, now + READY_TTL_SEC, search_id),
            )
            await db.commit()
            return int(cur.rowcount or 0) > 0

    async def mark_failed(self, search_id: str, *, reason: str) -> bool:
        async with get_db() as db:
            cur = await db.execute(
                "UPDATE web_search_pending SET status='failed', failure_reason=? "
                "WHERE id=? AND status IN ('queued','ready')",
                (str(reason or "failed")[:200], search_id),
            )
            await db.commit()
            return int(cur.rowcount or 0) > 0

    async def recoverable_queued(self, *, now: float) -> list[str]:
        async with get_db() as db:
            cur = await db.execute(
                "UPDATE web_search_pending SET status='failed', failure_reason='worker_deadline' "
                "WHERE status='queued' AND expires_at<=?",
                (now,),
            )
            del cur
            cur = await db.execute(
                "SELECT id FROM web_search_pending WHERE status='queued' AND expires_at>? "
                "ORDER BY created_at",
                (now,),
            )
            rows = await cur.fetchall()
            await db.commit()
        return [str(item[0]) for item in rows]

    async def claim_ready(
        self,
        *,
        conv_id: str,
        bound_turn_id: str,
        now: float,
    ) -> list[dict]:
        lease_cutoff = now - TURN_LEASE_SEC
        async with get_db() as db:
            db.row_factory = aiosqlite.Row
            await db.execute("BEGIN IMMEDIATE")
            try:
                cur = await db.execute(
                    "UPDATE web_search_pending SET bound_turn_id=?, bound_at=? WHERE id IN ("
                    "SELECT id FROM web_search_pending WHERE conv_id=? AND status='ready' "
                    "AND expires_at>? AND (bound_turn_id IS NULL OR bound_at<=?) "
                    "ORDER BY ready_at ASC LIMIT 3) "
                    "AND status='ready' AND (bound_turn_id IS NULL OR bound_at<=?) RETURNING *",
                    (
                        bound_turn_id,
                        now,
                        conv_id,
                        now,
                        lease_cutoff,
                        lease_cutoff,
                    ),
                )
                rows = [dict(item) for item in await cur.fetchall()]
                await db.commit()
            except BaseException:
                await db.rollback()
                raise
        return sorted(rows, key=lambda item: float(item.get("ready_at") or 0))

    @staticmethod
    async def consume_bound_in_tx(
        db,
        *,
        bound_turn_id: str,
        assistant_message_id: str,
        now: float,
    ) -> int:
        cur = await db.execute(
            "UPDATE web_search_pending SET status='consumed', consumed_by_message_id=?, "
            "consumed_at=? WHERE status='ready' AND bound_turn_id=?",
            (assistant_message_id, now, bound_turn_id),
        )
        return max(int(cur.rowcount or 0), 0)

    async def consumed_for_assistant(self, assistant_message_id: str) -> list[dict]:
        async with get_db() as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute(
                "SELECT * FROM web_search_pending WHERE status='consumed' "
                "AND consumed_by_message_id=? ORDER BY ready_at ASC",
                (assistant_message_id,),
            )
            return [dict(item) for item in await cur.fetchall()]

    @staticmethod
    async def reassign_consumed_in_tx(
        db,
        *,
        from_assistant_message_id: str,
        to_assistant_message_id: str,
    ) -> int:
        cur = await db.execute(
            "UPDATE web_search_pending SET consumed_by_message_id=? "
            "WHERE status='consumed' AND consumed_by_message_id=?",
            (to_assistant_message_id, from_assistant_message_id),
        )
        return max(int(cur.rowcount or 0), 0)

    @staticmethod
    async def cancel_for_message_in_tx(db, *, message_id: str) -> int:
        cur = await db.execute(
            "UPDATE web_search_pending SET status='failed', failure_reason='origin_or_turn_removed' "
            "WHERE status IN ('queued','ready') AND "
            "(origin_turn_id=? OR bound_turn_id=? OR bound_turn_id=?)",
            (message_id, message_id, f"send:{message_id}"),
        )
        return max(int(cur.rowcount or 0), 0)

    @staticmethod
    async def delete_for_conversation_in_tx(db, *, conv_id: str) -> int:
        cur = await db.execute("DELETE FROM web_search_pending WHERE conv_id=?", (conv_id,))
        return max(int(cur.rowcount or 0), 0)


__all__ = [
    "BUFFER_CAPACITY",
    "READY_TTL_SEC",
    "TURN_LEASE_SEC",
    "WebSearchRepository",
]
