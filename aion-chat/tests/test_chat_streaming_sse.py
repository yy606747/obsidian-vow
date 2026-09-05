import asyncio
import json
from types import SimpleNamespace

import pytest

from app.chat import basic_actions, side_effects, streaming
from app.chat.postprocess import PostProcessResult
from app.tools.schemas import ToolContext, ToolIntent, ToolResult, ToolStatus


def _prompt_meta():
    return {
        "recall_keywords": "",
        "recall_query": "",
        "recall_topic": "",
        "is_search_needed": False,
        "recalled_memories": [],
        "debug_top6": [],
        "memory_v2_recall": None,
        "prompt_messages": [{"role": "user", "content": "hi"}],
        "prompt_count": 1,
    }


class _FakeDB:
    def __init__(self, executed):
        self.executed = executed

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def execute(self, sql, params=()):
        self.executed.append((sql, params))
        return SimpleNamespace(fetchone=lambda: None, fetchall=lambda: [])

    async def commit(self):
        return None


async def _collect_sse(response):
    events = []
    async for raw in response.body_iterator:
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8")
        assert raw.startswith("data: ")
        events.append(json.loads(raw[len("data: "):].strip()))
    # 检查中的 asyncio.run 会立即关闭循环，需像应用退出一样先收尾。
    # 否则回复之后才启动的数据库工作线程会向已经关闭的循环回报。
    from app.background_tasks import _BACKGROUND_TASKS, begin_task_lifecycle, shutdown_tracked_tasks
    pending = {task for task in _BACKGROUND_TASKS
               if not task.done() and task.get_loop() is asyncio.get_running_loop()}
    if pending:
        await asyncio.wait(pending, timeout=0.1)
    await shutdown_tracked_tasks(timeout=1.0)
    begin_task_lifecycle()
    return events


def _patch_streaming_io(monkeypatch):
    executed = []
    broadcasts = []

    def fake_get_db():
        return _FakeDB(executed)

    async def fake_broadcast(payload):
        broadcasts.append(payload)

    async def noop_async(*_args, **_kwargs):
        return None

    monkeypatch.setattr(streaming, "get_db", fake_get_db)
    monkeypatch.setattr(streaming, "manager", SimpleNamespace(broadcast=fake_broadcast))
    monkeypatch.setattr(streaming, "export_conversation", noop_async)
    monkeypatch.setattr(streaming, "_maybe_auto_digest", noop_async)
    monkeypatch.setattr(streaming, "_record_v2_memory_usage_for_chat", noop_async)
    monkeypatch.setattr(streaming, "_toy_sys_msg", noop_async)
    return executed, broadcasts


class _FakeControlGateway:
    def __init__(self, result):
        self.result = result
        self.calls = []

    async def execute_toy_intent(self, intent, context):
        self.calls.append({"intent": intent, "context": context})
        return {**self.result, "legacy_command": intent.arguments["command"]}


def test_stream_chat_response_normal_mode_skips_toy_event(monkeypatch):
    executed, broadcasts = _patch_streaming_io(monkeypatch)

    async def fake_stream_ai(_history, _model_key, usage_meta, _temperature):
        usage_meta["provider"] = "mock"
        yield "收到"
        yield " [TOY:1]"

    monkeypatch.setattr(streaming, "stream_ai", fake_stream_ai)

    async def run():
        response = await streaming.stream_chat_response(
            conv_id="conv_sse",
            model_key="mock-model",
            history=[{"role": "user", "content": "hi"}],
            prompt_meta=_prompt_meta(),
            temperature=0.1,
        )
        return await _collect_sse(response)

    import asyncio
    events = asyncio.run(run())

    assert [event["type"] for event in events] == ["start", "chunk", "debug"]
    assert events[1]["content"] == "收到"
    assert "TOY" not in "".join(event.get("content", "") for event in events)
    assistant_inserts = [
        params for sql, params in executed
        if sql.startswith("INSERT INTO messages") and params[2] == "assistant"
    ]
    assert assistant_inserts[0][3] == "收到"
    assert not any(payload["type"] == "toy_command" for payload in broadcasts)
    assert events[-1]["chat_mode"] == "normal"
    assert events[-1]["mode_source"] == "prompt_meta"
    assert "device.toy" not in events[-1]["capabilities"]
    assert events[-1]["usage"] == {"provider": "mock"}


def test_stream_chat_response_hides_and_records_tide_intent(monkeypatch):
    executed, broadcasts = _patch_streaming_io(monkeypatch)
    recorded = []

    async def fake_stream_ai(_history, _model_key, usage_meta, _temperature):
        usage_meta["provider"] = "mock"
        yield "看着你。[TIDE"
        yield "_INTENT:藏起来"
        yield "[/TIDE_INTENT]继续"

    async def fake_record_intent(**kwargs):
        recorded.append(kwargs)
        return True

    import app.tide.intent as tide_intent_module

    monkeypatch.setattr(streaming, "stream_ai", fake_stream_ai)
    monkeypatch.setattr(tide_intent_module.tide_intent_service, "record_intent", fake_record_intent)

    async def run():
        prompt_meta = _prompt_meta()
        prompt_meta["chat_mode"] = "control_session"
        prompt_meta["control_kind"] = "tide"
        response = await streaming.stream_chat_response(
            conv_id="conv_tide_sse",
            model_key="mock-model",
            history=[{"role": "user", "content": "hi"}],
            prompt_meta=prompt_meta,
            temperature=0.1,
        )
        return await _collect_sse(response)

    import asyncio
    events = asyncio.run(run())

    assert [event["type"] for event in events] == ["start", "chunk", "chunk", "debug"]
    assert events[1]["content"] == "看着你。"
    assert events[2]["content"] == "继续"
    assert "藏起来" not in "".join(event.get("content", "") for event in events)
    assistant_inserts = [
        params for sql, params in executed
        if sql.startswith("INSERT INTO messages") and params[2] == "assistant"
    ]
    assert assistant_inserts[0][3] == "看着你。继续"
    assert len(recorded) == 1
    assert recorded[0]["conv_id"] == "conv_tide_sse"
    assert recorded[0]["msg_id"] == events[0]["id"]
    assert recorded[0]["intent_text"] == "藏起来"
    assert recorded[0]["invocation_id"].startswith("main_core_")
    assert not any(payload["type"] == "toy_command" for payload in broadcasts)


