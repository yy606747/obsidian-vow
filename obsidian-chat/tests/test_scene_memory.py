import asyncio
import json

import pytest

from app.scenes.experience import Experience, MAX_EVIDENCE_CHARS, MAX_ITEMS, TTL_SECONDS
from app.scenes.protocol import SceneOutput
from app.scenes.recent import load_recent, render_recent
from test_scenes import CARD, IDENTITY, harness, settle, start


async def recent(service, clock):
    return await load_recent(user_name=IDENTITY.user_name, ai_name=IDENTITY.ai_name,
                             repository=service.repo, now=clock(), timezone="Asia/Shanghai")


def spy_writer(service):
    calls = []

    async def write(content, **kwargs):
        assert not service.lock.locked()
        calls.append((content, kwargs))
        return {"id": "memory_" + kwargs["idempotency_key"]}

    service.memory.writer = write
    return calls


def test_remember_is_hidden_at_every_boundary_and_can_accompany_end():
    text = "前<remember>PRIVATE_NOTE</remember><toy1>PRIVATE_DEVICE</toy1>后<remember>第二条</remember><scene_end/>"
    for split in range(len(text)+1):
        parser = SceneOutput(["toy1"])
        assert parser.feed(text[:split]) + parser.feed(text[split:]) == "前后"
        result = parser.finish()
        assert result["notes"] == ["PRIVATE_NOTE", "第二条"]
        assert result["ended"] and result["intents"] == {"toy1": "PRIVATE_DEVICE"}
    parser = SceneOutput([])
    assert "".join(parser.feed(char) for char in text.replace("<toy1>PRIVATE_DEVICE</toy1>", "")) == "前后"


@pytest.mark.parametrize("tail", [
    "<remember>PRIVATE", "<remember>PRIVATE<toy1>BAD</remember>LEAK</toy1>",
    "<toy1>PRIVATE<remember>BAD</toy1>LEAK</remember>", "<remember></remember>LEAK",
    "<remember>" + "x"*4001 + "</remember>LEAK",
])
def test_invalid_note_structure_hides_the_tail_and_rejects_whole_turn(tail):
    parser = SceneOutput(["toy1"])
    assert "".join(parser.feed(char) for char in "可见"+tail) == "可见"
    with pytest.raises(ValueError, match="scene_output_protocol_invalid"):
        parser.finish()


def test_accepted_notes_use_existing_memory_and_wind_down_notes_are_not_lost(tmp_path):
    async def scenario():
        async with harness(tmp_path) as (service, clock, events):
            calls = spy_writer(service)
            service.models.outputs.clear()
            service.models.outputs.append("开场。<remember>PRIVATE_NOTE_1</remember><toy1>安排</toy1>")
            first = await start(service)
            await settle(service)
            assert len(calls) == 1 and calls[0][0] == "PRIVATE_NOTE_1"
            clock.advance(2)
            service.models.outputs.append("到这里。<remember>PRIVATE_NOTE_2</remember><scene_end/>")
            await service.message(first["id"], owner_id="owner1", text="就到这里吧")
            await settle(service)
            assert service.active.row["phase"] == "aftercare" and not any(service.devices.outputs.values())
            assert await recent(service, clock) == ""  # Not a finished experience yet.
            assert [call[0] for call in calls] == ["PRIVATE_NOTE_1", "PRIVATE_NOTE_2"]
            assert len(service.models.compilations) == 1
            for _, kwargs in calls:
                assert kwargs["source_conv"] == "conv1" and kwargs["source_start_ts"] == 1000
                assert kwargs["importance"] == 0.6
                assert kwargs["idempotency_key"].startswith("scene_remember_")
            await service.memory.drain()
            assert len(calls) == 2
            wire = json.dumps(events) + json.dumps(await service.get(first["id"]))
            assert "PRIVATE_NOTE" not in wire and "<remember>" not in wire
            async with service.repo.db() as db:
                rows = await (await db.execute("SELECT status,content FROM scene_memory_writes")).fetchall()
                assert rows == [("done", ""), ("done", "")]
                for table in ("messages", "memory_events"):
                    assert (await (await db.execute(f"SELECT count(*) FROM {table}")).fetchone())[0] == 0
            assert (await service.repo.notes())["content"] == ""
    asyncio.run(scenario())


