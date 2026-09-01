import asyncio
import sqlite3
import sys
from contextlib import asynccontextmanager
from types import SimpleNamespace

import schedule
from app.schedule import commands as schedule_commands
from app.schedule import store as schedule_store
from app.schedule import trigger as schedule_trigger
import pytest


@pytest.fixture(autouse=True)
def _empty_vow_context(monkeypatch):
    """誓约层（Phase 2）在本管道注入常驻读取；既有用例用空桩隔离，不读真实库。"""

    class _Stub:
        async def load_vow_prompt_context(self):
            return "", ""

    monkeypatch.setattr(schedule_trigger, "vow_service", _Stub())



def _init_db(path):
    conn = sqlite3.connect(path)
    try:
        conn.execute("""
            CREATE TABLE conversations (
                id TEXT PRIMARY KEY,
                title TEXT NOT NULL,
                model TEXT NOT NULL,
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL
            )
        """)
        conn.execute("""
            CREATE TABLE messages (
                id TEXT PRIMARY KEY,
                conv_id TEXT NOT NULL,
                role TEXT NOT NULL,
                content TEXT NOT NULL,
                created_at REAL NOT NULL,
                attachments TEXT DEFAULT ''
            )
        """)
        conn.execute("""
            CREATE TABLE schedules (
                id TEXT PRIMARY KEY,
                type TEXT NOT NULL,
                trigger_at TEXT NOT NULL,
                content TEXT NOT NULL,
                created_at REAL NOT NULL,
                status TEXT NOT NULL DEFAULT 'active'
            )
        """)
        conn.commit()
    finally:
        conn.close()


def _insert_conversation(path):
    conn = sqlite3.connect(path)
    try:
        conn.execute(
            "INSERT INTO conversations (id, title, model, created_at, updated_at) VALUES (?,?,?,?,?)",
            ("conv_monitor", "Monitor", "mock-model", 1.0, 2.0),
        )
        conn.execute(
            "INSERT INTO messages (id, conv_id, role, content, created_at, attachments) VALUES (?,?,?,?,?,?)",
            ("msg_user", "conv_monitor", "user", "我要专心工作一会", 1.0, "[]"),
        )
        conn.commit()
    finally:
        conn.close()


def _insert_monitor(path):
    conn = sqlite3.connect(path)
    try:
        conn.execute(
            "INSERT INTO schedules (id, type, trigger_at, content, created_at, status) VALUES (?,?,?,?,?,?)",
            ("sch_monitor", "monitor", "2026-05-14 20:00", "看她有没有休息", 1.0, "active"),
        )
        conn.commit()
    finally:
        conn.close()


def _fetch_all(path, sql):
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    try:
        return [dict(row) for row in conn.execute(sql).fetchall()]
    finally:
        conn.close()


class _AsyncCursor:
    def __init__(self, cursor):
        self._cursor = cursor

    async def fetchone(self):
        return self._cursor.fetchone()

    async def fetchall(self):
        return self._cursor.fetchall()


class _AsyncSqliteConn:
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


def _patch_monitor_env(monkeypatch, tmp_path, *, with_conversation=True):
    db_path = tmp_path / "monitor.db"
    _init_db(db_path)
    if with_conversation:
        _insert_conversation(db_path)
    _insert_monitor(db_path)

    @asynccontextmanager
    async def fake_get_db():
        async with _AsyncSqliteConn(db_path) as db:
            yield db

    broadcasts = []
    monitor_logs = []

    async def fake_broadcast(payload):
        broadcasts.append(payload)

    for module in (schedule_store, schedule_trigger, schedule_commands):
        monkeypatch.setattr(module, "get_db", fake_get_db, raising=False)
    for module in (schedule_trigger, schedule_commands):
        monkeypatch.setattr(module, "manager", SimpleNamespace(broadcast=fake_broadcast))
    monkeypatch.setattr(schedule_trigger, "append_monitor_log", lambda entry: monitor_logs.append(dict(entry)))
    monkeypatch.setattr(schedule_trigger, "load_worldbook", lambda: {"user_name": "用户", "ai_name": "Aion"})
    monkeypatch.setattr(schedule_trigger.store, "list_active", _fake_active_schedules)

    class _EmptyVowService:
        async def load_vow_prompt_context(self):
            return "", ""

    monkeypatch.setattr(schedule_trigger, "vow_service", _EmptyVowService())

    class _NoopLedger:
        def new_invocation_id(self, _prefix):
            return "schedule_monitor_test"

        async def record_model_request(self, *_args, **_kwargs):
            return 0

        async def record_model_output(self, *_args, **_kwargs):
            return 0

        async def record_postprocess(self, *_args, **_kwargs):
            return 0

        async def record_execution(self, *_args, **_kwargs):
            return 0

        async def record_visible_message(self, *_args, **_kwargs):
            return 0

        async def record_turn(self, *_args, **_kwargs):
            return 0

    monkeypatch.setattr(schedule_trigger, "tool_invocation_ledger", _NoopLedger())

    async def skip_timeline_usage(*_args, **_kwargs):
        return {"status": "skipped", "count": 0}

    monkeypatch.setattr(
        schedule_trigger.timeline_service,
        "start_background_refresh",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        schedule_trigger.timeline_service,
        "record_injection_usage",
        skip_timeline_usage,
    )

    monkeypatch.setattr(schedule_trigger, "export_conversation", _noop_export)

    return db_path, broadcasts, monitor_logs


