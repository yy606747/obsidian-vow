import asyncio
from collections import deque
from contextlib import asynccontextmanager
from copy import deepcopy
import json

import aiosqlite
import pytest

from app.scenes.cards import parse_cards
from app.scenes.compiler import validate_sequence
from app.scenes.config import DEFAULT_DEVICES, SceneConfig, defaults, validate_config
from app.scenes.context import SceneIdentity
from app.scenes.player import LoopPlayer, MockDevices
from app.scenes.protocol import SceneOutput
from app.scenes.repository import SceneRepository
from app.scenes.schema import init_scene_tables
from app.scenes.session import SceneConflict, SceneOwnerMismatch, SceneService


CARD = "## 卡1：隐藏卡名\n一段有开场、变化和收束的准备笔记。\n<devices>mock_a</devices>"
IDENTITY = SceneIdentity("小栀", "阿澈", "PERSONA_ONLY", "WORKING_ONLY")


class Clock:
    value = 1000.0

    def __call__(self):
        return self.value

    def advance(self, seconds):
        self.value += seconds


class Models:
    def __init__(self):
        self.turns = []
        self.compilations = []
        self.once_calls = []
        self.outputs = deque(["开始。<toy1>PRIVATE_CONTROL</toy1><toy2>SECOND_CONTROL</toy2><self_awake>180</self_awake>"])
        self.card_output = CARD
        self.note_output = "<notes>留一点变化的余地。</notes>"
        self.recent_output = "阿澈与小栀的这段亲密互动已经结束，接下来可以继续聊天。"
        self.turn_hook = self.compile_hook = self.once_hook = None

    async def once(self, model_key, messages, *, scope):
        self.once_calls.append((scope, deepcopy(messages)))
        if self.once_hook:
            return await self.once_hook(scope, messages)
        if scope == "recent":
            return self.recent_output
        return self.card_output if scope == "cards" else self.note_output

    async def stream(self, model_key, messages):
        self.turns.append(deepcopy(messages))
        if self.turn_hook:
            async for chunk in self.turn_hook(messages):
                yield chunk
            return
        output = self.outputs.popleft() if self.outputs else "<self_awake>240</self_awake>"
        for character in output:
            yield character

    async def compile(self, messages):
        self.compilations.append(deepcopy(messages))
        if self.compile_hook:
            return await self.compile_hook(messages)
        payload = json.loads(messages[1]["content"])
        channels = payload["capabilities"]["channels"]
        return json.dumps({"action": "replace", "steps": [
            {"duration_ms": 1000, "values": {key: 10 for key in channels}},
            {"duration_ms": 2000, "values": {key: 20 for key in channels}},
        ]})


@asynccontextmanager
async def harness(tmp_path, *, models=None, control_check=None):
    @asynccontextmanager
    async def db():
        async with aiosqlite.connect(tmp_path / "scene.db") as connection:
            yield connection
    async with db() as connection:
        await init_scene_tables(connection)
        await connection.execute("CREATE TABLE IF NOT EXISTS messages (content TEXT)")
        await connection.execute("CREATE TABLE IF NOT EXISTS memory_events (content TEXT)")
        await connection.commit()
    clock = Clock()
    repository = SceneRepository(db, now=clock, choose=lambda items: items[0])
    config = SceneConfig(tmp_path / "scene-config.json")
    config.save({**defaults(), "enabled": True})
    events = []
    async def broadcast(event):
        events.append(deepcopy(event))
    async def identity():
        return IDENTITY
    async def resolver(conv_id):
        return conv_id, "test-core"
    async def idle():
        return False
    service = SceneService(repository=repository, config=config, models=models or Models(),
        identity_loader=identity, model_resolver=resolver, now=clock, broadcast=broadcast,
        control_check=control_check or idle)
    for device in DEFAULT_DEVICES:
        await service.connect(device["id"])
    try:
        yield service, clock, events
    finally:
        await service.close()


async def settle(service):
    for _ in range(20):
        tasks = list(service.tasks)
        if not tasks:
            return
        await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True), timeout=3)
        await asyncio.sleep(0)
    raise AssertionError("scene tasks did not settle")


async def start(service, *, request_id="request-1", devices=None):
    return await service.start(request_id=request_id, conv_id="conv1", owner_id="owner1",
        device_ids=["mock_a", "mock_b"] if devices is None else devices, safeword="stop-now")


def test_config_is_off_by_default_and_only_known_drivers_are_accepted():
    assert defaults()["enabled"] is False
    value = defaults()
    value["devices"][0]["driver"] = "real"
    with pytest.raises(ValueError, match="driver_unsupported"):
        validate_config(value)


def test_card_requirements_are_explicit_not_all_candidates():
    assert parse_cards(CARD, {"mock_a", "mock_b"})[0]["required_devices"] == ["mock_a"]
    for invalid in (CARD.replace("mock_a", "unknown"), CARD.replace("mock_a", "mock_a,mock_a"),
                    CARD.replace("<devices>mock_a</devices>", "")):
        assert parse_cards(invalid, {"mock_a", "mock_b"}) == []