def test_stream_chat_response_hides_recall_intent_and_commits_it_with_reply(
    monkeypatch,
):
    executed, _broadcasts = _patch_streaming_io(monkeypatch)
    pending_calls = []
    started = []

    async def fake_stream_ai(_history, _model_key, _usage_meta, _temperature):
        yield "先说正文。[RECALL_"
        yield "INTENT]找南京误车那次[/RECALL_INTENT]"
        yield "继续。"

    async def fake_apply(_db, **kwargs):
        pending_calls.append(kwargs)
        return {"consumed": False, "created_pending_id": "pending-new"}

    monkeypatch.setattr(streaming, "stream_ai", fake_stream_ai)
    monkeypatch.setattr(
        streaming.pending_recall_service,
        "apply_after_assistant_in_tx",
        fake_apply,
    )
    monkeypatch.setattr(
        streaming.pending_recall_service,
        "start_background",
        lambda pending_id: started.append(pending_id),
    )

    async def run():
        prompt_meta = _prompt_meta()
        prompt_meta.update({
            "prompt_source": "send",
            "current_user_message_id": "user-current",
            "pending_recall": {"status": "none", "items": []},
            "memory_v3_config_snapshot": {"pending_recall_enabled": True},
        })
        response = await streaming.stream_chat_response(
            conv_id="conv-recall-sse",
            model_key="mock-model",
            history=[{"role": "user", "content": "hi"}],
            prompt_meta=prompt_meta,
            temperature=0.1,
        )
        return await _collect_sse(response)

    import asyncio
    events = asyncio.run(run())

    visible = "".join(event.get("content", "") for event in events)
    assert visible == "先说正文。继续。"
    assert "RECALL_INTENT" not in visible
    assistant_inserts = [
        params for sql, params in executed
        if sql.startswith("INSERT INTO messages") and params[2] == "assistant"
    ]
    assert assistant_inserts[0][3] == "先说正文。继续。"
    assert pending_calls[0]["recall_intent"] == "找南京误车那次"
    assert pending_calls[0]["current_user_message_id"] == "user-current"
    assert pending_calls[0]["allow_new_intent"] is True
    assert started == ["pending-new"]


def test_stream_chat_response_toy_uses_tool_service_adapter(monkeypatch):
    executed, broadcasts = _patch_streaming_io(monkeypatch)
    captured = {}
    toy_sys_calls = []

    async def fake_stream_ai(_history, _model_key, _usage_meta, _temperature):
        yield "收到 [TOY:SCENE:warmup]"

    async def fake_toy_sys_msg(conv_id, commands):
        toy_sys_calls.append((conv_id, list(commands)))

    class FakeToolService:
        async def execute_async(self, intents, *, context, adapters=None):
            intents = list(intents)
            if not intents:
                return []
            captured["intents"] = intents
            captured["context"] = context
            captured["adapters"] = adapters
            return [
                ToolResult(
                    tool_name="device.toy",
                    intent_id=intents[0].id,
                    status=ToolStatus.EXECUTED,
                    result={"type": "toy_command", "command": intents[0].arguments["command"]},
                )
            ]

    monkeypatch.setattr(streaming, "stream_ai", fake_stream_ai)
    monkeypatch.setattr(streaming, "_toy_sys_msg", fake_toy_sys_msg)
    monkeypatch.setattr(streaming, "tool_service", FakeToolService())

    async def run():
        prompt_meta = _prompt_meta()
        prompt_meta["chat_mode"] = "device_control"
        response = await streaming.stream_chat_response(
            conv_id="conv_toy_service",
            model_key="mock-model",
            history=[{"role": "user", "content": "hi"}],
            prompt_meta=prompt_meta,
            temperature=0.1,
        )
        return await _collect_sse(response)

    import asyncio
    events = asyncio.run(run())

    assert [event["type"] for event in events] == ["start", "chunk", "toy_command", "debug"]
    assert captured["intents"][0].tool_name == "device.toy"
    assert captured["intents"][0].arguments == {"command": "SCENE:warmup"}
    assert captured["context"].mode == "device_control"
    assert "device.toy" in captured["context"].capabilities
    assert "device.toy" in captured["adapters"]
    assert events[2]["commands"] == ["SCENE:warmup"]
    assert toy_sys_calls == [("conv_toy_service", ["SCENE:warmup"])]
    assistant_inserts = [
        params for sql, params in executed
        if sql.startswith("INSERT INTO messages") and params[2] == "assistant"
    ]
    assert assistant_inserts[0][3] == "收到"
    assert any(payload["type"] == "toy_command" for payload in broadcasts)


def test_stream_chat_response_structured_actions_use_tool_service(monkeypatch):
    executed, broadcasts = _patch_streaming_io(monkeypatch)
    captured = {}

    async def fake_stream_ai(_history, _model_key, _usage_meta, _temperature):
        yield json.dumps({
            "assistant_text": "收到",
            "actions": [{"type": "toy", "command": "SCENE:warmup"}],
        }, ensure_ascii=False)

    class FakeToolService:
        async def execute_async(self, intents, *, context, adapters=None):
            intents = list(intents)
            if not intents:
                return []
            captured["intents"] = intents
            captured["context"] = context
            return [
                ToolResult(
                    tool_name="device.toy",
                    intent_id=intents[0].id,
                    status=ToolStatus.EXECUTED,
                    result={"type": "toy_command", "command": intents[0].arguments["command"]},
                )
            ]

    monkeypatch.setattr(streaming, "stream_ai", fake_stream_ai)
    monkeypatch.setattr(streaming, "tool_service", FakeToolService())

    async def run():
        prompt_meta = _prompt_meta()
        prompt_meta["chat_mode"] = "device_control"
        response = await streaming.stream_chat_response(
            conv_id="conv_structured_toy",
            model_key="mock-model",
            history=[{"role": "user", "content": "hi"}],
            prompt_meta=prompt_meta,
            temperature=0.1,
        )
        return await _collect_sse(response)

    import asyncio
    events = asyncio.run(run())

    assert [event["type"] for event in events] == ["start", "chunk", "toy_command", "debug"]
    assert events[1]["content"] == "收到"
    assert "actions" not in events[1]["content"]
    assert captured["intents"][0].source == "structured_action"
    assert captured["intents"][0].arguments == {"command": "SCENE:warmup"}
    assert events[2]["commands"] == ["SCENE:warmup"]
    assistant_inserts = [
        params for sql, params in executed
        if sql.startswith("INSERT INTO messages") and params[2] == "assistant"
    ]
    assert assistant_inserts[0][3] == "收到"
    assert any(payload["type"] == "toy_command" for payload in broadcasts)


def test_stream_chat_response_device_control_mode_executes_toy_event(monkeypatch):
    executed, broadcasts = _patch_streaming_io(monkeypatch)
    toy_sys_calls = []
    fake_gateway = _FakeControlGateway({
        "type": "toy_command",
        "ok": True,
        "device_id": "mock_ring",
        "command": "2",
        "device_command": "pulse",
        "message": "mock_pulse_sent",
        "audit_event_id": "mev_toy_ok",
        "control_session_id": "ctrl_1",
        "control_epoch": 0,
        "owner_client_id": "tab1",
    })

    async def fake_stream_ai(_history, _model_key, _usage_meta, _temperature):
        yield "收到 [TOY:2]"

    async def fake_toy_sys_msg(conv_id, commands):
        toy_sys_calls.append((conv_id, list(commands)))

    monkeypatch.setattr(streaming, "stream_ai", fake_stream_ai)
    monkeypatch.setattr(streaming, "_toy_sys_msg", fake_toy_sys_msg)
    monkeypatch.setattr(streaming, "control_command_gateway", fake_gateway)

    async def run():
        prompt_meta = _prompt_meta()
        prompt_meta["chat_mode"] = "device_control"
        response = await streaming.stream_chat_response(
            conv_id="conv_toy_device_control",
            model_key="mock-model",
            history=[{"role": "user", "content": "hi"}],
            prompt_meta=prompt_meta,
            temperature=0.1,
        )
        return await _collect_sse(response)

    import asyncio
    events = asyncio.run(run())

    assert [event["type"] for event in events] == ["start", "chunk", "toy_command", "debug"]
    assert events[2]["commands"] == ["2"]
    assert events[2]["control_session_id"] == "ctrl_1"
    assert fake_gateway.calls[0]["intent"].arguments == {"command": "2"}
    assert fake_gateway.calls[0]["context"].conv_id == "conv_toy_device_control"
    assert events[-1]["chat_mode"] == "device_control"
    assert events[-1]["mode_source"] == "prompt_meta"
    assert "device.toy" in events[-1]["capabilities"]
    assert toy_sys_calls == [("conv_toy_device_control", ["2"])]
    assistant_inserts = [
        params for sql, params in executed
        if sql.startswith("INSERT INTO messages") and params[2] == "assistant"
    ]
    assert assistant_inserts[0][3] == "收到"
    assert any(payload["type"] == "toy_command" for payload in broadcasts)


