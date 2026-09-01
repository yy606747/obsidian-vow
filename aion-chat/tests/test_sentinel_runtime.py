import asyncio
import json
import sqlite3
import sys
from contextlib import asynccontextmanager
from types import ModuleType, SimpleNamespace

import sentinel_runtime
import pytest
from app.sentinel import build_attention_snapshot, build_sentinel_runtime_context
from sentinel_core_wake_adapters import LegacyCoreWakePorts


def test_attention_runtime_uses_structured_projection_and_drops_legacy_location_text():
    projection = {
        "schema_version": "context_delivery_projection.v2",
        "recent_events": [{
            "key": "location.place",
            "event": "transition",
            "from_value": "inside",
            "to_value": "outside",
            "observed_at": 990,
            "source": "location.v2",
            "confidence": 1,
            "payload": {
                "payload_schema": "location_geofence.v1",
                "event_type": "transition",
                "geofence_direction": "inside_to_outside",
                "boundary_side": "outside",
                "distance_m": 610,
                "accuracy_m": 30,
                "configured_enter_m": 400,
                "configured_exit_m": 560,
            },
        }],
        "observations": [],
    }

    attention_input = sentinel_runtime._legacy_attention_input_payload(
        reference_time=1000,
        location_text="旧句子声称范围外回到范围里，距离家约1米。",
        context_projection=projection,
    )
    location_signals = [
        item for item in attention_input["raw_signals"]
        if item["kind"].startswith("location.")
    ]
    snapshot = build_attention_snapshot(attention_input)

    assert len(location_signals) == 1
    assert location_signals[0]["source"] == "context_delivery.projection"
    assert "旧句子" not in str(attention_input)
    assert snapshot["world_state"]["device_geofence_transition"] == "inside_to_outside"
    assert "610米" in snapshot["compact_text"]


@pytest.fixture(autouse=True)
def _empty_vow_context(monkeypatch):
    """誓约层（Phase 2）在本管道经局部 import 读取 vow_service 单例；
    既有用例用空桩隔离，不读真实库。"""

    class _Stub:
        async def load_vow_prompt_context(self):
            return "", ""

    monkeypatch.setattr("app.vows.service.vow_service", _Stub())

    async def empty_working_model_context():
        return "", ""

    monkeypatch.setattr(
        sentinel_runtime,
        "load_sentinel_working_model_prompt_context",
        empty_working_model_context,
    )
    monkeypatch.setattr(
        "sentinel_core_wake_adapters.load_sentinel_working_model_prompt_context",
        empty_working_model_context,
    )

    async def empty_timeline_context(*, visible_message_ids, now=None):
        return {
            "status": "missing",
            "block": "",
            "entries": [],
            "visible_message_ids": list(visible_message_ids),
            "now": now,
        }

    async def skipped_timeline_usage(*_args, **_kwargs):
        return {"status": "skipped", "count": 0}

    monkeypatch.setattr(
        sentinel_runtime,
        "load_sentinel_timeline_prompt_context",
        empty_timeline_context,
    )
    monkeypatch.setattr(
        "sentinel_core_wake_adapters.load_sentinel_timeline_prompt_context",
        empty_timeline_context,
    )
    monkeypatch.setattr(
        sentinel_runtime,
        "record_sentinel_timeline_injection_usage",
        skipped_timeline_usage,
    )
    monkeypatch.setattr(
        "sentinel_core_wake_adapters.record_sentinel_timeline_injection_usage",
        skipped_timeline_usage,
    )

    class _NoopToolLedger:
        def __init__(self):
            self.serial = 0

        def new_invocation_id(self, kind):
            self.serial += 1
            return f"test-{kind}-{self.serial}"

        async def record_model_request(self, *_args, **_kwargs):
            return None

        async def record_model_output(self, *_args, **_kwargs):
            return None

        async def record_turn(self, *_args, **_kwargs):
            return None

        async def record_visible_message(self, *_args, **_kwargs):
            return None

        async def record_marker(self, *_args, **_kwargs):
            return None

    fake_ledger = _NoopToolLedger()
    monkeypatch.setattr(sentinel_runtime, "tool_invocation_ledger", fake_ledger)
    monkeypatch.setattr("app.tools.ledger.tool_invocation_ledger", fake_ledger)

    async def disabled_web_search(**_kwargs):
        return {"status": "disabled", "block": ""}

    async def skipped_web_search_finalize(**_kwargs):
        return {"status": "skipped"}

    monkeypatch.setattr(
        "app.web_search.web_search_service.prepare_dialogue_turn",
        disabled_web_search,
    )
    monkeypatch.setattr(
        "app.web_search.web_search_service.finalize_independent",
        skipped_web_search_finalize,
    )



def _install_signal_modules(monkeypatch):
    location_module = ModuleType("location")
    location_module.format_location_for_prompt = lambda: "当前位置：信号可用。"
    activity_module = ModuleType("activity")
    activity_module.get_activity_summary_for_prompt = lambda _hours: "近一小时主要在聊天。"
    sensing_module = ModuleType("sensing")
    sensing_module.format_sensing_for_prompt = lambda **_kwargs: "屏幕最近解锁过。"

    monkeypatch.setitem(sys.modules, "location", location_module)
    monkeypatch.setitem(sys.modules, "activity", activity_module)
    monkeypatch.setitem(sys.modules, "sensing", sensing_module)


def _patch_runtime_env(monkeypatch):
    logs = []
    broadcasts = []

    @asynccontextmanager
    async def fake_get_db():
        raise RuntimeError("recent chat unavailable")
        yield

    async def fake_broadcast(payload):
        broadcasts.append(payload)

    async def fake_last_user_msg_time():
        return 0

    async def fake_read_runtime_context(**_kwargs):
        return build_sentinel_runtime_context({})

    async def fake_read_core_wake_execution_context(**_kwargs):
        return {
            "user_name": "用户",
            "ai_name": "Aion",
            "recent_messages": [],
        }

    monkeypatch.setattr(sentinel_runtime, "cleanup_old_logs", lambda _keep_days=3: None)
    monkeypatch.setattr(sentinel_runtime, "load_worldbook", lambda: {"user_name": "用户", "ai_name": "Aion"})
    monkeypatch.setattr(sentinel_runtime, "load_chat_status", lambda: {"status": ""})
    monkeypatch.setattr(sentinel_runtime, "load_ai_behavior", lambda: {
        "sentinel_call_core_criteria": "score >= 7 时可以唤醒 Core。",
        "sentinel_wake_threshold": 7,
    })
    monkeypatch.setattr(sentinel_runtime, "load_cam_config", lambda: {})
    monkeypatch.setattr(sentinel_runtime, "async_get_last_user_msg_time", fake_last_user_msg_time)
    monkeypatch.setattr(sentinel_runtime, "read_sentinel_runtime_context", fake_read_runtime_context)
    monkeypatch.setattr(sentinel_runtime, "read_core_wake_execution_context", fake_read_core_wake_execution_context)
    monkeypatch.setattr(sentinel_runtime, "read_logs_since", lambda _since: [])
    monkeypatch.setattr(sentinel_runtime, "get_db", fake_get_db)
    monkeypatch.setattr(sentinel_runtime, "append_monitor_log", lambda entry: logs.append(dict(entry)))
    monkeypatch.setattr(sentinel_runtime, "manager", SimpleNamespace(broadcast=fake_broadcast))
    monkeypatch.setattr(sentinel_runtime, "record_sentinel_event", lambda event: dict(event))
    _install_signal_modules(monkeypatch)

    return logs, broadcasts


def test_location_reader_prefers_sentinel_specific_text(monkeypatch):
    location_module = ModuleType("location")
    location_module.format_location_for_prompt = lambda: ""
    location_module.format_location_for_sentinel = (
        lambda: (
            "手机的定位从你们标注的「家」范围里走到了范围外"
            "（距围栏中心约450米，配置半径约400米，定位精度约30米）；"
            "这只是手机越过了那条边界，说明不了她去了哪。"
        )
    )
    monkeypatch.setitem(sys.modules, "location", location_module)

    assert sentinel_runtime._read_location_text_for_sentinel() == (
        "手机的定位从你们标注的「家」范围里走到了范围外"
        "（距围栏中心约450米，配置半径约400米，定位精度约30米）；"
        "这只是手机越过了那条边界，说明不了她去了哪。"
    )


def test_location_reader_falls_back_to_core_prompt(monkeypatch):
    location_module = ModuleType("location")
    location_module.format_location_for_prompt = lambda: "当前位置：信号可用。"
    monkeypatch.setitem(sys.modules, "location", location_module)

    assert sentinel_runtime._read_location_text_for_sentinel() == "当前位置：信号可用。"


def _init_core_db(path, *, with_conversation=True):
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
        if with_conversation:
            conn.execute(
                "INSERT INTO conversations (id, title, model, created_at, updated_at) VALUES (?,?,?,?,?)",
                ("conv_sentinel", "Sentinel", "mock-model", 1.0, 2.0),
            )
            conn.execute(
                "INSERT INTO messages (id, conv_id, role, content, created_at, attachments) VALUES (?,?,?,?,?,?)",
                ("msg_user", "conv_sentinel", "user", "我刚忙完", 1.0, "[]"),
            )
        conn.commit()
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


class _NoControlSessions:
    async def get_current(self, *, conv_id):
        return None

    async def get_session(self, session_id):
        return None

    async def recent_safety_tombstone(self, conv_id):
        return None


def _fetch_all(path, sql):
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    try:
        return [dict(row) for row in conn.execute(sql).fetchall()]
    finally:
        conn.close()


