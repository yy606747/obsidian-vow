import asyncio
import json
from types import SimpleNamespace

from app.chat import initiative_helpers, initiative_routes
import pytest
from app.chat.models import DomInitiativeBody, WhisperInitiativeBody
from app.control import ControlPromptContext
from app.tools.schemas import ToolResult, ToolStatus


@pytest.fixture(autouse=True)
def _empty_vow_context(monkeypatch):
    """誓约层（Phase 2）在本管道注入常驻读取；既有用例用空桩隔离，不读真实库。"""

    class _Stub:
        async def load_vow_prompt_context(self):
            return "", ""

    monkeypatch.setattr(initiative_helpers, "vow_service", _Stub())



class _FakeCursor:
    def __init__(self, *, one=None, all_rows=None):
        self.one = one
        self.all_rows = all_rows or []

    async def fetchone(self):
        return self.one

    async def fetchall(self):
        return self.all_rows


class _FakeDB:
    def __init__(self, executed):
        self.executed = executed
        self.row_factory = None

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def execute(self, sql, params=()):
        self.executed.append((sql, params))
        if sql.startswith("SELECT model FROM conversations"):
            return _FakeCursor(one={"model": "mock-model"})
        if sql.startswith("SELECT role, content, attachments, created_at FROM messages"):
            return _FakeCursor(all_rows=[
                {
                    "role": "user",
                    "content": "上一句",
                    "attachments": "[]",
                    "created_at": 1778598000.0,
                }
            ])
        return _FakeCursor()

    async def commit(self):
        return None


async def _collect_sse(response):
    events = []
    async for raw in response.body_iterator:
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8")
        assert raw.startswith("data: ")
        events.append(json.loads(raw[len("data: "):].strip()))
    return events


def _patch_initiative_io(monkeypatch, *, control_context=None):
    executed = []
    broadcasts = []
    remember_calls = []
    toy_sys_calls = []

    def fake_get_db():
        return _FakeDB(executed)

    async def fake_broadcast(payload):
        broadcasts.append(payload)

    async def fake_export(_conv_id):
        return None

    async def fake_store_remember(notes, conv_id):
        remember_calls.append((notes, conv_id))

    async def fake_toy_sys_msg(conv_id, commands):
        toy_sys_calls.append((conv_id, commands))

    monkeypatch.setattr(initiative_routes, "get_db", fake_get_db)
    monkeypatch.setattr(initiative_routes, "manager", SimpleNamespace(broadcast=fake_broadcast))
    monkeypatch.setattr(initiative_routes, "export_conversation", fake_export)
    monkeypatch.setattr(initiative_routes, "_store_remember_notes", fake_store_remember)
    monkeypatch.setattr(initiative_routes, "_toy_sys_msg", fake_toy_sys_msg)
    monkeypatch.setattr(initiative_routes, "load_worldbook", lambda: {"user_name": "用户"})

    class FakeControlSessionService:
        async def get_prompt_context(self, conv_id, _body):
            if control_context is not None:
                return control_context
            kind = "whisper" if "whisper" in conv_id else "dom"
            return ControlPromptContext(
                session_id="ctrl_test",
                kind=kind,
                active=True,
                source="control_session",
                owner_client_id="tab_test",
                control_epoch=0,
            )

    monkeypatch.setattr(initiative_routes, "control_session_service", FakeControlSessionService())

    class FakeToolService:
        async def execute_async(self, intents, *, context, adapters=None):
            if context.metadata["control_context_source"] == "control_session":
                assert context.metadata["control_session_id"] == "ctrl_test"
                assert context.metadata["control_epoch"] == 0
                assert context.metadata["owner_client_id"] == "tab_test"
            else:
                assert context.metadata["control_context_source"] == "none"
                assert context.metadata["control_session_id"] is None
            results = []
            for intent in intents:
                if intent.tool_name == "device.toy":
                    assert "device.toy" in context.capabilities
                    payload = {
                        "type": "toy_command",
                        "command": intent.arguments["command"],
                        "control_session_id": "ctrl_test",
                        "control_epoch": 0,
                        "owner_client_id": "tab_test",
                    }
                else:
                    assert intent.tool_name == "memory.remember"
                    payload = await adapters[intent.tool_name](intent, context)
                results.append(
                    ToolResult(
                        tool_name=intent.tool_name,
                        intent_id=intent.id,
                        status=ToolStatus.EXECUTED,
                        result=payload,
                    )
                )
            return results

    monkeypatch.setattr(initiative_helpers, "tool_service", FakeToolService())

    return executed, broadcasts, remember_calls, toy_sys_calls