def test_stream_parser_hides_every_chunk_boundary_and_combines_devices():
    text = "前<toy1>PRIVATE</toy1>后<toy2>SECOND</toy2><toy1>AGAIN</toy1><self_awake>200</self_awake>"
    for split in range(len(text) + 1):
        parser = SceneOutput(["toy1", "toy2"])
        visible = parser.feed(text[:split]) + parser.feed(text[split:])
        result = parser.finish()
        assert visible == "前后"
        assert result["intents"] == {"toy1": "PRIVATE\nAGAIN", "toy2": "SECOND"}
        assert result["awake"] == 200
    parser = SceneOutput(["toy1"])
    assert parser.feed("<scene_end/>") == ""
    assert parser.finish()["ended"]


@pytest.mark.parametrize("text", ["安全<toy1>PRIVATE", "安全<toy", "安全<toy99>PRIVATE</toy99>", "安全<toy1 x='1'>PRIVATE</toy1>"])
def test_malformed_or_unknown_hidden_content_never_leaks(text):
    parser = SceneOutput(["toy1"])
    visible = "".join(parser.feed(char) for char in text)
    assert visible == "安全"
    with pytest.raises(ValueError):
        parser.finish()


@pytest.mark.parametrize("hidden", [
    "<toy1>外层<toy2>内层</toy1>HIDDEN_TAIL</toy2>",
    "<toy1>外层<toy2>内层</toy2>HIDDEN_TAIL</toy1>",
    "<toy1>外层<toy1>内层</toy1>HIDDEN_TAIL</toy1>",
    "<toy99>内层</toy99>HIDDEN_TAIL",
    "<self_awake>invalid</self_awake>HIDDEN_TAIL",
])
def test_invalid_hidden_structure_permanently_poisoned_at_every_boundary(hidden):
    text = "安全" + hidden
    chunkings = [[text[:split], text[split:]] for split in range(len(text) + 1)] + [list(text)]
    for chunks in chunkings:
        parser = SceneOutput(["toy1", "toy2"])
        assert "".join(parser.feed(chunk) for chunk in chunks) == "安全"
        assert parser.feed("LATER_HIDDEN_TAIL<toy1>more</toy1>") == ""
        with pytest.raises(ValueError, match="scene_output_protocol_invalid"):
            parser.finish()
    parser = SceneOutput(["toy1", "toy2"])
    assert parser.feed("<toy1>外层<toy2>") == ""
    assert parser.invalid  # Detect the nested opening before any closing arrives.


@pytest.mark.parametrize("chunk_size", [1, 7, 1000])
def test_crossed_tags_never_reach_websocket_or_http_and_do_not_compile(tmp_path, chunk_size):
    import httpx
    from fastapi import FastAPI
    from routes.scenes import router, get_scene_service

    async def scenario():
        models = Models()
        text = "安全<toy1>外层<toy2>内层</toy1>HIDDEN_TAIL</toy2>"
        async def stream(_):
            for offset in range(0, len(text), chunk_size):
                yield text[offset:offset + chunk_size]
        models.turn_hook = stream
        async with harness(tmp_path, models=models) as (service, _, events):
            initial = await start(service)
            await settle(service)
            app = FastAPI()
            app.include_router(router)
            app.dependency_overrides[get_scene_service] = lambda: service
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://test") as client:
                responses = [(await client.get(path)).json() for path in (
                    '/api/scenes/sessions/current', f'/api/scenes/sessions/{initial["id"]}')]
            wire = json.dumps([events, responses], ensure_ascii=False)
            assert "HIDDEN_TAIL" not in wire and "外层" not in wire and "内层" not in wire
            assert responses[0]["messages"][0]["content"] == "安全"
            assert responses[0]["status"] == "paused"
            assert not models.compilations
            assert not any(service.devices.outputs.values())
    asyncio.run(scenario())


