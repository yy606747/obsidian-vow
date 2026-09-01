import asyncio
import json

from app.control.schemas import ControlSession
from app.tide.renderer import TIDE_TTL_MS, TideRendererRegistry, _RenderMemory
from app.tools.schemas import ToolContext


def _session():
    return ControlSession(
        session_id="ctrl_tide_1",
        conv_id="conv_tide",
        kind="tide",
        status="active",
        owner_client_id="tab_tide",
        device_id="muse",
        control_resource_id="toy:muse",
        started_at=1.0,
        last_heartbeat_at=1.0,
        last_snapshot_at=None,
        ended_at=None,
        close_reason=None,
        control_epoch=0,
        safeword_set=False,
    )


def test_tide_renderer_uses_model_json_frame_from_slot(monkeypatch):
    import ai_providers
    import config

    calls = []
    registry = TideRendererRegistry()

    async def fake_latest_user_text(_conv_id):
        return "你在干嘛"

    async def fake_call_slot_chat(slot_name, messages, **kwargs):
        calls.append({"slot_name": slot_name, "messages": messages, "kwargs": kwargs})
        return json.dumps({"vib_pattern": 8, "thrust_pattern": 7, "ttl_ms": 2400})

    monkeypatch.setattr(registry, "_latest_user_text", fake_latest_user_text)
    monkeypatch.setattr(config, "get_slot", lambda name: {"endpoint": "mock", "model": "mock"} if name == "tide_renderer" else None)
    monkeypatch.setattr(ai_providers, "call_slot_chat", fake_call_slot_chat)

    memory = _RenderMemory(vib_pattern=1, thrust_pattern=0)
    frame = asyncio.run(registry._render_frame(_session(), "像月光压下来，别解释", memory))

    assert frame == {"vib_pattern": 8, "thrust_pattern": 7, "ttl_ms": 2400}
    assert memory.vib_pattern == 8
    assert memory.thrust_pattern == 7
    assert calls[0]["slot_name"] == "tide_renderer"
    assert calls[0]["kwargs"]["expect_json"] is True
    assert calls[0]["kwargs"]["scope"] == "tide_renderer"
    user_payload = json.loads(calls[0]["messages"][1]["content"])
    assert user_payload["intent"] == "像月光压下来，别解释"
    assert user_payload["latest_user_message"] == "你在干嘛"
    assert user_payload["current_frame"] == {"vib_pattern": 1, "thrust_pattern": 0, "ttl_ms": TIDE_TTL_MS}


def test_tide_renderer_falls_back_to_conversation_model_when_slot_missing(monkeypatch):
    import ai_providers
    import config

    registry = TideRendererRegistry()
    stream_calls = []

    async def fake_latest_user_text(_conv_id):
        return ""

    async def fake_conversation_model_key(_conv_id):
        return "mock-model"

    async def fake_stream_ai(messages, model_key, meta, temperature):
        stream_calls.append({"messages": messages, "model_key": model_key, "temperature": temperature})
        yield '{"vib_pattern":'
        yield '2,"thrust_pattern":9,"ttl_ms":3000}'

    monkeypatch.setattr(registry, "_latest_user_text", fake_latest_user_text)
    monkeypatch.setattr(registry, "_conversation_model_key", fake_conversation_model_key)
    monkeypatch.setattr(config, "get_slot", lambda _name: None)
    monkeypatch.setattr(ai_providers, "stream_ai", fake_stream_ai)

    frame = asyncio.run(registry._render_frame(_session(), "更贴近一点", _RenderMemory()))

    assert frame == {"vib_pattern": 2, "thrust_pattern": 9, "ttl_ms": 3000}
    assert stream_calls[0]["model_key"] == "mock-model"
    assert stream_calls[0]["temperature"] == 0.35