def test_stream_chat_response_control_session_falls_back_when_model_omits_toy(monkeypatch):
    executed, broadcasts = _patch_streaming_io(monkeypatch)
    toy_sys_calls = []
    fake_gateway = _FakeControlGateway({
        "type": "toy_command",
        "ok": True,
        "device_id": "browser_toy_bridge",
        "command": streaming.CONTROL_DOM_TOY_FALLBACK_COMMAND,
        "message": "bridge_command_queued",
        "audit_event_id": "mev_fallback",
        "control_session_id": "ctrl_1",
        "control_epoch": 0,
        "owner_client_id": "tab1",
    })

    async def fake_stream_ai(_history, _model_key, _usage_meta, _temperature):
        yield "我看到了。"

    async def fake_toy_sys_msg(conv_id, commands):
        toy_sys_calls.append((conv_id, list(commands)))

    monkeypatch.setattr(streaming, "stream_ai", fake_stream_ai)
    monkeypatch.setattr(streaming, "_toy_sys_msg", fake_toy_sys_msg)
    monkeypatch.setattr(streaming, "control_command_gateway", fake_gateway)

    async def run():
        prompt_meta = _prompt_meta()
        prompt_meta.update({
            "chat_mode": "control_session",
            "mode_source": "control_session",
            "control_context_source": "control_session",
            "control_kind": "dom",
            "control_session_id": "ctrl_1",
            "control_epoch": 0,
            "owner_client_id": "tab1",
            "capabilities": ["device.toy"],
        })
        response = await streaming.stream_chat_response(
            conv_id="conv_toy_fallback",
            model_key="mock-model",
            history=[{"role": "user", "content": "hi"}],
            prompt_meta=prompt_meta,
            temperature=0.1,
        )
        return await _collect_sse(response)

    import asyncio
    events = asyncio.run(run())

    assert [event["type"] for event in events] == ["start", "chunk", "toy_command", "debug"]
    assert events[2]["commands"] == [streaming.CONTROL_DOM_TOY_FALLBACK_COMMAND]
    assert fake_gateway.calls[0]["intent"].source == "control_session_fallback"
    assert fake_gateway.calls[0]["intent"].arguments == {"command": streaming.CONTROL_DOM_TOY_FALLBACK_COMMAND}
    assert fake_gateway.calls[0]["context"].metadata["control_context_source"] == "control_session"
    assert fake_gateway.calls[0]["context"].metadata["control_kind"] == "dom"
    assert events[-1]["toy_delivery"]["status"] == "gateway_accepted"
    assert events[-1]["toy_delivery"]["fallback"] is True
    assert events[-1]["toy_delivery"]["reason"] == "fallback_no_model_toy_marker"
    assert toy_sys_calls == [("conv_toy_fallback", [streaming.CONTROL_DOM_TOY_FALLBACK_COMMAND])]
    assistant_inserts = [
        params for sql, params in executed
        if sql.startswith("INSERT INTO messages") and params[2] == "assistant"
    ]
    assert assistant_inserts[0][3] == "我看到了。"
    assert any(payload["type"] == "toy_command" for payload in broadcasts)


def test_stream_chat_response_legacy_dom_falls_back_when_model_omits_toy(monkeypatch):
    executed, broadcasts = _patch_streaming_io(monkeypatch)
    toy_sys_calls = []
    fake_gateway = _FakeControlGateway({
        "type": "toy_command",
        "ok": True,
        "device_id": "browser_toy_bridge",
        "command": streaming.CONTROL_DOM_TOY_FALLBACK_COMMAND,
        "message": "bridge_command_queued",
        "audit_event_id": "mev_legacy_fallback",
        "legacy_allowed": True,
        "control_legacy_fallback": True,
    })

    async def fake_stream_ai(_history, _model_key, _usage_meta, _temperature):
        yield "我在。"

    async def fake_toy_sys_msg(conv_id, commands):
        toy_sys_calls.append((conv_id, list(commands)))

    monkeypatch.setattr(streaming, "stream_ai", fake_stream_ai)
    monkeypatch.setattr(streaming, "_toy_sys_msg", fake_toy_sys_msg)
    monkeypatch.setattr(streaming, "control_command_gateway", fake_gateway)

    async def run():
        prompt_meta = _prompt_meta()
        prompt_meta.update({
            "chat_mode": "device_control",
            "mode_source": "ai_dom_mode",
            "control_context_source": "legacy_body",
            "control_kind": "dom",
        })
        response = await streaming.stream_chat_response(
            conv_id="conv_legacy_dom_fallback",
            model_key="mock-model",
            history=[{"role": "user", "content": "hi"}],
            prompt_meta=prompt_meta,
            temperature=0.1,
        )
        return await _collect_sse(response)

    import asyncio
    events = asyncio.run(run())

    assert [event["type"] for event in events] == ["start", "chunk", "toy_command", "debug"]
    assert events[2]["commands"] == [streaming.CONTROL_DOM_TOY_FALLBACK_COMMAND]
    assert events[2]["control_legacy_fallback"] is True
    assert fake_gateway.calls[0]["intent"].source == "control_session_fallback"
    assert fake_gateway.calls[0]["context"].mode == "device_control"
    assert fake_gateway.calls[0]["context"].metadata["control_context_source"] == "legacy_body"
    assert fake_gateway.calls[0]["context"].metadata["control_kind"] == "dom"
    assert events[-1]["toy_delivery"]["status"] == "gateway_accepted"
    assert events[-1]["toy_delivery"]["fallback"] is True
    assert events[-1]["toy_delivery"]["reason"] == "fallback_no_model_toy_marker"
    assert toy_sys_calls == [("conv_legacy_dom_fallback", [streaming.CONTROL_DOM_TOY_FALLBACK_COMMAND])]
    assistant_inserts = [
        params for sql, params in executed
        if sql.startswith("INSERT INTO messages") and params[2] == "assistant"
    ]
    assert assistant_inserts[0][3] == "我在。"
    assert any(payload["type"] == "toy_command" for payload in broadcasts)