@pytest.mark.parametrize("stop", ["panic", "heartbeat"])
def test_websocket_backpressure_cannot_delay_panic_or_heartbeat_expiry(tmp_path, monkeypatch, stop):
    from app.scenes import session
    from ws import ConnectionManager
    monkeypatch.setattr(session, "BROADCAST_TIMEOUT", 0.1)

    async def scenario():
        async with harness(tmp_path) as (service, clock, _):
            await start(service)
            await settle(service)
            rt = service.active
            entered, cancelled = asyncio.Event(), asyncio.Event()
            sent = []
            class SlowSocket:
                async def send_text(self, text):
                    assert not service.lock.locked()
                    if not entered.is_set():
                        entered.set()
                        try:
                            await asyncio.Future()
                        finally:
                            cancelled.set()
                    sent.append(json.loads(text))
            manager = ConnectionManager()
            manager.active.append(SlowSocket())
            service.broadcast = manager.broadcast
            assert any(service.devices.outputs.values())
            # The scheduler itself must return without waiting for the socket.
            await asyncio.wait_for(service.tick(), 0.5)
            await asyncio.wait_for(entered.wait(), 0.5)
            if stop == "panic":
                await asyncio.wait_for(service.panic(), 0.5)
                assert rt.row["status"] == "paused" and "card" not in service.view(rt)
            else:
                clock.advance(46)
                await asyncio.wait_for(service.tick(), 0.5)
                assert rt.row["status"] == "paused" and rt.row["reason"] == "heartbeat_lost"
            assert not any(service.devices.outputs.values())
            await settle(service)
            assert cancelled.is_set()
            assert sent[-1]["type"] == "scene_state"
            assert sent[-1]["data"]["status"] == rt.row["status"]
            assert not any(d["output"] for d in sent[-1]["data"]["devices"])
    asyncio.run(scenario())


def test_backpressured_notifications_coalesce_without_losing_full_state(tmp_path):
    async def scenario():
        async with harness(tmp_path) as (service, clock, events):
            await start(service)
            await settle(service)
            rt = service.active
            entered, release = asyncio.Event(), asyncio.Event()
            async def broadcast(event):
                assert not service.lock.locked()
                entered.set()
                await release.wait()
                events.append(event)
            service.broadcast = broadcast
            await service.tick()
            await entered.wait()
            async with service.lock:
                rt.messages.append({"id": "new", "content": "snapshot"})
                service._emit_locked(rt)
                rt.messages[-1]["content"] = "latest"
                assert service._pending_events[rt.row["id"]]["data"]["messages"][-1]["content"] == "snapshot"
            for _ in range(20):
                clock.advance(0.3)
                await service.tick()
            assert len(service._pending_events) == 1
            pending = service._pending_events[rt.row["id"]]
            assert pending["type"] == "scene_state"
            assert pending["data"]["messages"][-1]["content"] == "latest"
            release.set()
            await settle(service)
            assert events[-1] == pending
    asyncio.run(scenario())


@pytest.mark.parametrize("provider", ["gemini", "vertex"])
@pytest.mark.parametrize("finish_reason", ["MAX_TOKENS", "STOP"])
def test_real_gemini_transport_finish_state_gates_control_commit(tmp_path, monkeypatch, provider, finish_reason):
    import httpx
    import ai_providers
    from app.scenes.models import SceneModels

    requests, logs = [], []
    body = "".join("data: " + json.dumps(frame) + "\n\n" for frame in [
        {"candidates": [{"content": {"parts": [{"text": "开始。<toy1>PRIVATE_CONTROL</toy1>"}]}}]},
        {"candidates": [{"finishReason": finish_reason}]},
        {"candidates": [], "usageMetadata": {"promptTokenCount": 10, "candidatesTokenCount": 12}},
    ])
    def transport(request):
        requests.append(request)
        return httpx.Response(200, content=body, headers={"content-type": "text/event-stream"})
    client = httpx.AsyncClient
    monkeypatch.setattr(ai_providers.httpx, "AsyncClient", lambda **_: client(transport=httpx.MockTransport(transport)))
    monkeypatch.setattr(ai_providers, "record_provider_event", lambda event: logs.append(event) or event)
    monkeypatch.setattr(ai_providers, "resolve_core_model", lambda _: {"_kind": "custom", "model": "test-gemini",
        "endpoint": {"id": "test", "name": "test", "type": provider, "base_url": "https://provider.invalid/v1", "api_key": "synthetic"}})

    async def scenario():
        models = Models()
        models.stream = SceneModels().stream  # Keep the real dispatch, SSE parser and metadata path.
        async with harness(tmp_path, models=models) as (service, _, events):
            await start(service)
            await settle(service)
            if finish_reason == "MAX_TOKENS":
                assert service.active.row["status"] == "paused"
                assert service.active.row["reason"] == "model_failed:scene_model_incomplete"
                assert not models.compilations
                assert not any(service.devices.outputs.values())
            else:
                assert service.active.row["status"] == "active"
                assert len(models.compilations) == 1
                assert service.devices.outputs["mock_a"]
            assert "PRIVATE_CONTROL" not in json.dumps(events)
    asyncio.run(scenario())
    assert len(requests) == 1
    assert "streamGenerateContent" in str(requests[0].url)
    assert logs[-1]["meta"]["finish_reason"] == finish_reason


