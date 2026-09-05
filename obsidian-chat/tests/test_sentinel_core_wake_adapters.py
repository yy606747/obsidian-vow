import asyncio
import sqlite3
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest

from app.sentinel import (
    ATTENTION_SNAPSHOT_SCHEMA_VERSION,
    build_core_wake_package,
    build_layer2_handoff,
    evaluate_sentinel_gate,
    run_core_wake_orchestrator_full_execute,
    run_core_wake_orchestrator_test_execute,
)
from sentinel_core_wake_adapters import LegacyCoreWakePorts


@pytest.fixture(autouse=True)
def _empty_vow_context(monkeypatch):
    """誓约层（Phase 2）在本管道经局部 import 读取 vow_service 单例；
    既有用例用空桩隔离，不读真实库。"""

    class _Stub:
        async def load_vow_prompt_context(self):
            return "", ""

    monkeypatch.setattr("app.vows.service.vow_service", _Stub())

    async def load_timeline(*, visible_message_ids, now=None):
        return {
            "status": "injected",
            "block": "[最近三天的事]\n· 今天 21:00 用户说忙完了。",
            "entries": [{"text": "用户说忙完了。"}],
            "visible_message_ids": list(visible_message_ids),
            "now": now,
        }

    async def record_timeline(*_args, **_kwargs):
        return {"status": "recorded", "count": 1}

    monkeypatch.setattr(
        "sentinel_core_wake_adapters.load_sentinel_timeline_prompt_context",
        load_timeline,
    )
    monkeypatch.setattr(
        "sentinel_core_wake_adapters.record_sentinel_timeline_injection_usage",
        record_timeline,
    )
    monkeypatch.setattr(
        "sentinel_core_wake_adapters.timeline_service.start_background_refresh",
        lambda *_args, **_kwargs: None,
    )



class _AsyncCursor:
    def __init__(self, cursor):
        self._cursor = cursor

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

    async def execute(self, sql, params=()):
        return _AsyncCursor(self._conn.execute(sql, params))

    async def commit(self):
        self._conn.commit()


class _FakeDeps:
    def __init__(self, *, stream_chunks=None):
        self.broadcasts = []
        self.monitor_logs = []
        self.stream_calls = []
        self.sleeps = []
        self.stream_chunks = stream_chunks if stream_chunks is not None else ["刚好", "想到你。"]

    async def broadcast(self, payload):
        self.broadcasts.append(payload)

    async def stream_core(self, messages, model_key, temperature=None):
        self.stream_calls.append({
            "messages": list(messages),
            "model_key": model_key,
            "temperature": temperature,
        })
        for chunk in self.stream_chunks:
            yield chunk

    async def write_monitor_log(self, entry):
        self.monitor_logs.append(dict(entry))
        return True

    async def sleep(self, seconds):
        self.sleeps.append(seconds)


class _FakeControlSessions:
    def __init__(self, *, current=None, known=(), tombstone=None):
        self.current = current
        self.tombstone = tombstone
        self.known = {item.session_id: item for item in known}
        if current is not None:
            self.known[current.session_id] = current
        if tombstone is not None:
            self.known[tombstone.session_id] = tombstone

    async def get_current(self, *, conv_id):
        return self.current if conv_id == "conv_sentinel" else None

    async def get_session(self, session_id):
        return self.known.get(session_id)

    async def recent_safety_tombstone(self, conv_id):
        return self.tombstone if conv_id == "conv_sentinel" else None


def _control_session(*, status="active", epoch=0, close_reason=None):
    return SimpleNamespace(
        session_id="ctrl_sentinel",
        conv_id="conv_sentinel",
        kind="whisper",
        status=status,
        owner_client_id="tab_sentinel",
        device_id="browser_toy_bridge",
        control_epoch=epoch,
        close_reason=close_reason,
    )


def _toy_capability_kwargs(session=None):
    session = session or _control_session()
    return {
        "toy_capability_allowed": True,
        "control_session_id": session.session_id,
        "control_epoch": session.control_epoch,
        "owner_client_id": session.owner_client_id,
        "control_device_id": session.device_id,
    }


