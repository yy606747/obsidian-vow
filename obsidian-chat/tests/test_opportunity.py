"""Tests for the opportunity mechanism."""

import asyncio
import sqlite3
import time
from types import SimpleNamespace

import aiosqlite
import pytest

import opportunity as opp
from app.chat.action_executor import ActionExecution
from app.chat.postprocess import PostProcessResult
from app.chat.prompt_builder import build_opportunity_ability_block
from app.chat.turn_profiles import opportunity_turn_profile
from app.tools.schemas import ToolIntent, ToolResult, ToolStatus
from opportunity import (
    OpportunityRunner,
    PreparedOpportunityTurn,
    _resolve_target_conv,
    _send_message,
    _CHAT_SILENCE_MIN_SEC,
    _COOLDOWN_SEC,
    _MAX_PER_HOUR,
)


@pytest.fixture(autouse=True)
def _empty_vow_context(monkeypatch):
    """誓约层（Phase 2）在本管道注入常驻读取；既有用例用空桩隔离，不读真实库。"""

    class _Stub:
        async def load_vow_prompt_context(self):
            return "", ""

    monkeypatch.setattr(opp, "vow_service", _Stub())



# ── Helpers ──────────────────────────────────────────

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


@pytest.fixture
def tmp_db(monkeypatch, tmp_path):
    """Provide a temporary SQLite DB with schema, patched into both database and opportunity."""
    db_path = tmp_path / "test.db"
    monkeypatch.setattr(aiosqlite, "connect", lambda path: _AsyncSqliteConn(path))

    async def _init():
        async with aiosqlite.connect(db_path) as db:
            await db.execute("""
                CREATE TABLE conversations (
                    id TEXT PRIMARY KEY, title TEXT NOT NULL,
                    model TEXT NOT NULL DEFAULT 'test-model',
                    created_at REAL NOT NULL, updated_at REAL NOT NULL
                )
            """)
            await db.execute("""
                CREATE TABLE messages (
                    id TEXT PRIMARY KEY, conv_id TEXT NOT NULL,
                    role TEXT NOT NULL, content TEXT NOT NULL,
                    created_at REAL NOT NULL, attachments TEXT DEFAULT ''
                )
            """)
            await db.execute("""
                CREATE TABLE heart_whispers (
                    id TEXT PRIMARY KEY, conv_id TEXT,
                    msg_id TEXT, content TEXT NOT NULL,
                    created_at REAL NOT NULL
                )
            """)
            await db.commit()

    asyncio.new_event_loop().run_until_complete(_init())

    import database
    monkeypatch.setattr(database, "DB_PATH", db_path)
    monkeypatch.setattr(opp, "get_db", database.get_db)

    return db_path


async def _insert_conv(db_path, conv_id="conv_1", model="test-model", ts=1000.0):
    async with aiosqlite.connect(db_path) as db:
        await db.execute(
            "INSERT INTO conversations (id, title, model, created_at, updated_at) VALUES (?,?,?,?,?)",
            (conv_id, "Test", model, ts, ts),
        )
        await db.commit()


async def _insert_msg(db_path, conv_id="conv_1", role="user", content="hello", ts=1000.0):
    msg_id = f"msg_{int(ts * 1000)}_{role}"
    async with aiosqlite.connect(db_path) as db:
        await db.execute(
            "INSERT INTO messages (id, conv_id, role, content, created_at, attachments) VALUES (?,?,?,?,?,?)",
            (msg_id, conv_id, role, content, ts, "[]"),
        )
        await db.commit()


def _stub_broadcast(monkeypatch):
    broadcasts = []

    async def fake_broadcast(msg):
        broadcasts.append(msg)

    monkeypatch.setattr(opp.manager, "broadcast", fake_broadcast)
    return broadcasts


def _stub_prepared_turn(
    monkeypatch,
    *,
    capabilities=(),
    reflection_context=None,
    web_search_allowed=False,
    kind="idle",
):
    prepared = PreparedOpportunityTurn(
        messages=[{"role": "user", "content": "idle trigger"}],
        profile=opportunity_turn_profile(
            runtime_capabilities=capabilities,
            reflection_allowed=reflection_context is not None,
            web_search_allowed=web_search_allowed,
            kind=kind,
        ),
        model_key="test-model",
        identity_snapshot={"text": "identity"},
        reflection_context=reflection_context,
        mobile_screen_target=None,
        kind=kind,
        user_name="Alice",
        ai_name="Arden",
    )

    async def prepare(**_kwargs):
        return prepared

    monkeypatch.setattr(opp, "_prepare_opportunity_turn", prepare)


# ── opportunity TurnProfile prompt ──────────────────

def test_prompt_renders_only_profile_capabilities_and_control_markers():
    profile = opportunity_turn_profile(
        runtime_capabilities={"heart.whisper", "memory.remember"},
        reflection_allowed=False,
    )
    block = str(
        build_opportunity_ability_block(
            profile=profile,
            user_name="Alice",
            ai_name="Arden",
            model_key="test-model",
        )
    )

    assert "[HEART:" in block
    assert "[REMEMBER:" in block
    assert "[OPPORTUNITY_NONE]" in block
    assert "[OPPORTUNITY_REFLECT]" not in block
    assert "[RING:" not in block
    assert "[SCREEN_CHECK:" not in block
    assert "不得输出 [WORKING_MODEL_REQUEST]" in block
    assert "本轮她没有发来新消息" in block
    assert "当前用户消息" not in block
    assert "最终回复仍应直接回应当前用户" not in block
    assert "额度可用也不代表应该立约" not in block
    assert "最近亲口说的情况，永远压过设备信号" in block
    assert "骗你、撒谎、编故事，或者被你抓到了" in block