def test_loop_replacement_hold_stop_and_validation():
    clock, devices = Clock(), MockDevices()
    devices.connect("a")
    player = LoopPlayer(devices, now=clock)
    caps = DEFAULT_DEVICES[0]["capabilities"]
    sequence = validate_sequence({"action": "replace", "steps": [
        {"duration_ms": 1000, "values": {"level": 10}},
        {"duration_ms": 1000, "values": {"level": 20}},
    ]}, caps)
    player.apply("a", sequence)
    clock.advance(1.2)
    player.tick()
    assert devices.outputs["a"] == {"level": 20}
    clock.advance(1)
    player.tick()
    assert devices.outputs["a"] == {"level": 10}
    started = player.started["a"]
    player.apply("a", {"action": "hold"})
    assert player.started["a"] == started
    player.apply("a", sequence)
    assert player.position("a")["elapsed_ms"] == 0
    player.apply("a", {"action": "stop"})
    assert devices.outputs["a"] == {} and not player.position("a")["playing"]
    for values in ({"level": 101}, {"missing": 10}, {"level": float("nan")}, {"level": True}):
        with pytest.raises(ValueError):
            validate_sequence({"action": "replace", "steps": [{"duration_ms": 1000, "values": values}]}, caps)


def test_entry_generation_blind_draw_idempotency_and_isolation(tmp_path):
    async def scenario():
        async with harness(tmp_path) as (service, clock, events):
            first = await start(service)
            assert first["status"] == "preparing" and "card" not in first
            assert (await start(service))["id"] == first["id"]
            await settle(service)
            rt = service.active
            assert rt.row["status"] == "active"
            assert len(service.models.compilations) == 2
            assert rt.card["required_devices"] == ["mock_a"]
            assert rt.row["bindings"] == {"toy1": "mock_a", "toy2": "mock_b"}
            wire = json.dumps(events, ensure_ascii=False)
            assert "PRIVATE_CONTROL" not in wire and "隐藏卡名" not in wire and "<toy" not in wire
            assert {event["type"] for event in events} == {"scene_state"}
            assert not await service.repo.matching(["mock_a", "mock_b"])
            card_input = str(service.models.once_calls[0])
            assert "PERSONA_ONLY" in card_input and "WORKING_ONLY" in card_input
            assert "capabilities" not in card_input and "max_step_ms" not in card_input
            await service.action(rt.row["id"], owner_id="owner1", action="finish")
            ended = await service.get(rt.row["id"])
            assert ended["card"]["raw_text"] == CARD
            async with service.repo.db() as db:
                for table in ("messages", "memory_events"):
                    assert (await (await db.execute(f"SELECT count(*) FROM {table}")).fetchone())[0] == 0
    asyncio.run(scenario())


def test_matching_subset_ordered_bindings_and_no_generation_for_existing_card(tmp_path):
    async def scenario():
        async with harness(tmp_path) as (service, clock, _):
            await service.repo.add_card(CARD, ["missing"], source="night", conv_id="c", model_key="m")
            selected = await service.repo.add_card(CARD, ["mock_b", "mock_a"], source="night", conv_id="c", model_key="m")
            assert await service.repo.matching(["mock_a"]) == []
            await start(service)
            await settle(service)
            assert service.active.card["id"] == selected["id"]
            assert service.active.row["bindings"] == {"toy1": "mock_b", "toy2": "mock_a"}
            prompt = service.models.turns[0][0]["content"]
            assert "【toy1：模拟设备 B】" in prompt
            assert "【toy2：模拟设备 A】" in prompt
            assert "<toy1>" in prompt and "</toy1>" in prompt
            assert "<toy2>" in prompt and "</toy2>" in prompt
            assert not service.models.once_calls
    asyncio.run(scenario())


def test_independent_updates_continue_during_compile_and_no_loop_calls(tmp_path):
    async def scenario():
        async with harness(tmp_path) as (service, clock, _):
            await start(service)
            await settle(service)
            rt = service.active
            b_started = rt.player.started["mock_b"]
            clock.advance(12)
            await service.tick()
            assert len(service.models.compilations) == 2
            gate, entered = asyncio.Event(), asyncio.Event()
            async def compile_later(messages):
                entered.set()
                await gate.wait()
                return '{"action":"replace","steps":[{"duration_ms":1000,"values":{"level":70}}]}'
            service.models.compile_hook = compile_later
            service.models.outputs.append("<toy1>换一种安排</toy1>")
            await service.message(rt.row["id"], owner_id="owner1", text="调整一下")
            await entered.wait()
            assert rt.player.accepted["mock_a"]["steps"][0]["values"] == {"level": 10}
            input_data = json.loads(service.models.compilations[-1][1]["content"])
            assert input_data["previous_sequence"] and input_data["playback_position"]["elapsed_ms"] == 12000
            gate.set()
            await settle(service)
            assert rt.player.started["mock_b"] == b_started
            assert service.devices.outputs["mock_a"] == {"level": 70}
    asyncio.run(scenario())


