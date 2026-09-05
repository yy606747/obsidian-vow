import asyncio
from contextlib import asynccontextmanager
import time

import aiosqlite

from app.chat import crud_routes
from app.self_wake import repository as repository_module
from app.self_wake.repository import SelfWakeRepository
from app.self_wake.schema import init_self_wake_tables
from app.memory_v3.schema import init_memory_v3_tables


def _run(awaitable):
    return asyncio.run(awaitable)


def _database(tmp_path, monkeypatch):
    path = tmp_path / "lifecycle.db"

    @asynccontextmanager
    async def get_db():
        async with aiosqlite.connect(path) as db:
            yield db

    async def initialize():
        async with get_db() as db:
            await db.execute(
                "CREATE TABLE conversations "
                "(id TEXT PRIMARY KEY, title TEXT, model TEXT, created_at REAL, updated_at REAL)"
            )
            await db.execute(
                "CREATE TABLE messages "
                "(id TEXT PRIMARY KEY, conv_id TEXT, role TEXT, content TEXT, "
                "created_at REAL, attachments TEXT, "
                "FOREIGN KEY (conv_id) REFERENCES conversations(id) ON DELETE CASCADE)"
            )
            await db.execute(
                "CREATE TABLE memory_chunks (id TEXT PRIMARY KEY, conv_id TEXT, "
                "message_ids_json TEXT NOT NULL DEFAULT '[]', content TEXT, "
                "created_at REAL, updated_at REAL, embedding BLOB, "
                "keywords_json TEXT NOT NULL DEFAULT '[]', metadata_json TEXT NOT NULL DEFAULT '{}')"
            )
            await db.execute(
                "CREATE TABLE memory_items (id TEXT PRIMARY KEY, legacy_memory_id TEXT, "
                "metadata_json TEXT NOT NULL DEFAULT '{}')"
            )
            await init_memory_v3_tables(db)
            await db.execute(
                "INSERT INTO conversations VALUES ('conv','title','m',0,0)"
            )
            await db.execute(
                "INSERT INTO messages VALUES ('source','conv','assistant','x',0,'[]')"
            )
            await init_self_wake_tables(db)
            await db.commit()

    _run(initialize())
    monkeypatch.setattr(repository_module, "get_db", get_db)
    monkeypatch.setattr(crud_routes, "get_db", get_db)
    return path, get_db


async def _insert_pending(get_db, *, wake_id="wake_pending", wake_at=None):
    now = time.time()
    wake_at = now + 60 if wake_at is None else wake_at
    async with get_db() as db:
        await db.execute(
            "INSERT INTO self_wakes "
            "(id,wake_at,intent,requested_capabilities_json,origin,origin_ref,source,"
            "conv_id,source_turn_id,owner_timezone,state,created_at,expires_at) "
            "VALUES (?,?, 'intent','[]','relationship','conv','chat','conv','source',"
            "'UTC','pending',?,?)",
            (wake_id, wake_at, now, wake_at + 7200),
        )
        await db.commit()


def _patch_route_dependencies(monkeypatch):
    class Vows:
        async def revoke_for_origin_conversation_in_tx(self, *_args, **_kwargs):
            return 0

    class Manager:
        async def broadcast(self, _event):
            return None

    async def delete_related(*_args, **_kwargs):
        return 0

    monkeypatch.setattr(crud_routes, "vow_service", Vows())
    monkeypatch.setattr(crud_routes, "manager", Manager())
    monkeypatch.setattr(
        crud_routes.PendingRecallRepository,
        "delete_for_conversation_in_tx",
        staticmethod(delete_related),
    )
    monkeypatch.setattr(
        crud_routes.WebSearchRepository,
        "delete_for_conversation_in_tx",
        staticmethod(delete_related),
    )
    monkeypatch.setattr("routes.files.delete_exported_file", lambda _conv_id: None)


def test_conversation_delete_invalidates_pending_in_same_transaction(tmp_path, monkeypatch):
    _path, get_db = _database(tmp_path, monkeypatch)
    _run(_insert_pending(get_db))
    _patch_route_dependencies(monkeypatch)

    result = _run(crud_routes.delete_conversation("conv"))
    assert result == {"ok": True}

    async def inspect():
        async with get_db() as db:
            cursor = await db.execute(
                "SELECT state, close_reason FROM self_wakes WHERE id='wake_pending'"
            )
            wake = await cursor.fetchone()
            cursor = await db.execute(
                "SELECT COUNT(*) FROM conversations WHERE id='conv'"
            )
            count = (await cursor.fetchone())[0]
            return wake, count

    wake, count = _run(inspect())
    assert wake == ("invalidated", "origin_terminated")
    assert count == 0


def test_delete_failure_rolls_back_both_origin_and_invalidation(tmp_path, monkeypatch):
    _path, get_db = _database(tmp_path, monkeypatch)
    _run(_insert_pending(get_db))
    _patch_route_dependencies(monkeypatch)

    async def add_abort_trigger():
        async with get_db() as db:
            await db.execute(
                "CREATE TRIGGER prevent_conv_delete BEFORE DELETE ON conversations "
                "BEGIN SELECT RAISE(ABORT, 'keep'); END"
            )
            await db.commit()

    _run(add_abort_trigger())
    result = _run(crud_routes.delete_conversation("conv"))
    assert result == {"ok": False, "error": "delete_failed"}

    async def inspect():
        async with get_db() as db:
            cursor = await db.execute(
                "SELECT state, close_reason FROM self_wakes WHERE id='wake_pending'"
            )
            wake = await cursor.fetchone()
            cursor = await db.execute(
                "SELECT COUNT(*) FROM conversations WHERE id='conv'"
            )
            return wake, (await cursor.fetchone())[0]

    wake, count = _run(inspect())
    assert wake == ("pending", None)
    assert count == 1


def test_source_message_deletion_does_not_cancel_relationship_wake(tmp_path, monkeypatch):
    _path, get_db = _database(tmp_path, monkeypatch)
    _run(_insert_pending(get_db))

    async def delete_source():
        async with get_db() as db:
            await db.execute("DELETE FROM messages WHERE id='source'")
            await db.commit()
            cursor = await db.execute(
                "SELECT state FROM self_wakes WHERE id='wake_pending'"
            )
            return (await cursor.fetchone())[0]

    assert _run(delete_source()) == "pending"


def test_two_hour_late_row_expires_without_provider_claim(tmp_path, monkeypatch):
    _path, get_db = _database(tmp_path, monkeypatch)
    wake_at = time.time() - 2 * 60 * 60
    _run(_insert_pending(get_db, wake_id="wake_late", wake_at=wake_at))
    repo = SelfWakeRepository()
    claimed = _run(repo.claim_due_batch(now=time.time(), timezone_name="UTC"))
    assert claimed == []
    row = _run(repo.get("wake_late"))
    assert row["state"] == "expired"
    assert row["close_reason"] == "expired"