def test_stream_chat_response_toy_does_not_broadcast_when_gateway_rejects(monkeypatch):
    executed, broadcasts = _patch_streaming_io(monkeypatch)
    toy_sys_calls = []
    fake_gateway = _FakeControlGateway({
        "type": "toy_command",
        "ok": False,
        "device_id": "mock_ring",
        "command": "",
        "device_command": "pulse",
        "message": "unsupported_command",
        "audit_event_id": "mev_toy_fail",
    })

    async def fake_stream_ai(_history, _model_key, _usage_meta, _temperature):
        yield "收到 [TOY:3]"

    async def fake_toy_sys_msg(conv_id, commands):
        toy_sys_calls.append((conv_id, list(commands)))

    monkeypatch.setattr(streaming, "stream_ai", fake_stream_ai)
    monkeypatch.setattr(streaming, "_toy_sys_msg", fake_toy_sys_msg)
    monkeypatch.setattr(streaming, "control_command_gateway", fake_gateway)

    async def run():
        prompt_meta = _prompt_meta()
        prompt_meta["chat_mode"] = "device_control"
        response = await streaming.stream_chat_response(
            conv_id="conv_toy_device_fail",
            model_key="mock-model",
            history=[{"role": "user", "content": "hi"}],
            prompt_meta=prompt_meta,
            temperature=0.1,
        )
        return await _collect_sse(response)

    import asyncio
    events = asyncio.run(run())

    assert [event["type"] for event in events] == ["start", "chunk", "toy_command_rejected", "debug"]
    assert events[2]["commands"] == ["3"]
    assert events[2]["status"] == "gateway_rejected"
    assert events[2]["reason"] == "unsupported_command"
    assert fake_gateway.calls[0]["intent"].arguments["command"] == "3"
    assert toy_sys_calls == []
    assert not any(payload["type"] == "toy_command" for payload in broadcasts)
    rejected = [payload for payload in broadcasts if payload["type"] == "toy_command_rejected"]
    assert rejected and rejected[0]["data"]["reason"] == "unsupported_command"
    assert events[-1]["toy_commands"] == ["3"]
    assert events[-1]["toy_delivery"]["status"] == "gateway_rejected"
    assert events[-1]["toy_delivery"]["reason"] == "unsupported_command"
    assistant_inserts = [
        params for sql, params in executed
        if sql.startswith("INSERT INTO messages") and params[2] == "assistant"
    ]
    assert assistant_inserts[0][3] == "收到"


def test_stream_chat_response_eval_mode_strips_side_effects(monkeypatch):
    executed, broadcasts = _patch_streaming_io(monkeypatch)

    async def fake_stream_ai(_history, _model_key, _usage_meta, _temperature):
        yield "回复 [TOY:1] [REMEMBER:不要落库]"

    async def fail_side_effect(*_args, **_kwargs):
        raise AssertionError("side effect should not run in memory_eval_mode")

    monkeypatch.setattr(streaming, "stream_ai", fake_stream_ai)
    monkeypatch.setattr(basic_actions, "_store_remember_notes", fail_side_effect)
    monkeypatch.setattr(streaming, "_toy_sys_msg", fail_side_effect)

    async def run():
        response = await streaming.stream_chat_response(
            conv_id="conv_eval",
            model_key="mock-model",
            history=[{"role": "user", "content": "hi"}],
            prompt_meta=_prompt_meta(),
            temperature=None,
            memory_eval_mode=True,
        )
        return await _collect_sse(response)

    import asyncio
    events = asyncio.run(run())

    assert [event["type"] for event in events] == ["start", "chunk", "debug"]
    assistant_inserts = [
        params for sql, params in executed
        if sql.startswith("INSERT INTO messages") and params[2] == "assistant"
    ]
    assert assistant_inserts[0][3] == "回复"
    assert not any(payload["type"] == "toy_command" for payload in broadcasts)


def test_stream_chat_response_heart_whisper_uses_tool_service(monkeypatch):
    executed, broadcasts = _patch_streaming_io(monkeypatch)
    captured = {}

    async def fake_stream_ai(_history, _model_key, _usage_meta, _temperature):
        yield "回复 【HE"
        yield "ART：走服务】"

    async def fail_direct_store(*_args, **_kwargs):
        raise AssertionError("heart whisper should be executed through ToolService")

    class FakeToolService:
        async def execute_async(self, intents, *, context, adapters=None):
            intents = list(intents)
            captured.setdefault("calls", []).append((intents, context, adapters))
            if not intents:
                return []
            captured["intents"] = intents
            captured["context"] = context
            captured["adapters"] = adapters
            return [
                ToolResult(
                    tool_name="heart.whisper",
                    intent_id=intents[0].id,
                    status=ToolStatus.EXECUTED,
                    result={
                        "type": "heart_whisper",
                        "id": "hw_service",
                        "msg_id": context.msg_id,
                        "content": intents[0].arguments["content"],
                        "created_at": 456.0,
                    },
                )
            ]

    monkeypatch.setattr(streaming, "stream_ai", fake_stream_ai)
    monkeypatch.setattr(basic_actions, "_store_heart_whisper", fail_direct_store)
    monkeypatch.setattr(streaming, "tool_service", FakeToolService())

    async def run():
        response = await streaming.stream_chat_response(
            conv_id="conv_heart_service",
            model_key="mock-model",
            history=[{"role": "user", "content": "hi"}],
            prompt_meta=_prompt_meta(),
            temperature=0.1,
        )
        return await _collect_sse(response)

    import asyncio
    events = asyncio.run(run())

    assert [event["type"] for event in events] == ["start", "chunk", "heart_whisper", "debug"]
    assert captured["intents"][0].tool_name == "heart.whisper"
    assert captured["intents"][0].arguments == {"content": "走服务"}
    assert captured["context"].conv_id == "conv_heart_service"
    assert captured["context"].msg_id
    assert "heart.whisper" in captured["adapters"]
    assert events[2]["id"] == "hw_service"
    assert events[2]["content"] == "走服务"
    assistant_inserts = [
        params for sql, params in executed
        if sql.startswith("INSERT INTO messages") and params[2] == "assistant"
    ]
    assert assistant_inserts[0][3] == "回复"
    assert any(payload["type"] == "heart_whisper" for payload in broadcasts)


def test_stream_chat_response_remember_uses_tool_service(monkeypatch):
    executed, broadcasts = _patch_streaming_io(monkeypatch)
    captured = {}

    async def fake_stream_ai(_history, _model_key, _usage_meta, _temperature):
        yield "回复 [REMEMBER:用户喜欢冷萃]"

    async def fail_direct_store(*_args, **_kwargs):
        raise AssertionError("remember should be executed through ToolService")

    class FakeToolService:
        async def execute_async(self, intents, *, context, adapters=None):
            intents = list(intents)
            if not intents:
                return []
            captured["intents"] = intents
            captured["context"] = context
            captured["adapters"] = adapters
            return [
                ToolResult(
                    tool_name="memory.remember",
                    intent_id=intents[0].id,
                    status=ToolStatus.EXECUTED,
                    result={"content": intents[0].arguments["content"], "stored": True},
                )
            ]

    monkeypatch.setattr(streaming, "stream_ai", fake_stream_ai)
    monkeypatch.setattr(basic_actions, "_store_remember_notes", fail_direct_store)
    monkeypatch.setattr(streaming, "tool_service", FakeToolService())

    async def run():
        response = await streaming.stream_chat_response(
            conv_id="conv_remember_service",
            model_key="mock-model",
            history=[{"role": "user", "content": "hi"}],
            prompt_meta=_prompt_meta(),
            temperature=0.1,
        )
        return await _collect_sse(response)

    import asyncio
    events = asyncio.run(run())

    assert [event["type"] for event in events] == ["start", "chunk", "debug"]
    assert captured["intents"][0].tool_name == "memory.remember"
    assert captured["intents"][0].arguments == {"content": "用户喜欢冷萃"}
    assert captured["context"].conv_id == "conv_remember_service"
    assert "memory.remember" in captured["adapters"]
    assistant_inserts = [
        params for sql, params in executed
        if sql.startswith("INSERT INTO messages") and params[2] == "assistant"
    ]
    assert assistant_inserts[0][3] == "回复"
    assert not any(payload["type"] == "memory_added" for payload in broadcasts)