def test_immediate_handoff_precedes_summary_and_no_notes_means_no_long_term_write(tmp_path):
    async def scenario():
        async with harness(tmp_path) as (service, clock, _):
            calls = spy_writer(service)
            first = await start(service)
            await settle(service)
            await service.message(first["id"], owner_id="owner1", text="最后这段我想聊聊")
            await settle(service)
            entered, gate = asyncio.Event(), asyncio.Event()

            async def summary(scope, messages):
                assert scope == "recent" and not service.lock.locked()
                assert "最后这段我想聊聊" in str(messages)
                assert "PRIVATE_CONTROL" in str(messages)  # Adopted intent, not the unexecuted card.
                assert "隐藏卡名" not in str(messages) and "<toy1>" not in str(messages)
                assert "WORKING_ONLY" not in str(messages)
                entered.set()
                await gate.wait()
                return "阿澈记得收尾时，小栀说想聊聊最后那段。"

            service.models.once_hook = summary
            await asyncio.wait_for(service.action(first["id"], owner_id="owner1", action="finish"), 1)
            await asyncio.wait_for(entered.wait(), 1)
            assert not any(service.devices.outputs.values())
            pending = await recent(service, clock)
            assert "最后这段我想聊聊" in pending and "小栀主动结束" in pending
            assert "模拟播放器采用" in pending and "+08:00" in pending
            assert "隐藏卡名" not in pending
            gate.set()
            await settle(service)
            assert "阿澈记得收尾时" in await recent(service, clock)
            assert calls == [] and await service.repo.pending_memories() == []
            assert (await service.repo.notes())["content"] == ""
    asyncio.run(scenario())


@pytest.mark.parametrize("bad_summary", ["<remember>不能执行</remember>", "", "x"*2001])
def test_invalid_summary_keeps_facts_and_cannot_write_memory(tmp_path, bad_summary):
    async def scenario():
        async with harness(tmp_path) as (service, clock, _):
            calls = spy_writer(service)
            service.models.recent_output = bad_summary
            first = await start(service)
            await settle(service)
            await service.message(first["id"], owner_id="owner1", text="stop-now，今天先到这里")
            await settle(service)
            assert await recent(service, clock) == ""
            await service.action(first["id"], owner_id="owner1", action="finish")
            await settle(service)
            text = await recent(service, clock)
            assert "使用安全词停下设备" in text and "今天先到这里" in text
            rows = await service.repo.recent(now=clock())
            assert rows[0]["summary_status"] == "failed"
            assert not calls and not any(service.devices.outputs.values())
            count = len(service.models.once_calls)
            await service.tick()
            await settle(service)
            assert len(service.models.once_calls) == count
    asyncio.run(scenario())


@pytest.mark.parametrize("action", ["pause", "finish"])
def test_stale_partial_note_is_never_saved(tmp_path, action):
    async def scenario():
        async with harness(tmp_path) as (service, clock, events):
            calls = spy_writer(service)
            entered, gate = asyncio.Event(), asyncio.Event()

            async def stream(messages):
                yield "已经说出的半句<remember>不能落盘"
                entered.set()
                try:
                    await gate.wait()
                except asyncio.CancelledError:
                    await gate.wait()
                yield "</remember><toy1>迟到指令</toy1>"

            service.models.turn_hook = stream
            first = await start(service)
            await entered.wait()
            await service.action(first["id"], owner_id="owner1", action=action)
            if action == "finish":
                assert "已经说出的半句" in await recent(service, clock)
            gate.set()
            await settle(service)
            assert calls == [] and not service.models.compilations
            assert "不能落盘" not in json.dumps(events, ensure_ascii=False)
            assert "迟到指令" not in str(await service.repo.recent(now=clock()))
    asyncio.run(scenario())


def test_slow_memory_write_cannot_block_stop_and_failure_cannot_pause_scene(tmp_path):
    async def scenario():
        async with harness(tmp_path) as (service, clock, _):
            entered, gate = asyncio.Event(), asyncio.Event()

            async def writer(content, **kwargs):
                assert not service.lock.locked()
                entered.set()
                await gate.wait()
                raise RuntimeError("PRIVATE_PROVIDER_ERROR")

            service.memory.writer = writer
            service.models.outputs.clear()
            service.models.outputs.append("<remember>主动记下</remember><toy1>安排</toy1>")
            first = await start(service)
            await entered.wait()
            rt = service.active
            if rt.worker:
                await rt.worker
            await asyncio.gather(*rt.compilers.values())
            assert any(service.devices.outputs.values()) and rt.row["status"] == "active"
            await asyncio.wait_for(service.panic(), 1)
            assert not any(service.devices.outputs.values())
            gate.set()
            await settle(service)
            async with service.repo.db() as db:
                row = await (await db.execute("SELECT status,error_type FROM scene_memory_writes")).fetchone()
                assert row == ("failed", "RuntimeError")
            assert (await service.get(first["id"]))["reason"] == "panic"
    asyncio.run(scenario())


