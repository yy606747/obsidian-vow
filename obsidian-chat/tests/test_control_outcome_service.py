import asyncio
import json
import sqlite3
from contextlib import asynccontextmanager

import ai_providers
import config

from app.control.ledger import ControlLedger
from app.control.outcome import ControlOutcomeService, init_control_outcome_tables
from app.control.agenda import ControlAgendaService, init_control_agenda_tables
from app.control.service import ControlSessionService, init_control_tables


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


async def _init_db(path):
    async with _AsyncConn(path) as db:
        await db.execute("CREATE TABLE messages (id TEXT PRIMARY KEY, conv_id TEXT, role TEXT, content TEXT, created_at REAL, attachments TEXT)")
        await db.execute("CREATE TABLE memory_events (id TEXT PRIMARY KEY, source TEXT NOT NULL, namespace TEXT NOT NULL DEFAULT 'normal', conv_id TEXT, role TEXT, content TEXT NOT NULL, metadata_json TEXT NOT NULL DEFAULT '{}', created_at REAL NOT NULL)")
        await init_control_tables(db)
        await init_control_outcome_tables(db)
        await init_control_agenda_tables(db)
        await db.commit()


def _service(tmp_path, *, now_ref, summarizer=None):
    db_path = tmp_path / "outcome.db"
    asyncio.run(_init_db(db_path))

    @asynccontextmanager
    async def fake_get_db():
        async with _AsyncConn(db_path) as db:
            yield db

    ledger = ControlLedger(get_db_factory=fake_get_db, now=lambda: now_ref[0])
    outcome = ControlOutcomeService(get_db_factory=fake_get_db, now=lambda: now_ref[0], ledger=ledger, summarizer=summarizer)
    service = ControlSessionService(get_db_factory=fake_get_db, now=lambda: now_ref[0], ledger=ledger, outcome_service=outcome)
    return service, db_path


def _rows(db_path, table):
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        return [dict(row) for row in conn.execute(f"SELECT * FROM {table} ORDER BY created_at")]
    finally:
        conn.close()


def test_normal_end_generates_outcome_and_control_notes(tmp_path):
    now = [1000.0]

    async def summarizer(context):
        assert context["close_reason"] == "normal"
        assert context["debug"]["message_count_used"] == 2
        return {"summary": "本次会话稳定收束。", "notes": ["偏好慢一点", "对停止响应好", "保留边界", "第四条丢弃"], "model": "fake"}

    service, db_path = _service(tmp_path, now_ref=now, summarizer=summarizer)

    async def flow():
        session = await service.start(conv_id="conv1", kind="dom", owner_client_id="tab1")
        async with service._get_db() as db:
            await db.execute("INSERT INTO messages VALUES (?,?,?,?,?,?)", ("m1", "conv1", "user", "继续", 1001.0, "[]"))
            await db.execute("INSERT INTO messages VALUES (?,?,?,?,?,?)", ("m2", "conv1", "assistant", "好。", 1002.0, "[]"))
            await db.commit()
        now[0] = 1010.0
        ended = await service.end(session_id=session.session_id, owner_client_id="tab1", close_reason="normal")
        await asyncio.sleep(0)
        return ended

    ended = asyncio.run(flow())
    outcomes = _rows(db_path, "control_session_outcomes")
    events = _rows(db_path, "memory_events")

    assert ended.status == "ended"
    assert outcomes[0]["outcome_status"] == "completed"
    assert outcomes[0]["summary"] == "本次会话稳定收束。"
    assert json.loads(outcomes[0]["notes_json"]) == ["偏好慢一点", "对停止响应好", "保留边界"]
    assert "session:" in json.loads(outcomes[0]["source_refs_json"])[0]
    event_types = [json.loads(event["metadata_json"])["event_type"] for event in events]
    assert "control.session.outcome" in event_types
    assert {event["source"] for event in events} == {"control"}
    assert {event["namespace"] for event in events} == {"control"}
    assert [event["content"] for event in events].count("偏好慢一点") == 1


def test_default_outcome_summarizer_uses_control_outcome_slot(tmp_path, monkeypatch):
    now = [1500.0]
    calls = []

    async def fake_call_slot_chat(slot_name, messages, expect_json=False, temperature=None):
        calls.append({"slot_name": slot_name, "messages": messages, "expect_json": expect_json, "temperature": temperature})
        return json.dumps({"summary": "她最后主动放慢了节奏。", "notes": ["下次先接慢节奏", "不要暗示设备已执行"]}, ensure_ascii=False)

    monkeypatch.setattr(config, "get_slot", lambda name: {"endpoint": "fake"} if name == "control_outcome" else None)
    monkeypatch.setattr(ai_providers, "call_slot_chat", fake_call_slot_chat)
    service, db_path = _service(tmp_path, now_ref=now)

    async def flow():
        session = await service.start(conv_id="conv_model", kind="dom", owner_client_id="tab1")
        async with service._get_db() as db:
            await db.execute("INSERT INTO messages VALUES (?,?,?,?,?,?)", ("old", "conv_model", "user", "旧消息不该进 summarizer", 1499.0, "[]"))
            await db.execute("INSERT INTO messages VALUES (?,?,?,?,?,?)", ("m1", "conv_model", "user", "慢一点", 1501.0, "[]"))
            await db.commit()
        now[0] = 1510.0
        await service.end(session_id=session.session_id, owner_client_id="tab1", close_reason="normal")
        await asyncio.sleep(0)

    asyncio.run(flow())
    outcomes = _rows(db_path, "control_session_outcomes")
    payload = json.loads(calls[0]["messages"][1]["content"])

    assert calls[0]["slot_name"] == "control_outcome"
    assert calls[0]["expect_json"] is True
    assert "旧消息不该进 summarizer" not in calls[0]["messages"][1]["content"]
    assert payload["messages"] == [{"role": "user", "content": "慢一点"}]
    assert outcomes[0]["summary"] == "她最后主动放慢了节奏。"
    assert json.loads(outcomes[0]["notes_json"]) == ["下次先接慢节奏", "不要暗示设备已执行"]


