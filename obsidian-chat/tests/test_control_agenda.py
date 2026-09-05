import asyncio
import json
import sqlite3
from contextlib import asynccontextmanager

from app.chat.models import MsgCreate
from app.chat import prompt_builder
from app.control.agenda import ControlAgendaService, init_control_agenda_tables
from app.control.outcome import init_control_outcome_tables
from app.control.service import ControlSessionService, init_control_tables


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


async def _init_db(path):
    async with _AsyncConn(path) as db:
        await db.execute("CREATE TABLE messages (id TEXT PRIMARY KEY, conv_id TEXT, role TEXT, content TEXT, created_at REAL, attachments TEXT)")
        await db.execute("CREATE TABLE memory_events (id TEXT PRIMARY KEY, source TEXT NOT NULL, namespace TEXT NOT NULL DEFAULT 'normal', conv_id TEXT, role TEXT, content TEXT NOT NULL, metadata_json TEXT NOT NULL DEFAULT '{}', created_at REAL NOT NULL)")
        await init_control_tables(db)
        await init_control_outcome_tables(db)
        await init_control_agenda_tables(db)
        await db.commit()


def _db(tmp_path):
    path = tmp_path / "agenda.db"
    asyncio.run(_init_db(path))

    @asynccontextmanager
    async def fake_get_db():
        async with _AsyncConn(path) as db:
            yield db

    return path, fake_get_db


def _rows(path, table):
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    try:
        return [dict(row) for row in conn.execute(f"SELECT * FROM {table} ORDER BY created_at")]
    finally:
        conn.close()


def test_agenda_job_is_nonblocking_and_runs_once(tmp_path):
    path, get_db = _db(tmp_path)
    now = [1000.0]
    service = ControlSessionService(get_db_factory=get_db, now=lambda: now[0])

    async def flow():
        async with get_db() as db:
            await db.execute(
                "INSERT INTO control_session_outcomes VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                ("out1", "old", "conv1", "dom", "normal", "completed", "上次稳定收束。", "[]", json.dumps(["out1"]), "{}", 900.0, 900.0),
            )
            await db.commit()
        session = await service.start(conv_id="conv1", kind="dom", owner_client_id="tab1")
        await ControlAgendaService(get_db_factory=get_db, now=lambda: now[0]).schedule_for_session(session.session_id)
        await asyncio.sleep(0.05)
        return session

    session = asyncio.run(flow())
    rows = _rows(path, "control_agendas")

    assert len(rows) == 1
    assert rows[0]["session_id"] == session.session_id
    assert rows[0]["agenda_status"] == "ready"
    assert "上次稳定收束" in rows[0]["brief"]


def test_agenda_empty_when_facts_are_insufficient(tmp_path):
    path, get_db = _db(tmp_path)
    now = [2000.0]
    service = ControlSessionService(get_db_factory=get_db, now=lambda: now[0])

    async def flow():
        session = await service.start(conv_id="conv2", kind="whisper", owner_client_id="tab1")
        agenda = ControlAgendaService(get_db_factory=get_db, now=lambda: now[0])
        await agenda.generate_for_session(session.session_id)
        return session

    session = asyncio.run(flow())
    rows = _rows(path, "control_agendas")

    assert rows[0]["session_id"] == session.session_id
    assert rows[0]["agenda_status"] == "empty"


def test_ready_agenda_injects_only_brief_and_stance_into_prompt(tmp_path, monkeypatch):
    path, get_db = _db(tmp_path)
    now = [3000.0]
    service = ControlSessionService(get_db_factory=get_db, now=lambda: now[0])
    monkeypatch.setattr(prompt_builder, "get_active_schedules", lambda: _async_value([]))
    monkeypatch.setattr(prompt_builder, "build_schedule_prompt", lambda _schedules: "（无）")

    async def flow():
        session_id = "ctrl_prompt"
        async with get_db() as db:
            await db.execute(
                "INSERT INTO control_sessions (session_id, conv_id, kind, status, owner_client_id, started_at, last_heartbeat_at, control_epoch, safeword_set, metadata_json) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (session_id, "conv3", "whisper", "active", "tab1", 3000.0, 3000.0, 0, 0, "{}"),
            )
            await db.execute(
                "INSERT OR REPLACE INTO control_agendas (agenda_id, session_id, conv_id, kind, agenda_status, brief, stance, source_refs_json, agenda_json, error, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                ("ag_ready", session_id, "conv3", "whisper", "ready", "只注入短暗线", "保持克制", json.dumps(["out1"]), json.dumps({"raw": "这段原文不能进 prompt"}), "", 3001.0, 3001.0),
            )
            await db.commit()
        ctx = await service.get_prompt_context("conv3", {"whisper_mode": True})
        block = await prompt_builder.build_send_ability_block(conv_id="conv3", body=MsgCreate(content="hi", whisper_mode=True), user_name="用户", capabilities=("device.toy",), control_context=ctx)
        return ctx, block

    ctx, block = asyncio.run(flow())

    assert ctx.hidden_agenda_status == "ready"
    assert ctx.hidden_agenda_source_refs == ["out1"]
    assert "只注入短暗线" in block
    assert "保持克制" in block
    assert "这段原文不能进 prompt" not in block


async def _async_value(value):
    return value
