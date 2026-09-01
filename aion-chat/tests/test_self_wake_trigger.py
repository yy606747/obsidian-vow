import asyncio
from contextlib import asynccontextmanager
import json
from types import SimpleNamespace

import aiosqlite

from app.chat.action_executor import ActionExecution
from app.chat.turn_profiles import (
    SELF_WAKE_NONE_TOKEN,
    classify_self_wake_control_output,
    self_wake_turn_profile,
)
from app.self_wake import trigger as trigger_module
from app.self_wake.trigger import PreparedSelfWakeTurn
from app.tools.schemas import ToolIntent, ToolResult, ToolStatus


def _run(awaitable):
    return asyncio.run(awaitable)


def _wake(*, capabilities=("memory.remember",)):
    return {
        "id": "wake_1",
        "wake_at": 1_800_000_000.0,
        "intent": "回来看看",
        "requested_capabilities_json": json.dumps(list(capabilities)),
        "origin": "relationship",
        "origin_ref": "conv",
        "source": "chat",
        "conv_id": "conv",
        "source_turn_id": "source-turn",
        "owner_timezone": "UTC",
        "state": "consumed",
    }


def _prepared(*, capabilities=("memory.remember",)):
    effective = frozenset(capabilities)
    return PreparedSelfWakeTurn(
        messages=[{"role": "user", "content": "trigger"}],
        profile=self_wake_turn_profile(effective),
        model_key="test-model",
        advertised_tools=tuple(sorted(effective)),
        requested_capabilities=effective,
        effective_capabilities=effective,
        unavailable_capabilities=frozenset(),
        mobile_screen_target=None,
        identity_snapshot={"text": "identity"},
    )


class _Repo:
    def __init__(self):
        self.finishes = []

    async def finish_trigger(self, wake_id, **kwargs):
        self.finishes.append((wake_id, kwargs))
        return True


class _Ledger:
    def __init__(self):
        self.requests = []
        self.outputs = []
        self.visible = []
        self.turns = []

    @staticmethod
    def new_invocation_id(_prefix):
        return "invocation"

    async def record_model_request(self, context, **kwargs):
        self.requests.append((context, kwargs))
        return 1

    async def record_model_output(self, context, **kwargs):
        self.outputs.append((context, kwargs))
        return 1

    async def record_visible_message(self, context, **kwargs):
        self.visible.append((context, kwargs))
        return 1

    async def record_turn(self, context, **kwargs):
        self.turns.append((context, kwargs))
        return 1


class _Processor:
    def __init__(self, *, content="", intents=()):
        self.content = content
        self.intents = list(intents)
        self.calls = 0

    async def process(self, *_args, **_kwargs):
        self.calls += 1
        return SimpleNamespace(content=self.content, tool_intents=self.intents)


def _install(
    monkeypatch,
    *,
    raw,
    content="",
    intents=(),
    execution=ActionExecution(()),
    persist=True,
):
    repo = _Repo()
    ledger = _Ledger()
    processor = _Processor(content=content, intents=intents)
    provider_calls = []
    persisted = []

    async def prepare(_wake, **_kwargs):
        return _prepared()

    async def provider(*args, **kwargs):
        provider_calls.append((args, kwargs))
        if isinstance(raw, BaseException):
            raise raw
        return raw

    async def execute(*_args, **_kwargs):
        return execution

    async def persist_message(**kwargs):
        persisted.append(kwargs)
        return persist

    async def noop(*_args, **_kwargs):
        return None

    monkeypatch.setattr(trigger_module, "self_wake_repository", repo)
    monkeypatch.setattr(trigger_module, "tool_invocation_ledger", ledger)
    monkeypatch.setattr(trigger_module, "_post_processor", processor)
    monkeypatch.setattr(trigger_module, "prepare_self_wake_turn", prepare)
    monkeypatch.setattr(trigger_module, "call_self_wake_core", provider)
    monkeypatch.setattr(trigger_module, "execute_postprocessed_actions", execute)
    monkeypatch.setattr(trigger_module, "_persist_visible_message", persist_message)
    monkeypatch.setattr(trigger_module, "_broadcast_action_results", noop)
    return repo, ledger, processor, provider_calls, persisted