def _init_db(path):
    conn = sqlite3.connect(path)
    try:
        conn.execute(
            """
            CREATE TABLE conversations (
                id TEXT PRIMARY KEY,
                title TEXT NOT NULL,
                model TEXT NOT NULL,
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE messages (
                id TEXT PRIMARY KEY,
                conv_id TEXT NOT NULL,
                role TEXT NOT NULL,
                content TEXT NOT NULL,
                created_at REAL NOT NULL,
                attachments TEXT DEFAULT ''
            )
            """
        )
        conn.execute(
            "INSERT INTO conversations (id, title, model, created_at, updated_at) VALUES (?,?,?,?,?)",
            ("conv_sentinel", "Sentinel", "mock-model", 1.0, 2.0),
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


def _ports(
    tmp_path,
    *,
    clock=lambda: 1_700_000_000.0,
    monitor_log_writer=None,
    stream_chunks=None,
    control_sessions=None,
    toy_gateway_adapter=None,
    real_working_model=False,
):
    db_path = tmp_path / "core_wake_ports.db"
    _init_db(db_path)

    @asynccontextmanager
    async def get_db():
        async with _AsyncSqliteConn(db_path) as db:
            yield db

    deps = _FakeDeps(stream_chunks=stream_chunks)
    ports = LegacyCoreWakePorts(
        db_factory=get_db,
        broadcaster=deps.broadcast,
        core_streamer=deps.stream_core,
        monitor_log_writer=monitor_log_writer or deps.write_monitor_log,
        clock=clock,
        default_temperature=0.4,
        sleeper=deps.sleep,
        control_session_service_obj=control_sessions or _FakeControlSessions(),
        toy_gateway_adapter=toy_gateway_adapter,
    )
    if not real_working_model:
        async def empty_relationship_context():
            return "", ""

        ports.load_working_model_prompt_context = empty_relationship_context

    async def disabled_web_search(**_kwargs):
        return {"status": "disabled", "block": ""}

    async def skipped_web_search_finalize(**_kwargs):
        return {"status": "skipped"}

    ports.prepare_web_search_turn = disabled_web_search
    ports.finalize_web_search_turn = skipped_web_search_finalize
    return db_path, deps, ports


def _wake_package():
    handoff = build_layer2_handoff({
        "schema_version": ATTENTION_SNAPSHOT_SCHEMA_VERSION,
        "runtime_mode": "dry_run",
        "side_effects": [],
        "fallback_used": False,
        "fallback_reason": "",
        "window": {"start_ts": 1.0, "end_ts": 2.0},
        "world_state": {
            "likely_awake": "unknown",
            "likely_busy": "unknown",
            "location_state": "unknown",
            "social_availability": "unknown",
            "device_availability": "unknown",
        },
        "attention_targets": ["轻唤醒窗口"],
        "hypotheses": [{"label": "possible_good_timing", "confidence": 0.7, "evidence": ["刚忙完"]}],
        "compact_text": "用户可能刚忙完，但证据有限。",
        "suggested_next_check_sec": 900,
    })
    judgment = {
        "monitoringlog": "她可能空闲，适合轻轻出现。",
        "summary": "当前是一个轻唤醒窗口。",
        "score": 7,
        "confidence": 0.72,
        "wake_intent": True,
        "call_core": True,
        "core_reason": "可能处在空闲窗口，适合轻轻出现。",
        "restraint_reason": "",
        "uncertainty": "不知道她是否愿意聊天。",
        "suggested_next_check_sec": 900,
        "tone_hint": "轻轻出现",
    }
    return build_core_wake_package(
        handoff=handoff,
        judgment=judgment,
        gate_result=evaluate_sentinel_gate(judgment),
        context={
            "recent_sentinel_logs": ["21:30 score:4 信号不足。"],
            "recent_chat": ["用户: 我先刷一会。"],
        },
    )


def test_legacy_core_wake_ports_write_db_broadcast_stream_and_monitor_log(tmp_path):
    db_path, deps, ports = _ports(tmp_path)

    async def run_flow():
        timeline = await ports.load_timeline_prompt_context(
            visible_message_ids=["m1", "m2"],
            now=1_700_000_000.0,
        )
        await ports.broadcast_monitor_alert("Arden的哨兵唤醒了主脑")
        system_message = await ports.insert_system_wake_notice(
            conv_id="conv_sentinel",
            content="Arden的哨兵唤醒了主脑",
            created_at=1_700_000_001.0,
        )
        await ports.broadcast_msg_created(system_message)
        core_reply = await ports.stream_core(
            messages=[{"role": "user", "content": "hi"}],
            model_key="mock-model",
        )
        await ports.sleep(1.5)
        assistant_message = await ports.insert_assistant_message(
            conv_id="conv_sentinel",
            content=core_reply,
            created_at=1_700_000_002.0,
        )
        await ports.update_conversation(conv_id="conv_sentinel", updated_at=assistant_message["created_at"])
        await ports.broadcast_msg_created(assistant_message)
        toy_delivery = await ports.broadcast_toy_command(
            commands=["2", "STOP"],
            msg_id=assistant_message["id"],
            conv_id="conv_sentinel",
            **_toy_capability_kwargs(),
        )
        await ports.write_monitor_log({
            "status": "core_succeeded",
            "call_core": False,
            "conv_id": "conv_sentinel",
            "core_reason": "适合出现",
            "summary": "信号偏少",
            "context_errors": [],
            "core_msg_id": assistant_message["id"],
        })
        timeline_usage = await ports.record_timeline_injection_usage(
            timeline,
            conv_id="conv_sentinel",
            assistant_message_id=assistant_message["id"],
            response_text=assistant_message["content"],
        )
        return timeline, timeline_usage, system_message, assistant_message, toy_delivery

    timeline, timeline_usage, system_message, assistant_message, toy_delivery = asyncio.run(run_flow())

    assert timeline["block"].startswith("[最近三天的事]")
    assert timeline["visible_message_ids"] == ["m1", "m2"]
    assert timeline_usage == {"status": "recorded", "count": 1}
    assert system_message["id"] == "msg_1700000001000_sentinel_sys"
    assert assistant_message["id"] == "msg_1700000002000_sentinel"
    assert deps.stream_calls == [{
        "messages": [{"role": "user", "content": "hi"}],
        "model_key": "mock-model",
        "temperature": 0.4,
    }]
    assert deps.sleeps == [1.5]
    assert [payload["type"] for payload in deps.broadcasts] == [
        "monitor_alert",
        "msg_created",
        "msg_created",
    ]
    assert deps.broadcasts[0]["data"]["content"] == "Arden的哨兵唤醒了主脑"
    assert deps.broadcasts[1]["data"]["id"] == system_message["id"]
    assert deps.broadcasts[2]["data"]["id"] == assistant_message["id"]
    assert toy_delivery["status"] == "gateway_rejected"
    assert toy_delivery["reason"] == "no_active_session"
    assert toy_delivery["source"] == "sentinel"

    rows = _fetch_all(db_path, "SELECT role, content, attachments FROM messages ORDER BY created_at")
    assert rows == [
        {"role": "system", "content": "Arden的哨兵唤醒了主脑", "attachments": "[]"},
        {"role": "assistant", "content": "刚好想到你。", "attachments": "[]"},
    ]
    conversations = _fetch_all(db_path, "SELECT updated_at FROM conversations WHERE id='conv_sentinel'")
    assert conversations == [{"updated_at": 1_700_000_002.0}]
    assert len(deps.monitor_logs) == 1
    assert deps.monitor_logs[0]["status"] == "core_succeeded"
    assert deps.monitor_logs[0]["source"] == "sentinel"
    assert deps.monitor_logs[0]["monitoringlog"] == "🧠 哨兵唤醒 Core 成功。"
    assert deps.monitor_logs[0]["core_msg_id"] == assistant_message["id"]
    assert deps.monitor_logs[0]["summary"] == "信号偏少"


def test_legacy_core_wake_ring_touch_skips_quiet_hours_before_translation(tmp_path, monkeypatch):
    import config
    import ring_touch_translator

    monkeypatch.setitem(config.SETTINGS, "smart_ring_touch_enabled", True)
    monkeypatch.setitem(config.SETTINGS, "smart_ring_quiet_hours_enabled", True)
    monkeypatch.setitem(config.SETTINGS, "smart_ring_quiet_hours_start", "00:00")
    monkeypatch.setitem(config.SETTINGS, "smart_ring_quiet_hours_end", "23:59")

    async def fail_translate(_touch):
        raise AssertionError("translator should not run during quiet hours")

    monkeypatch.setattr(ring_touch_translator, "translate_ring_touch", fail_translate)
    _, _, ports = _ports(tmp_path)

    result = asyncio.run(ports.execute_ring_touch(
        touch_descriptions=["轻轻碰一下"],
        conv_id="conv_sentinel",
        msg_id="msg_sentinel",
        model_key="mock-model",
        request_id="wake_1",
    ))

    assert result == {"status": "capability_disabled", "count": 1, "results": []}


def test_legacy_ports_can_drive_test_orchestrator_without_real_runtime_side_effects(
    tmp_path,
    monkeypatch,
):
    async def empty_relationship_context():
        return "", ""

    monkeypatch.setattr(
        "sentinel_core_wake_adapters.load_sentinel_working_model_prompt_context",
        empty_relationship_context,
    )

    async def disabled_web_search(**_kwargs):
        return {"status": "disabled", "block": ""}

    async def skipped_web_search_finalize(**_kwargs):
        return {"status": "skipped"}

    from app.web_search import web_search_service

    monkeypatch.setattr(web_search_service, "prepare_dialogue_turn", disabled_web_search)
    monkeypatch.setattr(web_search_service, "finalize_independent", skipped_web_search_finalize)
    db_path, deps, ports = _ports(tmp_path)

    trace = asyncio.run(run_core_wake_orchestrator_test_execute(
        wake_package=_wake_package(),
        execution_context={
            "conv_id": "conv_sentinel",
            "model_key": "mock-model",
            "last_user_message_age_sec": 1200,
            "user_name": "用户",
            "ai_name": "Arden",
            "toy_capability_allowed": False,
            "toy_capability_reason": "no_active_session",
            "recent_messages": [{"id": "m1", "role": "user", "content": "我刚忙完。"}],
        },
        ports=ports,
        request_id="adapter-test",
    ))

    assert trace["status"] == "core_succeeded"
    assert trace["production_side_effects"] == []
    assert trace["fallback_used"] is False
    assert trace["system_msg_id"].endswith("_sentinel_sys")
    assert trace["core_msg_id"].endswith("_sentinel")
    assert [payload["type"] for payload in deps.broadcasts] == [
        "monitor_alert",
        "msg_created",
        "msg_created",
    ]
    assert deps.stream_calls[0]["messages"][0]["content"].startswith("[最近三天的事]")
    assert not any(
        marker in message["content"]
        for marker in ("[相关记忆]", "可参考记忆")
        for message in deps.stream_calls[0]["messages"]
    )
    assert deps.sleeps == []
    assert deps.monitor_logs[0]["status"] == "core_succeeded"
    rows = _fetch_all(db_path, "SELECT role FROM messages ORDER BY created_at")
    assert [row["role"] for row in rows] == ["system", "assistant"]


def test_sentinel_working_model_context_shared_gate_off_skips_both_heads(monkeypatch, tmp_path):
    from app.working_model import runtime as working_model_runtime
    from app.working_model import service as working_model_service

    async def forbidden_load():
        raise AssertionError("disabled injection must not read either durable head")

    monkeypatch.setattr(working_model_runtime, "working_model_v2_injection_enabled", lambda: False)
    monkeypatch.setattr(working_model_service, "load_v2_prompt_heads", forbidden_load)
    _db_path, _deps, ports = _ports(tmp_path, real_working_model=True)

    assert asyncio.run(ports.load_working_model_prompt_context()) == ("", "")


def test_sentinel_working_model_context_loads_and_builds_both_heads_once(monkeypatch, tmp_path):
    from app.working_model import runtime as working_model_runtime
    from app.working_model import service as working_model_service

    calls = []

    async def load_heads():
        calls.append("load_v2_prompt_heads")
        return (
            {"content": "她看重边界，也喜欢一起拆系统问题。"},
            {"content": "主动但不替她做决定。"},
        )

    monkeypatch.setattr(working_model_runtime, "working_model_v2_injection_enabled", lambda: True)
    monkeypatch.setattr(working_model_service, "load_v2_prompt_heads", load_heads)
    _db_path, _deps, ports = _ports(tmp_path, real_working_model=True)

    assert asyncio.run(ports.load_working_model_prompt_context()) == (
        "[你对她的当前认识]\n她看重边界，也喜欢一起拆系统问题。",
        "[你此刻想以怎样的姿态与她相处]\n主动但不替她做决定。",
    )
    assert calls == ["load_v2_prompt_heads"]


def test_sentinel_toy_port_rejects_without_frozen_capability(tmp_path):
    _db_path, deps, ports = _ports(tmp_path)

    result = asyncio.run(ports.broadcast_toy_command(
        commands=["2"],
        msg_id="core_1",
        conv_id="conv_sentinel",
    ))

    assert result["status"] == "gateway_rejected"
    assert result["reason"] == "capability_not_frozen"
    assert not any(payload["type"] == "toy_command" for payload in deps.broadcasts)


def test_sentinel_toy_port_rejects_frozen_snapshot_without_active_session(tmp_path):
    _db_path, deps, ports = _ports(tmp_path)

    result = asyncio.run(ports.broadcast_toy_command(
        commands=["2"],
        msg_id="core_1",
        conv_id="conv_sentinel",
        **_toy_capability_kwargs(),
    ))

    assert result["status"] == "gateway_rejected"
    assert result["reason"] == "no_active_session"
    assert result["source"] == "sentinel"
    assert not any(payload["type"] == "toy_command" for payload in deps.broadcasts)


def test_sentinel_toy_port_has_no_legacy_no_session_fallback(tmp_path):
    _db_path, deps, ports = _ports(tmp_path)

    result = asyncio.run(ports.broadcast_toy_command(
        commands=["2"],
        msg_id="core_1",
        conv_id="conv_sentinel",
        **_toy_capability_kwargs(),
        request_id="wake_req_legacy",
    ))

    assert result["status"] == "gateway_rejected"
    assert result["broadcast"] is False
    assert result["reason"] == "no_active_session"
    assert result["request_id"] == "wake_req_legacy"
    assert not any(payload["type"] == "toy_command" for payload in deps.broadcasts)


def test_sentinel_toy_port_uses_gateway_for_active_session(tmp_path):
    session = _control_session(epoch=2)
    gateway_calls = []

    async def fake_gateway(intent, context):
        gateway_calls.append((intent, context))
        return {
            "type": "toy_command",
            "ok": True,
            "command": intent.arguments["command"],
            "control_session_id": session.session_id,
            "control_epoch": session.control_epoch,
            "owner_client_id": session.owner_client_id,
        }

    _db_path, deps, ports = _ports(
        tmp_path,
        control_sessions=_FakeControlSessions(current=session),
        toy_gateway_adapter=fake_gateway,
    )

    result = asyncio.run(ports.broadcast_toy_command(
        commands=["2"],
        msg_id="core_1",
        conv_id="conv_sentinel",
        **_toy_capability_kwargs(session),
        request_id="wake_req_1",
        wake_id="wake_1",
    ))

    assert result["status"] == "gateway_accepted"
    assert gateway_calls[0][0].tool_name == "device.toy"
    assert gateway_calls[0][0].metadata["source"] == "sentinel"
    assert gateway_calls[0][0].metadata["request_id"] == "wake_req_1"
    assert gateway_calls[0][0].metadata["wake_id"] == "wake_1"
    assert gateway_calls[0][1].request_id == "wake_req_1"
    assert gateway_calls[0][1].metadata["source"] == "sentinel"
    assert gateway_calls[0][1].metadata["delivery_path"] == "core_wake"
    assert gateway_calls[0][1].metadata["control_session_id"] == "ctrl_sentinel"
    assert result["source"] == "sentinel"
    assert result["request_id"] == "wake_req_1"
    assert result["wake_id"] == "wake_1"
    assert deps.broadcasts[-1]["data"]["commands"] == ["2"]
    assert deps.broadcasts[-1]["data"]["control_session_id"] == "ctrl_sentinel"
    assert deps.broadcasts[-1]["data"]["control_epoch"] == 2


def test_sentinel_toy_port_rejects_stale_or_panic_snapshot(tmp_path):
    stale = _control_session(status="stale")
    _db_path, deps, ports = _ports(tmp_path, control_sessions=_FakeControlSessions(current=stale))

    stale_result = asyncio.run(ports.broadcast_toy_command(
        commands=["2"],
        msg_id="core_1",
        conv_id="conv_sentinel",
        **_toy_capability_kwargs(stale),
    ))

    ended = _control_session(status="ended", epoch=1, close_reason="panic")
    ended_dir = tmp_path / "ended"
    ended_dir.mkdir()
    _db_path, ended_deps, ended_ports = _ports(ended_dir, control_sessions=_FakeControlSessions(known=(ended,)))
    ended_result = asyncio.run(ended_ports.broadcast_toy_command(
        commands=["STOP"],
        msg_id="core_2",
        conv_id="conv_sentinel",
        **_toy_capability_kwargs(ended),
    ))

    assert stale_result["status"] == "gateway_rejected"
    assert stale_result["reason"] == "session_stale"
    assert ended_result["status"] == "gateway_rejected"
    assert ended_result["reason"] == "session_ended"
    assert not any(payload["type"] == "toy_command" for payload in deps.broadcasts)
    assert not any(payload["type"] == "toy_command" for payload in ended_deps.broadcasts)


def test_sentinel_toy_port_rejects_session_mismatch_without_gateway_call(tmp_path):
    session = _control_session(epoch=2)
    gateway_calls = []

    async def fake_gateway(intent, context):
        gateway_calls.append((intent, context))
        return {"type": "toy_command", "ok": True, "command": intent.arguments["command"]}

    _db_path, deps, ports = _ports(
        tmp_path,
        control_sessions=_FakeControlSessions(current=session),
        toy_gateway_adapter=fake_gateway,
    )

    result = asyncio.run(ports.broadcast_toy_command(
        commands=["2"],
        msg_id="core_1",
        conv_id="conv_sentinel",
        toy_capability_allowed=True,
        control_session_id="ctrl_old",
        control_epoch=session.control_epoch,
        owner_client_id=session.owner_client_id,
        control_device_id=session.device_id,
        request_id="wake_req_mismatch",
    ))

    assert result["status"] == "gateway_rejected"
    assert result["reason"] == "session_mismatch"
    assert result["control_session_id"] == "ctrl_sentinel"
    assert result["request_id"] == "wake_req_mismatch"
    assert gateway_calls == []
    assert not any(payload["type"] == "toy_command" for payload in deps.broadcasts)


def test_sentinel_toy_port_rejects_recent_safety_tombstone(tmp_path):
    tombstone = _control_session(status="ended", epoch=3, close_reason="safeword")
    _db_path, deps, ports = _ports(
        tmp_path,
        control_sessions=_FakeControlSessions(tombstone=tombstone),
    )

    result = asyncio.run(ports.broadcast_toy_command(
        commands=["STOP"],
        msg_id="core_1",
        conv_id="conv_sentinel",
        toy_capability_allowed=True,
        control_session_id="ctrl_frozen_before_tombstone",
        control_epoch=2,
        owner_client_id="tab_sentinel",
        control_device_id="browser_toy_bridge",
        request_id="wake_req_tombstone",
    ))

    assert result["status"] == "gateway_rejected"
    assert result["reason"] == "safety_tombstone"
    assert result["control_session_id"] == "ctrl_sentinel"
    assert result["control_epoch"] == 3
    assert result["request_id"] == "wake_req_tombstone"
    assert not any(payload["type"] == "toy_command" for payload in deps.broadcasts)


def test_full_execute_records_gateway_accepted_side_effect(tmp_path):
    session = _control_session(epoch=4)

    async def fake_gateway(intent, context):
        return {
            "type": "toy_command",
            "ok": True,
            "command": intent.arguments["command"],
            "control_session_id": session.session_id,
            "control_epoch": session.control_epoch,
            "owner_client_id": session.owner_client_id,
        }

    _db_path, deps, ports = _ports(
        tmp_path,
        stream_chunks=["靠近一点 [TOY:2]"],
        control_sessions=_FakeControlSessions(current=session),
        toy_gateway_adapter=fake_gateway,
    )

    trace = asyncio.run(run_core_wake_orchestrator_full_execute(
        wake_package=_wake_package(),
        execution_context={
            "conv_id": "conv_sentinel",
            "model_key": "mock-model",
            "last_user_message_age_sec": 1200,
            "user_name": "用户",
            "ai_name": "Arden",
            "control_session_id": session.session_id,
            "control_kind": session.kind,
            "control_status": session.status,
            "control_epoch": session.control_epoch,
            "owner_client_id": session.owner_client_id,
            "control_device_id": session.device_id,
            "toy_capability_allowed": True,
            "toy_capability_reason": "allowed",
            "recent_messages": [{"role": "user", "content": "我刚忙完。"}],
        },
        ports=ports,
        request_id="adapter-gateway",
        allow_production_side_effects=True,
    ))

    assert trace["toy_command_delivery"]["status"] == "gateway_accepted"
    assert "control.gateway.execute_toy_command" in trace["production_side_effects"]
    assert "websocket.broadcast.toy_command" in trace["production_side_effects"]
    assert deps.broadcasts[-1]["data"]["control_session_id"] == "ctrl_sentinel"
    assert deps.monitor_logs[0]["toy_command_delivery"]["status"] == "gateway_accepted"


def test_full_execute_records_gateway_reject_without_broadcast(tmp_path):
    _db_path, deps, ports = _ports(tmp_path, stream_chunks=["靠近一点 [TOY:2]"])

    trace = asyncio.run(run_core_wake_orchestrator_full_execute(
        wake_package=_wake_package(),
        execution_context={
            "conv_id": "conv_sentinel",
            "model_key": "mock-model",
            "last_user_message_age_sec": 1200,
            "user_name": "用户",
            "ai_name": "Arden",
            "recent_messages": [{"role": "user", "content": "我刚忙完。"}],
        },
        ports=ports,
        request_id="adapter-reject",
        allow_production_side_effects=True,
    ))

    assert trace["toy_command_delivery"]["status"] == "gateway_rejected"
    assert trace["toy_command_delivery"]["reason"] == "capability_not_frozen"
    assert "control.gateway.reject_toy_command" in trace["production_side_effects"]
    assert "websocket.broadcast.toy_command" not in trace["production_side_effects"]
    assert not any(payload["type"] == "toy_command" for payload in deps.broadcasts)


def test_legacy_ports_drive_full_execute_and_reject_toy_without_control_session(tmp_path):
    db_path, deps, ports = _ports(tmp_path, stream_chunks=["靠近一点 [TOY:2]", "，我在。"])

    trace = asyncio.run(run_core_wake_orchestrator_full_execute(
        wake_package=_wake_package(),
        execution_context={
            "conv_id": "conv_sentinel",
            "model_key": "mock-model",
            "last_user_message_age_sec": 1200,
            "user_name": "用户",
            "ai_name": "Arden",
            "toy_capability_allowed": True,
            "toy_capability_reason": "allowed",
            "control_session_id": "ctrl_frozen",
            "control_kind": "whisper",
            "control_status": "active",
            "control_epoch": 0,
            "owner_client_id": "tab_frozen",
            "control_device_id": "browser_toy_bridge",
            "recent_messages": [{"role": "user", "content": "我刚忙完。"}],
        },
        ports=ports,
        request_id="adapter-full",
        allow_production_side_effects=True,
    ))

    assert trace["runtime_mode"] == "full"
    assert trace["status"] == "core_succeeded"
    assert trace["production_side_effects"][-2:] == [
        "control.gateway.reject_toy_command",
        "monitor_log.write.core_result",
    ]
    assert trace["toy_commands"] == ["2"]
    assert trace["toy_command_delivery"]["status"] == "gateway_rejected"
    assert trace["toy_command_delivery"]["reason"] == "no_active_session"
    assert [payload["type"] for payload in deps.broadcasts] == [
        "monitor_alert",
        "msg_created",
        "msg_created",
    ]
    assert deps.sleeps == [5]
    rows = _fetch_all(db_path, "SELECT role, content FROM messages ORDER BY created_at")
    assert [row["role"] for row in rows] == ["system", "assistant"]
    assert rows[-1]["content"] == "靠近一点 ，我在。"
    assert "[TOY:" not in rows[-1]["content"]
    assert deps.monitor_logs[0]["toy_commands"] == ["2"]
    assert deps.monitor_logs[0]["toy_command_delivery"]["status"] == "gateway_rejected"


def test_legacy_core_wake_ports_fail_loud_when_monitor_log_writer_reports_failure(tmp_path):
    async def failing_writer(_entry):
        return False

    _db_path, deps, ports = _ports(tmp_path, monitor_log_writer=failing_writer)

    with pytest.raises(RuntimeError, match="monitor log writer returned false"):
        asyncio.run(ports.write_monitor_log({"status": "core_failed"}))
    assert deps.monitor_logs == []