def test_awake_fallback_and_message_timer_collision(tmp_path):
    async def scenario():
        async with harness(tmp_path) as (service, clock, _):
            await start(service)
            await settle(service)
            rt = service.active
            assert rt.due_at == clock() + 180
            clock.advance(180)
            await service.heartbeat(rt.row["id"], owner_id="owner1")
            service.models.outputs.append("只说一句。")
            # Neither coroutine yields to the model task before both triggers
            # are queued; a single turn contains both.
            await service.tick()
            await service.message(rt.row["id"], owner_id="owner1", text="碰巧发来消息")
            await settle(service)
            assert len(service.models.turns) == 2
            assert rt.due_at == clock() + 480
            assert any(m["content"] == "碰巧发来消息" for m in service.models.turns[-1])
            clock.advance(481)
            await service.heartbeat(rt.row["id"], owner_id="owner1")
            await service.tick()
            await settle(service)
            assert len(service.models.turns) == 3
    asyncio.run(scenario())


@pytest.mark.parametrize("action", ["pause", "end", "finish"])
def test_late_compiler_cannot_restart_after_pause_or_end(tmp_path, action):
    async def scenario():
        models = Models()
        gate, entered = asyncio.Event(), asyncio.Event()
        async def delayed(messages):
            entered.set()
            try:
                await gate.wait()
            except asyncio.CancelledError:
                await gate.wait()
            return '{"action":"replace","steps":[{"duration_ms":1000,"values":{"level":90}}]}'
        models.outputs = deque(["<toy1>等待编译</toy1>"])
        models.compile_hook = delayed
        async with harness(tmp_path, models=models) as (service, _, _):
            first = await start(service)
            await entered.wait()
            await service.action(first["id"], owner_id="owner1", action=action)
            gate.set()
            await settle(service)
            assert service.devices.outputs.get("mock_a", {}) == {}
            assert not service.retained[first["id"]].player.sequences
    asyncio.run(scenario())


def test_pause_resume_heartbeat_disconnect_and_safeword(tmp_path):
    async def scenario():
        async with harness(tmp_path) as (service, clock, _):
            first = await start(service)
            await settle(service)
            rt = service.active
            with pytest.raises(SceneOwnerMismatch):
                await service.action(first["id"], owner_id="other", action="pause")
            clock.advance(46)
            await service.tick()
            assert rt.row["status"] == "paused" and rt.due_at is None
            await service.action(first["id"], owner_id="owner1", action="resume")
            assert not rt.player.sequences
            await settle(service)
            assert len(service.models.turns) == 2
            await service.connect("mock_b", False)
            assert rt.row["status"] == "paused"
            with pytest.raises(SceneConflict):
                await service.action(first["id"], owner_id="owner1", action="resume")
            count = len(service.models.turns)
            await service.message(first["id"], owner_id="owner1", text="STOP-NOW")
            assert rt.row["status"] == "paused" and rt.row["reason"] == "safeword"
            await settle(service)
            assert len(service.models.turns) == count + 1
            assert not rt.player.sequences and "card" not in service.view(rt)
    asyncio.run(scenario())


def test_feedback_notes_keep_clear_edit_no_reason_and_future_card_only(tmp_path):
    async def scenario():
        async with harness(tmp_path) as (service, _, _):
            first = await start(service)
            await settle(service)
            await service.action(first["id"], owner_id="owner1", action="finish")
            await service.feedback(first["id"], rating="up", reason="")
            await settle(service)
            note = await service.repo.notes()
            assert note["content"] == "留一点变化的余地。"
            assert "原因：未填写" in service.models.once_calls[-1][1][1]["content"]
            calls = len(service.models.once_calls)
            await service.feedback(first["id"], rating="up", reason="")
            await settle(service)
            assert len(service.models.once_calls) == calls
            await start(service, request_id="next")
            await settle(service)
            assert note["content"] in str([c for c in service.models.once_calls if c[0] == "cards"][-1])
            assert note["content"] not in str(service.models.turns[-1])
            service.models.note_output = "<keep/>"
            await service.feedback(first["id"], rating="down", reason="不同的评价")
            await settle(service)
            assert (await service.repo.notes())["version"] == note["version"]
            service.models.note_output = "<notes></notes>"
            await service.feedback(first["id"], rating=None, reason="清空笔记")
            await settle(service)
            assert (await service.repo.notes())["content"] == ""
    asyncio.run(scenario())


def test_preparation_cancellation_keeps_card_unused_and_cannot_revive(tmp_path):
    async def scenario():
        models = Models()
        entered, gate = asyncio.Event(), asyncio.Event()
        async def late(scope, messages):
            entered.set()
            try:
                await gate.wait()
            except asyncio.CancelledError:
                await gate.wait()
            return CARD
        models.once_hook = late
        async with harness(tmp_path, models=models) as (service, _, _):
            session = await start(service)
            await entered.wait()
            await service.action(session["id"], owner_id="owner1", action="finish")
            gate.set()
            await settle(service)
            assert service.active is None and not models.turns
            assert len(await service.repo.matching(["mock_a", "mock_b"])) == 1
            assert (await service.get(session["id"]))["status"] == "ended"
    asyncio.run(scenario())