def test_dom_initiative_reuses_postprocessor_without_changing_sse_shape(monkeypatch):
    executed, broadcasts, remember_calls, toy_sys_calls = _patch_initiative_io(monkeypatch)

    async def fake_stream_ai(_history, model_key, usage_meta, temperature):
        assert model_key == "mock-model"
        usage_meta["provider"] = "mock"
        yield "出手 [TOY:1] [REMEMBER:喜欢突袭] <meta>hide</meta>"

    monkeypatch.setattr(initiative_routes, "stream_ai", fake_stream_ai)

    async def run():
        response = await initiative_routes.dom_initiative("conv_dom", DomInitiativeBody())
        return await _collect_sse(response)

    events = asyncio.run(run())

    assert [event["type"] for event in events] == ["start", "chunk", "toy_command"]
    assert events[2]["commands"] == ["1"]
    assistant_inserts = [
        params for sql, params in executed
        if sql.startswith("INSERT INTO messages") and params[2] == "assistant"
    ]
    assert assistant_inserts[0][3] == "出手"
    assert remember_calls == [(["喜欢突袭"], "conv_dom")]
    assert toy_sys_calls == [("conv_dom", ["1"])]
    assert any(payload["type"] == "msg_created" for payload in broadcasts)
    assert any(payload["type"] == "toy_command" for payload in broadcasts)


def test_whisper_initiative_reuses_postprocessor_and_keeps_legacy_toy_event(monkeypatch):
    executed, broadcasts, remember_calls, toy_sys_calls = _patch_initiative_io(monkeypatch)

    async def fake_stream_ai(_history, model_key):
        assert model_key == "mock-model"
        yield {"provider": "mock"}
        yield "靠近一点 [TOY:2] [REMEMBER:喜欢户外突袭] <meta>hide</meta>"

    monkeypatch.setattr(initiative_routes, "stream_ai", fake_stream_ai)

    async def run():
        response = await initiative_routes.whisper_initiative("conv_whisper", WhisperInitiativeBody())
        return await _collect_sse(response)

    events = asyncio.run(run())

    assert [event["type"] for event in events] == ["start", "chunk", "toy_command"]
    assert events[2]["commands"] == ["2"]
    assert events[2]["control_session_id"] == "ctrl_test"
    assert events[2]["control_epoch"] == 0
    assert events[2]["owner_client_id"] == "tab_test"
    assistant_inserts = [
        params for sql, params in executed
        if sql.startswith("INSERT INTO messages") and params[2] == "assistant"
    ]
    assert assistant_inserts[0][3] == "靠近一点"
    assert remember_calls == [(["喜欢户外突袭"], "conv_whisper")]
    assert toy_sys_calls == [("conv_whisper", ["2"])]
    assert any(payload["type"] == "msg_created" for payload in broadcasts)
    assert any(
        payload["type"] == "toy_command" and payload["data"]["commands"] == ["2"]
        for payload in broadcasts
    )


def test_dom_initiative_buffers_structured_actions(monkeypatch):
    executed, broadcasts, remember_calls, toy_sys_calls = _patch_initiative_io(monkeypatch)

    async def fake_stream_ai(_history, model_key, usage_meta, temperature):
        assert model_key == "mock-model"
        usage_meta["provider"] = "mock"
        yield json.dumps({
            "assistant_text": "靠近一点",
            "actions": [
                {"type": "toy", "command": "SCENE:warmup"},
                {"tool_name": "memory.remember", "arguments": {"content": "用户喜欢结构化突袭"}},
            ],
        }, ensure_ascii=False)

    monkeypatch.setattr(initiative_routes, "stream_ai", fake_stream_ai)

    async def run():
        response = await initiative_routes.dom_initiative("conv_dom_structured", DomInitiativeBody())
        return await _collect_sse(response)

    events = asyncio.run(run())

    assert [event["type"] for event in events] == ["start", "chunk", "toy_command"]
    assert events[1]["content"] == "靠近一点"
    assert "actions" not in events[1]["content"]
    assert events[2]["commands"] == ["SCENE:warmup"]
    assistant_inserts = [
        params for sql, params in executed
        if sql.startswith("INSERT INTO messages") and params[2] == "assistant"
    ]
    assert assistant_inserts[0][3] == "靠近一点"
    assert remember_calls == [(["用户喜欢结构化突袭"], "conv_dom_structured")]
    assert toy_sys_calls == [("conv_dom_structured", ["SCENE:warmup"])]
    assert any(payload["type"] == "msg_created" for payload in broadcasts)
    assert any(payload["type"] == "toy_command" for payload in broadcasts)