async def _fake_active_schedules():
    return []


async def _noop_export(_conv_id):
    return None


def _monitor_item():
    return {
        "id": "sch_monitor",
        "type": "monitor",
        "trigger_at": "2026-05-14 20:00",
        "content": "看她有没有休息",
    }


def test_monitor_log_output_is_best_effort_when_append_fails(monkeypatch):
    broadcasts = []

    def fail_append(_entry):
        raise RuntimeError("disk down")

    async def fake_broadcast(payload):
        broadcasts.append(payload)

    monkeypatch.setattr(schedule_trigger, "append_monitor_log", fail_append)
    monkeypatch.setattr(schedule_trigger, "manager", SimpleNamespace(broadcast=fake_broadcast))

    asyncio.run(schedule_trigger._append_and_broadcast_monitor_log({
        "status": "started",
        "monitoringlog": "查岗开始",
    }))

    assert broadcasts == [{
        "type": "monitor_log",
        "data": {"status": "started", "monitoringlog": "查岗开始"},
    }]


def test_fire_monitor_writes_messages_and_monitor_logs(monkeypatch, tmp_path):
    db_path, broadcasts, monitor_logs = _patch_monitor_env(monkeypatch, tmp_path)
    captured = {}

    async def fake_stream_ai(messages, model_key, temperature=None):
        captured["messages"] = messages
        captured["model_key"] = model_key
        yield "起来喝口水。"

    monkeypatch.setitem(sys.modules, "activity", SimpleNamespace(
        get_activity_summary_for_prompt=lambda _n: "过去两小时主要在写代码。"
    ))
    monkeypatch.setitem(sys.modules, "sensing", SimpleNamespace(
        format_sensing_for_prompt=lambda **_kwargs: "19:40 解锁；19:50 光线较亮。"
    ))
    monkeypatch.setattr(schedule_trigger, "stream_ai", fake_stream_ai)

    asyncio.run(schedule.ScheduleManager()._fire_monitor(_monitor_item()))

    statuses = [entry["status"] for entry in monitor_logs]
    assert statuses == ["started", "succeeded"]
    assert monitor_logs[0]["call_core"] is True
    assert monitor_logs[1]["ai_msg_id"].endswith("_ma")
    assert captured["model_key"] == "mock-model"
    prompt = captured["messages"][-1]["content"]
    assert "定时查岗触发" in prompt
    assert "过去两小时主要在写代码" in prompt
    assert "19:40 解锁" in prompt
    assert "摄像头" not in prompt

    schedules = _fetch_all(db_path, "SELECT status FROM schedules")
    assert schedules == [{"status": "triggered"}]
    messages = _fetch_all(db_path, "SELECT role, content FROM messages ORDER BY created_at")
    assert [row["role"] for row in messages] == ["user", "system", "trigger", "assistant"]
    assert messages[-1]["content"] == "起来喝口水。"
    assert "monitor_alert" in [payload["type"] for payload in broadcasts]
    assert [payload["data"]["status"] for payload in broadcasts if payload["type"] == "monitor_log"] == statuses