def test_stream_chat_response_music_uses_tool_service_adapter(monkeypatch):
    executed, broadcasts = _patch_streaming_io(monkeypatch)
    search_calls = []
    audio_calls = []

    async def fake_stream_ai(_history, _model_key, _usage_meta, _temperature):
        yield "回复 [MUSIC:夜曲 周杰伦]"

    def fake_search_songs(query, limit=5):
        search_calls.append((query, limit))
        return [
            {"id": 1, "name": "夜曲", "artist": "周杰伦"},
            {"id": 2, "name": "晴天", "artist": "周杰伦"},
            {"id": 3, "name": "七里香", "artist": "周杰伦"},
            {"id": 4, "name": "一路向北", "artist": "周杰伦"},
        ]

    def fake_get_audio_url(song_id):
        audio_calls.append(song_id)
        return f"https://music.test/{song_id}"

    monkeypatch.setattr(streaming, "stream_ai", fake_stream_ai)
    monkeypatch.setattr(basic_actions, "search_songs", fake_search_songs)
    monkeypatch.setattr(basic_actions, "get_audio_url", fake_get_audio_url)

    async def run():
        response = await streaming.stream_chat_response(
            conv_id="conv_music_service",
            model_key="mock-model",
            history=[{"role": "user", "content": "hi"}],
            prompt_meta=_prompt_meta(),
            temperature=0.1,
        )
        return await _collect_sse(response)

    import asyncio
    events = asyncio.run(run())

    assert [event["type"] for event in events] == ["start", "chunk", "music", "debug"]
    assert search_calls == [("夜曲 周杰伦", 5)]
    assert audio_calls == [1]
    assert events[2]["cards"][0]["audio_url"] == "https://music.test/1"
    assert [song["id"] for song in events[2]["cards"][0]["candidates"]] == [2, 3, 4]
    assistant_inserts = [
        params for sql, params in executed
        if sql.startswith("INSERT INTO messages") and params[2] == "assistant"
    ]
    assert assistant_inserts[0][3] == "回复"
    assert json.loads(assistant_inserts[0][5]) == [
        {"type": "music", "name": "夜曲", "artist": "周杰伦", "id": 1}
    ]
    assert any(payload["type"] == "music" for payload in broadcasts)


def test_stream_chat_response_schedule_uses_tool_service_adapter(monkeypatch):
    executed, broadcasts = _patch_streaming_io(monkeypatch)
    schedule_calls = []
    list_followups = []

    async def fake_stream_ai(_history, _model_key, _usage_meta, _temperature):
        yield (
            "回复 [ALARM:2026-05-13 08:00|起床] "
            "[REMINDER:2026-05-14|交材料] "
            "[Monitor:2026-05-15 20:00|看一眼] "
            "[SCHEDULE_DEL:sch_1] [SCHEDULE_LIST]"
        )

    async def fake_process_schedule_commands_with_results(text, conv_id, **kwargs):
        schedule_calls.append((text, conv_id))
        marker_to_tool = {
            "[ALARM:": "schedule.alarm",
            "[REMINDER:": "schedule.reminder",
            "[Monitor:": "schedule.monitor",
            "[SCHEDULE_DEL:": "schedule.delete",
            "[SCHEDULE_LIST]": "schedule.list",
        }
        tool_name = next(
            value for marker, value in marker_to_tool.items() if marker in text
        )
        return "", [{
            "type": "schedule_command",
            "tool_name": tool_name,
            "status": "failed",
            "reason": "test_stub",
        }]

    monkeypatch.setattr(streaming, "stream_ai", fake_stream_ai)
    monkeypatch.setattr(
        streaming,
        "process_schedule_commands_with_results",
        fake_process_schedule_commands_with_results,
    )
    monkeypatch.setattr(
        streaming,
        "load_worldbook_names",
        lambda: ("阿玖", "Aion"),
    )

    async def fake_schedule_list_followup(
        conv_id,
        model_key,
        result,
        *,
        parent_request_id="",
    ):
        list_followups.append({
            "conv_id": conv_id,
            "model_key": model_key,
            "result": result,
            "parent_request_id": parent_request_id,
        })

    monkeypatch.setattr(
        streaming,
        "perform_schedule_list_followup",
        fake_schedule_list_followup,
    )

    async def run():
        response = await streaming.stream_chat_response(
            conv_id="conv_schedule_service",
            model_key="mock-model",
            history=[{"role": "user", "content": "hi"}],
            prompt_meta=_prompt_meta(),
            temperature=0.1,
        )
        return await _collect_sse(response)

    import asyncio
    events = asyncio.run(run())

    assert [event["type"] for event in events] == ["start", "chunk", "debug"]
    assert schedule_calls == [
        ("[ALARM:2026-05-13 08:00|起床]", "conv_schedule_service"),
        ("[REMINDER:2026-05-14|交材料]", "conv_schedule_service"),
        ("[Monitor:2026-05-15 20:00|看一眼]", "conv_schedule_service"),
        ("[SCHEDULE_DEL:sch_1]", "conv_schedule_service"),
        ("[SCHEDULE_LIST]", "conv_schedule_service"),
    ]
    assistant_inserts = [
        params for sql, params in executed
        if sql.startswith("INSERT INTO messages") and params[2] == "assistant"
    ]
    assert assistant_inserts[0][3] == "回复"
    assert not any(payload["type"].startswith("schedule") for payload in broadcasts)
    assert len(list_followups) == 1
    assert list_followups[0]["result"]["status"] == "failed"
    assert list_followups[0]["result"]["reason"] == "test_stub"


def test_schedule_list_failure_followup_does_not_claim_empty_schedule():
    prompt = side_effects._schedule_list_followup_prompt(
        {"status": "failed", "reason": "db_unavailable"},
        "小云",
    )

    assert "db_unavailable" in prompt
    assert "当前日程列表是未知的" in prompt
    assert "不是“暂无日程”" in prompt
    assert "以下是系统刚刚从日程表读取的真实结果" not in prompt


def test_successful_alarm_results_map_to_android_set_and_cancel_events():
    set_event = streaming._android_alarm_event({
        "tool_name": "schedule.alarm",
        "ok": True,
        "status": "succeeded",
        "schedule_id": "sch_1",
        "trigger_at": "2026-05-13 08:00",
        "content": "起床",
    })
    cancel_event = streaming._android_alarm_event({
        "tool_name": "schedule.delete",
        "ok": True,
        "status": "succeeded",
        "schedule_id": "sch_1",
        "schedule": {
            "type": "alarm",
            "trigger_at": "2026-05-13 08:00",
            "content": "起床",
        },
    })

    assert set_event == {
        "type": "android_alarm_set",
        "data": {
            "id": "sch_1",
            "trigger_at": "2026-05-13 08:00",
            "content": "起床",
        },
    }
    assert cancel_event["type"] == "android_alarm_cancel"
    assert cancel_event["data"]["id"] == "sch_1"
    assert streaming._android_alarm_event({
        "tool_name": "schedule.alarm",
        "ok": False,
        "status": "rejected",
    }) is None