def test_tide_renderer_bad_model_output_uses_conservative_fallback(monkeypatch):
    registry = TideRendererRegistry()
    memory = _RenderMemory(last_intent="继续", vib_pattern=6, thrust_pattern=5, quiet_ticks=3)

    async def fake_call_renderer_model(_session, _intent, _memory):
        return "不是 json"

    monkeypatch.setattr(registry, "_call_renderer_model", fake_call_renderer_model)

    frame = asyncio.run(registry._render_frame(_session(), "继续", memory))

    assert frame == {"vib_pattern": 6, "thrust_pattern": 5, "ttl_ms": TIDE_TTL_MS}
    assert memory.vib_pattern == 6
    assert memory.thrust_pattern == 5


def test_tide_renderer_non_object_json_uses_fallback(monkeypatch):
    registry = TideRendererRegistry()
    memory = _RenderMemory(vib_pattern=2, thrust_pattern=2)

    async def fake_call_renderer_model(_session, _intent, _memory):
        return "[1, 2, 3]"

    monkeypatch.setattr(registry, "_call_renderer_model", fake_call_renderer_model)

    frame = asyncio.run(registry._render_frame(_session(), "继续", memory))

    assert frame == {"vib_pattern": 2, "thrust_pattern": 2, "ttl_ms": TIDE_TTL_MS}


def test_tide_renderer_bad_first_output_stays_idle(monkeypatch):
    registry = TideRendererRegistry()
    memory = _RenderMemory()

    async def fake_call_renderer_model(_session, _intent, _memory):
        return "不是 json"

    monkeypatch.setattr(registry, "_call_renderer_model", fake_call_renderer_model)

    frame = asyncio.run(registry._render_frame(_session(), "继续", memory))

    assert frame is None
    assert memory.vib_pattern == 0
    assert memory.thrust_pattern == 0


def test_tide_frame_without_model_call_does_not_reuse_previous_invocation(
    monkeypatch,
):
    import app.tide.renderer as renderer_module

    recorded = []

    class CapturingLedger:
        @staticmethod
        def new_invocation_id(_prefix):
            return "fresh-frame-invocation"

        async def record_renderer_frame(self, context, **kwargs):
            recorded.append((context, kwargs))
            return 1

    registry = TideRendererRegistry()
    registry._last_model_observation[_session().session_id] = (
        ToolContext(
            conv_id="conv_tide",
            request_id="old-request",
            metadata={"invocation_id": "old-model-invocation"},
        ),
        "old-model-invocation",
    )

    async def no_model_call(_session, _intent, _memory):
        return ""

    monkeypatch.setattr(renderer_module, "tool_invocation_ledger", CapturingLedger())
    monkeypatch.setattr(registry, "_call_renderer_model", no_model_call)

    frame = asyncio.run(
        registry._render_frame(
            _session(),
            "继续",
            _RenderMemory(vib_pattern=2, thrust_pattern=3),
        )
    )

    assert frame == {
        "vib_pattern": 2,
        "thrust_pattern": 3,
        "ttl_ms": TIDE_TTL_MS,
    }
    assert recorded[0][1]["invocation_id"] == "fresh-frame-invocation"
    assert recorded[0][1]["metadata"]["model_called"] is False


def test_tide_renderer_rejects_double_zero_model_frame(monkeypatch):
    registry = TideRendererRegistry()
    memory = _RenderMemory(vib_pattern=3, thrust_pattern=4)

    async def fake_call_renderer_model(_session, _intent, _memory):
        return json.dumps({"vib_pattern": 0, "thrust_pattern": 0, "ttl_ms": TIDE_TTL_MS})

    monkeypatch.setattr(registry, "_call_renderer_model", fake_call_renderer_model)

    frame = asyncio.run(registry._render_frame(_session(), "歇一下", memory))

    assert frame == {"vib_pattern": 3, "thrust_pattern": 4, "ttl_ms": TIDE_TTL_MS}


