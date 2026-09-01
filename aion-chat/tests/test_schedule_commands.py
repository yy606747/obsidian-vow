import asyncio
import sqlite3
from contextlib import asynccontextmanager
from datetime import datetime
from types import SimpleNamespace

from app.schedule import commands, store
import pytest


@pytest.fixture(autouse=True)
def _stub_alarm_context_capture(monkeypatch):
    async def capture(*_args, **_kwargs):
        return {"status": "captured", "message_count": 0}

    monkeypatch.setattr(
        commands.alarm_context,
        "capture_creation_context",
        capture,
    )


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


def _init_db(path):
    conn = sqlite3.connect(path)
    try:
        conn.execute("CREATE TABLE messages (id TEXT PRIMARY KEY, conv_id TEXT, role TEXT, content TEXT, created_at REAL, attachments TEXT)")
        conn.execute("CREATE TABLE schedules (id TEXT PRIMARY KEY, type TEXT, trigger_at TEXT, content TEXT, created_at REAL, status TEXT)")
        conn.commit()
    finally:
        conn.close()


def _fetch(path, sql):
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    try:
        return [dict(row) for row in conn.execute(sql).fetchall()]
    finally:
        conn.close()


def _freeze_schedule_clock(monkeypatch):
    class FixedDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(2026, 5, 20, 12, 0, 0)

    monkeypatch.setattr(commands, "datetime", FixedDateTime)


def test_process_schedule_commands_writes_schedules_system_messages_and_broadcasts(monkeypatch, tmp_path):
    db_path = tmp_path / "commands.db"
    _init_db(db_path)

    @asynccontextmanager
    async def fake_get_db():
        async with _AsyncConn(db_path) as db:
            yield db

    broadcasts = []
    context_captures = []

    async def fake_broadcast(payload):
        broadcasts.append(payload)

    async def capture_context(schedule_id, conv_id, *, source_message_id=None):
        context_captures.append({
            "schedule_id": schedule_id,
            "conv_id": conv_id,
            "source_message_id": source_message_id,
        })
        return {"status": "captured", "message_count": 1}

    monkeypatch.setattr(store, "get_db", fake_get_db)
    monkeypatch.setattr(commands, "get_db", fake_get_db)
    monkeypatch.setattr(commands, "manager", SimpleNamespace(broadcast=fake_broadcast))
    monkeypatch.setattr(
        commands.alarm_context,
        "capture_creation_context",
        capture_context,
    )
    _freeze_schedule_clock(monkeypatch)

    cleaned, _results = asyncio.run(
        commands.process_schedule_commands_with_results(
            "好的[ALARM:2026-05-25T08:00|起床][REMINDER:2026-05-26|交作业][Monitor:2026-05-25T22:00|看状态]",
            "conv1",
            ai_name="Aion",
            source_message_id="msg-setting-alarm",
        )
    )

    assert cleaned == "好的"
    schedules = _fetch(db_path, "SELECT type, trigger_at, content, status FROM schedules ORDER BY type")
    assert {row["type"] for row in schedules} == {"alarm", "reminder", "monitor"}
    assert all(row["status"] == "active" for row in schedules)
    messages = _fetch(db_path, "SELECT role, content FROM messages ORDER BY created_at")
    assert [row["role"] for row in messages] == ["system", "system", "system"]
    assert all(row["content"].startswith("Aion ") for row in messages)
    assert any("设置了 2026-05-25 08:00 的闹铃" in row["content"] for row in messages)
    assert [payload["type"] for payload in broadcasts].count("schedule_changed") == 3
    assert [payload["type"] for payload in broadcasts].count("msg_created") == 3
    assert len(context_captures) == 1
    assert context_captures[0]["schedule_id"].startswith("sch_")
    assert context_captures[0]["conv_id"] == "conv1"
    assert context_captures[0]["source_message_id"] == "msg-setting-alarm"


def test_process_schedule_commands_deduplicates_and_deletes(monkeypatch, tmp_path):
    db_path = tmp_path / "commands.db"
    _init_db(db_path)

    @asynccontextmanager
    async def fake_get_db():
        async with _AsyncConn(db_path) as db:
            yield db

    broadcasts = []

    async def fake_broadcast(payload):
        broadcasts.append(payload)

    monkeypatch.setattr(store, "get_db", fake_get_db)
    monkeypatch.setattr(commands, "get_db", fake_get_db)
    monkeypatch.setattr(commands, "manager", SimpleNamespace(broadcast=fake_broadcast))
    _freeze_schedule_clock(monkeypatch)

    asyncio.run(commands.process_schedule_commands(
        "[ALARM:2026-05-25T08:00|起床]",
        "conv1",
        ai_name="Aion",
    ))
    sid = _fetch(db_path, "SELECT id FROM schedules")[0]["id"]
    asyncio.run(commands.process_schedule_commands(
        "[ALARM:2026-05-25T08:00|起床][SCHEDULE_DEL:%s]" % sid,
        "conv1",
        ai_name="Aion",
    ))

    schedules = _fetch(db_path, "SELECT status FROM schedules")
    assert schedules == [{"status": "cancelled"}]
    assert [payload["type"] for payload in broadcasts].count("schedule_changed") == 2


def test_parse_dt_corrects_stale_ai_year_and_rejects_past_explicit_date(monkeypatch):
    class FixedDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(2026, 5, 20, 12, 0, 0)

    monkeypatch.setattr(commands, "datetime", FixedDateTime)

    assert commands._parse_dt("2024-05-21 08:00") == "2026-05-21 08:00"
    assert commands._parse_dt("2024-05-19 08:00") is None
    assert commands._parse_dt("05-21 08:00") == "2026-05-21 08:00"


def test_schedule_list_returns_the_current_truthful_snapshot(monkeypatch, tmp_path):
    db_path = tmp_path / "schedule-list.db"
    _init_db(db_path)

    @asynccontextmanager
    async def fake_get_db():
        async with _AsyncConn(db_path) as db:
            yield db

    monkeypatch.setattr(store, "get_db", fake_get_db)
    monkeypatch.setattr(commands, "get_db", fake_get_db)
    asyncio.run(store.add_schedule("alarm", "2026-08-15 08:00", "起床"))

    cleaned, results = asyncio.run(
        commands.process_schedule_commands_with_results(
            "我看一下。[SCHEDULE_LIST]",
            "conv1",
            ai_name="Aion",
        )
    )

    assert cleaned == "我看一下。"
    assert len(results) == 1
    assert results[0]["tool_name"] == "schedule.list"
    assert results[0]["status"] == "succeeded"
    assert results[0]["count"] == 1
    assert results[0]["schedules"][0]["content"] == "起床"
    assert "#" + results[0]["schedules"][0]["id"] in results[0]["schedule_text"]