def test_normal_chat_alarm_execution_broadcasts_to_android(monkeypatch):
    broadcasts = []
    capture_calls = []

    async def fake_process(_text, _conv_id, **kwargs):
        capture_calls.append(kwargs)
        return "", [{
            "tool_name": "schedule.alarm",
            "ok": True,
            "status": "succeeded",
            "schedule_id": "sch_1",
            "trigger_at": "2026-05-13 08:00",
            "content": "起床",
        }]

    async def fake_broadcast(payload):
        broadcasts.append(payload)

    monkeypatch.setattr(
        streaming,
        "process_schedule_commands_with_results",
        fake_process,
    )
    monkeypatch.setattr(
        streaming,
        "manager",
        SimpleNamespace(broadcast=fake_broadcast),
    )
    monkeypatch.setattr(
        streaming,
        "load_worldbook_names",
        lambda: ("阿玖", "Aion"),
    )
    result = asyncio.run(streaming._execute_schedule_command(
        ToolIntent(
            id="alarm",
            tool_name="schedule.alarm",
            raw_text="[ALARM:2026-05-13 08:00|起床]",
        ),
        ToolContext(
            conv_id="conv",
            metadata={
                "source_chain": "main",
                "current_user_message_id": "user-setting-alarm",
            },
        ),
    ))

    assert result["schedule_id"] == "sch_1"
    assert capture_calls == [{
        "ai_name": "Aion",
        "source_message_id": "user-setting-alarm",
    }]
    assert broadcasts == [{
        "type": "android_alarm_set",
        "data": {
            "id": "sch_1",
            "trigger_at": "2026-05-13 08:00",
            "content": "起床",
        },
    }]


def test_stream_chat_response_poi_uses_tool_service_adapter(monkeypatch):
    executed, broadcasts = _patch_streaming_io(monkeypatch)
    captured = {}

    async def fake_stream_ai(_history, _model_key, _usage_meta, _temperature):
        yield "回复 [POI_SEARCH:咖啡]"

    def fail_direct_poi(*_args, **_kwargs):
        raise AssertionError("poi search should be executed through ToolService")

    class FakeToolService:
        async def execute_async(self, intents, *, context, adapters=None):
            intents = list(intents)
            if not intents:
                return []
            captured["intents"] = intents
            captured["context"] = context
            captured["adapters"] = adapters
            return [
                ToolResult(
                    tool_name="location.poi_search",
                    intent_id=intents[0].id,
                    status=ToolStatus.EXECUTED,
                    result={
                        "type": "poi_search",
                        "conv_id": context.conv_id,
                        "categories": [intents[0].arguments["category"]],
                        "msg_id": context.msg_id,
                    },
                )
            ]

    monkeypatch.setattr(streaming, "stream_ai", fake_stream_ai)
    monkeypatch.setattr(streaming, "perform_poi_check", fail_direct_poi)
    monkeypatch.setattr(streaming, "tool_service", FakeToolService())

    async def run():
        response = await streaming.stream_chat_response(
            conv_id="conv_poi_service",
            model_key="mock-model",
            history=[{"role": "user", "content": "hi"}],
            prompt_meta=_prompt_meta(),
            temperature=0.1,
        )
        return await _collect_sse(response)

    import asyncio
    events = asyncio.run(run())

    assert [event["type"] for event in events] == ["start", "chunk", "poi_search", "debug"]
    assert captured["intents"][0].tool_name == "location.poi_search"
    assert captured["intents"][0].arguments == {"category": "咖啡"}
    assert captured["context"].conv_id == "conv_poi_service"
    assert captured["context"].model_key == "mock-model"
    assert "location.poi_search" in captured["adapters"]
    assert events[2]["categories"] == ["咖啡"]
    assistant_inserts = [
        params for sql, params in executed
        if sql.startswith("INSERT INTO messages") and params[2] == "assistant"
    ]
    assert assistant_inserts[0][3] == "回复"
    assert any(payload["type"] == "poi_search" for payload in broadcasts)


def test_stream_chat_response_activity_uses_tool_service_adapter(monkeypatch):
    executed, broadcasts = _patch_streaming_io(monkeypatch)
    captured = {}

    async def fake_stream_ai(_history, _model_key, _usage_meta, _temperature):
        yield "回复 [查看动态:9]"

    def fail_direct_activity(*_args, **_kwargs):
        raise AssertionError("activity summary should be executed through ToolService")

    class FakeToolService:
        async def execute_async(self, intents, *, context, adapters=None):
            intents = list(intents)
            if not intents:
                return []
            captured["intents"] = intents
            captured["context"] = context
            captured["adapters"] = adapters
            return [
                ToolResult(
                    tool_name="activity.summary",
                    intent_id=intents[0].id,
                    status=ToolStatus.EXECUTED,
                    result={
                        "type": "activity_check",
                        "conv_id": context.conv_id,
                        "n": intents[0].arguments["n"],
                        "msg_id": context.msg_id,
                    },
                )
            ]

    monkeypatch.setattr(streaming, "stream_ai", fake_stream_ai)
    monkeypatch.setattr(streaming, "perform_activity_check", fail_direct_activity)
    monkeypatch.setattr(streaming, "tool_service", FakeToolService())

    async def run():
        response = await streaming.stream_chat_response(
            conv_id="conv_activity_service",
            model_key="mock-model",
            history=[{"role": "user", "content": "hi"}],
            prompt_meta=_prompt_meta(),
            temperature=0.1,
        )
        return await _collect_sse(response)

    import asyncio
    events = asyncio.run(run())

    assert [event["type"] for event in events] == ["start", "chunk", "activity_check", "debug"]
    assert captured["intents"][0].tool_name == "activity.summary"
    assert captured["intents"][0].arguments == {"raw_window": "9", "n": 9}
    assert captured["context"].conv_id == "conv_activity_service"
    assert captured["context"].model_key == "mock-model"
    assert "activity.summary" in captured["adapters"]
    assert events[2]["n"] == 9
    assistant_inserts = [
        params for sql, params in executed
        if sql.startswith("INSERT INTO messages") and params[2] == "assistant"
    ]
    assert assistant_inserts[0][3] == "回复"
    assert any(payload["type"] == "activity_check" for payload in broadcasts)


def test_stream_chat_response_camera_marker_returns_disabled_event(monkeypatch):
    executed, broadcasts = _patch_streaming_io(monkeypatch)

    async def fake_stream_ai(_history, _model_key, _usage_meta, _temperature):
        yield f"看一下 {streaming.CAM_CHECK_CMD}"

    monkeypatch.setattr(streaming, "stream_ai", fake_stream_ai)

    async def run():
        response = await streaming.stream_chat_response(
            conv_id="conv_cam_service",
            model_key="mock-model",
            history=[{"role": "user", "content": "hi"}],
            prompt_meta=_prompt_meta(),
            temperature=0.1,
        )
        return await _collect_sse(response)

    import asyncio
    events = asyncio.run(run())

    assert [event["type"] for event in events] == ["start", "chunk", "cam_disabled", "debug"]
    assert events[2]["conv_id"] == "conv_cam_service"
    assert events[2]["reason"] == "legacy_local_camera_disabled"
    assistant_inserts = [
        params for sql, params in executed
        if sql.startswith("INSERT INTO messages") and params[2] == "assistant"
    ]
    assert assistant_inserts[0][3] == "看一下"
    assert not any(payload["type"] == "cam_check" for payload in broadcasts)