def test_prompt_locks_mobile_target_and_exposes_reflection_only_when_allowed():
    profile = opportunity_turn_profile(
        runtime_capabilities={"device.ring_touch", "mobile.screen_check"},
        reflection_allowed=True,
    )
    block = str(
        build_opportunity_ability_block(
            profile=profile,
            user_name="Alice",
            ai_name="Arden",
            model_key="test-model",
            mobile_screen_target={"device_id": "android_tab", "label": "华为平板"},
        )
    )

    assert "[RING:" in block
    assert "[MOBILE_SCREEN_CHECK:android_tab|原因]" in block
    assert "华为平板" in block
    assert "目标设备已由系统锁定" in block
    assert "[OPPORTUNITY_REFLECT]" in block


def test_prompt_uses_frozen_profile_for_pc_screen_and_poi():
    profile = opportunity_turn_profile(
        runtime_capabilities={"pc.screen_check", "location.poi_search"},
        reflection_allowed=False,
    )

    block = str(
        build_opportunity_ability_block(
            profile=profile,
            user_name="Alice",
            ai_name="Arden",
            model_key="test-model",
        )
    )

    assert "[SCREEN_CHECK:原因]" in block
    assert "[POI_SEARCH:类型名]" in block


def _patch_prepare_context(monkeypatch):
    async def fake_history(*_args, **_kwargs):
        return SimpleNamespace(
            history=[],
            cap_idx=0,
            wb={
                "ai_name": "AI",
                "user_name": "用户",
                "ai_persona": "诚实",
            },
        )

    async def no_mobile_target(**_kwargs):
        return None

    async def no_tools(**_kwargs):
        return frozenset()

    async def no_self_wake_context(*_args, **_kwargs):
        return ""

    monkeypatch.setattr(opp, "prepare_chat_history", fake_history)
    monkeypatch.setattr(opp, "_autonomous_mobile_screen_target", no_mobile_target)
    monkeypatch.setattr(opp, "_runtime_capabilities", no_tools)
    monkeypatch.setattr(opp, "_runtime_context_text", lambda **_kwargs: "runtime")
    monkeypatch.setattr(opp, "load_self_wake_prompt_context", no_self_wake_context)
    monkeypatch.setattr(opp.web_search_service, "enabled", lambda: False)


def test_prepare_turn_skips_v2_heads_when_injection_and_reflection_are_off(monkeypatch):
    _patch_prepare_context(monkeypatch)
    monkeypatch.setattr(opp, "working_model_v2_injection_enabled", lambda: False)
    monkeypatch.setattr(
        opp,
        "load_ai_behavior",
        lambda: {"working_model_reflection_enabled": False},
    )

    async def unexpected_heads():
        raise AssertionError("disabled V2 injection loaded prompt heads")

    monkeypatch.setattr(
        opp.working_model_service,
        "load_v2_prompt_heads",
        unexpected_heads,
    )

    prepared = asyncio.run(
        opp._prepare_opportunity_turn(
            target={"conv_id": "conv", "model_key": "core", "last_user_ts": 1.0},
            now=1000.0,
        )
    )

    assert prepared.reflection_context is None
    assert not prepared.profile.allows_marker("opportunity_reflect")


def test_identity_context_is_shared_by_idle_and_summon_but_absent_from_night(
    monkeypatch,
):
    _patch_prepare_context(monkeypatch)

    async def history(*_args, **_kwargs):
        return SimpleNamespace(
            history=[],
            cap_idx=0,
            wb={"ai_name": "阿澈", "user_name": "小栀", "ai_persona": "诚实"},
        )

    async def identity_head():
        return {
            "baseline": {
                "created_at": 100.0,
                "prompt": "银白短发，深紫外套",
                "description": "阿澈选择了清晰利落的轮廓。",
            },
            "non_seed_count": 2,
            "timezone_name": "UTC",
        }

    monkeypatch.setattr(opp, "prepare_chat_history", history)
    monkeypatch.setattr(opp, "presence_identity_head", identity_head)
    monkeypatch.setattr(opp, "working_model_v2_injection_enabled", lambda: False)
    monkeypatch.setattr(
        opp,
        "load_ai_behavior",
        lambda: {"working_model_reflection_enabled": False},
    )

    rendered = {}
    for kind in ("idle", "summon", "night"):
        prepared = asyncio.run(opp._prepare_opportunity_turn(
            target={"conv_id": "conv", "model_key": "core", "last_user_ts": 1.0},
            now=1000.0,
            kind=kind,
        ))
        rendered[kind] = "\n".join(
            str(message.get("content") or "") for message in prepared.messages
        )

    for kind in ("idle", "summon"):
        assert "[关于阿澈曾选择的人形]" in rendered[kind]
        assert "向小栀谈论" in rendered[kind]
    assert "[关于阿澈曾选择的人形]" not in rendered["night"]
    assert "银白短发" not in rendered["night"]