def test_messages_during_generation_are_serialized_and_coalesced(tmp_path):
    async def scenario():
        models = Models()
        entered, gate = asyncio.Event(), asyncio.Event()
        live = peak = 0
        async def gated(messages):
            nonlocal live, peak
            live += 1
            peak = max(peak, live)
            try:
                entered.set()
                if len(models.turns) == 1:
                    await gate.wait()
                yield "<self_awake>240</self_awake>"
            finally:
                live -= 1
        models.turn_hook = gated
        async with harness(tmp_path, models=models) as (service, _, _):
            session = await start(service)
            await entered.wait()
            await service.message(session["id"], owner_id="owner1", text="第一条")
            await service.message(session["id"], owner_id="owner1", text="第二条")
            assert len(models.turns) == 1
            gate.set()
            await settle(service)
            assert peak == 1 and len(models.turns) == 2
            assert [m["content"] for m in models.turns[-1] if m["role"] == "user"][:2] == ["第一条", "第二条"]
    asyncio.run(scenario())


@pytest.mark.parametrize("action", ["pause", "end", "finish"])
def test_late_core_cannot_emit_or_control_after_lifecycle_change(tmp_path, action):
    async def scenario():
        models = Models()
        entered, gate = asyncio.Event(), asyncio.Event()
        async def late(messages):
            yield "之前的可见表达。"
            entered.set()
            try:
                await gate.wait()
            except asyncio.CancelledError:
                await gate.wait()
            yield "不该送达<toy1>不该执行</toy1><self_awake>1</self_awake>"
        models.turn_hook = late
        async with harness(tmp_path, models=models) as (service, clock, events):
            session = await start(service)
            await entered.wait()
            await service.action(session["id"], owner_id="owner1", action=action)
            gate.set()
            await settle(service)
            assert "不该送达" not in json.dumps(events, ensure_ascii=False)
            assert not models.compilations
            assert service.retained[session["id"]].due_at is None
    asyncio.run(scenario())


def test_older_compile_loses_to_newer_intent_and_new_session(tmp_path):
    async def scenario():
        models = Models()
        models.outputs = deque(["<toy1>旧意图</toy1>"])
        gate, entered = asyncio.Event(), asyncio.Event()
        async def compile_one(messages):
            intent = json.loads(messages[1]["content"])["intent"]
            if intent == "旧意图":
                entered.set()
                try:
                    await gate.wait()
                except asyncio.CancelledError:
                    await gate.wait()
            level = 5 if intent == "旧意图" else 75
            return json.dumps({"action": "replace", "steps": [{"duration_ms": 1000, "values": {"level": level}}]})
        models.compile_hook = compile_one
        async with harness(tmp_path, models=models) as (service, _, _):
            session = await start(service)
            await entered.wait()
            models.outputs.append("<toy1>新意图</toy1>")
            await service.message(session["id"], owner_id="owner1", text="更换")
            # Wait only for the second turn/compile, not the intentionally
            # non-cooperative first compiler.
            for _ in range(100):
                if service.devices.outputs.get("mock_a") == {"level": 75}:
                    break
                await asyncio.sleep(0.01)
            assert service.devices.outputs["mock_a"] == {"level": 75}
            await service.action(session["id"], owner_id="owner1", action="finish")
            models.outputs.append("<toy1>下一场</toy1>")
            new = await start(service, request_id="second")
            gate.set()
            await settle(service)
            assert service.active.row["id"] == new["id"]
            assert service.devices.outputs["mock_a"] == {"level": 75}
    asyncio.run(scenario())


def test_compile_failure_pauses_all_and_paused_conversation_cannot_compile(tmp_path):
    async def scenario():
        async with harness(tmp_path) as (service, clock, _):
            first = await start(service)
            await settle(service)
            async def invalid(messages):
                return '{"action":"replace","steps":[]}'
            service.models.compile_hook = invalid
            service.models.outputs.append("<toy1>新的安排</toy1>")
            await service.message(first["id"], owner_id="owner1", text="调整")
            await settle(service)
            assert service.active.row["status"] == "paused"
            assert all(not value for value in service.devices.outputs.values())
            calls = len(service.models.compilations)
            service.models.outputs.append("结束了。<toy1>不编译这一条</toy1><scene_end/>")
            await service.message(first["id"], owner_id="owner1", text="先聊两句")
            await settle(service)
            assert service.active.row["status"] == "paused" and service.active.row["phase"] == "aftercare"
            assert len(service.models.compilations) == calls and not any(service.devices.outputs.values())
    asyncio.run(scenario())


def test_feature_disable_control_conflict_and_retention(tmp_path):
    async def scenario():
        busy = True
        async def legacy():
            return busy
        async with harness(tmp_path, control_check=legacy) as (service, clock, _):
            with pytest.raises(SceneConflict, match="control_busy"):
                await start(service)
            busy = False
            session = await start(service)
            await settle(service)
            with pytest.raises(SceneConflict, match="control_busy"):
                await start(service, request_id="other")
            config = service.config.load()
            await service.configure({**config, "enabled": False})
            assert service.active is None
            clock.advance(1801)
            await service.tick()
            assert session["id"] not in service.retained
            result = await service.get(session["id"])
            assert result["messages"] == [] and result["has_card"] and "card" not in result
            result = await service.action(session["id"], owner_id="owner1", action="finish")
            assert result["card"]["raw_text"] == CARD
    asyncio.run(scenario())