def _patch_core_env(monkeypatch, tmp_path, *, with_conversation=True):
    import app.control.toy_capability as toy_capability_module

    db_path = tmp_path / "sentinel_core.db"
    _init_core_db(db_path, with_conversation=with_conversation)

    @asynccontextmanager
    async def fake_get_db():
        async with _AsyncSqliteConn(db_path) as db:
            yield db

    logs = []
    broadcasts = []

    async def fake_append_and_broadcast(entry):
        logs.append(dict(entry))
        broadcasts.append({"type": "monitor_log", "data": dict(entry)})
        return True

    async def fake_broadcast(payload):
        broadcasts.append(payload)

    async def no_sleep(_seconds):
        return None

    async def fake_core_streamer(*_args, **_kwargs):
        if False:
            yield ""

    async def fake_toy_capability(**_kwargs):
        return toy_capability_module.ToyCapabilitySnapshot(
            False,
            "no_active_session",
            "conv_sentinel",
            "",
            "",
            "",
            None,
            "",
            "",
        )

    monkeypatch.setattr(sentinel_runtime, "load_cam_config", lambda: {})
    monkeypatch.setattr(sentinel_runtime, "load_worldbook", lambda: {"user_name": "用户", "ai_name": "Aion"})
    monkeypatch.setattr(sentinel_runtime, "get_db", fake_get_db)
    monkeypatch.setattr(sentinel_runtime, "aiosqlite", SimpleNamespace(Row=sqlite3.Row))
    monkeypatch.setattr(sentinel_runtime, "append_and_broadcast_monitor_log", fake_append_and_broadcast)
    monkeypatch.setattr(sentinel_runtime, "manager", SimpleNamespace(broadcast=fake_broadcast))
    monkeypatch.setattr(
        toy_capability_module,
        "resolve_toy_capability_snapshot",
        fake_toy_capability,
    )
    monkeypatch.setattr(
        sentinel_runtime.timeline_service,
        "start_background_refresh",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(sentinel_runtime.asyncio, "sleep", no_sleep)
    monkeypatch.setattr(sentinel_runtime, "build_legacy_core_wake_ports", lambda **_kwargs: LegacyCoreWakePorts(
        db_factory=fake_get_db,
        broadcaster=fake_broadcast,
        core_streamer=fake_core_streamer,
        monitor_log_writer=fake_append_and_broadcast,
        control_session_service_obj=_NoControlSessions(),
    ))
    _install_signal_modules(monkeypatch)

    return db_path, logs, broadcasts


def _patch_v2_full_wake_env(
    monkeypatch,
    tmp_path,
    *,
    legacy_fallback_enabled=False,
    provider_enabled=False,
    legacy_score=8,
    slot_call_kinds=None,
    v2_provider_error: Exception | None = None,
    runtime_context=None,
    core_context_error: Exception | None = None,
    stream_chunks=None,
    stream_error: BaseException | None = None,
    toy_capability_allowed=False,
):
    db_path = tmp_path / "sentinel_v2_full_wake.db"
    _init_core_db(db_path, with_conversation=True)
    logs = []
    broadcasts = []
    stream_calls = []
    timeline_calls = []
    clock_value = 1_700_000_000.0

    @asynccontextmanager
    async def fake_get_db():
        async with _AsyncSqliteConn(db_path) as db:
            yield db

    async def fake_append_and_broadcast(entry):
        logs.append(dict(entry))
        broadcasts.append({"type": "monitor_log", "data": dict(entry)})
        return True

    async def fake_broadcast(payload):
        broadcasts.append(payload)

    async def fake_last_user_msg_time():
        return 0

    async def fake_read_runtime_context(**_kwargs):
        return build_sentinel_runtime_context(runtime_context or {
            "recent_chat": [{"role": "user", "content": "我刚忙完。"}],
            "last_user_message_age_sec": 3600,
            "last_wake_age_sec": 3600,
        })

    async def fake_read_core_wake_execution_context(**_kwargs):
        if core_context_error:
            raise core_context_error
        context = {
            "conv_id": "conv_sentinel",
            "model_key": "mock-model",
            "recent_messages": [{"id": "m1", "role": "user", "content": "我刚忙完。"}],
            "last_user_message_age_sec": 3600,
            "user_name": "用户",
            "ai_name": "Aion",
        }
        if toy_capability_allowed:
            context.update({
                "toy_capability_allowed": True,
                "toy_capability_reason": "allowed",
                "control_session_id": "ctrl_frozen",
                "control_kind": "whisper",
                "control_status": "active",
                "control_epoch": 0,
                "owner_client_id": "tab_frozen",
                "control_device_id": "browser_toy_bridge",
            })
        return context

    async def fake_call_slot_chat(*_args, **_kwargs):
        messages = _kwargs.get("messages") or []
        if messages and "wake_intent" in messages[0]["content"]:
            if slot_call_kinds is not None:
                slot_call_kinds.append("v2")
            if v2_provider_error:
                raise v2_provider_error
            return json.dumps({
                "monitoringlog": "新链路看到用户可能刚空下来。",
                "summary": "当前像轻唤醒窗口。",
                "score": 8,
                "confidence": 0.8,
                "wake_intent": True,
                "call_core": True,
                "core_reason": "V2 判断她刚忙完，适合轻轻出现。",
                "restraint_reason": "",
                "uncertainty": "不知道她是否愿意展开聊天。",
                "suggested_next_check_sec": 600,
                "tone_hint": "轻轻问一句刚忙完了吗",
            }, ensure_ascii=False)
        if slot_call_kinds is not None:
            slot_call_kinds.append("legacy")
        return json.dumps({
            "monitoringlog": "用户可能刚空下来。",
            "summary": "当前像轻唤醒窗口。",
            "score": legacy_score,
            "core_reason": "她刚忙完，适合轻轻出现。" if legacy_score >= 7 else "",
        }, ensure_ascii=False)

    async def fake_timeline_context(*, visible_message_ids, now=None):
        timeline_calls.append({
            "visible_message_ids": list(visible_message_ids),
            "now": now,
        })
        return {
            "status": "injected",
            "block": "[最近三天的事]\n· 今天 21:00 用户说忙完了。",
            "entries": [{"text": "用户说忙完了。"}],
        }

    async def fake_stream_core(messages, model_key, temperature=None):
        stream_calls.append({
            "messages": list(messages),
            "model_key": model_key,
            "temperature": temperature,
        })
        if stream_error is not None:
            raise stream_error
        for chunk in stream_chunks if stream_chunks is not None else ["刚好想到你。"]:
            yield chunk

    async def fake_sleep(_seconds):
        return None

    def fake_clock():
        nonlocal clock_value
        clock_value += 1
        return clock_value

    ports = LegacyCoreWakePorts(
        db_factory=fake_get_db,
        broadcaster=fake_broadcast,
        core_streamer=fake_stream_core,
        monitor_log_writer=fake_append_and_broadcast,
        clock=fake_clock,
        default_temperature=0.4,
        sleeper=fake_sleep,
        control_session_service_obj=_NoControlSessions(),
    )

    async def disabled_web_search(**_kwargs):
        return {"status": "disabled", "block": ""}

    async def skipped_web_search_finalize(**_kwargs):
        return {"status": "skipped"}

    ports.prepare_web_search_turn = disabled_web_search
    ports.finalize_web_search_turn = skipped_web_search_finalize

    monkeypatch.setattr(sentinel_runtime, "cleanup_old_logs", lambda _keep_days=3: None)
    monkeypatch.setattr(sentinel_runtime, "load_cam_config", lambda: {})
    monkeypatch.setattr(sentinel_runtime, "load_worldbook", lambda: {"user_name": "用户", "ai_name": "Aion"})
    monkeypatch.setattr(sentinel_runtime, "load_chat_status", lambda: {"status": ""})
    monkeypatch.setattr(sentinel_runtime, "load_ai_behavior", lambda: {
        "sentinel_call_core_criteria": "score >= 7 时可以唤醒 Core。",
        "sentinel_wake_threshold": 7,
        "sentinel_v2_provider_enabled": provider_enabled,
        "sentinel_v2_full_wake_enabled": True,
        "sentinel_v2_full_wake_legacy_fallback_enabled": legacy_fallback_enabled,
    })
    monkeypatch.setattr(sentinel_runtime, "async_get_last_user_msg_time", fake_last_user_msg_time)
    monkeypatch.setattr(sentinel_runtime, "read_logs_since", lambda _since: [])
    monkeypatch.setattr(sentinel_runtime, "get_db", fake_get_db)
    monkeypatch.setattr(sentinel_runtime, "append_and_broadcast_monitor_log", fake_append_and_broadcast)
    monkeypatch.setattr(sentinel_runtime, "manager", SimpleNamespace(broadcast=fake_broadcast))
    monkeypatch.setattr(sentinel_runtime, "read_sentinel_runtime_context", fake_read_runtime_context)
    monkeypatch.setattr(sentinel_runtime, "read_core_wake_execution_context", fake_read_core_wake_execution_context)
    monkeypatch.setattr(
        "sentinel_core_wake_adapters.load_sentinel_timeline_prompt_context",
        fake_timeline_context,
    )
    monkeypatch.setattr(sentinel_runtime, "build_legacy_core_wake_ports", lambda **_kwargs: ports)
    monkeypatch.setattr(
        sentinel_runtime.timeline_service,
        "start_background_refresh",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(sentinel_runtime, "call_slot_chat", fake_call_slot_chat)
    monkeypatch.setattr(sentinel_runtime, "record_sentinel_event", lambda event: dict(event))
    _install_signal_modules(monkeypatch)

    return db_path, logs, broadcasts, stream_calls, timeline_calls


def test_sentinel_v2_provider_enabled_accepts_legacy_shadow_config_name():
    assert sentinel_runtime._sentinel_v2_provider_enabled({
        "sentinel_v2_provider_shadow_enabled": True,
    }) is True
    assert sentinel_runtime._sentinel_v2_provider_enabled({
        "sentinel_v2_provider_enabled": False,
        "sentinel_v2_provider_shadow_enabled": True,
    }) is False


def test_append_monitor_log_creates_missing_log_dir(monkeypatch, tmp_path):
    log_dir = tmp_path / "missing" / "monitor_logs"
    monkeypatch.setattr(sentinel_runtime, "MONITOR_LOGS_DIR", log_dir)

    sentinel_runtime.append_monitor_log({"timestamp": 1, "monitoringlog": "ok"})

    files = list(log_dir.glob("*.jsonl"))
    assert len(files) == 1
    assert json.loads(files[0].read_text(encoding="utf-8").strip())["monitoringlog"] == "ok"


def test_append_and_broadcast_monitor_log_is_best_effort(monkeypatch):
    async def broken_broadcast(_payload):
        raise RuntimeError("ws down")

    monkeypatch.setattr(
        sentinel_runtime,
        "append_monitor_log",
        lambda _entry: (_ for _ in ()).throw(RuntimeError("disk down")),
    )
    monkeypatch.setattr(sentinel_runtime, "manager", SimpleNamespace(broadcast=broken_broadcast))

    appended = asyncio.run(sentinel_runtime.append_and_broadcast_monitor_log({"monitoringlog": "x"}))

    assert appended is False


def test_public_monitor_log_hides_exception_but_keeps_durable_diagnostics(monkeypatch, tmp_path):
    broadcasts = []

    async def capture_broadcast(payload):
        broadcasts.append(payload)

    monkeypatch.setattr(sentinel_runtime, "MONITOR_LOGS_DIR", tmp_path)
    monkeypatch.setattr(
        sentinel_runtime,
        "manager",
        SimpleNamespace(broadcast=capture_broadcast),
    )
    entry = {
        "timestamp": 1,
        "source": "sentinel",
        "status": "sentinel_v2_primary_failed",
        "monitoringlog": (
            "ValueError: sentinel runtime reader message.content must be non-empty text"
        ),
        "error_type": "ValueError",
        "error": "sentinel runtime reader message.content must be non-empty text",
        "original_error": "ValueError: internal detail",
        "context_errors": ["internal detail"],
        "core_wake_execution": {"error_type": "ValueError"},
        "core_wake_preflight": {"error_type": "ValueError"},
        "sentinel_v2_shadow": {
            "status": "failed",
            "error_type": "ValueError",
            "error": "internal detail",
        },
    }

    appended = asyncio.run(sentinel_runtime.append_and_broadcast_monitor_log(entry))

    assert appended is True
    raw = json.loads(next(tmp_path.glob("*.jsonl")).read_text(encoding="utf-8"))
    assert raw["error_type"] == "ValueError"
    assert raw["error"] == "sentinel runtime reader message.content must be non-empty text"

    public_entries = sentinel_runtime.read_monitor_logs()
    public_payload = broadcasts[0]["data"]
    for public in (public_entries[0], public_payload):
        assert public["monitoringlog"] == "⚠️ 哨兵本轮判断失败，已跳过。"
        assert "error_type" not in public
        assert "error" not in public
        assert "original_error" not in public
        assert "context_errors" not in public
        assert "core_wake_execution" not in public
        assert "core_wake_preflight" not in public
        assert "sentinel_v2_shadow" not in public


def test_analyze_logs_provider_failure_without_core_wake(monkeypatch):
    logs, broadcasts = _patch_runtime_env(monkeypatch)
    core_calls = []

    async def failing_call_slot_chat(*_args, **_kwargs):
        raise RuntimeError("provider down")

    async def fake_call_core(*args, **kwargs):
        core_calls.append((args, kwargs))

    monkeypatch.setattr(sentinel_runtime, "call_slot_chat", failing_call_slot_chat)
    runtime = sentinel_runtime.SentinelRuntime()
    monkeypatch.setattr(runtime, "_call_core", fake_call_core)

    asyncio.run(runtime._analyze_and_log())

    assert [entry["status"] for entry in logs] == ["provider_failed"]
    assert logs[0]["source"] == "sentinel"
    assert logs[0]["call_core"] is False
    assert logs[0]["error_type"] == "RuntimeError"
    assert "provider down" in logs[0]["error"]
    assert core_calls == []
    assert broadcasts[0]["type"] == "monitor_log"


def test_analyze_records_cycle_summary_without_prompt_text(monkeypatch):
    logs, _broadcasts = _patch_runtime_env(monkeypatch)
    events = []

    async def fake_call_slot_chat(*_args, **_kwargs):
        return json.dumps({
            "monitoringlog": "信号不足。",
            "summary": "暂无明确状态。",
            "score": 0,
            "core_reason": "",
        }, ensure_ascii=False)

    monkeypatch.setattr(sentinel_runtime, "call_slot_chat", fake_call_slot_chat)
    monkeypatch.setattr(sentinel_runtime, "record_sentinel_event", lambda event: events.append(dict(event)) or event)
    runtime = sentinel_runtime.SentinelRuntime()

    asyncio.run(runtime._analyze_and_log())

    assert [entry["status"] for entry in logs] == ["decided"]
    assert len(events) == 1
    event = events[0]
    assert event["scope"] == "sentinel:cycle_summary"
    assert event["status"] == "decided"
    assert event["ok"] is True
    assert event["meta"]["score"] == 0
    assert event["meta"]["call_core"] is False
    assert event["meta"]["location_present"] is True
    assert event["meta"]["activity_present"] is True
    assert event["meta"]["sensing_present"] is True
    assert event["meta"]["context_error_count"] >= 1
    assert "recent_chat_failed: RuntimeError: recent chat unavailable" in event["meta"]["context_errors"]
    assert "monitoringlog" not in event["meta"]
    assert "recent_chat_text" not in event["meta"]


def test_analyze_does_not_feed_retired_legacy_chat_status_to_judgment(monkeypatch):
    logs, _broadcasts = _patch_runtime_env(monkeypatch)
    stale_status = "用户正在准备去上八点的游泳课"
    captured_messages = []
    events = []

    monkeypatch.setattr(
        sentinel_runtime,
        "load_chat_status",
        lambda: {"status": stale_status, "updated_at": 1},
    )

    async def fake_call_slot_chat(*_args, **kwargs):
        captured_messages.extend(kwargs["messages"])
        return json.dumps({
            "monitoringlog": "信号不足。",
            "summary": "暂无明确状态。",
            "score": 0,
            "core_reason": "",
        }, ensure_ascii=False)

    monkeypatch.setattr(sentinel_runtime, "call_slot_chat", fake_call_slot_chat)
    monkeypatch.setattr(
        sentinel_runtime,
        "record_sentinel_event",
        lambda event: events.append(dict(event)) or event,
    )

    asyncio.run(sentinel_runtime.SentinelRuntime()._analyze_and_log())

    prompt_text = "\n".join(message["content"] for message in captured_messages)
    assert stale_status not in prompt_text
    assert "最后的聊天状态" not in prompt_text
    assert "最近亲口说的情况，永远压过设备信号" in prompt_text
    assert "骗你、撒谎、编故事，或者被你抓到了" in prompt_text
    assert logs[0]["status"] == "decided"
    assert events[0]["meta"]["chat_status_present"] is False


def test_cp3b_legacy_judgment_uses_shared_context_once(monkeypatch):
    _logs, _broadcasts = _patch_runtime_env(monkeypatch)
    captured_messages = []
    shared = (
        "[设备与环境上下文]\n"
        "直接观测：\n"
        "- 15:00 手机报告屏幕亮起。"
    )

    monkeypatch.setattr(sentinel_runtime, "load_worldbook", lambda: {
        "user_name": "阿玖",
        "ai_name": "Aion",
    })
    monkeypatch.setattr(sentinel_runtime, "load_ai_behavior", lambda: {
        "sentinel_call_core_criteria": "score >= 7 时可以唤醒 Core。",
        "sentinel_wake_threshold": 7,
        "sentinel_v2_provider_enabled": False,
        "sentinel_v2_full_wake_enabled": False,
        "context_delivery_autonomous_enabled": True,
    })
    async def load_shared_context(**kwargs):
        return shared if kwargs["user_name"] == "阿玖" else ""

    monkeypatch.setattr(
        sentinel_runtime,
        "load_autonomous_context_delivery",
        load_shared_context,
    )

    async def fake_call_slot_chat(*_args, **kwargs):
        captured_messages.extend(kwargs["messages"])
        return json.dumps({
            "monitoringlog": "信号不足。",
            "summary": "暂无明确状态。",
            "score": 0,
            "core_reason": "",
        }, ensure_ascii=False)

    monkeypatch.setattr(sentinel_runtime, "call_slot_chat", fake_call_slot_chat)

    asyncio.run(sentinel_runtime.SentinelRuntime()._analyze_and_log())

    prompt = "\n".join(item["content"] for item in captured_messages)
    assert prompt.count("[设备与环境上下文]") == 1
    assert "当前位置：信号可用。" not in prompt
    assert "屏幕最近解锁过。" not in prompt
    assert "近一小时主要在聊天。" not in prompt
    assert "最后的聊天状态" not in prompt


def test_cp3b_legacy_core_uses_shared_context_once(monkeypatch, tmp_path):
    _db_path, _logs, _broadcasts = _patch_core_env(monkeypatch, tmp_path)
    captured = {}
    shared = (
        "[设备与环境上下文]\n"
        "直接观测：\n"
        "- 15:00 手机报告屏幕亮起。"
    )

    async def fake_stream_ai(messages, _model_key, temperature=None):
        captured["messages"] = list(messages)
        yield "我在。"

    import ai_providers

    monkeypatch.setattr(sentinel_runtime, "load_worldbook", lambda: {
        "user_name": "阿玖",
        "ai_name": "Aion",
    })
    monkeypatch.setattr(
        sentinel_runtime,
        "load_ai_behavior",
        lambda: {"context_delivery_autonomous_enabled": True},
    )
    async def load_shared_context(**kwargs):
        return shared if kwargs["user_name"] == "阿玖" else ""

    monkeypatch.setattr(
        sentinel_runtime,
        "load_autonomous_context_delivery",
        load_shared_context,
    )
    monkeypatch.setattr(ai_providers, "stream_ai", fake_stream_ai)

    asyncio.run(sentinel_runtime.SentinelRuntime()._call_core(
        "设备状态值得看一眼",
        last_user_ts=0,
        summary="信号偏少",
        core_reason="适合出现",
        cached_logs=[],
    ))

    prompt = captured["messages"][-1]["content"]
    assert prompt.count("[设备与环境上下文]") == 1
    assert "当前位置：信号可用。" not in prompt
    assert "屏幕最近解锁过。" not in prompt
    assert "阿玖最近亲口说的情况" in prompt


def test_analyze_records_cycle_crash_summary(monkeypatch):
    _logs, _broadcasts = _patch_runtime_env(monkeypatch)
    events = []

    def crashing_worldbook():
        raise RuntimeError("worldbook exploded")

    monkeypatch.setattr(sentinel_runtime, "load_worldbook", crashing_worldbook)
    monkeypatch.setattr(sentinel_runtime, "record_sentinel_event", lambda event: events.append(dict(event)) or event)
    runtime = sentinel_runtime.SentinelRuntime()

    asyncio.run(runtime._analyze_and_log())

    assert len(events) == 1
    event = events[0]
    assert event["scope"] == "sentinel:cycle_summary"
    assert event["status"] == "cycle_crashed"
    assert event["ok"] is False
    assert event["error_type"] == "RuntimeError"
    assert event["meta"]["wake_blocked_reason"] == "cycle_crashed"
    assert "cycle_crashed: RuntimeError: worldbook exploded" in event["meta"]["context_errors"]


def test_analyze_continues_when_context_reads_fail(monkeypatch):
    logs, _broadcasts = _patch_runtime_env(monkeypatch)

    async def failing_last_user_time():
        raise RuntimeError("db down")

    def failing_logs(_since):
        raise RuntimeError("logs broken")

    async def fake_call_slot_chat(*_args, **_kwargs):
        return json.dumps({
            "monitoringlog": "信号不足。",
            "summary": "暂无明确状态。",
            "score": 0,
            "core_reason": "",
        }, ensure_ascii=False)

    monkeypatch.setattr(sentinel_runtime, "async_get_last_user_msg_time", failing_last_user_time)
    monkeypatch.setattr(sentinel_runtime, "read_logs_since", failing_logs)
    monkeypatch.setattr(sentinel_runtime, "call_slot_chat", fake_call_slot_chat)
    runtime = sentinel_runtime.SentinelRuntime()

    asyncio.run(runtime._analyze_and_log())

    assert [entry["status"] for entry in logs] == ["decided"]
    assert logs[0]["call_core"] is False
    assert "last_user_time_failed: RuntimeError: db down" in logs[0]["context_errors"]
    assert "monitor_logs_failed: RuntimeError: logs broken" in logs[0]["context_errors"]


def test_analyze_parse_fallback_preserves_core_wake(monkeypatch):
    logs, _broadcasts = _patch_runtime_env(monkeypatch)
    core_calls = []

    async def malformed_call_slot_chat(*_args, **_kwargs):
        return (
            '{"monitoringlog":"用户刚解锁，可能有空。",'
            '"summary":"近期信号偏活跃。","score":8,'
            '"core_reason":"用户可能正适合被联系。"'
        )

    async def fake_call_core(*args, **kwargs):
        core_calls.append((args, kwargs))

    monkeypatch.setattr(sentinel_runtime, "call_slot_chat", malformed_call_slot_chat)
    runtime = sentinel_runtime.SentinelRuntime()
    monkeypatch.setattr(runtime, "_call_core", fake_call_core)

    asyncio.run(runtime._analyze_and_log())

    assert [entry["status"] for entry in logs] == ["parse_fallback"]
    assert logs[0]["parse_fallback"] is True
    assert logs[0]["score"] == 8
    assert logs[0]["call_core"] is True
    assert logs[0]["core_reason"] == "用户可能正适合被联系。"
    assert len(core_calls) == 1


def test_analyze_attaches_v2_shadow_trace_without_changing_legacy_wake(monkeypatch):
    logs, _broadcasts = _patch_runtime_env(monkeypatch)
    core_calls = []

    async def fake_read_runtime_context(**_kwargs):
        return build_sentinel_runtime_context({
            "recent_chat": [{"role": "user", "content": "我刚忙完。"}],
            "last_user_message_age_sec": 700,
            "last_wake_age_sec": 2000,
        })

    async def fake_call_slot_chat(*_args, **_kwargs):
        return json.dumps({
            "monitoringlog": "用户可能刚空下来。",
            "summary": "当前像轻唤醒窗口。",
            "score": 8,
            "core_reason": "她刚忙完，适合轻轻出现。",
        }, ensure_ascii=False)

    async def fake_call_core(*args, **kwargs):
        core_calls.append((args, kwargs))

    async def fake_core_wake_execution_context(**_kwargs):
        return {
            "conv_id": "conv_sentinel",
            "model_key": "mock-model",
            "recent_messages": [{"role": "user", "content": "我刚忙完。"}],
            "last_user_message_age_sec": 700,
            "user_name": "用户",
            "ai_name": "Aion",
        }

    monkeypatch.setattr(sentinel_runtime, "read_sentinel_runtime_context", fake_read_runtime_context)
    monkeypatch.setattr(sentinel_runtime, "read_core_wake_execution_context", fake_core_wake_execution_context)
    monkeypatch.setattr(sentinel_runtime, "call_slot_chat", fake_call_slot_chat)
    runtime = sentinel_runtime.SentinelRuntime()
    monkeypatch.setattr(runtime, "_call_core", fake_call_core)

    asyncio.run(runtime._analyze_and_log())

    shadow = logs[0]["sentinel_v2_shadow"]
    assert logs[0]["status"] == "core_wake_requested"
    assert logs[0]["call_core"] is True
    assert len(core_calls) == 1
    assert shadow["schema_version"] == sentinel_runtime.SENTINEL_V2_SHADOW_SCHEMA_VERSION
    assert shadow["runtime_mode"] == "dry_run"
    assert shadow["status"] == "ok"
    assert shadow["side_effects"] == []
    assert shadow["fallback_used"] is False
    assert shadow["judgment"]["wake_intent"] is True
    assert shadow["gate"]["status"] == "passed"
    assert shadow["wake_package_created"] is True
    assert shadow["metrics"]["wake_package_created"] is True
    assert shadow["core_wake_preflight"]["status"] == "ready"
    assert shadow["core_wake_preflight"]["would_call_core"] is True
    assert shadow["core_wake_preflight"]["core_request"]["history_message_count"] == 1
    assert shadow["core_wake_preflight"]["core_request"]["prompt_char_count"] > 0
    assert shadow["core_wake_execution"]["status"] == "disabled"
    assert shadow["core_wake_execution"]["execution_mode"] == "disabled"
    assert shadow["core_wake_execution"]["execution_enabled"] is False
    assert shadow["core_wake_execution"]["would_call_core"] is True
    assert shadow["core_wake_execution"]["core_request"]["history_message_count"] == 1
    assert shadow["core_wake_execution"]["side_effects"] == []
    assert shadow["core_wake_execution"]["production_side_effects"] == []
    assert next(
        item for item in shadow["core_wake_execution"]["execution_steps"] if item["name"] == "stream_core"
    )["status"] == "planned"
    assert "messages" not in shadow
    assert "raw_output" not in shadow
    assert "messages" not in json.dumps(shadow["core_wake_preflight"], ensure_ascii=False)
    assert "core_prompt" not in json.dumps(shadow["core_wake_preflight"], ensure_ascii=False)
    assert '"messages"' not in json.dumps(shadow["core_wake_execution"], ensure_ascii=False)
    assert "core_prompt" not in json.dumps(shadow["core_wake_execution"], ensure_ascii=False)


def test_analyze_can_run_v2_provider_shadow_without_changing_legacy_behavior(monkeypatch):
    logs, _broadcasts = _patch_runtime_env(monkeypatch)
    core_calls = []
    provider_calls = []

    async def fake_read_runtime_context(**_kwargs):
        return build_sentinel_runtime_context({
            "recent_chat": [{"role": "user", "content": "我刚忙完。"}],
            "last_user_message_age_sec": 900,
            "last_wake_age_sec": 3600,
        })

    async def fake_call_slot_chat(_slot_name, *, messages, **_kwargs):
        provider_calls.append(messages)
        if "wake_intent" in messages[0]["content"]:
            return json.dumps({
                "monitoringlog": "新链路看到用户可能刚空下来。",
                "summary": "当前像轻唤醒窗口。",
                "score": 7,
                "confidence": 0.8,
                "wake_intent": True,
                "call_core": True,
                "core_reason": "她刚忙完，适合轻轻出现。",
                "restraint_reason": "",
                "uncertainty": "不知道她是否愿意展开聊天。",
                "suggested_next_check_sec": 600,
                "tone_hint": "轻轻问一句刚忙完了吗",
            }, ensure_ascii=False)
        return json.dumps({
            "monitoringlog": "旧链路认为信号不足。",
            "summary": "旧链路不唤醒。",
            "score": 1,
            "core_reason": "",
        }, ensure_ascii=False)

    async def fake_call_core(*args, **kwargs):
        core_calls.append((args, kwargs))

    monkeypatch.setattr(sentinel_runtime, "read_sentinel_runtime_context", fake_read_runtime_context)
    monkeypatch.setattr(sentinel_runtime, "load_ai_behavior", lambda: {
        "sentinel_call_core_criteria": "score >= 7 时可以唤醒 Core。",
        "sentinel_wake_threshold": 7,
        "sentinel_v2_provider_enabled": True,
    })
    monkeypatch.setattr(sentinel_runtime, "call_slot_chat", fake_call_slot_chat)
    runtime = sentinel_runtime.SentinelRuntime()
    monkeypatch.setattr(runtime, "_call_core", fake_call_core)

    asyncio.run(runtime._analyze_and_log())

    shadow = logs[0]["sentinel_v2_shadow"]
    assert len(provider_calls) == 2
    assert logs[0]["status"] == "decided"
    assert logs[0]["call_core"] is False
    assert core_calls == []
    assert shadow["status"] == "ok"
    assert shadow["judgment_source"] == "provider"
    assert shadow["side_effects"] == [sentinel_runtime.SENTINEL_V2_PROVIDER_SHADOW_EFFECT]
    assert shadow["production_side_effects"] == []
    assert shadow["judgment"]["wake_intent"] is True
    assert shadow["gate"]["status"] == "passed"
    assert shadow["wake_package_created"] is True
    assert "messages" not in shadow
    assert "raw_output" not in shadow


def test_analyze_provider_shadow_records_gate_block_reason(monkeypatch):
    logs, _broadcasts = _patch_runtime_env(monkeypatch)

    async def fake_read_runtime_context(**_kwargs):
        return build_sentinel_runtime_context({
            "last_user_message_age_sec": 3600,
            "last_wake_age_sec": 3600,
            "quiet_hours_active": True,
            "clear_sleep": False,
            "device_effect_requested": False,
            "device_effect_allowed": False,
        })

    async def fake_call_slot_chat(_slot_name, *, messages, **_kwargs):
        if "wake_intent" in messages[0]["content"]:
            return json.dumps({
                "monitoringlog": "新链路认为适合轻唤醒。",
                "summary": "当前像轻唤醒窗口。",
                "score": 8,
                "confidence": 0.8,
                "wake_intent": True,
                "call_core": True,
                "core_reason": "她可能刚空下来，适合轻轻出现。",
                "restraint_reason": "",
                "uncertainty": "不知道她是否愿意展开聊天。",
                "suggested_next_check_sec": 600,
                "tone_hint": "轻轻问一句",
            }, ensure_ascii=False)
        return json.dumps({
            "monitoringlog": "旧链路认为信号不足。",
            "summary": "旧链路不唤醒。",
            "score": 1,
            "core_reason": "",
        }, ensure_ascii=False)

    monkeypatch.setattr(sentinel_runtime, "read_sentinel_runtime_context", fake_read_runtime_context)
    monkeypatch.setattr(sentinel_runtime, "load_ai_behavior", lambda: {
        "sentinel_call_core_criteria": "score >= 7 时可以唤醒 Core。",
        "sentinel_wake_threshold": 7,
        "sentinel_v2_provider_enabled": True,
    })
    monkeypatch.setattr(sentinel_runtime, "call_slot_chat", fake_call_slot_chat)
    runtime = sentinel_runtime.SentinelRuntime()

    asyncio.run(runtime._analyze_and_log())

    shadow = logs[0]["sentinel_v2_shadow"]
    assert logs[0]["call_core"] is False
    assert shadow["judgment_source"] == "provider"
    assert shadow["judgment"]["wake_intent"] is True
    assert shadow["gate"]["status"] == "blocked"
    assert shadow["gate"]["wake_allowed"] is False
    assert shadow["gate"]["blocked_reasons"] == ["quiet_hours"]
    assert shadow["wake_package_created"] is False
    assert shadow["core_wake_preflight"] is None
    assert shadow["core_wake_execution"] is None


def test_analyze_records_core_wake_preflight_reader_failure_loudly(monkeypatch):
    logs, _broadcasts = _patch_runtime_env(monkeypatch)
    core_calls = []

    async def fake_read_runtime_context(**_kwargs):
        return build_sentinel_runtime_context({
            "recent_chat": [{"role": "user", "content": "我刚忙完。"}],
            "last_user_message_age_sec": 900,
            "last_wake_age_sec": 3600,
        })

    async def failing_core_wake_context(**_kwargs):
        raise RuntimeError("core context reader down")

    async def fake_call_slot_chat(*_args, **_kwargs):
        return json.dumps({
            "monitoringlog": "用户可能刚空下来。",
            "summary": "当前像轻唤醒窗口。",
            "score": 8,
            "core_reason": "她刚忙完，适合轻轻出现。",
        }, ensure_ascii=False)

    async def fake_call_core(*args, **kwargs):
        core_calls.append((args, kwargs))

    monkeypatch.setattr(sentinel_runtime, "read_sentinel_runtime_context", fake_read_runtime_context)
    monkeypatch.setattr(sentinel_runtime, "read_core_wake_execution_context", failing_core_wake_context)
    monkeypatch.setattr(sentinel_runtime, "call_slot_chat", fake_call_slot_chat)
    runtime = sentinel_runtime.SentinelRuntime()
    monkeypatch.setattr(runtime, "_call_core", fake_call_core)

    asyncio.run(runtime._analyze_and_log())

    shadow = logs[0]["sentinel_v2_shadow"]
    assert logs[0]["call_core"] is True
    assert len(core_calls) == 1
    assert shadow["status"] == "ok"
    assert shadow["wake_package_created"] is True
    assert shadow["core_wake_preflight"]["status"] == "failed"
    assert shadow["core_wake_preflight"]["would_call_core"] is False
    assert shadow["core_wake_preflight"]["would_write_monitor_log"] == "core_preflight_reader_failed"
    assert shadow["core_wake_preflight"]["error_type"] == "RuntimeError"
    assert "core context reader down" in shadow["core_wake_preflight"]["error"]
    assert shadow["core_wake_preflight"]["side_effects"] == []
    assert shadow["core_wake_preflight"]["production_side_effects"] == []
    assert shadow["core_wake_execution"]["status"] == "failed"
    assert shadow["core_wake_execution"]["would_call_core"] is False
    assert shadow["core_wake_execution"]["would_write_monitor_log"] == "core_orchestrator_reader_failed"
    assert shadow["core_wake_execution"]["error_type"] == "RuntimeError"
    assert "core context reader down" in shadow["core_wake_execution"]["error"]
    assert shadow["core_wake_execution"]["side_effects"] == []
    assert shadow["core_wake_execution"]["production_side_effects"] == []


def test_analyze_records_v2_provider_shadow_bad_output_loudly(monkeypatch):
    logs, _broadcasts = _patch_runtime_env(monkeypatch)
    provider_calls = []

    async def fake_call_slot_chat(_slot_name, *, messages, **_kwargs):
        provider_calls.append(messages)
        if "wake_intent" in messages[0]["content"]:
            return "not json"
        return json.dumps({
            "monitoringlog": "旧链路认为信号不足。",
            "summary": "旧链路不唤醒。",
            "score": 1,
            "core_reason": "",
        }, ensure_ascii=False)

    monkeypatch.setattr(sentinel_runtime, "load_ai_behavior", lambda: {
        "sentinel_call_core_criteria": "score >= 7 时可以唤醒 Core。",
        "sentinel_wake_threshold": 7,
        "sentinel_v2_provider_enabled": True,
    })
    monkeypatch.setattr(sentinel_runtime, "call_slot_chat", fake_call_slot_chat)
    runtime = sentinel_runtime.SentinelRuntime()

    asyncio.run(runtime._analyze_and_log())

    shadow = logs[0]["sentinel_v2_shadow"]
    assert len(provider_calls) == 2
    assert logs[0]["status"] == "decided"
    assert logs[0]["call_core"] is False
    assert shadow["status"] == "failed"
    assert shadow["judgment_source"] == "provider"
    assert shadow["side_effects"] == [sentinel_runtime.SENTINEL_V2_PROVIDER_SHADOW_EFFECT]
    assert shadow["production_side_effects"] == []
    assert shadow["fallback_used"] is False
    assert shadow["error_type"] == "ValueError"
    assert "sentinel judgment output must be JSON" in shadow["error"]


def test_analyze_records_v2_shadow_failure_loudly_without_breaking_legacy(monkeypatch):
    logs, _broadcasts = _patch_runtime_env(monkeypatch)

    async def failing_read_runtime_context(**_kwargs):
        raise RuntimeError("shadow reader down")

    async def fake_call_slot_chat(*_args, **_kwargs):
        return json.dumps({
            "monitoringlog": "信号不足。",
            "summary": "暂无明确状态。",
            "score": 0,
            "core_reason": "",
        }, ensure_ascii=False)

    monkeypatch.setattr(sentinel_runtime, "read_sentinel_runtime_context", failing_read_runtime_context)
    monkeypatch.setattr(sentinel_runtime, "call_slot_chat", fake_call_slot_chat)
    runtime = sentinel_runtime.SentinelRuntime()

    asyncio.run(runtime._analyze_and_log())

    shadow = logs[0]["sentinel_v2_shadow"]
    assert logs[0]["status"] == "decided"
    assert logs[0]["call_core"] is False
    assert shadow["status"] == "failed"
    assert shadow["error_type"] == "RuntimeError"
    assert "shadow reader down" in shadow["error"]
    assert shadow["side_effects"] == []
    assert shadow["fallback_used"] is False


def test_analyze_full_wake_switch_uses_v2_orchestrator_without_legacy_call(monkeypatch, tmp_path):
    db_path, logs, broadcasts, stream_calls, timeline_calls = _patch_v2_full_wake_env(monkeypatch, tmp_path)
    legacy_calls = []

    async def fake_legacy_call_core(*args, **kwargs):
        legacy_calls.append((args, kwargs))

    runtime = sentinel_runtime.SentinelRuntime()
    monkeypatch.setattr(runtime, "_call_core", fake_legacy_call_core)

    asyncio.run(runtime._analyze_and_log())

    assert [entry["status"] for entry in logs] == ["core_wake_requested", "core_succeeded"]
    assert logs[0]["call_core"] is True
    assert logs[0]["legacy_call_core"] is True
    assert logs[0]["sentinel_v2_full_wake_enabled"] is True
    assert logs[0]["sentinel_v2_full_wake_legacy_fallback_enabled"] is False
    assert logs[0]["sentinel_v2_shadow"]["status"] == "ok"
    assert logs[0]["sentinel_v2_shadow"]["wake_package_created"] is True
    assert logs[0]["sentinel_v2_shadow"]["core_wake_execution"]["execution_mode"] == "disabled"
    assert logs[1]["conv_id"] == "conv_sentinel"
    assert logs[1]["core_msg_id"].endswith("_sentinel")
    assert legacy_calls == []
    assert len(stream_calls) == 1
    assert stream_calls[0]["model_key"] == "mock-model"
    assert len(timeline_calls) == 1
    assert timeline_calls[0]["visible_message_ids"] == ["m1"]
    assert isinstance(timeline_calls[0]["now"], float)
    assert any(
        message["content"].startswith("[最近三天的事]")
        for message in stream_calls[0]["messages"]
    )
    assert not any(
        marker in message["content"]
        for marker in ("[相关记忆]", "可参考记忆")
        for message in stream_calls[0]["messages"]
    )
    rows = _fetch_all(db_path, "SELECT role, content FROM messages ORDER BY created_at")
    assert [row["role"] for row in rows] == ["user", "system", "assistant"]
    assert rows[-1]["content"] == "刚好想到你。"
    assert "monitor_alert" in [payload["type"] for payload in broadcasts]
    assert "msg_created" in [payload["type"] for payload in broadcasts]


def test_analyze_full_wake_switch_can_wake_from_v2_provider_when_legacy_would_not(monkeypatch, tmp_path):
    slot_call_kinds = []
    db_path, logs, _broadcasts, stream_calls, _timeline_calls = _patch_v2_full_wake_env(
        monkeypatch,
        tmp_path,
        provider_enabled=True,
        legacy_score=1,
        slot_call_kinds=slot_call_kinds,
    )
    legacy_calls = []

    async def fake_legacy_call_core(*args, **kwargs):
        legacy_calls.append((args, kwargs))

    runtime = sentinel_runtime.SentinelRuntime()
    monkeypatch.setattr(runtime, "_call_core", fake_legacy_call_core)

    asyncio.run(runtime._analyze_and_log())

    assert [entry["status"] for entry in logs] == ["core_wake_requested", "core_succeeded"]
    assert slot_call_kinds == ["v2"]
    assert logs[0]["legacy_call_core"] is False
    assert logs[0]["call_core"] is True
    assert logs[0]["core_reason"] == "V2 判断她刚忙完，适合轻轻出现。"
    assert logs[0]["sentinel_v2_primary_enabled"] is True
    assert logs[0]["sentinel_v2_shadow"]["judgment_source"] == "provider"
    assert logs[0]["sentinel_v2_shadow"]["wake_package_created"] is True
    assert legacy_calls == []
    assert len(stream_calls) == 1
    rows = _fetch_all(db_path, "SELECT role FROM messages ORDER BY created_at")
    assert [row["role"] for row in rows] == ["user", "system", "assistant"]


def test_analyze_full_wake_primary_provider_failure_is_loud_without_legacy_prompt(monkeypatch, tmp_path):
    slot_call_kinds = []
    _db_path, logs, _broadcasts, stream_calls, _timeline_calls = _patch_v2_full_wake_env(
        monkeypatch,
        tmp_path,
        provider_enabled=True,
        slot_call_kinds=slot_call_kinds,
        v2_provider_error=RuntimeError("v2 provider down"),
    )
    legacy_calls = []
    events = []

    async def fake_legacy_call_core(*args, **kwargs):
        legacy_calls.append((args, kwargs))

    runtime = sentinel_runtime.SentinelRuntime()
    monkeypatch.setattr(runtime, "_call_core", fake_legacy_call_core)
    monkeypatch.setattr(sentinel_runtime, "record_sentinel_event", lambda event: events.append(dict(event)) or event)

    asyncio.run(runtime._analyze_and_log())

    assert slot_call_kinds == ["v2"]
    assert [entry["status"] for entry in logs] == ["sentinel_v2_primary_failed"]
    assert logs[0]["call_core"] is False
    assert logs[0]["legacy_call_core"] is False
    assert logs[0]["wake_blocked_reason"] == "sentinel_v2_primary_failed"
    assert logs[0]["error_type"] == "RuntimeError"
    assert "v2 provider down" in logs[0]["error"]
    assert logs[0]["sentinel_v2_shadow"]["status"] == "failed"
    assert legacy_calls == []
    assert stream_calls == []
    assert len(events) == 1
    assert events[0]["scope"] == "sentinel:cycle_summary"
    assert events[0]["status"] == "sentinel_v2_primary_failed"
    assert events[0]["ok"] is False
    assert events[0]["meta"]["used_v2_primary"] is True
    assert events[0]["meta"]["v2_shadow_status"] == "failed"
    assert events[0]["meta"]["v2_error_type"] == "RuntimeError"


def test_analyze_full_wake_switch_rejects_toy_without_control_session(monkeypatch, tmp_path):
    db_path, logs, broadcasts, stream_calls, _timeline_calls = _patch_v2_full_wake_env(
        monkeypatch,
        tmp_path,
        stream_chunks=["靠近一点 [TOY:2]", "，我在。"],
        toy_capability_allowed=True,
    )
    legacy_calls = []

    async def fake_legacy_call_core(*args, **kwargs):
        legacy_calls.append((args, kwargs))

    runtime = sentinel_runtime.SentinelRuntime()
    monkeypatch.setattr(runtime, "_call_core", fake_legacy_call_core)

    asyncio.run(runtime._analyze_and_log())

    assert [entry["status"] for entry in logs] == ["core_wake_requested", "core_succeeded"]
    assert logs[1]["toy_commands"] == ["2"]
    assert logs[1]["toy_command_delivery"]["status"] == "gateway_rejected"
    assert logs[1]["toy_command_delivery"]["reason"] == "no_active_session"
    assert legacy_calls == []
    assert len(stream_calls) == 1
    rows = _fetch_all(db_path, "SELECT role, content FROM messages ORDER BY created_at")
    assert [row["role"] for row in rows] == ["user", "system", "assistant"]
    assert rows[-1]["content"] == "靠近一点 ，我在。"
    assert "[TOY:" not in rows[-1]["content"]
    assert not any(payload["type"] == "toy_command" for payload in broadcasts)


def test_analyze_full_wake_switch_records_core_empty_without_legacy_fallback(monkeypatch, tmp_path):
    db_path, logs, _broadcasts, stream_calls, _timeline_calls = _patch_v2_full_wake_env(
        monkeypatch,
        tmp_path,
        stream_chunks=[],
    )
    legacy_calls = []

    async def fake_legacy_call_core(*args, **kwargs):
        legacy_calls.append((args, kwargs))

    runtime = sentinel_runtime.SentinelRuntime()
    monkeypatch.setattr(runtime, "_call_core", fake_legacy_call_core)

    asyncio.run(runtime._analyze_and_log())

    assert [entry["status"] for entry in logs] == ["core_wake_requested", "core_empty"]
    assert logs[1]["error_type"] == "core_empty"
    assert logs[1]["conv_id"] == "conv_sentinel"
    assert legacy_calls == []
    assert len(stream_calls) == 2
    rows = _fetch_all(db_path, "SELECT role FROM messages ORDER BY created_at")
    assert [row["role"] for row in rows] == ["user"]


def test_analyze_full_wake_cancellation_records_status_without_visible_wake(monkeypatch, tmp_path):
    db_path, logs, broadcasts, stream_calls, _timeline_calls = _patch_v2_full_wake_env(
        monkeypatch,
        tmp_path,
        stream_error=asyncio.CancelledError(),
    )
    runtime = sentinel_runtime.SentinelRuntime()

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(runtime._analyze_and_log())

    assert [entry["status"] for entry in logs] == [
        "core_wake_requested",
        "core_cancelled",
    ]
    assert logs[-1]["error_type"] == "CancelledError"
    assert logs[-1]["core_wake_execution"]["status"] == "core_cancelled"
    assert len(stream_calls) == 1
    rows = _fetch_all(db_path, "SELECT role FROM messages ORDER BY created_at")
    assert [row["role"] for row in rows] == ["user"]
    assert not any(
        payload["type"] in {"monitor_alert", "msg_created"}
        for payload in broadcasts
    )


def test_analyze_full_wake_switch_does_not_silent_fallback_by_default(monkeypatch, tmp_path):
    _db_path, logs, _broadcasts, stream_calls, _timeline_calls = _patch_v2_full_wake_env(
        monkeypatch,
        tmp_path,
        core_context_error=RuntimeError("core context reader down"),
    )
    legacy_calls = []

    async def fake_legacy_call_core(*args, **kwargs):
        legacy_calls.append((args, kwargs))

    runtime = sentinel_runtime.SentinelRuntime()
    monkeypatch.setattr(runtime, "_call_core", fake_legacy_call_core)

    asyncio.run(runtime._analyze_and_log())

    assert [entry["status"] for entry in logs] == ["core_wake_requested", "sentinel_v2_full_wake_failed"]
    assert logs[1]["fallback_used"] is False
    assert logs[1]["fallback_reason"] == ""
    assert logs[1]["error_type"] == "RuntimeError"
    assert "core context reader down" in logs[1]["error"]
    assert logs[1]["core_wake_execution"]["runtime_mode"] == "full"
    assert logs[1]["core_wake_execution"]["execution_mode"] == "full"
    assert legacy_calls == []
    assert stream_calls == []


def test_analyze_full_wake_switch_uses_explicit_legacy_fallback(monkeypatch, tmp_path):
    _db_path, logs, _broadcasts, stream_calls, _timeline_calls = _patch_v2_full_wake_env(
        monkeypatch,
        tmp_path,
        legacy_fallback_enabled=True,
        core_context_error=RuntimeError("core context reader down"),
    )
    legacy_calls = []

    async def fake_legacy_call_core(*args, **kwargs):
        legacy_calls.append((args, kwargs))

    runtime = sentinel_runtime.SentinelRuntime()
    monkeypatch.setattr(runtime, "_call_core", fake_legacy_call_core)

    asyncio.run(runtime._analyze_and_log())

    assert [entry["status"] for entry in logs] == ["core_wake_requested", "sentinel_v2_full_wake_failed"]
    assert logs[0]["sentinel_v2_full_wake_legacy_fallback_enabled"] is True
    assert logs[1]["fallback_used"] is True
    assert logs[1]["fallback_reason"] == "legacy_core_wake"
    assert logs[1]["source_path"] == "sentinel_v2.full_wake"
    assert len(legacy_calls) == 1
    assert stream_calls == []


def test_analyze_full_wake_switch_respects_v2_gate_block_without_fallback(monkeypatch, tmp_path):
    _db_path, logs, _broadcasts, stream_calls, _timeline_calls = _patch_v2_full_wake_env(
        monkeypatch,
        tmp_path,
        runtime_context={
            "recent_chat": [{"role": "user", "content": "我刚忙完。"}],
            "last_user_message_age_sec": 3600,
            "last_wake_age_sec": 3600,
            "quiet_hours_active": True,
            "clear_sleep": False,
            "device_effect_requested": False,
            "device_effect_allowed": False,
        },
    )
    legacy_calls = []

    async def fake_legacy_call_core(*args, **kwargs):
        legacy_calls.append((args, kwargs))

    runtime = sentinel_runtime.SentinelRuntime()
    monkeypatch.setattr(runtime, "_call_core", fake_legacy_call_core)

    asyncio.run(runtime._analyze_and_log())

    assert [entry["status"] for entry in logs] == ["sentinel_v2_gate_blocked"]
    assert logs[0]["legacy_call_core"] is True
    assert logs[0]["call_core"] is False
    assert logs[0]["wake_blocked_reason"] == "sentinel_v2_gate_blocked"
    assert logs[0]["sentinel_v2_full_wake_unavailable_reason"] == "sentinel_v2_gate_blocked"
    assert logs[0]["sentinel_v2_shadow"]["gate"]["status"] == "blocked"
    assert logs[0]["sentinel_v2_shadow"]["wake_package_created"] is False
    assert legacy_calls == []
    assert stream_calls == []


def test_call_core_records_no_conversation(monkeypatch, tmp_path):
    _db_path, logs, broadcasts = _patch_core_env(
        monkeypatch,
        tmp_path,
        with_conversation=False,
    )
    runtime = sentinel_runtime.SentinelRuntime()

    asyncio.run(runtime._call_core(
        "用户可能需要被联系",
        last_user_ts=0,
        summary="信号偏少",
        core_reason="适合出现",
        cached_logs=[],
    ))

    assert [entry["status"] for entry in logs] == ["core_no_conversation"]
    assert logs[0]["error_type"] == "no_conversation"
    assert broadcasts == [{"type": "monitor_log", "data": logs[0]}]


def test_call_core_records_empty_reply(monkeypatch, tmp_path):
    db_path, logs, _broadcasts = _patch_core_env(monkeypatch, tmp_path)

    async def empty_stream_ai(*_args, **_kwargs):
        if False:
            yield ""

    import ai_providers

    monkeypatch.setattr(ai_providers, "stream_ai", empty_stream_ai)
    runtime = sentinel_runtime.SentinelRuntime()

    asyncio.run(runtime._call_core(
        "用户可能需要被联系",
        last_user_ts=0,
        summary="信号偏少",
        core_reason="适合出现",
        cached_logs=[],
    ))

    assert [entry["status"] for entry in logs] == ["core_empty"]
    assert logs[0]["conv_id"] == "conv_sentinel"
    messages = _fetch_all(db_path, "SELECT role FROM messages ORDER BY created_at")
    assert [row["role"] for row in messages] == ["user", "system"]


def test_call_core_records_stream_failure(monkeypatch, tmp_path):
    _db_path, logs, _broadcasts = _patch_core_env(monkeypatch, tmp_path)

    async def failing_stream_ai(*_args, **_kwargs):
        raise RuntimeError("core down")
        yield ""

    import ai_providers

    monkeypatch.setattr(ai_providers, "stream_ai", failing_stream_ai)
    runtime = sentinel_runtime.SentinelRuntime()

    asyncio.run(runtime._call_core(
        "用户可能需要被联系",
        last_user_ts=0,
        summary="信号偏少",
        core_reason="适合出现",
        cached_logs=[],
    ))

    assert [entry["status"] for entry in logs] == ["core_failed"]
    assert logs[0]["error_type"] == "RuntimeError"
    assert "core down" in logs[0]["error"]


def test_call_core_records_success(monkeypatch, tmp_path):
    db_path, logs, broadcasts = _patch_core_env(monkeypatch, tmp_path)

    async def fake_stream_ai(messages, model_key, temperature=None):
        assert model_key == "mock-model"
        assert "哨兵唤醒你的原因" in messages[-1]["content"]
        yield "刚好想到你。"

    import ai_providers

    monkeypatch.setattr(ai_providers, "stream_ai", fake_stream_ai)
    runtime = sentinel_runtime.SentinelRuntime()

    asyncio.run(runtime._call_core(
        "用户可能需要被联系",
        last_user_ts=0,
        summary="信号偏少",
        core_reason="适合出现",
        cached_logs=[],
    ))

    assert [entry["status"] for entry in logs] == ["core_succeeded"]
    assert logs[0]["conv_id"] == "conv_sentinel"
    assert logs[0]["core_msg_id"].endswith("_sentinel")
    messages = _fetch_all(db_path, "SELECT role, content FROM messages ORDER BY created_at")
    assert [row["role"] for row in messages] == ["user", "system", "assistant"]
    assert messages[-1]["content"] == "刚好想到你。"
    assert "msg_created" in [payload["type"] for payload in broadcasts]


def test_call_core_legacy_web_search_is_tail_injected_stripped_and_finalized(
    monkeypatch,
    tmp_path,
):
    db_path, _logs, broadcasts = _patch_core_env(monkeypatch, tmp_path)
    captured = {}
    finalized = []

    async def fake_prepare(**kwargs):
        captured["prepare"] = kwargs
        return {
            "status": "bound",
            "bound_turn_id": kwargs["bound_turn_id"],
            "block": "WEB_READY_BLOCK",
        }

    async def fake_finalize(**kwargs):
        finalized.append(kwargs)
        return {"status": "queued", "search_id": "web-next"}

    async def fake_stream_ai(messages, _model_key, temperature=None):
        del temperature
        captured["messages"] = list(messages)
        yield "想起你了。[WEB_SEARCH_INTENT]查明天的天气[/WEB_SEARCH_INTENT]"

    class FakeLedger:
        def new_invocation_id(self, _kind):
            return "legacy-web-invocation"

        async def record_model_request(self, *_args, **_kwargs):
            return None

        async def record_model_output(self, *_args, **_kwargs):
            return None

        async def record_turn(self, *_args, **_kwargs):
            return None

        async def record_visible_message(self, *_args, **_kwargs):
            return None

    import ai_providers
    from app.web_search import web_search_service

    monkeypatch.setattr(ai_providers, "stream_ai", fake_stream_ai)
    monkeypatch.setattr(sentinel_runtime, "tool_invocation_ledger", FakeLedger())
    monkeypatch.setattr(web_search_service, "prepare_dialogue_turn", fake_prepare)
    monkeypatch.setattr(web_search_service, "finalize_independent", fake_finalize)

    runtime = sentinel_runtime.SentinelRuntime()
    asyncio.run(runtime._call_core(
        "用户可能需要被联系",
        last_user_ts=0,
        summary="信号偏少",
        core_reason="适合出现",
        cached_logs=[],
    ))

    assert captured["messages"][-3]["content"].find("WEB_READY_BLOCK") >= 0
    assert captured["messages"][-1]["content"].find("哨兵唤醒你的原因") >= 0
    assert finalized[0]["intent_text"] == "查明天的天气"
    assert finalized[0]["bound_turn_id"].startswith("sentinel_legacy:")
    messages = _fetch_all(db_path, "SELECT role, content FROM messages ORDER BY created_at")
    assert messages[-1]["content"] == "想起你了。"
    assert all("WEB_SEARCH_INTENT" not in str(payload) for payload in broadcasts)


def test_call_core_prompt_includes_persona_temperature_and_live_toy_capability(monkeypatch, tmp_path):
    _db_path, _logs, _broadcasts = _patch_core_env(monkeypatch, tmp_path)
    captured = {}

    async def fake_stream_ai(messages, model_key, temperature=None):
        captured["messages"] = list(messages)
        captured["model_key"] = model_key
        captured["temperature"] = temperature
        yield "我在。"

    import ai_providers
    import config
    import app.control.toy_capability as toy_capability_module

    async def fake_toy_capability(**_kwargs):
        return toy_capability_module.ToyCapabilitySnapshot(
            True,
            "allowed",
            "conv_sentinel",
            "ctrl_frozen",
            "whisper",
            "active",
            0,
            "tab_frozen",
            "browser_toy_bridge",
        )

    monkeypatch.setattr(sentinel_runtime, "load_worldbook", lambda: {
        "user_name": "云",
        "ai_name": "Aion",
        "ai_persona": "Aion 是温柔但有掌控感的伴侣。",
        "user_persona": "用户最近压力偏大。",
    })
    monkeypatch.setitem(config.SETTINGS, "temperature", 0.66)
    monkeypatch.setattr(toy_capability_module, "resolve_toy_capability_snapshot", fake_toy_capability)
    monkeypatch.setattr(ai_providers, "stream_ai", fake_stream_ai)
    runtime = sentinel_runtime.SentinelRuntime()

    asyncio.run(runtime._call_core(
        "用户可能需要被联系",
        last_user_ts=0,
        summary="信号偏少",
        core_reason="适合出现",
        cached_logs=[],
    ))

    messages = captured["messages"]
    assert captured["model_key"] == "mock-model"
    assert captured["temperature"] == 0.66
    assert messages[0] == {"role": "user", "content": "[关于你自己：Aion]\nAion 是温柔但有掌控感的伴侣。"}
    assert messages[1]["content"] == "（嗯，我知道自己是Aion。）"
    assert messages[2] == {"role": "user", "content": "[关于云]\n用户最近压力偏大。"}
    assert messages[3]["content"] == "（嗯，这是我所了解的云。）"
    assert any(item["role"] == "user" and item["content"] == "我刚忙完" for item in messages)
    prompt = messages[-1]["content"]
    assert "哨兵唤醒你的原因：适合出现" in prompt
    assert "这段时间云的整体状况：信号偏少" in prompt
    assert "当前位置：信号可用。" in prompt
    assert "最近 2 小时体感/体征" in prompt
    assert "屏幕最近解锁过。" in prompt
    assert "最近亲口说的情况，永远压过设备信号" in prompt
    assert "骗你、撒谎、编故事，或者被你抓到了" in prompt
    assert "密语模式" in prompt
    assert "[TOY:1]~[TOY:9]" in prompt


def test_call_core_legacy_fallback_injects_working_model_and_desire_together(monkeypatch, tmp_path):
    _db_path, logs, _broadcasts = _patch_core_env(monkeypatch, tmp_path)
    captured = {}

    async def load_relationship_context():
        return (
            "[你对她的当前认识]\n她看重边界。",
            "[你此刻想以怎样的姿态与她相处]\n主动而尊重。",
        )

    async def fake_stream_ai(messages, model_key, temperature=None):
        captured["messages"] = list(messages)
        yield "我在。"

    import ai_providers

    monkeypatch.setattr(
        sentinel_runtime,
        "load_sentinel_working_model_prompt_context",
        load_relationship_context,
    )
    monkeypatch.setattr(ai_providers, "stream_ai", fake_stream_ai)
    runtime = sentinel_runtime.SentinelRuntime()

    asyncio.run(runtime._call_core(
        "用户可能需要被联系",
        last_user_ts=0,
        summary="信号偏少",
        core_reason="适合出现",
        cached_logs=[],
    ))

    messages = captured["messages"]
    working_model_index = next(
        index for index, message in enumerate(messages)
        if message["content"].startswith("[你对她的当前认识]")
    )
    assert messages[working_model_index:working_model_index + 4] == [
        {"role": "user", "content": "[你对她的当前认识]\n她看重边界。"},
        {"role": "assistant", "content": "（嗯，这是我此刻对她的认识。）"},
        {"role": "user", "content": "[你此刻想以怎样的姿态与她相处]\n主动而尊重。"},
        {"role": "assistant", "content": "（嗯，这是我此刻想带进这段关系里的姿态。）"},
    ]
    assert [entry["status"] for entry in logs] == ["core_succeeded"]


def test_call_core_legacy_fallback_skips_wake_when_working_model_read_fails(monkeypatch, tmp_path):
    db_path, logs, broadcasts = _patch_core_env(monkeypatch, tmp_path)

    async def failing_relationship_context():
        raise RuntimeError("working model unavailable")

    monkeypatch.setattr(
        sentinel_runtime,
        "load_sentinel_working_model_prompt_context",
        failing_relationship_context,
    )
    runtime = sentinel_runtime.SentinelRuntime()

    asyncio.run(runtime._call_core(
        "用户可能需要被联系",
        last_user_ts=0,
        summary="信号偏少",
        core_reason="适合出现",
        cached_logs=[],
    ))

    assert [entry["status"] for entry in logs] == ["working_model_read_failed"]
    assert logs[0]["error_type"] == "working_model_read_failed"
    assert logs[0]["error"] == "working model unavailable"
    assert [row["role"] for row in _fetch_all(db_path, "SELECT role FROM messages ORDER BY created_at")] == ["user"]
    assert [payload["type"] for payload in broadcasts] == ["monitor_log"]


def test_call_core_strips_toy_commands_and_broadcasts_them(monkeypatch, tmp_path):
    db_path, logs, broadcasts = _patch_core_env(monkeypatch, tmp_path)

    async def fake_stream_ai(messages, model_key, temperature=None):
        yield "靠近一点 [TOY:2]，我在。"

    import ai_providers
    import app.control.toy_capability as toy_capability_module

    async def fake_toy_capability(**_kwargs):
        return toy_capability_module.ToyCapabilitySnapshot(
            True,
            "allowed",
            "conv_sentinel",
            "ctrl_frozen",
            "whisper",
            "active",
            0,
            "tab_frozen",
            "browser_toy_bridge",
        )

    monkeypatch.setattr(ai_providers, "stream_ai", fake_stream_ai)
    monkeypatch.setattr(toy_capability_module, "resolve_toy_capability_snapshot", fake_toy_capability)
    runtime = sentinel_runtime.SentinelRuntime()

    asyncio.run(runtime._call_core(
        "用户可能需要被联系",
        last_user_ts=0,
        summary="信号偏少",
        core_reason="适合出现",
        cached_logs=[],
    ))

    assert [entry["status"] for entry in logs] == ["core_succeeded"]
    assert logs[0]["toy_commands"] == ["2"]
    assert logs[0]["toy_command_delivery"]["status"] == "gateway_rejected"
    assert logs[0]["toy_command_delivery"]["reason"] == "no_active_session"
    messages = _fetch_all(db_path, "SELECT role, content FROM messages ORDER BY created_at")
    assert messages[-1]["content"] == "靠近一点 ，我在。"
    assert "[TOY:" not in messages[-1]["content"]
    assert not any(payload["type"] == "toy_command" for payload in broadcasts)


def test_call_core_legacy_fallback_injects_timeline_without_query_memory(monkeypatch, tmp_path):
    db_path, logs, _broadcasts = _patch_core_env(monkeypatch, tmp_path)

    async def load_timeline(*, visible_message_ids, now=None):
        assert visible_message_ids == ["msg_user"]
        assert isinstance(now, float)
        return {
            "status": "injected",
            "block": "[最近三天的事]\n· 今天 21:00 用户说忙完了。",
            "entries": [{"text": "用户说忙完了。"}],
        }

    async def fake_stream_ai(messages, model_key, temperature=None):
        assert any(msg["content"].startswith("[最近三天的事]") for msg in messages)
        assert not any(
            marker in msg["content"]
            for marker in ("[相关记忆]", "可参考记忆")
            for msg in messages
        )
        yield "记得你刚忙完。"

    import ai_providers

    monkeypatch.setattr(sentinel_runtime, "load_sentinel_timeline_prompt_context", load_timeline)
    monkeypatch.setattr(ai_providers, "stream_ai", fake_stream_ai)
    runtime = sentinel_runtime.SentinelRuntime()

    asyncio.run(runtime._call_core(
        "用户可能需要被联系",
        last_user_ts=0,
        summary="信号偏少",
        core_reason="适合出现",
        cached_logs=[],
    ))

    assert [entry["status"] for entry in logs] == ["core_succeeded"]
    assert logs[0]["context_errors"] == []
    messages = _fetch_all(db_path, "SELECT role, content FROM messages ORDER BY created_at")
    assert messages[-1]["content"] == "记得你刚忙完。"


def test_call_core_continues_when_monitor_log_history_fails(monkeypatch, tmp_path):
    db_path, logs, _broadcasts = _patch_core_env(monkeypatch, tmp_path)

    def failing_logs(_since):
        raise RuntimeError("logs broken")

    async def fake_stream_ai(messages, model_key, temperature=None):
        assert "最新一条哨兵日志" in messages[-1]["content"]
        yield "我在。"

    import ai_providers

    monkeypatch.setattr(sentinel_runtime, "read_logs_since", failing_logs)
    monkeypatch.setattr(ai_providers, "stream_ai", fake_stream_ai)
    runtime = sentinel_runtime.SentinelRuntime()

    asyncio.run(runtime._call_core(
        "用户可能需要被联系",
        last_user_ts=0,
        summary="信号偏少",
        core_reason="适合出现",
        cached_logs=None,
    ))

    assert [entry["status"] for entry in logs] == ["core_succeeded"]
    assert "monitor_logs_failed: RuntimeError: logs broken" in logs[0]["context_errors"]
    messages = _fetch_all(db_path, "SELECT role, content FROM messages ORDER BY created_at")
    assert messages[-1]["content"] == "我在。"