def test_reflection_can_capture_head_without_injecting_it_into_opportunity(monkeypatch):
    _patch_prepare_context(monkeypatch)
    monkeypatch.setattr(opp, "working_model_v2_injection_enabled", lambda: False)
    monkeypatch.setattr(
        opp,
        "load_ai_behavior",
        lambda: {"working_model_reflection_enabled": True},
    )
    load_calls = []
    reflection_context = object()

    async def load_heads():
        load_calls.append(True)
        return (
            {"id": "wm-1", "content": "WM_HEAD_MUST_STAY_PRIVATE"},
            {"id": "desire-1", "content": "DESIRE_HEAD_MUST_STAY_PRIVATE"},
        )

    async def capture(**kwargs):
        assert kwargs["working_model_head"]["id"] == "wm-1"
        return reflection_context

    monkeypatch.setattr(opp.working_model_service, "load_v2_prompt_heads", load_heads)
    monkeypatch.setattr(opp, "capture_reflection_context", capture)

    prepared = asyncio.run(
        opp._prepare_opportunity_turn(
            target={"conv_id": "conv", "model_key": "core", "last_user_ts": 1.0},
            now=1000.0,
        )
    )

    provider_text = "\n".join(str(message.get("content") or "") for message in prepared.messages)
    assert load_calls == [True]
    assert prepared.reflection_context is reflection_context
    assert "WM_HEAD_MUST_STAY_PRIVATE" not in provider_text
    assert "DESIRE_HEAD_MUST_STAY_PRIVATE" not in provider_text


# ── _resolve_target_conv ─────────────────────────────

def test_resolve_target_conv_empty_db(tmp_db):
    result = asyncio.new_event_loop().run_until_complete(_resolve_target_conv())
    assert result is None


def test_resolve_target_conv_skips_empty_conversation(tmp_db):
    loop = asyncio.new_event_loop()
    loop.run_until_complete(_insert_conv(tmp_db, "conv_empty", ts=2000.0))
    loop.run_until_complete(_insert_conv(tmp_db, "conv_with_msg", ts=1000.0))
    loop.run_until_complete(_insert_msg(tmp_db, "conv_with_msg", "user", "hi", ts=1000.0))

    result = loop.run_until_complete(_resolve_target_conv())
    assert result is not None
    assert result["conv_id"] == "conv_with_msg"


def test_resolve_target_conv_picks_latest_with_user_msg(tmp_db):
    loop = asyncio.new_event_loop()
    loop.run_until_complete(_insert_conv(tmp_db, "old_conv", ts=1000.0))
    loop.run_until_complete(_insert_msg(tmp_db, "old_conv", "user", "old", ts=1000.0))
    loop.run_until_complete(_insert_conv(tmp_db, "new_conv", ts=2000.0))
    loop.run_until_complete(_insert_msg(tmp_db, "new_conv", "user", "new", ts=2000.0))

    result = loop.run_until_complete(_resolve_target_conv())
    assert result["conv_id"] == "new_conv"
    assert result["last_user_ts"] == 2000.0


# ── _send_message ────────────────────────────────────

def test_send_message_writes_to_correct_conv(tmp_db, monkeypatch):
    loop = asyncio.new_event_loop()
    broadcasts = _stub_broadcast(monkeypatch)

    def _unexpected_timeline_refresh(*args, **kwargs):
        raise AssertionError("opportunity turns must not schedule timeline refresh")

    monkeypatch.setattr(
        "app.memory_v3.timeline.timeline_service.start_background_refresh",
        _unexpected_timeline_refresh,
    )

    loop.run_until_complete(_insert_conv(tmp_db, "conv_1", ts=1000.0))
    loop.run_until_complete(_insert_msg(tmp_db, "conv_1", "user", "hi", ts=1000.0))

    ok = loop.run_until_complete(
        _send_message("test msg", conv_id="conv_1", user_name="Alice", ai_name="Bot")
    )
    assert ok is True

    async def _check():
        async with aiosqlite.connect(tmp_db) as db:
            cur = await db.execute(
                "SELECT conv_id, role, content FROM messages WHERE role='assistant' AND content='test msg'"
            )
            row = await cur.fetchone()
            assert row is not None
            assert row[0] == "conv_1"

    loop.run_until_complete(_check())
    assert any(b["type"] == "msg_created" for b in broadcasts)


# ── OpportunityRunner gates ──────────────────────────

def test_runner_default_disabled():
    runner = OpportunityRunner()
    assert runner.enabled is False
    result = asyncio.new_event_loop().run_until_complete(runner.maybe_fire())
    assert result is None


def test_runner_respects_config_enable(tmp_db, monkeypatch):
    monkeypatch.setattr(opp, "load_ai_behavior", lambda: {"opportunity_enabled": True})
    runner = OpportunityRunner()
    assert runner.enabled is False
    loop = asyncio.new_event_loop()
    loop.run_until_complete(runner.maybe_fire())
    assert runner.enabled is True


def test_runner_config_disable_gates(tmp_db, monkeypatch):
    monkeypatch.setattr(opp, "load_ai_behavior", lambda: {"opportunity_enabled": False})
    runner = OpportunityRunner()
    runner.enabled = True
    runner._next_fire_at = 0
    loop = asyncio.new_event_loop()
    gate, target = loop.run_until_complete(runner._check_gates(time.time()))
    assert gate == "disabled"