def test_panic_outcome_is_fixed_and_does_not_call_summarizer(tmp_path):
    now = [2000.0]

    async def fail_summarizer(_context):
        raise AssertionError("panic outcome must not summarize")

    service, db_path = _service(tmp_path, now_ref=now, summarizer=fail_summarizer)
    session = asyncio.run(service.start(conv_id="conv2", kind="whisper", owner_client_id="tab1"))
    ended = asyncio.run(service.end(session_id=session.session_id, owner_client_id="tab1", close_reason="panic"))
    outcomes = _rows(db_path, "control_session_outcomes")

    assert ended.status == "ended"
    assert outcomes[0]["outcome_status"] == "completed"
    assert outcomes[0]["summary"] == "控制会话因安全停止而结束。"
    assert json.loads(outcomes[0]["notes_json"]) == []


def test_outcome_failure_writes_deterministic_fallback(tmp_path):
    now = [3000.0]

    async def broken_summarizer(_context):
        raise RuntimeError("model down")

    service, db_path = _service(tmp_path, now_ref=now, summarizer=broken_summarizer)

    async def flow():
        session = await service.start(conv_id="conv3", kind="dom", owner_client_id="tab1")
        ended = await service.end(session_id=session.session_id, owner_client_id="tab1", close_reason="normal")
        await asyncio.sleep(0)
        return ended

    ended = asyncio.run(flow())
    outcomes = _rows(db_path, "control_session_outcomes")

    assert ended.status == "ended"
    assert outcomes[0]["outcome_status"] == "completed"
    assert outcomes[0]["summary"].startswith("本次控制会话已正常结束。")
    assert "model down" in outcomes[0]["metadata_json"]
    assert "fallback_used" in outcomes[0]["metadata_json"]


def test_next_control_session_agenda_reads_previous_outcome_but_normal_chat_does_not(tmp_path):
    now = [4000.0]

    async def summarizer(_context):
        return {"summary": "上一轮她最后愿意放慢下来。", "notes": ["下次先接住慢节奏"], "model": "fake"}

    service, _db_path = _service(tmp_path, now_ref=now, summarizer=summarizer)

    async def flow():
        first = await service.start(conv_id="conv_follow", kind="dom", owner_client_id="tab1")
        async with service._get_db() as db:
            await db.execute("INSERT INTO messages VALUES (?,?,?,?,?,?)", ("m1", "conv_follow", "user", "慢一点", 4001.0, "[]"))
            await db.commit()
        now[0] = 4010.0
        await service.end(session_id=first.session_id, owner_client_id="tab1", close_reason="normal")
        await asyncio.sleep(0)
        ordinary = await service.get_prompt_context("conv_follow", {"content": "日常说一句"})
        now[0] = 4020.0
        second = await service.start(conv_id="conv_follow", kind="dom", owner_client_id="tab2")
        agenda = await ControlAgendaService(get_db_factory=service._get_db, now=lambda: now[0]).generate_for_session(second.session_id)
        control_ctx = await service.get_prompt_context("conv_follow", {"ai_dom_mode": True})
        return ordinary, agenda, control_ctx

    ordinary, agenda, control_ctx = asyncio.run(flow())

    assert ordinary.source == "none"
    assert ordinary.hidden_agenda_status == "none"
    assert agenda["agenda_status"] == "ready"
    assert "上一轮她最后愿意放慢下来" in agenda["brief"]
    assert control_ctx.hidden_agenda_status == "ready"
    assert "上一轮她最后愿意放慢下来" in control_ctx.hidden_agenda_brief


def test_outcome_completion_refreshes_already_started_next_session_agenda(tmp_path):
    now = [5000.0]

    async def summarizer(_context):
        return {"summary": "上一轮结束时她认真停住了。", "notes": [], "model": "fake"}

    service, db_path = _service(tmp_path, now_ref=now, summarizer=summarizer)

    async def flow():
        first = await service.start(conv_id="conv_race", kind="dom", owner_client_id="tab1")
        now[0] = 5010.0
        await service.end(session_id=first.session_id, owner_client_id="tab1", close_reason="normal")
        now[0] = 5011.0
        second = await service.start(conv_id="conv_race", kind="dom", owner_client_id="tab2")
        await asyncio.sleep(0.05)
        return second

    second = asyncio.run(flow())
    agendas = [row for row in _rows(db_path, "control_agendas") if row["session_id"] == second.session_id]

    assert agendas[-1]["agenda_status"] == "ready"
    assert "上一轮结束时她认真停住了" in agendas[-1]["brief"]