def test_none_is_success_without_message_or_tool(monkeypatch):
    repo, ledger, processor, calls, persisted = _install(
        monkeypatch,
        raw=SELF_WAKE_NONE_TOKEN,
    )
    result = _run(trigger_module.fire_claimed_wake(_wake()))
    assert result["status"] == "none_explicit"
    assert processor.calls == 0
    assert persisted == []
    assert len(calls) == 1
    assert calls[0][1]["timeout"] == 120.0
    assert calls[0][1]["max_tokens"] == 4096
    assert repo.finishes[-1][1]["outcome"] == "none_explicit"
    context, request = ledger.requests[0]
    assert context.request_id == "self_wake:wake_1"
    assert context.metadata["source_chain"] == "self_wake"
    assert request["metadata"]["intent"] == "回来看看"


def test_tool_only_success_does_not_create_empty_message(monkeypatch):
    intent = ToolIntent(
        id="remember",
        tool_name="memory.remember",
        raw_text="[REMEMBER:x]",
        arguments={"content": "x"},
        side_effect_level="write",
    )
    execution = ActionExecution(
        (
            ToolResult.from_intent(
                intent,
                status=ToolStatus.EXECUTED,
                result={"stored": True},
            ),
        )
    )
    repo, _ledger, _processor, _calls, persisted = _install(
        monkeypatch,
        raw="[REMEMBER:x]",
        intents=(intent,),
        execution=execution,
    )
    result = _run(trigger_module.fire_claimed_wake(_wake()))
    assert result["status"] == "tool_only"
    assert persisted == []
    assert repo.finishes[-1][1]["outcome"] == "tool_only"


def test_visible_text_persists_one_assistant_and_records_visible(monkeypatch):
    repo, ledger, _processor, _calls, persisted = _install(
        monkeypatch,
        raw="在吗",
        content="在吗",
    )
    result = _run(trigger_module.fire_claimed_wake(_wake()))
    assert result["status"] == "succeeded"
    assert result["visible"] is True
    assert len(persisted) == 1
    assert persisted[0]["content"] == "在吗"
    assert len(ledger.visible) == 1
    assert repo.finishes[-1][1]["outcome"] == "succeeded"


def test_mixed_none_marker_is_invalid_before_postprocess(monkeypatch):
    repo, _ledger, processor, calls, persisted = _install(
        monkeypatch,
        raw=f"正文 {SELF_WAKE_NONE_TOKEN}",
    )
    result = _run(trigger_module.fire_claimed_wake(_wake()))
    assert result["status"] == "invalid_control_output"
    assert len(calls) == 1
    assert processor.calls == 0
    assert persisted == []
    assert repo.finishes[-1][1]["outcome"] == "invalid_control_output"
    profile = self_wake_turn_profile(())
    assert classify_self_wake_control_output(
        f"{SELF_WAKE_NONE_TOKEN} x", profile=profile
    ) == "invalid"


def test_provider_failure_is_consumed_without_retry(monkeypatch):
    repo, ledger, processor, calls, persisted = _install(
        monkeypatch,
        raw=RuntimeError("provider down"),
    )
    result = _run(trigger_module.fire_claimed_wake(_wake()))
    assert result["status"] == "provider_failed"
    assert len(calls) == 1
    assert processor.calls == 0
    assert persisted == []
    assert repo.finishes[-1][1]["outcome"] == "provider_failed"
    assert ledger.outputs[-1][1]["outcome"] == "failed"


def test_message_write_failure_stays_consumed(monkeypatch):
    repo, _ledger, _processor, _calls, persisted = _install(
        monkeypatch,
        raw="正文",
        content="正文",
        persist=False,
    )
    result = _run(trigger_module.fire_claimed_wake(_wake()))
    assert len(persisted) == 1
    assert result["status"] == "message_persist_failed"
    assert repo.finishes[-1][1]["outcome"] == "message_persist_failed"


def test_all_tools_rejected_is_not_a_success(monkeypatch):
    intent = ToolIntent(
        id="remember",
        tool_name="memory.remember",
        raw_text="[REMEMBER:x]",
        arguments={"content": "x"},
        side_effect_level="write",
    )
    execution = ActionExecution(
        (
            ToolResult.from_intent(
                intent,
                status=ToolStatus.EXECUTED,
                result={"ok": False, "status": "rejected", "reason": "disabled"},
            ),
        )
    )
    repo, _ledger, _processor, _calls, persisted = _install(
        monkeypatch,
        raw="[REMEMBER:x]",
        intents=(intent,),
        execution=execution,
    )
    result = _run(trigger_module.fire_claimed_wake(_wake()))
    assert result["status"] == "all_tools_rejected"
    assert persisted == []
    assert repo.finishes[-1][1]["outcome"] == "all_tools_rejected"