def test_runner_rate_limit_gate(tmp_db, monkeypatch):
    monkeypatch.setattr(opp, "load_ai_behavior", lambda: {"opportunity_enabled": True})
    runner = OpportunityRunner()
    now = time.time()
    runner._action_timestamps = [now - 100, now - 50]

    loop = asyncio.new_event_loop()
    gate, target = loop.run_until_complete(runner._check_gates(now))
    assert gate == "rate_limit"


def test_runner_attempt_budget_is_independent_of_success_count(tmp_db, monkeypatch):
    monkeypatch.setattr(opp, "load_ai_behavior", lambda: {"opportunity_enabled": True})
    runner = OpportunityRunner()
    now = time.time()
    runner._attempt_timestamps = [now - 100, now - 50]
    runner._action_timestamps = []

    gate, target = asyncio.new_event_loop().run_until_complete(
        runner._check_gates(now)
    )
    assert gate == "rate_limit"
    assert target is None


def test_configurable_intervals_are_validated(monkeypatch):
    monkeypatch.setattr(
        opp,
        "load_ai_behavior",
        lambda: {"opportunity_intervals_min": [13, "21", 0, "bad", 2000]},
    )
    assert opp._configured_intervals_min() == (13, 21, 2000)


def test_runner_cooldown_gate(tmp_db, monkeypatch):
    monkeypatch.setattr(opp, "load_ai_behavior", lambda: {"opportunity_enabled": True})
    runner = OpportunityRunner()
    now = time.time()
    runner._last_action_at = now - 60

    loop = asyncio.new_event_loop()
    gate, target = loop.run_until_complete(runner._check_gates(now))
    assert gate == "cooldown"


def test_runner_no_conversation_gate(tmp_db, monkeypatch):
    monkeypatch.setattr(opp, "load_ai_behavior", lambda: {"opportunity_enabled": True})
    runner = OpportunityRunner()

    loop = asyncio.new_event_loop()
    gate, target = loop.run_until_complete(runner._check_gates(time.time()))
    assert gate == "no_conversation"


def test_runner_recent_chat_gate(tmp_db, monkeypatch):
    monkeypatch.setattr(opp, "load_ai_behavior", lambda: {"opportunity_enabled": True})
    now = time.time()
    loop = asyncio.new_event_loop()
    loop.run_until_complete(_insert_conv(tmp_db, "conv_1", ts=now))
    loop.run_until_complete(_insert_msg(tmp_db, "conv_1", "user", "hi", ts=now - 60))

    runner = OpportunityRunner()
    gate, target = loop.run_until_complete(runner._check_gates(now))
    assert gate == "recent_chat"


def test_runner_all_gates_pass(tmp_db, monkeypatch):
    monkeypatch.setattr(opp, "load_ai_behavior", lambda: {"opportunity_enabled": True})
    monkeypatch.setattr(opp, "_load_cam_cfg", lambda: {"quiet_hours_enabled": False})
    now = time.time()
    old_ts = now - _CHAT_SILENCE_MIN_SEC - 60
    loop = asyncio.new_event_loop()
    loop.run_until_complete(_insert_conv(tmp_db, "conv_1", ts=old_ts))
    loop.run_until_complete(_insert_msg(tmp_db, "conv_1", "user", "hi", ts=old_ts))

    runner = OpportunityRunner()
    gate, target = loop.run_until_complete(runner._check_gates(now))
    assert gate is None
    assert target is not None
    assert target["conv_id"] == "conv_1"


# ── Failed actions should NOT count toward cooldown/rate ─

def test_failed_action_does_not_count(tmp_db, monkeypatch):
    monkeypatch.setattr(opp, "load_ai_behavior", lambda: {"opportunity_enabled": True})
    monkeypatch.setattr(opp, "_load_cam_cfg", lambda: {"quiet_hours_enabled": False})
    _stub_broadcast(monkeypatch)

    now = time.time()
    old_ts = now - _CHAT_SILENCE_MIN_SEC - 60

    loop = asyncio.new_event_loop()
    loop.run_until_complete(_insert_conv(tmp_db, "conv_1", ts=old_ts))
    loop.run_until_complete(_insert_msg(tmp_db, "conv_1", "user", "hi", ts=old_ts))
    _stub_prepared_turn(monkeypatch, capabilities={"device.ring_touch"})

    async def fake_llm(*a, **kw):
        return "hey"

    monkeypatch.setattr(opp, "call_opportunity_core", fake_llm)

    runner = OpportunityRunner()
    runner.enabled = True
    runner._next_fire_at = 0

    result = loop.run_until_complete(runner.maybe_fire())
    assert result is not None
    assert result["executed"] is True
    assert len(runner._action_timestamps) == 1

    runner._action_timestamps.clear()
    runner._last_action_at = 0
    runner._next_fire_at = 0

    async def fake_llm_ring(*a, **kw):
        return "[RING:hi]"

    monkeypatch.setattr(opp, "call_opportunity_core", fake_llm_ring)

    async def fail_ring(postprocessed, **_kwargs):
        assert postprocessed.ring_touch_descriptions == ["hi"]
        return ActionExecution(
            (
                ToolResult(
                    tool_name="device.ring_touch",
                    status=ToolStatus.EXECUTED,
                    result={"ok": False, "message": "gateway_unavailable"},
                ),
            )
        )

    monkeypatch.setattr(opp, "execute_postprocessed_actions", fail_ring)

    result = loop.run_until_complete(runner.maybe_fire())
    assert result is not None
    assert result["executed"] is False
    assert len(runner._action_timestamps) == 0
    assert len(runner._attempt_timestamps) == 2
    assert runner._last_action_at == 0