def test_stream_chat_response_emits_non_device_tool_event_shapes(monkeypatch):
    executed, broadcasts = _patch_streaming_io(monkeypatch)
    remembered = []
    poi_calls = []
    activity_calls = []

    async def fake_stream_ai(_history, _model_key, _usage_meta, _temperature):
        yield "工具回复"

    class FakePostProcessor:
        async def process(self, full_text, *, conv_id, memory_eval_mode=False):
            assert full_text == "工具回复"
            assert conv_id == "conv_tools"
            assert memory_eval_mode is False
            return PostProcessResult(
                content="工具回复",
                music_cards=[{
                    "id": 7,
                    "name": "七里香",
                    "artist": "周杰伦",
                    "audio_url": "https://music.test/7",
                }],
                cam_triggered=True,
                activity_n=6,
                poi_categories=["咖啡"],
                heart_whispers=["悄悄话"],
                remember_notes=["用户喜欢冷萃"],
            )

    async def fake_store_heart(conv_id, msg_id, content):
        return {
            "type": "heart_whisper",
            "id": "hw_test",
            "msg_id": msg_id,
            "content": content,
            "created_at": 123.0,
        }

    async def fake_store_remember(notes, conv_id):
        remembered.append((conv_id, list(notes)))

    async def fake_poi(conv_id, model_key, categories, *, request_id=None):
        poi_calls.append((conv_id, model_key, list(categories)))

    async def fake_activity(conv_id, model_key, n, *, request_id=None):
        activity_calls.append((conv_id, model_key, n))

    monkeypatch.setattr(streaming, "stream_ai", fake_stream_ai)
    monkeypatch.setattr(streaming, "_post_processor", FakePostProcessor())
    monkeypatch.setattr(basic_actions, "_store_heart_whisper", fake_store_heart)
    monkeypatch.setattr(basic_actions, "_store_remember_notes", fake_store_remember)
    monkeypatch.setattr(streaming, "perform_poi_check", fake_poi)
    monkeypatch.setattr(streaming, "perform_activity_check", fake_activity)

    async def run():
        response = await streaming.stream_chat_response(
            conv_id="conv_tools",
            model_key="mock-model",
            history=[{"role": "user", "content": "hi"}],
            prompt_meta=_prompt_meta(),
            temperature=0.1,
        )
        events = await _collect_sse(response)
        import asyncio
        await asyncio.sleep(0)
        return events

    import asyncio
    events = asyncio.run(run())

    event_types = [event["type"] for event in events]
    assert event_types == [
        "start",
        "chunk",
        "heart_whisper",
        "cam_disabled",
        "poi_search",
        "activity_check",
        "music",
        "debug",
    ]
    assert events[2]["content"] == "悄悄话"
    assert events[3]["conv_id"] == "conv_tools"
    assert events[3]["reason"] == "legacy_local_camera_disabled"
    assert events[4]["categories"] == ["咖啡"]
    assert events[5]["n"] == 6
    assert events[6]["cards"][0]["name"] == "七里香"
    assert remembered == [("conv_tools", ["用户喜欢冷萃"])]
    assert poi_calls == [("conv_tools", "mock-model", ["咖啡"])]
    assert activity_calls == [("conv_tools", "mock-model", 6)]
    assistant_inserts = [
        params for sql, params in executed
        if sql.startswith("INSERT INTO messages") and params[2] == "assistant"
    ]
    assert assistant_inserts[0][3] == "工具回复"
    assert json.loads(assistant_inserts[0][5]) == [
        {"type": "music", "name": "七里香", "artist": "周杰伦", "id": 7}
    ]
    broadcast_types = [payload["type"] for payload in broadcasts]
    assert "cam_check" not in broadcast_types
    for expected in ("heart_whisper", "poi_search", "activity_check", "music"):
        assert expected in broadcast_types


def test_working_model_v2_flag_off_hides_tag_without_scheduling(monkeypatch):
    executed, _broadcasts = _patch_streaming_io(monkeypatch)
    scheduled = []

    async def fake_stream_ai(_history, _model_key, _usage_meta, _temperature):
        yield "正文[WORKING_MODEL_"
        yield 'REQUEST]{"statement":"她看重掌控感","source":"用户原话"}'
        yield "[/WORKING_MODEL_REQUEST]结尾"

    def fake_create(coro, *, name):
        if name.startswith("chat_stream:"):
            return asyncio.create_task(coro)
        scheduled.append(name)
        coro.close()
        return None

    monkeypatch.setattr(streaming, "stream_ai", fake_stream_ai)
    monkeypatch.setattr(streaming, "working_model_v2_write_enabled", lambda: False)
    monkeypatch.setattr(streaming, "create_tracked_task", fake_create)
    monkeypatch.setattr(streaming, "_schedule_chunk_index_update", lambda *_args, **_kwargs: {})
    monkeypatch.setattr(streaming.timeline_service, "start_background_refresh", lambda *_args: None)

    async def run():
        meta = _prompt_meta()
        meta["current_user_message_id"] = "user-current"
        response = await streaming.stream_chat_response(
            conv_id="conv-wm-off",
            model_key="captured-core",
            history=[{"role": "user", "content": "hi"}],
            prompt_meta=meta,
            temperature=0.1,
        )
        return await _collect_sse(response)

    events = asyncio.run(run())
    visible = "".join(event.get("content", "") for event in events)
    assert visible == "正文结尾"
    assistant = [
        params for sql, params in executed
        if sql.startswith("INSERT INTO messages") and params[2] == "assistant"
    ][0]
    assert assistant[3] == "正文结尾"
    assert not any(name.startswith("working_model_v2:") for name in scheduled)


def test_working_model_v2_missing_identity_is_rejected_before_capture_or_gate(
    monkeypatch,
    caplog,
):
    captured = []
    scheduled = []
    monkeypatch.setattr(streaming, "working_model_v2_write_enabled", lambda: True)
    monkeypatch.setattr(
        streaming,
        "capture_working_model_pipeline_input",
        lambda **kwargs: captured.append(kwargs),
    )
    monkeypatch.setattr(
        streaming,
        "create_tracked_task",
        lambda coro, *, name: scheduled.append((coro, name)),
    )

    did_schedule = streaming._schedule_working_model_pipeline_after_commit(
        conv_id="conv-missing-identity",
        origin_user_message_id="user-frozen",
        origin_assistant_message_id="assistant-frozen",
        model_key="captured-core",
        request_candidate=SimpleNamespace(statement="她看重掌控感", source="用户原话"),
        identity_snapshot={"text": "   "},
    )

    assert did_schedule is False
    assert captured == []
    assert scheduled == []
    assert "missing frozen writer identity" in caplog.text