def test_initiative_provider_error_releases_buffered_text_without_effects(monkeypatch):
    executed, broadcasts, remember_calls, toy_sys_calls = _patch_initiative_io(monkeypatch)

    async def fake_stream_ai(_history, _model_key, _usage_meta, _temperature):
        yield json.dumps(
            {
                "assistant_text": "靠近一点",
                "actions": [
                    {"type": "toy", "command": "SCENE:warmup"},
                    {
                        "tool_name": "memory.remember",
                        "arguments": {"content": "不应写入"},
                    },
                ],
            },
            ensure_ascii=False,
        )
        raise RuntimeError("provider down")

    monkeypatch.setattr(initiative_routes, "stream_ai", fake_stream_ai)

    async def run():
        response = await initiative_routes.dom_initiative(
            "conv_dom_error",
            DomInitiativeBody(),
        )
        return await _collect_sse(response)

    events = asyncio.run(run())

    assert [event["type"] for event in events] == ["start", "chunk", "chunk"]
    assert events[1]["content"] == "靠近一点"
    assert events[2]["content"] == "\n[请求出错: provider down]"
    assert not any(
        sql.startswith("INSERT INTO messages") and params[2] == "assistant"
        for sql, params in executed
    )
    assert remember_calls == []
    assert toy_sys_calls == []
    assert broadcasts == []


def test_dom_initiative_without_active_control_strips_toy_without_execution(monkeypatch):
    executed, broadcasts, remember_calls, toy_sys_calls = _patch_initiative_io(
        monkeypatch,
        control_context=ControlPromptContext(),
    )

    async def fake_stream_ai(_history, model_key, usage_meta, temperature):
        assert model_key == "mock-model"
        usage_meta["provider"] = "mock"
        yield "出手 [TOY:9] [REMEMBER:没有控制也要记]"

    monkeypatch.setattr(initiative_routes, "stream_ai", fake_stream_ai)

    async def run():
        response = await initiative_routes.dom_initiative("conv_dom_none", DomInitiativeBody())
        return await _collect_sse(response)

    events = asyncio.run(run())

    assert [event["type"] for event in events] == ["start", "chunk"]
    assert events[1]["content"] == "出手"
    assistant_inserts = [
        params for sql, params in executed
        if sql.startswith("INSERT INTO messages") and params[2] == "assistant"
    ]
    assert assistant_inserts[0][3] == "出手"
    assert remember_calls == [(["没有控制也要记"], "conv_dom_none")]
    assert toy_sys_calls == []
    assert any(payload["type"] == "msg_created" for payload in broadcasts)
    assert not any(payload["type"] == "toy_command" for payload in broadcasts)


def test_dom_initiative_without_toy_still_streams_text_chunks(monkeypatch):
    _executed, _broadcasts, _remember_calls, _toy_sys_calls = _patch_initiative_io(
        monkeypatch,
        control_context=ControlPromptContext(),
    )

    async def fake_stream_ai(_history, model_key, usage_meta, temperature):
        usage_meta["provider"] = "mock"
        yield "先抱"
        yield "一下"

    monkeypatch.setattr(initiative_routes, "stream_ai", fake_stream_ai)

    async def run():
        response = await initiative_routes.dom_initiative("conv_dom_none", DomInitiativeBody())
        return await _collect_sse(response)

    events = asyncio.run(run())

    assert [event["type"] for event in events] == ["start", "chunk", "chunk"]
    assert [event["content"] for event in events[1:]] == ["先抱", "一下"]