def test_tool_only_turn_reuses_outer_id_without_empty_assistant_row(tmp_db, monkeypatch):
    monkeypatch.setattr(opp, "load_ai_behavior", lambda: {"opportunity_enabled": True})
    monkeypatch.setattr(opp, "_load_cam_cfg", lambda: {"quiet_hours_enabled": False})
    _stub_broadcast(monkeypatch)
    _stub_prepared_turn(monkeypatch, capabilities={"heart.whisper"})

    now = time.time()
    old_ts = now - _CHAT_SILENCE_MIN_SEC - 60
    loop = asyncio.new_event_loop()
    loop.run_until_complete(_insert_conv(tmp_db, "conv_1", ts=old_ts))
    loop.run_until_complete(_insert_msg(tmp_db, "conv_1", "user", "hi", ts=old_ts))

    async def fake_llm(*_args, **_kwargs):
        return "[HEART:只是想起她了]"

    seen_ids = []

    async def execute_heart(postprocessed, *, context, **_kwargs):
        assert postprocessed.content == ""
        seen_ids.append(context.msg_id)
        return ActionExecution(
            (
                ToolResult(
                    tool_name="heart.whisper",
                    status=ToolStatus.EXECUTED,
                    result={"id": "hw-1", "msg_id": context.msg_id},
                ),
            )
        )

    monkeypatch.setattr(opp, "call_opportunity_core", fake_llm)
    monkeypatch.setattr(opp, "execute_postprocessed_actions", execute_heart)

    runner = OpportunityRunner()
    runner.enabled = True
    runner._next_fire_at = 0
    result = loop.run_until_complete(runner.maybe_fire())

    assert result["status"] == "acted"
    assert seen_ids and seen_ids[0].endswith("_opp")

    async def check_messages():
        async with aiosqlite.connect(tmp_db) as db:
            cursor = await db.execute(
                "SELECT role, content FROM messages ORDER BY created_at"
            )
            return [(row[0], row[1]) for row in await cursor.fetchall()]

    assert loop.run_until_complete(check_messages()) == [("user", "hi")]


def test_reflection_turn_is_not_exposed_on_general_websocket(monkeypatch):
    secret_clue = "PRIVATE_REFLECTION_CLUE"
    _stub_prepared_turn(monkeypatch, reflection_context=object())
    broadcasts = _stub_broadcast(monkeypatch)

    async def fake_llm(*_args, **_kwargs):
        return "[OPPORTUNITY_REFLECT]"

    async def fake_reflection(_captured):
        return {
            "entered": True,
            "status": "ok",
            "log": {
                "id": "refl-1",
                "outcome": "ok",
                "clue": secret_clue,
                "reason": "private reason",
            },
            "pipeline": {"writers": [{"working_model": "private"}]},
        }

    monkeypatch.setattr(opp, "call_opportunity_core", fake_llm)
    monkeypatch.setattr(opp, "run_reflection", fake_reflection)

    result = asyncio.run(
        OpportunityRunner()._run(
            time.time(),
            {"conv_id": "conv_1", "model_key": "test-model", "last_user_ts": 1.0},
        )
    )
    assert result["status"] == "acted"
    assert result["reflection"] == {
        "entered": True,
        "status": "ok",
        "log_id": "refl-1",
        "outcome": "ok",
    }
    event = next(item for item in broadcasts if item["type"] == "opportunity_log")
    assert "raw_response" not in event["data"]
    assert "reflection" not in event["data"]
    assert secret_clue not in str(event)


def test_reflection_failure_details_are_not_exposed_on_general_websocket(monkeypatch):
    broadcasts = _stub_broadcast(monkeypatch)

    asyncio.run(
        opp._broadcast_log(
            {
                "status": "action_failed",
                "raw_response": "[OPPORTUNITY_REFLECT]",
                "error": "reflection_runtime_failed:PRIVATE_DETAIL",
            }
        )
    )

    event = broadcasts[0]
    assert event == {
        "type": "opportunity_log",
        "data": {"status": "action_failed"},
    }


