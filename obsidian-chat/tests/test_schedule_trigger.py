import asyncio
import sqlite3
from contextlib import asynccontextmanager
from types import SimpleNamespace

from app.schedule import commands, store, trigger
import pytest


class _NoopLedger:
    def new_invocation_id(self, prefix):
        return f"{prefix}_test"

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


class _NoopTimeline:
    def start_background_refresh(self, *_args, **_kwargs):
        return None

    async def prompt_context(self, **_kwargs):
        return {"status": "missing", "block": "", "entries": []}

    async def record_injection_usage(self, *_args, **_kwargs):
        return {"status": "skipped", "count": 0}


@pytest.fixture(autouse=True)
def _empty_vow_context(monkeypatch):
    """誓约层（Phase 2）在本管道注入常驻读取；既有用例用空桩隔离，不读真实库。"""

    class _Stub:
        async def load_vow_prompt_context(self):
            return "", ""

    monkeypatch.setattr(trigger, "vow_service", _Stub())

    async def _noop_push(_data):
        return {"sent": 0, "deleted": 0, "failed": 0}

    monkeypatch.setattr(trigger.web_push_service, "broadcast_alarm", _noop_push)

    async def _missing_alarm_context(*_args, **_kwargs):
        return {
            "status": "missing",
            "block": "",
            "visible_messages": [],
        }

    monkeypatch.setattr(
        trigger.alarm_context,
        "load_prompt_context",
        _missing_alarm_context,
    )
    monkeypatch.setattr(trigger, "timeline_service", _NoopTimeline())



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
        conn.execute("CREATE TABLE conversations (id TEXT PRIMARY KEY, title TEXT, model TEXT, created_at REAL, updated_at REAL)")
        conn.execute("CREATE TABLE messages (id TEXT PRIMARY KEY, conv_id TEXT, role TEXT, content TEXT, created_at REAL, attachments TEXT)")
        conn.execute("CREATE TABLE schedules (id TEXT PRIMARY KEY, type TEXT, trigger_at TEXT, content TEXT, created_at REAL, status TEXT)")
        conn.execute("INSERT INTO conversations VALUES (?,?,?,?,?)", ("conv1", "Test", "mock-model", 1.0, 2.0))
        conn.execute("INSERT INTO messages VALUES (?,?,?,?,?,?)", ("msg_user", "conv1", "user", "你好", 1.0, "[]"))
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


def _patch_trigger(monkeypatch, tmp_path):
    db_path = tmp_path / "trigger.db"
    _init_db(db_path)

    @asynccontextmanager
    async def fake_get_db():
        async with _AsyncConn(db_path) as db:
            yield db

    broadcasts = []
    monitor_logs = []
    calls = []

    async def fake_broadcast(payload):
        broadcasts.append(payload)

    async def fake_stream(messages, model_key, temperature=None):
        calls.append(messages)
        yield "收到。"

    for module in (store, trigger, commands):
        monkeypatch.setattr(module, "get_db", fake_get_db, raising=False)
    for module in (trigger, commands):
        monkeypatch.setattr(module, "manager", SimpleNamespace(broadcast=fake_broadcast))
    monkeypatch.setattr(trigger, "stream_ai", fake_stream)
    monkeypatch.setattr(trigger, "tool_invocation_ledger", _NoopLedger())
    monkeypatch.setattr(trigger, "load_worldbook", lambda: {"user_name": "用户", "ai_name": "Arden"})
    monkeypatch.setattr(trigger.evidence, "load_evidence", lambda _user: ("\n证据\n", []))
    monkeypatch.setattr(trigger, "append_monitor_log", lambda entry: monitor_logs.append(dict(entry)))
    monkeypatch.setattr(trigger, "export_conversation", _noop_export)
    return db_path, broadcasts, monitor_logs, calls


async def _noop_export(_conv_id):
    return None


def test_merged_due_items_call_llm_once_and_broadcast_merged_payloads(monkeypatch, tmp_path):
    db_path, broadcasts, monitor_logs, calls = _patch_trigger(monkeypatch, tmp_path)
    items = [
        {"id": "m1", "type": "monitor", "trigger_at": "2026-05-14 20:00", "content": "看看状态"},
        {"id": "a1", "type": "alarm", "trigger_at": "2026-05-14 20:00", "content": "喝水"},
    ]

    asyncio.run(trigger.fire_due_items(items))

    assert len(calls) == 1
    assert [entry["status"] for entry in monitor_logs] == ["started", "succeeded"]
    schedule_alarm = [payload for payload in broadcasts if payload["type"] == "schedule_alarm"][0]
    assert schedule_alarm["data"]["id"] == "a1"
    assert schedule_alarm["data"]["content"] == "2 条日程同时到期"
    assert [payload["type"] for payload in broadcasts].count("msg_created") == 2
    messages = _fetch(db_path, "SELECT role, content FROM messages ORDER BY created_at")
    assert [row["role"] for row in messages] == ["user", "system", "trigger", "assistant"]
    assert messages[1]["content"] == "⏰ 2 条日程同时到期"
    assert "[日程批量触发]" in messages[2]["content"]