@pytest.mark.parametrize("provider_failed", [False, True])
def test_invalid_or_truncated_turn_does_not_commit_an_otherwise_complete_note(tmp_path, provider_failed):
    async def scenario():
        async with harness(tmp_path) as (service, _, _):
            calls = spy_writer(service)

            async def stream(messages):
                yield "已经显示<remember>不应保存</remember>"
                if provider_failed:
                    raise ValueError("scene_model_incomplete")
                yield "<toy1>未闭合"

            service.models.turn_hook = stream
            await start(service)
            await settle(service)
            assert service.active.row["status"] == "paused"
            assert calls == [] and not service.models.compilations
            assert not await service.repo.pending_memories()
    asyncio.run(scenario())


def test_native_arrangements_require_adoption_and_polling_does_not_duplicate_evidence(tmp_path):
    from app.scenes.hardware import devices
    from test_scene_hardware import hardware_models

    async def scenario():
        async with harness(tmp_path, models=hardware_models()) as (service, clock, _):
            await service.configure({"enabled": True, "devices": devices(bench=True)})
            await service.report_bridge(owner_id="owner1", connected=["muse", "sosexy"])
            await start(service, devices=["muse", "sosexy"])
            await settle(service)
            rt = service.active
            assert all(item["kind"] != "device" for item in rt.experience.items)
            params = dict(owner_id="owner1", generation=rt.row["generation"], connected=["muse", "sosexy"])
            first = await service.output(rt.row["id"], **params, adopted={})
            muse = first["devices"][0]
            for _ in range(3):
                await service.output(rt.row["id"], **params,
                    adopted={muse["id"]: {"version": muse["version"], "elapsed_ms": 100}})
            changes = [item for item in rt.experience.items if item["kind"] == "device"]
            assert len(changes) == 1 and not changes[0]["simulated"]
            assert changes[0]["content"] == "PRIVATE_CONTROL"
            service.models.recent_output = ""  # Render the actual factual fallback.
            await service.action(rt.row["id"], owner_id="owner1", action="finish")
            await settle(service)
            text = await recent(service, clock)
            assert "手机确认采用" in text and "PRIVATE_CONTROL" in text
            assert "SECOND_CONTROL" not in text
    asyncio.run(scenario())


def test_recovery_replays_only_accepted_notes_through_ai_note_writer(tmp_path, monkeypatch):
    from app.memory_v2 import memory_service
    from app.provider_privacy import private_provider_scope
    from ws import manager
    calls, notifications = [], []

    async def create_memory(content, kind, **kwargs):
        assert private_provider_scope.get() == "scene:remember"
        calls.append((content, kind, kwargs))
        return {"id": "saved-note", "content": content, "deduplicated": False}

    async def broadcast(event):
        notifications.append(event)

    monkeypatch.setattr(memory_service, "create_memory", create_memory)
    monkeypatch.setattr(manager, "broadcast", broadcast)

    async def scenario():
        async with harness(tmp_path) as (service, clock, _):
            row = await service.repo.create_session(request_id="recover", conv_id="conv1", model_key="m",
                                                    owner_id="owner1", devices=["mock_a"])
            card = await service.repo.add_card(CARD, ["mock_a"], source="entry", conv_id="conv1", model_key="m")
            await service.repo.enter(row["id"], card, {"toy1": "mock_a"}, generation=0)
            await service.repo.queue_memories(row["id"], "turn", ["主动留下的内容"], created_at=clock())
            await service.repo.claim_memory("scene_remember_turn_0")
            assert await service.recover() == 1
            await settle(service)
            assert calls == [("主动留下的内容", "ai_note", dict(source_conv="conv1", source_start_ts=1000,
                source_end_ts=1000, importance=0.6, idempotency_key="scene_remember_turn_0"))]
            assert notifications[0]["type"] == "memory_added"
            await service.repo.queue_memories(row["id"], "turn", ["主动留下的内容"], created_at=clock())
            await service.recover()
            await settle(service)
            assert len(calls) == 1
            text = await recent(service, clock)
            assert "服务重启中断" in text and "没有留下过程细节" in text
            assert "隐藏卡名" not in text and not service.models.once_calls
    asyncio.run(scenario())


