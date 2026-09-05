import asyncio
import sqlite3
from contextlib import asynccontextmanager

from app.schedule import store


class _AsyncCursor:
    def __init__(self, cursor):
        self._cursor = cursor

    async def fetchone(self):
        return self._cursor.fetchone()

    async def fetchall(self):
        return self._cursor.fetchall()


class _AsyncConn:
    def __init__(self, path):
        self._conn = sqlite3.connect(path)
        self._conn.row_factory = sqlite3.Row

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        self._conn.close()
        return False

    @property
    def row_factory(self):
        return self._conn.row_factory

    @row_factory.setter
    def row_factory(self, value):
        self._conn.row_factory = value

    async def execute(self, sql, params=()):
        return _AsyncCursor(self._conn.execute(sql, params))

    async def commit(self):
        self._conn.commit()


def _patch_store(monkeypatch, tmp_path):
    db_path = tmp_path / "store.db"
    conn = sqlite3.connect(db_path)
    try:
        conn.execute("CREATE TABLE schedules (id TEXT PRIMARY KEY, type TEXT, trigger_at TEXT, content TEXT, created_at REAL, status TEXT)")
        conn.commit()
    finally:
        conn.close()

    @asynccontextmanager
    async def fake_get_db():
        async with _AsyncConn(db_path) as db:
            yield db

    monkeypatch.setattr(store, "get_db", fake_get_db)
    return db_path


def test_store_deduplicates_and_lists_due_without_reminders(monkeypatch, tmp_path):
    _db_path = _patch_store(monkeypatch, tmp_path)

    first = asyncio.run(store.add_schedule("alarm", "2026-05-15T08:00", "起床"))
    second = asyncio.run(store.add_schedule("alarm", "2026-05-15 08:00", "起床"))
    asyncio.run(store.add_schedule("reminder", "2026-05-15", "交作业"))
    asyncio.run(store.add_schedule("monitor", "2026-05-15 08:00", "看状态"))

    assert first
    assert second is None
    due = asyncio.run(store.list_due("2026-05-15 08:00"))
    assert [item["type"] for item in due] == ["alarm", "monitor"]


def test_store_mark_triggered_and_missed(monkeypatch, tmp_path):
    _db_path = _patch_store(monkeypatch, tmp_path)
    alarm = asyncio.run(store.add_schedule("alarm", "2026-05-15 08:00", "起床"))
    monitor = asyncio.run(store.add_schedule("monitor", "2026-05-14 08:00", "看状态"))

    asyncio.run(store.mark_triggered(alarm))
    asyncio.run(store.mark_missed([monitor]))

    active = asyncio.run(store.list_active())
    assert active == []
    assert asyncio.run(store.get_schedule(alarm))["status"] == "triggered"
    assert asyncio.run(store.get_schedule(monitor))["status"] == "missed"
