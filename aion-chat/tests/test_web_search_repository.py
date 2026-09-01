import asyncio
from contextlib import asynccontextmanager
import sqlite3

import aiosqlite

from app.web_search import repository as repository_module
from app.web_search.repository import TURN_LEASE_SEC, WebSearchRepository
from app.web_search.schema import init_web_search_tables


def _repo(tmp_path, monkeypatch):
    path = tmp_path / "web.db"

    class Cursor:
        def __init__(self, cursor):
            self.cursor = cursor
            self.rowcount = cursor.rowcount

        async def fetchone(self):
            return self.cursor.fetchone()

        async def fetchall(self):
            return self.cursor.fetchall()

    class Connection:
        def __init__(self):
            self.conn = sqlite3.connect(path)

        @property
        def row_factory(self):
            return self.conn.row_factory

        @row_factory.setter
        def row_factory(self, value):
            self.conn.row_factory = value

        async def execute(self, sql, params=()):
            return Cursor(self.conn.execute(sql, params))

        async def commit(self):
            self.conn.commit()

        async def rollback(self):
            self.conn.rollback()

        def close(self):
            self.conn.close()

    @asynccontextmanager
    async def get_db():
        db = Connection()
        try:
            yield db
        finally:
            db.close()

    async def initialize():
        async with get_db() as db:
            await db.execute("CREATE TABLE conversations(id TEXT PRIMARY KEY)")
            await db.execute("INSERT INTO conversations(id) VALUES ('conv')")
            await init_web_search_tables(db)
            await db.commit()

    asyncio.run(initialize())
    monkeypatch.setattr(repository_module, "get_db", get_db)
    return WebSearchRepository(), path


def test_repository_capacity_priority_and_atomic_lease(tmp_path, monkeypatch):
    repo, path = _repo(tmp_path, monkeypatch)

    async def run():
        created = []
        for index, source in enumerate(("send", "opportunity", "send")):
            item = await repo.enqueue(
                conv_id="conv",
                origin_source=source,
                origin_turn_id=f"origin-{index}",
                intent_text=f"intent-{index}",
                now=10 + index,
            )
            created.append(item["search_id"])
            await repo.mark_ready(
                item["search_id"],
                result={"digest": str(index), "searched_at": 20 + index},
                now=20 + index,
            )
        replacement = await repo.enqueue(
            conv_id="conv",
            origin_source="send",
            origin_turn_id="new-origin",
            intent_text="new",
            now=30,
        )
        assert replacement["status"] == "queued"
        assert (await repo.get(created[1]))["failure_reason"] == "evicted_buffer_capacity"

        claimed = await repo.claim_ready(conv_id="conv", bound_turn_id="turn-a", now=40)
        assert len(claimed) == 2
        assert await repo.claim_ready(
            conv_id="conv", bound_turn_id="turn-b", now=40 + TURN_LEASE_SEC - 1
        ) == []
        reclaimed = await repo.claim_ready(
            conv_id="conv", bound_turn_id="turn-b", now=40 + TURN_LEASE_SEC + 1
        )
        assert len(reclaimed) == 2
        async with repository_module.get_db() as db:
            consumed = await repo.consume_bound_in_tx(
                db,
                bound_turn_id="turn-b",
                assistant_message_id="assistant",
                now=2000,
            )
            await db.commit()
        assert consumed == 2
        assert len(await repo.consumed_for_assistant("assistant")) == 2
        async with repository_module.get_db() as db:
            reassigned = await repo.reassign_consumed_in_tx(
                db,
                from_assistant_message_id="assistant",
                to_assistant_message_id="assistant-regenerated",
            )
            await db.commit()
        assert reassigned == 2
        assert await repo.consumed_for_assistant("assistant") == []
        assert len(await repo.consumed_for_assistant("assistant-regenerated")) == 2

    asyncio.run(run())


def test_repository_rejects_fourth_when_all_three_are_queued(tmp_path, monkeypatch):
    repo, _path = _repo(tmp_path, monkeypatch)

    async def run():
        for index in range(3):
            result = await repo.enqueue(
                conv_id="conv",
                origin_source="send",
                origin_turn_id=f"turn-{index}",
                intent_text=str(index),
                now=10 + index,
            )
            assert result["status"] == "queued"
        fourth = await repo.enqueue(
            conv_id="conv",
            origin_source="opportunity",
            origin_turn_id="turn-4",
            intent_text="fourth",
            now=20,
        )
        assert fourth == {"status": "buffer_full", "search_id": None}

    asyncio.run(run())


def test_expired_queued_rows_do_not_hold_capacity(tmp_path, monkeypatch):
    repo, _path = _repo(tmp_path, monkeypatch)

    async def run():
        for index in range(3):
            result = await repo.enqueue(
                conv_id="conv",
                origin_source="send",
                origin_turn_id=f"expired-{index}",
                intent_text=str(index),
                now=10,
            )
            assert result["status"] == "queued"

        fresh = await repo.enqueue(
            conv_id="conv",
            origin_source="send",
            origin_turn_id="fresh",
            intent_text="fresh",
            now=200,
        )
        assert fresh["status"] == "queued"
        assert await repo.capacity_snapshot("conv", now=200) == {
            "count": 1,
            "full": False,
            "recent_intent": "fresh",
        }

    asyncio.run(run())