def test_fire_monitor_records_partial_evidence_failures(monkeypatch, tmp_path):
    _db_path, _broadcasts, monitor_logs = _patch_monitor_env(monkeypatch, tmp_path)
    captured = {}

    def fail_activity(_n):
        raise RuntimeError("activity boom")

    def fail_sensing(**_kwargs):
        raise RuntimeError("sensing boom")

    async def fake_stream_ai(messages, model_key, temperature=None):
        captured["prompt"] = messages[-1]["content"]
        yield "不确定你在干嘛，出来冒个泡。"

    monkeypatch.setitem(sys.modules, "activity", SimpleNamespace(
        get_activity_summary_for_prompt=fail_activity
    ))
    monkeypatch.setitem(sys.modules, "sensing", SimpleNamespace(
        format_sensing_for_prompt=fail_sensing
    ))
    monkeypatch.setattr(schedule_trigger, "stream_ai", fake_stream_ai)

    asyncio.run(schedule.ScheduleManager()._fire_monitor(_monitor_item()))

    statuses = [entry["status"] for entry in monitor_logs]
    assert statuses == ["started", "evidence_partial", "succeeded"]
    assert "activity_summary_failed" in monitor_logs[1]["evidence_errors"][0]
    assert "sensing_timeline_failed" in monitor_logs[1]["evidence_errors"][1]
    assert "部分查岗证据读取失败" in captured["prompt"]
    assert "不能把缺失数据当成她没有活动" in captured["prompt"]


def test_fire_monitor_records_empty_core_reply(monkeypatch, tmp_path):
    db_path, _broadcasts, monitor_logs = _patch_monitor_env(monkeypatch, tmp_path)

    async def fake_stream_ai(messages, model_key, temperature=None):
        if False:
            yield ""

    monkeypatch.setitem(sys.modules, "activity", SimpleNamespace(
        get_activity_summary_for_prompt=lambda _n: ""
    ))
    monkeypatch.setitem(sys.modules, "sensing", SimpleNamespace(
        format_sensing_for_prompt=lambda **_kwargs: ""
    ))
    monkeypatch.setattr(schedule_trigger, "stream_ai", fake_stream_ai)

    asyncio.run(schedule.ScheduleManager()._fire_monitor(_monitor_item()))

    assert [entry["status"] for entry in monitor_logs] == ["started", "core_empty"]
    messages = _fetch_all(db_path, "SELECT role FROM messages ORDER BY created_at")
    assert [row["role"] for row in messages] == ["user"]


def test_fire_monitor_records_no_conversation(monkeypatch, tmp_path):
    _db_path, broadcasts, monitor_logs = _patch_monitor_env(
        monkeypatch,
        tmp_path,
        with_conversation=False,
    )

    asyncio.run(schedule.ScheduleManager()._fire_monitor(_monitor_item()))

    assert [entry["status"] for entry in monitor_logs] == ["started", "failed"]
    assert monitor_logs[1]["error_type"] == "no_conversation"
    assert "没有可用对话" in monitor_logs[1]["monitoringlog"]
    assert "monitor_alert" in [payload["type"] for payload in broadcasts]


def test_fire_monitor_does_not_fail_when_export_fails(monkeypatch, tmp_path):
    db_path, _broadcasts, monitor_logs = _patch_monitor_env(monkeypatch, tmp_path)

    async def fake_stream_ai(messages, model_key, temperature=None):
        yield "查岗正常完成。"

    async def failing_export(_conv_id):
        raise RuntimeError("export down")

    monkeypatch.setitem(sys.modules, "activity", SimpleNamespace(
        get_activity_summary_for_prompt=lambda _n: ""
    ))
    monkeypatch.setitem(sys.modules, "sensing", SimpleNamespace(
        format_sensing_for_prompt=lambda **_kwargs: ""
    ))
    monkeypatch.setattr(schedule_trigger, "stream_ai", fake_stream_ai)
    monkeypatch.setattr(schedule_trigger, "export_conversation", failing_export)

    asyncio.run(schedule.ScheduleManager()._fire_monitor(_monitor_item()))

    assert [entry["status"] for entry in monitor_logs] == ["started", "succeeded"]
    messages = _fetch_all(db_path, "SELECT role, content FROM messages ORDER BY created_at")
    assert messages[-1]["content"] == "查岗正常完成。"