def test_web_search_marker_only_counts_as_acted_without_empty_message(monkeypatch):
    _stub_prepared_turn(monkeypatch, web_search_allowed=True)
    broadcasts = _stub_broadcast(monkeypatch)

    async def fake_core(*_args, **_kwargs):
        return "[WEB_SEARCH_INTENT]查最近的新应用[/WEB_SEARCH_INTENT]"

    class Processor:
        async def process(self, *_args, **_kwargs):
            return PostProcessResult(content="", web_search_intent="查最近的新应用")

    class Ledger:
        def new_invocation_id(self, _name):
            return "inv-1"

        async def record_turn(self, *_args, **_kwargs):
            return None

    class WebSearch:
        async def enqueue_opportunity(self, **kwargs):
            assert kwargs["origin_turn_id"].endswith("_opp")
            return {"status": "queued", "search_id": "web-1"}

    async def no_message(*_args, **_kwargs):
        raise AssertionError("marker-only opportunity persisted an empty message")

    monkeypatch.setattr(opp, "call_opportunity_core", fake_core)
    monkeypatch.setattr(opp, "_post_processor", Processor())
    monkeypatch.setattr(opp, "tool_invocation_ledger", Ledger())
    monkeypatch.setattr(opp, "web_search_service", WebSearch())
    monkeypatch.setattr(opp, "execute_postprocessed_actions", lambda *_args, **_kwargs: None)

    async def no_actions(*_args, **_kwargs):
        return ActionExecution(())

    monkeypatch.setattr(opp, "execute_postprocessed_actions", no_actions)
    monkeypatch.setattr(opp, "_send_message", no_message)

    result = asyncio.run(OpportunityRunner()._run(
        time.time(),
        {"conv_id": "conv", "model_key": "core", "last_user_ts": 1.0},
    ))
    assert result["status"] == "acted"
    assert result["web_search_queued"] is True
    event = next(item for item in broadcasts if item["type"] == "opportunity_log")
    assert "WEB_SEARCH" not in str(event)


def test_model_error_message_action_does_not_persist_or_count(tmp_db, monkeypatch):
    monkeypatch.setattr(opp, "load_ai_behavior", lambda: {"opportunity_enabled": True})
    monkeypatch.setattr(opp, "_load_cam_cfg", lambda: {"quiet_hours_enabled": False})
    broadcasts = _stub_broadcast(monkeypatch)

    now = time.time()
    old_ts = now - _CHAT_SILENCE_MIN_SEC - 60

    loop = asyncio.new_event_loop()
    loop.run_until_complete(_insert_conv(tmp_db, "conv_1", ts=old_ts))
    loop.run_until_complete(_insert_msg(tmp_db, "conv_1", "user", "hi", ts=old_ts))
    _stub_prepared_turn(monkeypatch)

    async def fake_llm(*_args, **_kwargs):
        return "[硅基流动错误: HTTP 429 rate limit]"

    monkeypatch.setattr(opp, "call_opportunity_core", fake_llm)

    runner = OpportunityRunner()
    runner.enabled = True
    runner._next_fire_at = 0

    result = loop.run_until_complete(runner.maybe_fire())
    assert result is not None
    assert result["executed"] is False
    assert result["status"] == "provider_failed"
    assert len(runner._action_timestamps) == 0
    assert len(runner._attempt_timestamps) == 1
    assert runner._last_action_at == 0

    async def _check():
        async with aiosqlite.connect(tmp_db) as db:
            cur = await db.execute("SELECT role, content FROM messages ORDER BY created_at")
            rows = await cur.fetchall()
            assert [(row[0], row[1]) for row in rows] == [("user", "hi")]

    loop.run_until_complete(_check())
    assert not any(b["type"] == "msg_created" for b in broadcasts)


@pytest.mark.parametrize(
    ("raw", "expected_status"),
    [
        ("[OPPORTUNITY_NONE]", "none_explicit"),
        ("正文 [OPPORTUNITY_NONE]", "invalid_control_output"),
        ("[OPPORTUNITY_BROKEN", "invalid_control_output"),
        ("[OPPORTUNITY_REFLECT]", "invalid_control_output"),
        ("", "empty"),
    ],
)
def test_control_and_empty_outputs_stop_before_postprocess(
    monkeypatch,
    raw,
    expected_status,
):
    _stub_prepared_turn(monkeypatch)
    _stub_broadcast(monkeypatch)

    async def fake_core(*_args, **kwargs):
        if raw == "":
            kwargs["usage_meta"]["provider_last"] = {"ok": True}
        return raw

    async def unexpected_postprocess(*_args, **_kwargs):
        raise AssertionError("reserved control output reached PostProcessor")

    monkeypatch.setattr(opp, "call_opportunity_core", fake_core)
    monkeypatch.setattr(opp._post_processor, "process", unexpected_postprocess)

    result = asyncio.run(
        OpportunityRunner()._run(
            1000.0,
            {"conv_id": "conv1", "model_key": "core", "last_user_ts": 1.0},
        )
    )

    assert result["status"] == expected_status
    assert result["executed"] is False


def test_provider_exception_stops_before_postprocess(monkeypatch):
    _stub_prepared_turn(monkeypatch)
    _stub_broadcast(monkeypatch)

    async def failed_core(*_args, **_kwargs):
        raise RuntimeError("provider down")

    async def unexpected_postprocess(*_args, **_kwargs):
        raise AssertionError("provider failure reached PostProcessor")

    monkeypatch.setattr(opp, "call_opportunity_core", failed_core)
    monkeypatch.setattr(opp._post_processor, "process", unexpected_postprocess)

    result = asyncio.run(
        OpportunityRunner()._run(
            1000.0,
            {"conv_id": "conv1", "model_key": "core", "last_user_ts": 1.0},
        )
    )

    assert result["status"] == "provider_failed"
    assert result["executed"] is False