def test_feedback_revision_race_failure_retry_and_restart(tmp_path):
    async def scenario():
        async with harness(tmp_path) as (service, _, _):
            session = await start(service)
            await settle(service)
            await service.action(session["id"], owner_id="owner1", action="finish")
            gate, entered = asyncio.Event(), asyncio.Event()
            async def note(scope, messages):
                if "旧评价" in messages[1]["content"]:
                    entered.set()
                    await gate.wait()
                    return "<notes>过期笔记</notes>"
                return "<notes>最新笔记</notes>"
            service.models.once_hook = note
            await service.feedback(session["id"], rating="up", reason="旧评价")
            await entered.wait()
            await service.feedback(session["id"], rating="down", reason="新评价")
            gate.set()
            await settle(service)
            assert (await service.repo.notes())["content"] == "最新笔记"
            assert (await service.repo.feedback(session["id"]))["processed_version"] == 2
            service.models.once_hook = None
            service.models.note_output = "malformed"
            await service.feedback(session["id"], rating=None, reason="仅原因")
            await settle(service)
            assert (await service.repo.feedback(session["id"]))["note_status"] == "failed"
            assert (await service.repo.notes())["content"] == "最新笔记"
            service.models.note_output = "<keep/>"
            await service.feedback(session["id"], rating=None, reason="仅原因")
            await settle(service)
            assert (await service.repo.feedback(session["id"]))["note_status"] == "processed"
            await service.repo.save_feedback(session["id"], rating=None, reason="等待中的反馈")
            pending = await service.repo.create_session(request_id="restart", conv_id="c", model_key="m", owner_id="o", devices=["mock_a"])
            assert await service.repo.recover() == 1
            assert (await service.repo.session(pending["id"]))["reason"] == "server_restart"
            assert (await service.repo.feedback(session["id"]))["note_status"] == "failed"
    asyncio.run(scenario())


def test_http_two_device_simulation_and_permission_boundaries(tmp_path):
    import httpx
    from fastapi import FastAPI
    from routes.scenes import router, get_scene_service
    async def scenario():
        async with harness(tmp_path) as (service, clock, _):
            app = FastAPI()
            app.include_router(router)
            app.dependency_overrides[get_scene_service] = lambda: service
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://test") as client:
                body = dict(request_id="http", conv_id="c", owner_id="owner1", device_ids=["mock_a", "mock_b"], safeword="stop-now")
                assert (await client.post('/api/scenes/sessions', json={**body, 'device_ids':['unknown']})).status_code == 409
                response = await client.post('/api/scenes/sessions', json=body)
                assert response.status_code == 200 and "card" not in response.json()
                sid = response.json()["id"]
                await settle(service)
                assert "card" not in (await client.get('/api/scenes/sessions/current')).json()
                assert (await client.get('/api/scenes/sessions')).json() == []
                assert (await client.post(f'/api/scenes/sessions/{sid}/pause', json={'owner_id':'intruder'})).status_code == 403
                clock.advance(181)
                await client.post(f'/api/scenes/sessions/{sid}/heartbeat', json={'owner_id':'owner1'})
                await service.tick()
                await settle(service)
                assert len(service.models.turns) == 2
                await client.post(f'/api/scenes/sessions/{sid}/messages', json={'owner_id':'owner1','text':'普通反馈'})
                await settle(service)
                await client.post('/api/scenes/panic')
                stopped = (await client.get(f'/api/scenes/sessions/{sid}')).json()
                assert stopped['status'] == 'paused' and 'card' not in stopped
                assert (await client.put(f'/api/scenes/sessions/{sid}/feedback', json={'rating':'up'})).status_code == 409
                await client.post(f'/api/scenes/sessions/{sid}/finish', json={'owner_id':'owner1'})
                ended = (await client.get(f'/api/scenes/sessions/{sid}')).json()
                assert ended['status'] == 'ended' and ended['card']['raw_text'] == CARD
                assert (await client.get('/api/scenes/sessions')).json()[0]['id'] == sid
                assert (await client.put(f'/api/scenes/sessions/{sid}/feedback', json={'rating':'up'})).status_code == 200
                await settle(service)
                assert (await service.repo.notes())["content"]
                assert service.devices.outputs == {"mock_a": {}, "mock_b": {}}
    asyncio.run(scenario())