def test_alarm_start_is_forwarded_to_web_push(monkeypatch, tmp_path):
    _db_path, broadcasts, _monitor_logs, _calls = _patch_trigger(monkeypatch, tmp_path)
    pushed = []

    async def capture_push(data):
        pushed.append(dict(data))
        return {"sent": 1, "deleted": 0, "failed": 0}

    monkeypatch.setattr(trigger.web_push_service, "broadcast_alarm", capture_push)
    item = {
        "id": "a1",
        "type": "alarm",
        "trigger_at": "2026-05-14 20:00",
        "content": "喝水",
    }

    asyncio.run(trigger._broadcast_start([item]))

    ws_alarm = next(payload["data"] for payload in broadcasts if payload["type"] == "schedule_alarm")
    assert pushed == [ws_alarm]


def test_web_push_failure_cannot_interrupt_schedule_broadcast(monkeypatch, tmp_path):
    _db_path, broadcasts, _monitor_logs, _calls = _patch_trigger(monkeypatch, tmp_path)

    async def fail_push(_data):
        raise RuntimeError("proxy unavailable")

    monkeypatch.setattr(trigger.web_push_service, "broadcast_alarm", fail_push)
    item = {
        "id": "a1",
        "type": "alarm",
        "trigger_at": "2026-05-14 20:00",
        "content": "喝水",
    }

    asyncio.run(trigger._broadcast_start([item]))

    assert [payload["type"] for payload in broadcasts] == [
        "schedule_alarm",
        "schedule_changed",
    ]


def test_monitor_core_failure_logs_failure_and_inserts_failed_reply(monkeypatch, tmp_path):
    db_path, _broadcasts, monitor_logs, _calls = _patch_trigger(monkeypatch, tmp_path)

    async def fail_stream(messages, model_key, temperature=None):
        raise RuntimeError("model down")
        yield ""

    monkeypatch.setattr(trigger, "stream_ai", fail_stream)

    asyncio.run(trigger.fire_due_items([
        {"id": "m1", "type": "monitor", "trigger_at": "2026-05-14 20:00", "content": "看看状态"},
    ]))

    assert [entry["status"] for entry in monitor_logs] == ["started", "core_failed", "failed_reply_inserted"]
    messages = _fetch(db_path, "SELECT role, content FROM messages ORDER BY created_at")
    assert messages[-1]["role"] == "assistant"
    assert "定时查岗回复失败" in messages[-1]["content"]


def test_non_user_schedule_trigger_cannot_create_another_alarm():
    cleaned = trigger._strip_trigger_alarm_commands(
        "醒醒。[ALARM:2026-05-15T08:10|再响一次] 该起床了。"
    )

    assert "[ALARM:" not in cleaned
    assert cleaned == "醒醒。 该起床了。"


def test_alarm_prompt_uses_creation_context_and_timeline_not_fire_time_history(
    monkeypatch,
    tmp_path,
):
    _db_path, _broadcasts, _monitor_logs, _calls = _patch_trigger(
        monkeypatch,
        tmp_path,
    )
    timeline_calls = []

    async def load_creation_context(*_args, **_kwargs):
        return {
            "status": "loaded",
            "block": "[设置闹钟时的上下文]\nYang：明早有早八，七点十分叫我。",
            "visible_messages": [{"id": "setting-turn"}],
        }

    class _Timeline(_NoopTimeline):
        async def prompt_context(self, **kwargs):
            timeline_calls.append(kwargs)
            return {
                "status": "injected",
                "block": "[最近三天的事]\n今天：早八已经取消。",
                "entries": [{"index": 0, "text": "早八已经取消。"}],
            }

    monkeypatch.setattr(
        trigger.alarm_context,
        "load_prompt_context",
        load_creation_context,
    )
    monkeypatch.setattr(trigger, "timeline_service", _Timeline())

    messages, _trigger_prompt, _evidence_errors, context_meta = asyncio.run(
        trigger._build_messages(
            [{
                "id": "alarm-1",
                "type": "alarm",
                "trigger_at": "2026-08-25 07:10",
                "content": "起床",
            }],
            {"user_name": "Yang", "ai_name": "Alaric"},
            "conv1",
            "2026年08月25日 07:10:00",
            "Yang",
        )
    )

    contents = [str(message.get("content") or "") for message in messages]
    assert any("[设置闹钟时的上下文]" in content for content in contents)
    assert any("[最近三天的事]" in content for content in contents)
    assert not any(content == "你好" for content in contents)
    assert not any("[相关记忆]" in content for content in contents)
    assert timeline_calls == [{
        "visible_messages": [{"id": "setting-turn"}],
    }]
    assert context_meta["timeline"]["status"] == "injected"