def test_length_limited_output_retries_with_larger_budget(monkeypatch):
    _stub_prepared_turn(monkeypatch)
    _stub_broadcast(monkeypatch)
    calls = []
    sent = []

    async def fake_core(*_args, **kwargs):
        calls.append(kwargs["max_tokens"])
        if len(calls) == 1:
            kwargs["usage_meta"]["finish_reason"] = "length"
            return "这是一条被截断的半"
        kwargs["usage_meta"]["finish_reason"] = "stop"
        return "这一次内容完整。"

    async def fake_send(text, **_kwargs):
        sent.append(text)
        return True

    monkeypatch.setattr(opp, "call_opportunity_core", fake_core)
    monkeypatch.setattr(opp, "_send_message", fake_send)
    monkeypatch.setattr(opp, "load_worldbook", lambda: {"ai_name": "AI"})

    result = asyncio.run(
        OpportunityRunner()._run(
            1000.0,
            {"conv_id": "conv1", "model_key": "core", "last_user_ts": 1.0},
        )
    )

    assert calls == [4096, 8192]
    assert sent == ["这一次内容完整。"]
    assert result["status"] == "acted"


def test_length_limited_output_twice_is_never_persisted(monkeypatch):
    _stub_prepared_turn(monkeypatch)
    _stub_broadcast(monkeypatch)
    calls = []

    async def fake_core(*_args, **kwargs):
        calls.append(kwargs["max_tokens"])
        kwargs["usage_meta"]["finish_reason"] = "MAX_TOKENS"
        return "仍然只有半句"

    async def unexpected_postprocess(*_args, **_kwargs):
        raise AssertionError("truncated output reached PostProcessor")

    monkeypatch.setattr(opp, "call_opportunity_core", fake_core)
    monkeypatch.setattr(opp._post_processor, "process", unexpected_postprocess)

    result = asyncio.run(
        OpportunityRunner()._run(
            1000.0,
            {"conv_id": "conv1", "model_key": "core", "last_user_ts": 1.0},
        )
    )

    assert calls == [4096, 8192]
    assert result["status"] == "provider_failed"
    assert result["executed"] is False
    assert result["error"] == "output_truncated_after_retry"
    assert result["finish_reason"] == "MAX_TOKENS"


def test_working_model_request_is_stripped_and_never_handed_off(monkeypatch):
    _stub_prepared_turn(monkeypatch)
    _stub_broadcast(monkeypatch)
    sent = []

    async def fake_core(*_args, **_kwargs):
        return (
            "在呢。"
            "[WORKING_MODEL_REQUEST]"
            '{"statement":"她喜欢安静","source":"她刚才没说话"}'
            "[/WORKING_MODEL_REQUEST]"
        )

    async def execute_without_private_handoff(postprocessed, **_kwargs):
        # The parser may recognize the private candidate, but opportunity's
        # outer lifecycle must discard it and invoke only tool execution.
        assert postprocessed.working_model_request is not None
        return ActionExecution(())

    async def fake_send(text, **_kwargs):
        sent.append(text)
        return True

    monkeypatch.setattr(opp, "call_opportunity_core", fake_core)
    monkeypatch.setattr(
        opp,
        "execute_postprocessed_actions",
        execute_without_private_handoff,
    )
    monkeypatch.setattr(opp, "_send_message", fake_send)
    monkeypatch.setattr(opp, "load_worldbook", lambda: {"ai_name": "AI"})

    result = asyncio.run(
        OpportunityRunner()._run(
            1000.0,
            {"conv_id": "conv1", "model_key": "core", "last_user_ts": 1.0},
        )
    )

    assert result["status"] == "acted"
    assert sent == ["在呢。"]


def test_normal_night_profile_exposes_only_draw_reflect_and_none():
    profile = opportunity_turn_profile(
        runtime_capabilities={
            "desktop.presence.draw",
            "desktop.presence.show",
            "heart.whisper",
        },
        reflection_allowed=True,
        web_search_allowed=True,
        kind="night",
    )
    block = str(build_opportunity_ability_block(
        profile=profile,
        user_name="Alice",
        ai_name="Arden",
        model_key="core",
        kind="night",
        presence_requires_human=False,
    ))
    assert profile.allowed_tool_capabilities == frozenset({"desktop.presence.draw"})
    assert "[PRESENCE_DRAW:" in block
    assert "[OPPORTUNITY_REFLECT]" in block
    assert "[OPPORTUNITY_NONE]" in block
    assert "[PRESENCE_SHOW:" not in block
    assert "[HEART:" not in block
    assert "Arden今晚不需要向Alice说话" in block


def test_empty_library_night_profile_exposes_only_first_human_draw():
    profile = opportunity_turn_profile(
        runtime_capabilities={
            "desktop.presence.draw",
            "desktop.presence.show",
            "heart.whisper",
        },
        reflection_allowed=True,
        web_search_allowed=True,
        kind="night",
        presence_bootstrap_required=True,
    )
    block = str(build_opportunity_ability_block(
        profile=profile,
        user_name="Alice",
        ai_name="Arden",
        model_key="core",
        kind="night",
        presence_requires_human=True,
    ))
    assert profile.allowed_tool_capabilities == frozenset({"desktop.presence.draw"})
    assert profile.allowed_markers == frozenset()
    assert "[PRESENCE_DRAW:" in block
    assert "OPPORTUNITY_REFLECT" not in block
    assert "OPPORTUNITY_NONE" not in block
    assert "脸、身形、头发、衣着、配色和视角" in block
    assert "唯一合法出口是一个人形 PRESENCE_DRAW" in block