def test_autonomous_simulation_from_night_card_through_end_and_future_notes(tmp_path):
    """Offline acceptance: autonomous wind-down; only the owner reveals."""
    async def scenario():
        async with harness(tmp_path) as (service, clock, events):
            cards = await service.night_cards(conv_id="conv1", model_key="test-core")
            service.models.outputs.extend(["<toy1>换一个节奏</toy1>", "收束。<scene_end/>"])
            initial = await start(service)
            await settle(service)
            assert service.active.card["id"] == cards[0]["id"]
            rt = service.active
            b_started = rt.player.started["mock_b"]
            for _ in range(12):
                clock.advance(15)
                await service.heartbeat(initial["id"], owner_id="owner1")
                await service.tick()
            await settle(service)
            assert len(service.models.turns) == 2 and len(service.models.compilations) == 3
            assert rt.player.started["mock_b"] == b_started
            for _ in range(32):
                clock.advance(15)
                await service.heartbeat(initial["id"], owner_id="owner1")
                await service.tick()
            await settle(service)
            winding_down = await service.get(initial["id"])
            assert winding_down["status"] == "active" and winding_down["phase"] == "aftercare"
            assert "card" not in winding_down and service.active is rt
            ended = await service.action(initial["id"], owner_id="owner1", action="finish")
            assert ended["card"]["raw_text"] == CARD and service.active is None
            assert service.devices.outputs == {"mock_a": {}, "mock_b": {}}
            assert all(m["role"] != "user" for m in ended["messages"])
            assert len(service.models.turns) == 3 and len(service.models.compilations) == 3
            await service.feedback(initial["id"], rating="up", reason="")
            await settle(service)
            await service.night_cards(conv_id="conv1", model_key="test-core")
            assert "留一点变化的余地" in str(service.models.once_calls[-1])
            assert "留一点变化的余地" not in str(service.models.turns)
            wire = json.dumps(events)
            assert "PRIVATE_CONTROL" not in wire and "SECOND_CONTROL" not in wire
            assert "messages" not in (await service.history())[0]
    asyncio.run(scenario())


def test_main_app_scene_route_auth_startup_and_chat_isolation(tmp_path, monkeypatch):
    import database
    import httpx
    import main
    from app.scenes import session as scene_module
    from routes.scenes import get_scene_service

    monkeypatch.setattr(database, "DB_PATH", tmp_path / "full-app.db")
    monkeypatch.setattr(main, "_AUTH_TOKEN", "test-scene-auth")
    clock = Clock()
    cfg = SceneConfig(tmp_path / "app-scenes.json")
    cfg.save({**defaults(), "enabled": True})

    async def identity():
        return IDENTITY

    async def broadcast(event):
        assert event["type"].startswith("scene_")

    async def scenario():
        service = SceneService(config=cfg, models=Models(), identity_loader=identity,
            now=clock, broadcast=broadcast)
        monkeypatch.setattr(scene_module, "_service", service)
        main.app.dependency_overrides[get_scene_service] = lambda: service
        try:
            async with main.lifespan(main.app):
                async with database.get_db() as db:
                    await db.execute("INSERT INTO conversations(id,title,model,created_at,updated_at) VALUES(?,?,?,?,?)", ("scene-conv", "test", "test-core", clock(), clock()))
                    await db.commit()
                async with httpx.AsyncClient(transport=httpx.ASGITransport(main.app), base_url="http://test") as client:
                    assert (await client.get('/api/scenes/config')).status_code == 401
                    client.headers['Authorization'] = 'Bearer test-scene-auth'
                    page = await client.get('/scenes')
                    assert page.status_code == 200 and page.headers['cache-control'] == 'no-store'
                    for key in ('mock_a', 'mock_b'):
                        assert (await client.post(f'/api/scenes/devices/{key}/connect')).status_code == 200
                    response = await client.post('/api/scenes/sessions', json={
                        'request_id':'app-test', 'conv_id':'scene-conv', 'owner_id':'owner1',
                        'device_ids':['mock_a','mock_b'], 'safeword':'stop-now'})
                    assert response.status_code == 200
                    await settle(service)
                    assert (await client.get('/api/scenes/sessions/current')).json()['status'] == 'active'
                    legacy = await client.post('/api/control/sessions/start', json={
                        'conv_id':'scene-conv','owner_client_id':'owner1','kind':'dom','safeword_set':True})
                    assert legacy.status_code == 409
                    assert (await client.get('/api/conversations')).status_code == 200
                    await client.post('/api/scenes/panic')
                    assert service.occupies_control()  # Conversation still owns this scene.
                    await client.post(f'/api/scenes/sessions/{response.json()["id"]}/finish', json={'owner_id':'owner1'})
                    assert not service.occupies_control()
                async with database.get_db() as db:
                    assert (await (await db.execute('SELECT COUNT(*) FROM messages')).fetchone())[0] == 0
                    assert (await (await db.execute('SELECT COUNT(*) FROM memory_events')).fetchone())[0] == 0
        finally:
            main.app.dependency_overrides.pop(get_scene_service, None)
            await service.close()

    asyncio.run(scenario())