def test_prepare_intersects_requested_runtime_and_registered(monkeypatch):
    history_ctx = SimpleNamespace(
        history=[{"role": "user", "content": "history", "attachments": []}],
        cap_idx=0,
        wb={"user_name": "小栀", "ai_name": "阿澈"},
        model_key="test-model",
    )

    async def load_target(_conv_id):
        return {"model_key": "test-model", "last_user_ts": 1_799_999_000.0}

    async def history(*_args, **_kwargs):
        return history_ctx

    async def no_mobile(**_kwargs):
        return None

    async def runtime(**_kwargs):
        return frozenset({"memory.remember"})

    async def vow_context():
        return "", ""

    async def presence_identity():
        return {
            "baseline": {
                "created_at": 100.0,
                "prompt": "银白短发，深紫外套",
                "description": "阿澈选择了清晰利落的轮廓。",
            },
            "non_seed_count": 1,
            "timezone_name": "UTC",
        }

    monkeypatch.setattr(trigger_module, "_load_target", load_target)
    monkeypatch.setattr(trigger_module, "prepare_chat_history", history)
    monkeypatch.setattr(trigger_module, "_autonomous_mobile_screen_target", no_mobile)
    monkeypatch.setattr(trigger_module, "resolve_autonomous_capabilities", runtime)
    monkeypatch.setattr(
        trigger_module.vow_service,
        "load_vow_prompt_context",
        vow_context,
    )
    monkeypatch.setattr(
        trigger_module,
        "build_writer_identity_snapshot",
        lambda *_args, **_kwargs: {"text": "identity"},
    )
    monkeypatch.setattr(
        trigger_module,
        "presence_identity_head",
        presence_identity,
    )
    monkeypatch.setattr(
        trigger_module,
        "working_model_v2_injection_enabled",
        lambda: False,
    )
    monkeypatch.setattr(
        trigger_module,
        "load_ai_behavior",
        lambda: {"heart_whisper_prompt": "[HEART:{user_name}]"},
    )
    prepared = _run(
        trigger_module.prepare_self_wake_turn(
            _wake(capabilities=("memory.remember", "desktop.presence.show")),
            now=1_800_000_100.0,
        )
    )
    assert prepared.effective_capabilities == frozenset({"memory.remember"})
    assert prepared.unavailable_capabilities == frozenset(
        {"desktop.presence.show"}
    )
    assert prepared.advertised_tools == ("memory.remember",)
    assert not prepared.profile.allows_tool("self_wake.schedule")
    final_prompt = prepared.messages[-1]["content"]
    assert "实际挂载能力：memory.remember" in final_prompt
    assert "当前不可用能力：desktop.presence.show" in final_prompt
    assert "当时想做的事只是你自己安排的念头，不是她现在的状态" in final_prompt
    provider_text = "\n".join(message["content"] for message in prepared.messages)
    assert "[关于阿澈曾选择的人形]" in provider_text
    assert "向小栀谈论" in provider_text
    assert "最近亲口说的情况，永远压过设备信号" in provider_text
    assert "骗你、撒谎、编故事，或者被你抓到了" in provider_text


def test_visible_persistence_writes_no_system_message(tmp_path, monkeypatch):
    path = tmp_path / "trigger.db"

    @asynccontextmanager
    async def get_db():
        async with aiosqlite.connect(path) as db:
            yield db

    async def initialize():
        async with get_db() as db:
            await db.execute(
                "CREATE TABLE conversations "
                "(id TEXT PRIMARY KEY, updated_at REAL, model TEXT)"
            )
            await db.execute(
                "CREATE TABLE messages "
                "(id TEXT PRIMARY KEY, conv_id TEXT, role TEXT, content TEXT, "
                "created_at REAL, attachments TEXT)"
            )
            await db.execute(
                "INSERT INTO conversations(id,updated_at,model) VALUES ('conv',0,'m')"
            )
            await db.commit()

    class Manager:
        def __init__(self):
            self.events = []

        async def broadcast(self, event):
            self.events.append(event)

    async def export(_conv_id):
        return None

    async def run():
        await initialize()
        assert await trigger_module._persist_visible_message(
            conv_id="conv",
            msg_id="assistant",
            content="正文",
            created_at=10.0,
        )
        async with get_db() as db:
            cursor = await db.execute(
                "SELECT role, content FROM messages ORDER BY created_at"
            )
            return await cursor.fetchall()

    manager = Manager()
    monkeypatch.setattr(trigger_module, "get_db", get_db)
    monkeypatch.setattr(trigger_module, "manager", manager)
    monkeypatch.setattr(trigger_module, "export_conversation", export)
    rows = _run(run())
    assert rows == [("assistant", "正文")]
    assert [event["data"]["role"] for event in manager.events] == ["assistant"]