def test_short_term_injection_is_dynamic_only_and_failure_is_optional(monkeypatch):
    from app.scenes import recent as module

    async def scenario():
        async def source(**names):
            return "【近期亲密经历】已经结束。"
        monkeypatch.setattr(module, "load_recent", source)
        history = [{"role": "system", "content": "稳定前缀"}, {"role": "user", "content": "刚才呢"}]
        await module.inject_recent(history, user_name="小栀", ai_name="阿澈")
        assert history[0]["content"] == "稳定前缀" and len(history) == 4
        assert history[2]["content"] == "【近期亲密经历】已经结束。"

        async def unavailable(**names):
            raise RuntimeError("unavailable")
        monkeypatch.setattr(module, "load_recent", unavailable)
        before = list(history)
        await module.inject_recent(history, user_name="小栀", ai_name="阿澈")
        assert history == before
    asyncio.run(scenario())


def test_summary_expiry_feedback_and_restart_are_independent_of_long_term_notes(tmp_path):
    async def scenario():
        async with harness(tmp_path) as (service, clock, _):
            calls = spy_writer(service)
            first = await start(service)
            await settle(service)
            await service.action(first["id"], owner_id="owner1", action="finish")
            await settle(service)
            await service.feedback(first["id"], rating="up", reason="")
            await settle(service)
            text = await recent(service, clock)
            assert "点赞；未填写原因" in text
            assert "留一点变化的余地" not in text  # The creative notebook stays separate.
            await service.feedback(first["id"], rating="down", reason="节奏变得太快")
            await settle(service)
            assert "点踩；原因原话：节奏变得太快" in await recent(service, clock)
            await service.recover()
            await settle(service)
            assert "节奏变得太快" in await recent(service, clock)
            clock.advance(TTL_SECONDS)
            assert await recent(service, clock) == ""
            await service.tick()
            await settle(service)
            async with service.repo.db() as db:
                assert (await (await db.execute("SELECT count(*) FROM scene_recent")).fetchone())[0] == 0
            assert calls == [] and (await service.get(first["id"]))["card"]["raw_text"] == CARD
    asyncio.run(scenario())


def test_experience_is_bounded_retains_opening_and_last_words_and_copies_snapshot():
    evidence = Experience()
    message = {"id": "opening", "at": 1, "content": "开场原话"}
    evidence.message(message, speaker="阿澈")
    for index in range(250):
        evidence.device(now=index+2, name="模拟设备", intent="x"*2000, simulated=True, action="replace")
    last = {"id": "last", "at": 255, "content": "最后半句"}
    evidence.message(last, speaker="小栀")
    last["content"] += "也要留下"
    snapshot = evidence.snapshot(user_name="小栀", ai_name="阿澈")
    assert len(evidence.items) <= MAX_ITEMS and snapshot["omitted"] > 0
    assert sum(len(str(item)) for item in snapshot["items"]) <= MAX_EVIDENCE_CHARS
    assert snapshot["items"][0]["content"] == "开场原话"
    assert snapshot["items"][-1]["content"] == "最后半句也要留下"
    last["content"] = "后来变化"
    assert snapshot["items"][-1]["content"] != last["content"]


def test_recent_only_includes_last_three_ended_nonexpired_scenes():
    now = 100000
    rows = [{"id": str(i), "status": "ended", "started_at": now-100, "ended_at": now-i,
             "expires_at": now+100, "reason": "model_ended", "evidence": {}, "summary": f"第{i}场"}
            for i in range(1, 6)]
    rows += [{**rows[0], "id": "active", "status": "active", "summary": "还在进行"},
             {**rows[0], "id": "expired", "expires_at": now, "summary": "已经过期"},
             {**rows[0], "id": "future", "ended_at": now+1, "summary": "未来结束"}]
    text = render_recent(rows, user_name="小栀", ai_name="阿澈", now=now)
    assert all(f"第{i}场" in text for i in (1, 2, 3))
    assert all(value not in text for value in ("第4场", "第5场", "还在进行", "已经过期", "未来结束"))
