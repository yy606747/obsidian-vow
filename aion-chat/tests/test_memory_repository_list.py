import asyncio
from contextlib import asynccontextmanager
import json
import sqlite3

from app.memory_v2 import repository


class _AsyncCursor:
    def __init__(self, cursor):
        self._cursor = cursor
        self.rowcount = cursor.rowcount

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
        conn.execute(
            "CREATE TABLE memories ("
            "id TEXT PRIMARY KEY, content TEXT, type TEXT, created_at REAL, source_conv TEXT, "
            "keywords TEXT, importance REAL, source_start_ts REAL, source_end_ts REAL, unresolved INTEGER)"
        )
        conn.execute(
            "CREATE TABLE memory_items ("
            "id TEXT PRIMARY KEY, legacy_memory_id TEXT, kind TEXT, namespace TEXT, content TEXT, "
            "importance REAL, keywords_json TEXT, source_conv TEXT, source_start_ts REAL, source_end_ts REAL, "
            "created_at REAL, updated_at REAL, metadata_json TEXT, status TEXT, visibility TEXT, embedding BLOB)"
        )
        conn.execute(
            "CREATE TABLE messages (id TEXT PRIMARY KEY, role TEXT, content TEXT, created_at REAL)"
        )
        conn.commit()
    finally:
        conn.close()


def _connect(path):
    @asynccontextmanager
    async def fake_get_db():
        async with _AsyncConn(path) as db:
            yield db

    return fake_get_db


def test_list_memories_includes_v2_digest_notes_without_legacy_duplicates(monkeypatch, tmp_path):
    db_path = tmp_path / "memory.db"
    _init_db(db_path)
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            "INSERT INTO memories VALUES (?,?,?,?,?,?,?,?,?,?)",
            ("mem_legacy", "旧记忆摘要", "digest", 10.0, None, "", 0.5, None, None, 0),
        )
        conn.execute(
            "INSERT INTO memory_items VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                "memv2_note",
                None,
                "episode",
                "normal",
                "V2 提炼的短记忆",
                0.8,
                json.dumps(["短记忆"], ensure_ascii=False),
                "conv1",
                20.0,
                22.0,
                30.0,
                30.0,
                json.dumps({"source": "digest.multi_note"}, ensure_ascii=False),
                "active",
                "prompt",
                None,
            ),
        )
        conn.execute(
            "INSERT INTO memory_items VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                "memv2_mem_legacy",
                "mem_legacy",
                "episode",
                "normal",
                "旧记忆摘要",
                0.5,
                "[]",
                None,
                None,
                None,
                10.0,
                10.0,
                "{}",
                "active",
                "prompt",
                None,
            ),
        )
        conn.commit()
    finally:
        conn.close()

    monkeypatch.setattr(repository, "get_db", _connect(db_path))

    items = asyncio.run(repository.list_memories())

    assert [item["id"] for item in items] == ["memv2_note", "mem_legacy"]
    assert items[0]["type"] == "digest_note"
    assert items[0]["source_table"] == "memory_items"


def test_list_memories_page_limits_filters_and_reports_more(monkeypatch, tmp_path):
    db_path = tmp_path / "memory.db"
    _init_db(db_path)
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            "INSERT INTO memories VALUES (?,?,?,?,?,?,?,?,?,?)",
            ("mem_old", "旧记忆", "event", 10.0, None, "", 0.5, None, None, 0),
        )
        conn.execute(
            "INSERT INTO memories VALUES (?,?,?,?,?,?,?,?,?,?)",
            ("mem_mid", "一条 AI 笔记", "ai_note", 20.0, None, "", 0.6, None, None, 0),
        )
        conn.execute(
            "INSERT INTO memory_items VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                "memv2_new",
                None,
                "episode",
                "normal",
                "V2 分页摘要",
                0.8,
                json.dumps(["分页"], ensure_ascii=False),
                "conv1",
                30.0,
                32.0,
                30.0,
                30.0,
                json.dumps({"source": "digest.multi_note"}, ensure_ascii=False),
                "active",
                "prompt",
                None,
            ),
        )
        conn.commit()
    finally:
        conn.close()

    monkeypatch.setattr(repository, "get_db", _connect(db_path))

    first_page = asyncio.run(repository.list_memories_page(limit=2))
    assert [item["id"] for item in first_page["items"]] == ["memv2_new", "mem_mid"]
    assert first_page["has_more"] is True
    assert first_page["next_offset"] == 2
    assert first_page["items"][0]["type"] == "digest_note"

    second_page = asyncio.run(repository.list_memories_page(limit=2, offset=first_page["next_offset"]))
    assert [item["id"] for item in second_page["items"]] == ["mem_old"]
    assert second_page["has_more"] is False

    digest_page = asyncio.run(repository.list_memories_page(limit=10, memory_type="digest_note"))
    assert [item["id"] for item in digest_page["items"]] == ["memv2_new"]

    search_page = asyncio.run(repository.list_memories_page(limit=10, query="AI"))
    assert [item["id"] for item in search_page["items"]] == ["mem_mid"]


def test_v2_memory_source_uses_memory_item_time_range(monkeypatch, tmp_path):
    db_path = tmp_path / "memory.db"
    _init_db(db_path)
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            "INSERT INTO memory_items VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                "memv2_note",
                None,
                "episode",
                "normal",
                "V2 提炼的短记忆",
                0.8,
                "[]",
                "conv1",
                20.0,
                22.0,
                30.0,
                30.0,
                json.dumps({"source": "digest.multi_note"}, ensure_ascii=False),
                "active",
                "prompt",
                None,
            ),
        )
        conn.execute("INSERT INTO messages VALUES (?,?,?,?)", ("msg1", "user", "原文一", 20.0))
        conn.execute("INSERT INTO messages VALUES (?,?,?,?)", ("msg2", "assistant", "原文二", 22.0))
        conn.commit()
    finally:
        conn.close()

    monkeypatch.setattr(repository, "get_db", _connect(db_path))
    monkeypatch.setattr(repository, "load_worldbook", lambda: {"user_name": "用户", "ai_name": "AI"})

    source = asyncio.run(repository.get_memory_source("memv2_note"))

    assert source["ok"] is True
    assert [msg["content"] for msg in source["messages"]] == ["原文一", "原文二"]