def test_tide_renderer_same_intent_does_not_hard_stop_after_long_quiet(monkeypatch):
    registry = TideRendererRegistry()
    memory = _RenderMemory(vib_pattern=3, thrust_pattern=4, quiet_ticks=30, last_intent="same")
    calls = []

    async def fake_call_renderer_model(_session, _intent, render_memory):
        calls.append(render_memory.quiet_ticks)
        return json.dumps({"vib_pattern": 3, "thrust_pattern": 4, "ttl_ms": TIDE_TTL_MS})

    monkeypatch.setattr(registry, "_call_renderer_model", fake_call_renderer_model)

    frame = asyncio.run(registry._render_frame(_session(), "same", memory))

    assert frame == {"vib_pattern": 3, "thrust_pattern": 4, "ttl_ms": TIDE_TTL_MS}
    assert calls == [31]


def test_tide_renderer_snapshots_only_changed_intents_or_frames(monkeypatch):
    import ai_providers
    import app.tide.renderer as renderer_module
    import config

    records = {
        "requests": [],
        "outputs": [],
        "turns": [],
        "frames": [],
    }

    class CapturingLedger:
        def __init__(self):
            self.counter = 0

        def new_invocation_id(self, prefix):
            self.counter += 1
            return f"{prefix}-{self.counter}"

        async def record_model_request(self, context, **kwargs):
            records["requests"].append((context, kwargs))
            return 1

        async def record_model_output(self, context, **kwargs):
            records["outputs"].append((context, kwargs))
            return 1

        async def record_turn(self, context, **kwargs):
            records["turns"].append((context, kwargs))
            return 1

        async def record_renderer_frame(self, context, **kwargs):
            records["frames"].append((context, kwargs))
            return 1

    registry = TideRendererRegistry()
    rendered = {"vib_pattern": 4, "thrust_pattern": 5, "ttl_ms": 3000}

    async def fake_latest_user_text(_conv_id):
        return "继续"

    async def fake_call_slot_chat(*_args, **_kwargs):
        return json.dumps(rendered)

    monkeypatch.setattr(renderer_module, "tool_invocation_ledger", CapturingLedger())
    monkeypatch.setattr(registry, "_latest_user_text", fake_latest_user_text)
    monkeypatch.setattr(config, "get_slot", lambda _name: {"model": "mock"})
    monkeypatch.setattr(ai_providers, "call_slot_chat", fake_call_slot_chat)

    memory = _RenderMemory()
    for _ in range(5):
        asyncio.run(registry._render_frame(
            _session(),
            "保持",
            memory,
            intent_version=1,
        ))
    asyncio.run(registry._render_frame(
        _session(),
        "保持",
        memory,
        intent_version=2,
    ))
    rendered["vib_pattern"] = 7
    asyncio.run(registry._render_frame(
        _session(),
        "保持",
        memory,
        intent_version=2,
    ))

    assert len(records["frames"]) == 7
    assert len(records["requests"]) == 3
    assert len(records["outputs"]) == 3
    assert len(records["turns"]) == 3
    assert len(records["requests"]) < len(records["frames"])
    assert sum(
        not kwargs["metadata"]["snapshot_stored"]
        for _context, kwargs in records["frames"]
    ) == 4
    assert all(
        kwargs["invocation_id"]
        for _context, kwargs in records["frames"]
    )


def test_tide_renderer_reactivate_same_session_keeps_current_intent(monkeypatch):
    import app.tide.renderer as renderer_module

    registry = TideRendererRegistry()
    binds = []

    class FakeIntentService:
        async def bind_active_session(self, session):
            binds.append(session.session_id)

    async def fake_run(_session):
        await asyncio.Event().wait()

    async def run():
        session = _session()
        await registry.activate(session)
        await registry.activate(session)
        await registry.stop_session(session, emit_stop=False, reason="test")

    monkeypatch.setattr(renderer_module, "tide_intent_service", FakeIntentService())
    monkeypatch.setattr(registry, "_run", fake_run)

    asyncio.run(run())

    assert binds == ["ctrl_tide_1"]