@pytest.mark.parametrize("with_vow", [False, True])
def test_working_model_v2_schedules_once_after_commit_and_does_not_block_done(
    monkeypatch,
    with_vow,
):
    events = []
    broadcasts = []
    captured = []
    pipeline_started = asyncio.Event()
    pipeline_release = asyncio.Event()
    pipeline_finished = asyncio.Event()
    background_tasks = []

    class EventDB(_FakeDB):
        async def commit(self):
            events.append("assistant_commit")

        async def rollback(self):
            events.append("rollback")

    executed = []
    monkeypatch.setattr(streaming, "get_db", lambda: EventDB(executed))

    async def fake_broadcast(payload):
        broadcasts.append(payload)

    async def noop_async(*_args, **_kwargs):
        return None

    monkeypatch.setattr(streaming, "manager", SimpleNamespace(broadcast=fake_broadcast))
    monkeypatch.setattr(streaming, "export_conversation", noop_async)
    monkeypatch.setattr(streaming, "_maybe_auto_digest", noop_async)
    monkeypatch.setattr(streaming, "_record_v2_memory_usage_for_chat", noop_async)
    monkeypatch.setattr(streaming, "_schedule_chunk_index_update", lambda *_args, **_kwargs: {})
    monkeypatch.setattr(streaming.timeline_service, "start_background_refresh", lambda *_args: None)
    monkeypatch.setattr(streaming, "working_model_v2_write_enabled", lambda: True)

    async def fake_stream_ai(_history, _model_key, _usage_meta, _temperature):
        prefix = "[VOW:一直允许彼此犯错|说定了]" if with_vow else ""
        yield prefix + "正文"
        yield (
            '[WORKING_MODEL_REQUEST]{"statement":"她看重掌控感",'
            '"source":"用户刚才说想自己拍板"}[/WORKING_MODEL_REQUEST]'
        )

    monkeypatch.setattr(streaming, "stream_ai", fake_stream_ai)

    if with_vow:
        async def fake_admit(_db, **_kwargs):
            return {"id": "vow"}, "说定了", None

        monkeypatch.setattr(streaming.vow_service, "admit_ai_vow_in_tx", fake_admit)

    async def fake_pending(_db, **_kwargs):
        return {"created_pending_id": None}

    monkeypatch.setattr(
        streaming.pending_recall_service,
        "apply_after_assistant_in_tx",
        fake_pending,
    )

    def fake_capture(**kwargs):
        captured.append(kwargs)
        return SimpleNamespace(origin_assistant_message_id=kwargs["origin_assistant_message_id"])

    async def slow_pipeline(_value):
        events.append("pipeline_started")
        pipeline_started.set()
        await pipeline_release.wait()
        events.append("pipeline_finished")
        pipeline_finished.set()

    monkeypatch.setattr(streaming, "capture_working_model_pipeline_input", fake_capture)
    monkeypatch.setattr(streaming, "run_working_model_pipeline", slow_pipeline)

    def fake_create(coro, *, name):
        events.append(f"scheduled:{name}")
        task = asyncio.create_task(coro)
        background_tasks.append(task)
        return task

    monkeypatch.setattr(streaming, "create_tracked_task", fake_create)

    async def run():
        meta = _prompt_meta()
        meta.update({
            "current_user_message_id": "user-frozen",
            "working_model_writer_identity": {
                "text": "[身份] 测试人格",
                "sha256": "a" * 64,
            },
        })
        response = await streaming.stream_chat_response(
            conv_id="conv-wm",
            model_key="captured-core",
            history=[{"role": "user", "content": "hi"}],
            prompt_meta=meta,
            temperature=0.1,
        )
        sse = await _collect_sse(response)
        await pipeline_started.wait()
        assert pipeline_finished.is_set() is False
        pipeline_release.set()
        await asyncio.gather(*background_tasks)
        return sse

    sse = asyncio.run(run())
    wm_schedules = [item for item in events if item.startswith("scheduled:working_model_v2:")]
    assert len(wm_schedules) == 1
    assert events.index("assistant_commit") < events.index(wm_schedules[0])
    assert events.index(wm_schedules[0]) < events.index("pipeline_finished")
    assert len(captured) == 1
    assert captured[0]["origin_user_message_id"] == "user-frozen"
    assert captured[0]["model_key"] == "captured-core"
    assert captured[0]["statement"] == "她看重掌控感"
    visible = "".join(event.get("content", "") for event in sse)
    assert "WORKING_MODEL_REQUEST" not in visible
    assert "她看重掌控感" not in visible
    assistant = [
        params for sql, params in executed
        if sql.startswith("INSERT INTO messages") and params[2] == "assistant"
    ][0]
    assert "WORKING_MODEL_REQUEST" not in assistant[3]
    assert any(payload["type"] == "msg_created" for payload in broadcasts)


def test_assistant_commit_failure_never_schedules_working_model_pipeline(monkeypatch):
    scheduled = []

    class BrokenCommitDB(_FakeDB):
        async def commit(self):
            raise RuntimeError("commit failed")

    monkeypatch.setattr(streaming, "get_db", lambda: BrokenCommitDB([]))
    monkeypatch.setattr(streaming, "working_model_v2_write_enabled", lambda: True)
    monkeypatch.setattr(streaming, "_schedule_chunk_index_update", lambda *_args, **_kwargs: {})
    monkeypatch.setattr(streaming.timeline_service, "start_background_refresh", lambda *_args: None)

    async def fake_stream_ai(_history, _model_key, _usage_meta, _temperature):
        yield (
            '正文[WORKING_MODEL_REQUEST]{"statement":"她看重掌控感",'
            '"source":"用户原话"}[/WORKING_MODEL_REQUEST]'
        )

    async def noop_async(*_args, **_kwargs):
        return None

    monkeypatch.setattr(streaming, "stream_ai", fake_stream_ai)
    monkeypatch.setattr(streaming, "export_conversation", noop_async)
    monkeypatch.setattr(streaming, "_maybe_auto_digest", noop_async)
    monkeypatch.setattr(streaming, "_record_v2_memory_usage_for_chat", noop_async)

    def fake_create(coro, *, name):
        if name.startswith("chat_stream:"):
            return asyncio.create_task(coro)
        scheduled.append(name)
        coro.close()
        return None

    monkeypatch.setattr(streaming, "create_tracked_task", fake_create)

    async def run():
        meta = _prompt_meta()
        meta["current_user_message_id"] = "user-frozen"
        response = await streaming.stream_chat_response(
            conv_id="conv-commit-fail",
            model_key="captured-core",
            history=[{"role": "user", "content": "hi"}],
            prompt_meta=meta,
            temperature=0.1,
        )
        return await _collect_sse(response)

    asyncio.run(run())
    assert not any(name.startswith("working_model_v2:") for name in scheduled)


def test_stream_chat_response_emits_cam_disabled_when_camera_unavailable(monkeypatch):
    _patch_streaming_io(monkeypatch)

    async def fake_stream_ai(_history, _model_key, _usage_meta, _temperature):
        yield "看一下"

    class FakePostProcessor:
        async def process(self, full_text, *, conv_id, memory_eval_mode=False):
            return PostProcessResult(content="看一下", cam_triggered=True)

    monkeypatch.setattr(streaming, "stream_ai", fake_stream_ai)
    monkeypatch.setattr(streaming, "_post_processor", FakePostProcessor())

    async def run():
        response = await streaming.stream_chat_response(
            conv_id="conv_cam_offline",
            model_key="mock-model",
            history=[{"role": "user", "content": "hi"}],
            prompt_meta=_prompt_meta(),
            temperature=0.1,
        )
        return await _collect_sse(response)

    import asyncio
    events = asyncio.run(run())

    assert [event["type"] for event in events] == ["start", "chunk", "cam_disabled", "debug"]
    assert events[2]["reason"] == "legacy_local_camera_disabled"
