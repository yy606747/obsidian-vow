from __future__ import annotations

import asyncio
import sqlite3
from contextlib import asynccontextmanager

from app.schedule import alarm_context


def _run(awaitable):
    return asyncio.run(awaitable)


class _AsyncCursor:
    def __init__(self, cursor):
        self._cursor = cursor

    @property
    def rowcount(self):
        return self._cursor.rowcount

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

    async def executemany(self, sql, params):
        return _AsyncCursor(self._conn.executemany(sql, params))

    async def commit(self):
        self._conn.commit()


def _setup(tmp_path, monkeypatch):
    path = tmp_path / "alarm-context.db"

    @asynccontextmanager
    async def fake_get_db():
        async with _AsyncConn(path) as db:
            yield db

    async def initialize():
        async with fake_get_db() as db:
            await db.execute(
                "CREATE TABLE schedules (id TEXT PRIMARY KEY, type TEXT, "
                "trigger_at TEXT, content TEXT, created_at REAL, status TEXT)"
            )
            await db.execute(
                "CREATE TABLE messages (id TEXT PRIMARY KEY, conv_id TEXT, role TEXT, "
                "content TEXT, created_at REAL, attachments TEXT)"
            )
            await alarm_context.init_alarm_context_tables(db)
            await db.execute(
                "INSERT INTO schedules VALUES (?,?,?,?,?,?)",
                ("alarm-1", "alarm", "2026-08-25 07:10", "早八叫醒", 10.0, "active"),
            )
            await db.executemany(
                "INSERT INTO messages VALUES (?,?,?,?,?,?)",
                [
                    ("m1", "conv-1", "user", "昨晚身体有点不舒服", 100.0, "[]"),
                    ("m2", "conv-1", "assistant", "那就早点睡。", 101.0, "[]"),
                    ("m3", "conv-1", "system", "[相关记忆] 不应冻结", 102.0, "[]"),
                    ("m4", "conv-1", "user", "明早八点上课，七点十分叫我。", 103.0, "[]"),
                    ("m5", "conv-1", "assistant", "这是锚点之后的消息。", 104.0, "[]"),
                ],
            )
            await db.commit()

    monkeypatch.setattr(alarm_context, "get_db", fake_get_db)
    _run(initialize())
    return path


def test_alarm_context_freezes_visible_messages_at_the_source_turn(tmp_path, monkeypatch):
    path = _setup(tmp_path, monkeypatch)

    missing = _run(alarm_context.capture_creation_context(
        "alarm-1",
        "conv-1",
        source_message_id="does-not-exist",
    ))
    assert missing == {
        "status": "source_missing",
        "message_count": 0,
        "source_message_ids": [],
    }

    result = _run(alarm_context.capture_creation_context(
        "alarm-1",
        "conv-1",
        source_message_id="m4",
    ))

    assert result["status"] == "captured"
    assert result["source_message_ids"] == ["m1", "m2", "m4"]
    conn = sqlite3.connect(path)
    try:
        stored = conn.execute(
            "SELECT messages_json FROM alarm_creation_contexts WHERE schedule_id='alarm-1'"
        ).fetchone()[0]
        assert "明早八点上课" in stored
        assert "[相关记忆]" not in stored
        assert "锚点之后" not in stored

        conn.execute("UPDATE messages SET content='后来被编辑' WHERE id='m4'")
        conn.commit()
    finally:
        conn.close()

    second = _run(alarm_context.capture_creation_context(
        "alarm-1",
        "conv-1",
        source_message_id="m4",
    ))
    assert second["status"] == "exists"
    prompt = _run(alarm_context.load_prompt_context(
        [{
            "id": "alarm-1",
            "type": "alarm",
            "trigger_at": "2026-08-25 07:10",
            "content": "早八叫醒",
        }],
        user_name="Yang",
        ai_name="Alaric",
    ))
    assert prompt["status"] == "loaded"
    assert prompt["visible_messages"] == [{"id": "m1"}, {"id": "m2"}, {"id": "m4"}]
    assert "明早八点上课" in prompt["block"]
    assert "后来被编辑" not in prompt["block"]
    assert "历史快照，不自动代表现在" in prompt["block"]


def test_alarm_context_missing_snapshot_is_a_clean_fallback(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)

    prompt = _run(alarm_context.load_prompt_context(
        [{
            "id": "old-alarm",
            "type": "alarm",
            "trigger_at": "2026-08-25 08:00",
            "content": "旧闹钟",
        }],
        user_name="Yang",
        ai_name="Alaric",
    ))

    assert prompt == {
        "status": "missing",
        "block": "",
        "schedule_ids": ["old-alarm"],
        "visible_messages": [],
        "message_count": 0,
    }