def test_prepare_empty_library_night_freezes_bootstrap_profile(monkeypatch):
    _patch_prepare_context(monkeypatch)

    async def draw_capability(**_kwargs):
        return frozenset({"desktop.presence.draw"})

    async def no_non_seed_sprites():
        return False

    async def load_heads():
        return {}, {}

    async def capture_reflection(**_kwargs):
        return object()

    monkeypatch.setattr(opp, "_runtime_capabilities", draw_capability)
    monkeypatch.setattr(opp, "working_model_v2_injection_enabled", lambda: False)
    monkeypatch.setattr(
        opp,
        "load_ai_behavior",
        lambda: {"working_model_reflection_enabled": True},
    )
    monkeypatch.setattr(opp.working_model_service, "load_v2_prompt_heads", load_heads)
    monkeypatch.setattr(opp, "capture_reflection_context", capture_reflection)

    from app.presence import sprite_library

    monkeypatch.setattr(
        sprite_library,
        "has_non_seed_sprites",
        no_non_seed_sprites,
    )

    prepared = asyncio.run(opp._prepare_opportunity_turn(
        target={"conv_id": "conv", "model_key": "core", "last_user_ts": 1.0},
        now=1000.0,
        kind="night",
    ))
    rendered = "\n".join(
        str(message.get("content") or "") for message in prepared.messages
    )

    assert prepared.profile.allowed_tool_capabilities == frozenset(
        {"desktop.presence.draw"}
    )
    assert prepared.profile.allowed_markers == frozenset()
    assert prepared.reflection_context is None
    assert "第一次为自己留下正式形象" in rendered
    assert "OPPORTUNITY_REFLECT" not in rendered
    assert "OPPORTUNITY_NONE" not in rendered


def test_night_plain_text_is_dropped_without_message_or_broadcast(monkeypatch):
    _stub_prepared_turn(
        monkeypatch,
        capabilities={"desktop.presence.draw"},
        reflection_context=object(),
        kind="night",
    )
    broadcasts = _stub_broadcast(monkeypatch)
    turns = []

    async def fake_core(*_args, **_kwargs):
        return "凌晨偷偷说一句话"

    class Processor:
        async def process(self, *_args, **_kwargs):
            return PostProcessResult(content="凌晨偷偷说一句话")

    class Ledger:
        def new_invocation_id(self, _name):
            return "night-invocation"

        async def record_turn(self, *_args, **kwargs):
            turns.append(kwargs)

    async def forbidden_send(*_args, **_kwargs):
        raise AssertionError("night attempted to persist a message")

    async def forbidden_execute(*_args, **_kwargs):
        raise AssertionError("night plain text reached action execution")

    monkeypatch.setattr(opp, "call_opportunity_core", fake_core)
    monkeypatch.setattr(opp, "_post_processor", Processor())
    monkeypatch.setattr(opp, "tool_invocation_ledger", Ledger())
    monkeypatch.setattr(opp, "_send_message", forbidden_send)
    monkeypatch.setattr(opp, "execute_postprocessed_actions", forbidden_execute)

    result = asyncio.run(opp.run_autonomous_turn(
        kind="night",
        now=1000.0,
        target={"conv_id": "conv", "model_key": "core", "last_user_ts": 1.0},
    ))
    assert result["status"] == "invalid_control_output"
    assert result["round_branch"] == "invalid"
    assert broadcasts == []
    assert turns[0]["metadata"]["round_kind"] == "night"


def test_night_draw_executes_without_general_broadcast(monkeypatch):
    _stub_prepared_turn(
        monkeypatch,
        capabilities={"desktop.presence.draw"},
        kind="night",
    )
    broadcasts = _stub_broadcast(monkeypatch)

    async def fake_core(*_args, **_kwargs):
        return "[PRESENCE_DRAW:人形|黑发紫衣正面全身|这是Arden想留下的第一个人的样子]"

    draw_intent = ToolIntent(
        id="draw-1",
        tool_name="desktop.presence.draw",
        raw_text="[PRESENCE_DRAW:...]",
        arguments={
            "form": "human",
            "prompt": "黑发紫衣正面全身",
            "description": "这是Arden想留下的第一个人的样子",
        },
    )

    class Processor:
        async def process(self, *_args, **_kwargs):
            return PostProcessResult(content="", tool_intents=[draw_intent])

    class Ledger:
        def new_invocation_id(self, _name):
            return "night-draw"

        async def record_turn(self, *_args, **_kwargs):
            return None

    async def execute(*_args, **_kwargs):
        return ActionExecution((ToolResult(
            tool_name="desktop.presence.draw",
            status=ToolStatus.EXECUTED,
            result={"ok": True},
        ),))

    monkeypatch.setattr(opp, "call_opportunity_core", fake_core)
    monkeypatch.setattr(opp, "_post_processor", Processor())
    monkeypatch.setattr(opp, "tool_invocation_ledger", Ledger())
    monkeypatch.setattr(opp, "execute_postprocessed_actions", execute)

    result = asyncio.run(opp.run_autonomous_turn(
        kind="night",
        now=1000.0,
        target={"conv_id": "conv", "model_key": "core", "last_user_ts": 1.0},
    ))
    assert result["status"] == "acted"
    assert result["round_branch"] == "draw"
    assert broadcasts == []
