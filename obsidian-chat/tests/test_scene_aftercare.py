"""Wind-down is not reveal, and talking after a stop is not authorization."""
import asyncio
import json

import aiosqlite
import pytest

from app.scenes.config import DEFAULT_DEVICES
from app.scenes.hardware import devices
from app.scenes.prompts import turn_messages
from app.scenes.schema import init_scene_tables
from app.scenes.session import SceneConflict, SceneOwnerMismatch
from test_scene_hardware import hardware_models
from test_scene_memory import recent, spy_writer
from test_scenes import CARD, IDENTITY, Models, harness, settle, start


def test_wind_down_preserves_conversation_authorization_and_only_finish_reveals(tmp_path):
    async def scenario():
        async with harness(tmp_path) as (service, clock, events):
            notes = spy_writer(service)
            initial = await start(service)
            await settle(service)
            rt = service.active
            generation = rt.row["generation"]
            service.models.outputs.append("先到这里。<scene_end/><toy2>新安排</toy2><self_awake>180</self_awake>")
            await service.message(initial["id"], owner_id="owner1", text="我缴械了")
            await settle(service)
            current = await service.current()
            assert current["status"] == "active" and current["phase"] == "aftercare"
            assert current["generation"] == generation and current["ended_at"] is None
            assert "card" not in current and current["messages"][-1]["content"] == "先到这里。"
            assert current["next_wake_at"] == clock() + 180
            assert not service.devices.outputs["mock_a"] and service.devices.outputs["mock_b"]
            assert json.loads(service.models.compilations[-1][1]["content"])["intent"] == "新安排"
            assert await recent(service, clock) == ""
            assert not await service.history()
            with pytest.raises(SceneConflict):
                await service.feedback(initial["id"], rating="up", reason="")

            versions = dict(rt.player.versions)
            started = dict(rt.player.started)
            service.models.outputs.append("还在这里。<scene_end/>")
            await service.message(initial["id"], owner_id="owner1", text="再说两句")
            await settle(service)
            assert rt.player.versions == versions and rt.player.started == started
            assert service.devices.outputs["mock_b"]  # Repeated marker is inert.
            assert await recent(service, clock) == ""

            service.models.outputs.append("<toy1>接下来的新安排</toy1><remember>收尾时还聊了两句。</remember>")
            # No reply is required: the 8-minute fallback still works here.
            clock.advance(481)
            await service.heartbeat(initial["id"], owner_id="owner1")
            await service.tick()
            await settle(service)
            assert service.devices.outputs["mock_a"] and service.devices.outputs["mock_b"]
            assert notes[0][0] == "收尾时还聊了两句。"
            assert rt.player.started["mock_b"] == started["mock_b"]
            assert "此前的安排已经收束" in str(service.models.turns[-1])
            assert "还在这里。" in str(service.models.turns[-1])
            assert "PRIVATE_CONTROL" not in json.dumps(events)

            service.models.recent_output = ""  # Inspect factual evidence, not a synthetic summary.
            final = await service.action(initial["id"], owner_id="owner1", action="finish")
            await settle(service)
            assert final["status"] == "ended" and final["card"]["raw_text"] == CARD
            assert final["revealed_at"] == clock() and service.active is None
            assert any(m["content"] == "还在这里。" for m in final["messages"])
            assert not any(service.devices.outputs.values()) and rt.due_at is None
            text = await recent(service, clock)
            assert "收束此前安排，交流仍在继续" in text and "再说两句" in text
            assert "隐藏卡名" not in text and "小栀主动结束" in text
            count = len(service.models.turns)
            clock.advance(600)
            await service.tick()
            await settle(service)
            assert len(service.models.turns) == count
    asyncio.run(scenario())


@pytest.mark.parametrize("stale_fails", [False, True])
def test_wind_down_fences_noncooperative_old_compiler_without_revoking_native_generation(tmp_path, stale_fails):
    async def scenario():
        models = Models()
        entered, release = asyncio.Event(), asyncio.Event()

        async def compile(messages):
            payload = json.loads(messages[1]["content"])
            if payload["intent"] == "PRIVATE_CONTROL":
                entered.set()
                try:
                    await release.wait()
                except asyncio.CancelledError:
                    await release.wait()
                if stale_fails:
                    return "invalid"
            return json.dumps({"action": "replace", "steps": [{"duration_ms": 1000,
                "values": {key: 12 for key in payload["capabilities"]["channels"]}}]})

        models.compile_hook = compile
        async with harness(tmp_path, models=models) as (service, _, _):
            initial = await start(service)
            await entered.wait()
            rt = service.active
            generation = rt.row["generation"]
            models.outputs.append("收束。<scene_end/><toy2>全新安排</toy2>")
            await service.message(initial["id"], owner_id="owner1", text="先收束")
            await rt.worker
            await rt.compilers["mock_b"]
            assert not service.devices.outputs["mock_a"] and service.devices.outputs["mock_b"]
            assert rt.row["generation"] == generation
            release.set()
            await settle(service)
            assert not service.devices.outputs["mock_a"] and rt.row["status"] == "active"
            models.outputs.append("<toy1>后来重新安排</toy1>")
            await service.message(initial["id"], owner_id="owner1", text="还有呢")
            await settle(service)
            assert service.devices.outputs["mock_a"]
    asyncio.run(scenario())


@pytest.mark.parametrize("stop", ["pause", "end", "panic", "safeword"])
def test_safety_stop_keeps_chat_but_all_control_is_latched_until_explicit_resume(tmp_path, stop):
    async def scenario():
        async with harness(tmp_path) as (service, clock, _):
            notes = spy_writer(service)
            initial = await start(service)
            await settle(service)
            rt = service.active
            service.models.outputs.append("可以继续说。<scene_end/><toy1>不得执行</toy1><self_awake>1</self_awake><remember>停下后继续聊。</remember>")
            if stop == "safeword":
                await service.message(initial["id"], owner_id="owner1", text="stop-now")
            else:
                if stop == "panic":
                    await service.panic()
                else:
                    await service.action(initial["id"], owner_id="owner1", action=stop)
                await service.message(initial["id"], owner_id="owner1", text="陪我说两句")
            await settle(service)
            current = await service.current()
            assert current["status"] == "paused" and current["phase"] == "aftercare"
            assert current["messages"][-1]["content"] == "可以继续说。" and "card" not in current
            assert current["next_wake_at"] is None and not any(service.devices.outputs.values())
            assert len(service.models.compilations) == 2 and len(notes) == 1
            assert "设备保持暂停" in str(service.models.turns[-1])
            clock.advance(600)
            await service.heartbeat(initial["id"], owner_id="owner1")
            await service.connect("mock_a", True)
            await service.tick()
            await settle(service)
            assert len(service.models.turns) == 2 and not any(service.devices.outputs.values())
            assert await recent(service, clock) == ""
            service.models.outputs.append("<toy2>明确恢复后的安排</toy2>")
            await service.action(initial["id"], owner_id="owner1", action="resume")
            await settle(service)
            assert rt.row["status"] == "active" and rt.row["phase"] == "aftercare"
            assert not service.devices.outputs["mock_a"] and service.devices.outputs["mock_b"]
            # A delayed native stop cannot stop a newer explicit authorization.
            await service.action(initial["id"], owner_id="owner1", action="end", generation=initial["generation"])
            assert rt.row["status"] == "active"
    asyncio.run(scenario())


def test_native_wind_down_uses_sequence_versions_and_preserves_output_transport(tmp_path):
    async def scenario():
        models = hardware_models()
        async with harness(tmp_path, models=models) as (service, _, _):
            await service.configure({"enabled": True, "devices": devices(bench=True)})
            await service.report_bridge(owner_id="owner1", connected=["muse", "sosexy"])
            initial = await start(service, devices=["muse", "sosexy"])
            await settle(service)
            args = dict(owner_id="owner1", generation=initial["generation"], connected=["muse", "sosexy"])
            first = await service.output(initial["id"], **args, adopted={})
            old_ack = {d["id"]: {"version": d["version"], "elapsed_ms": 100} for d in first["devices"]}
            await service.output(initial["id"], **args, adopted=old_ack)
            models.outputs.append("<scene_end/><toy2>新的安排</toy2>")
            await service.message(initial["id"], owner_id="owner1", text="先收束")
            await settle(service)
            second = await service.output(initial["id"], **args, adopted=old_ack)
            assert second["generation"] == first["generation"] and second["status"] == "active"
            assert second["devices"][0]["sequence"] == {"action": "stop"}
            assert second["devices"][1]["sequence"]["action"] == "replace"
            assert all(new["version"] > old["version"] for old, new in zip(first["devices"], second["devices"]))
            assert all(not d["position"]["playing"] for d in (await service.current())["devices"])
            await service.output(initial["id"], **args, adopted={d["id"]: {"version": d["version"], "elapsed_ms": 0} for d in second["devices"]})
            assert [d["position"]["playing"] for d in (await service.current())["devices"]] == [False, True]
            models.outputs.append("<toy1>全新安排</toy1>")
            await service.message(initial["id"], owner_id="owner1", text="继续交流")
            await settle(service)
            assert (await service.output(initial["id"], **args, adopted={}))["devices"][0]["sequence"]["action"] == "replace"
            # Local stop races with the page's finish POST; reveal still succeeds.
            await service.action(initial["id"], owner_id="owner1", action="end", generation=args["generation"])
            final = await service.action(initial["id"], owner_id="owner1", action="finish", generation=args["generation"])
            assert final["card"]["raw_text"] == models.card_output and final["status"] == "ended"
            assert (await service.output(initial["id"], **args, adopted=old_ack))["devices"] == []
    asyncio.run(scenario())


def test_paused_reply_racing_resume_is_discarded_and_next_turn_is_serialized(tmp_path, monkeypatch):
    import ai_providers
    from app.scenes.models import SceneModels

    async def scenario():
        async with harness(tmp_path) as (service, _, _):
            notes = spy_writer(service)
            initial = await start(service)
            await settle(service)
            await service.action(initial["id"], owner_id="owner1", action="pause")
            entered, release = asyncio.Event(), asyncio.Event()
            live = peak = 0

            async def stream(messages, model_key, meta, **kwargs):
                nonlocal live, peak
                service.models.turns.append(messages)
                live += 1
                peak = max(peak, live)
                try:
                    if not entered.is_set():
                        entered.set()
                        try:
                            await release.wait()
                        except asyncio.CancelledError:
                            await release.wait()
                        yield "STALE_REPLY<scene_end/><toy1>STALE_INTENT</toy1><remember>STALE_NOTE</remember>"
                    else:
                        yield "新的回应。<toy2>新授权下的安排</toy2>"
                finally:
                    live -= 1

            monkeypatch.setattr(ai_providers, "stream_ai", stream)
            service.models.stream = SceneModels().stream
            await service.message(initial["id"], owner_id="owner1", text="暂停时说的话")
            await entered.wait()
            await service.action(initial["id"], owner_id="owner1", action="resume")
            await service.message(initial["id"], owner_id="owner1", text="恢复后补充的话")
            release.set()
            await settle(service)
            current = await service.current()
            assert peak == 1 and len(service.models.turns) == 3
            assert current["phase"] == "play" and current["status"] == "active"
            assert "STALE" not in str(current) and notes == []
            assert "暂停时说的话" in str(service.models.turns[-1]) and "恢复后补充的话" in str(service.models.turns[-1])
            assert current["messages"][-1]["content"] == "新的回应。"
            assert not service.devices.outputs["mock_a"] and service.devices.outputs["mock_b"]
            assert len(service.models.compilations) == 3
    asyncio.run(scenario())


@pytest.mark.parametrize("failure", ["truncated", "malformed"])
def test_only_complete_valid_model_turn_can_change_phase(tmp_path, failure):
    async def scenario():
        async with harness(tmp_path) as (service, _, _):
            initial = await start(service)
            await settle(service)

            async def incomplete(messages):
                yield "最后几句仍然看得到。<scene_end/><toy1>新意图</toy1>"
                if failure == "truncated":
                    raise ValueError("scene_model_incomplete")
                yield "<toy2>未闭合"

            service.models.turn_hook = incomplete
            await service.message(initial["id"], owner_id="owner1", text="说两句")
            await settle(service)
            result = await service.get(initial["id"])
            assert result["phase"] == "play" and result["status"] == "paused"
            assert "card" not in result and result["messages"][-1]["content"] == "最后几句仍然看得到。"
            assert len(service.models.compilations) == 2 and not any(service.devices.outputs.values())
    asyncio.run(scenario())


def test_plain_device_stop_does_not_change_phase_or_stop_other_device(tmp_path):
    async def scenario():
        async with harness(tmp_path) as (service, _, _):
            initial = await start(service)
            await settle(service)
            started = service.active.player.started["mock_b"]
            service.models.outputs.append("<toy1>停下这台</toy1>")

            async def stop(messages):
                return '{"action":"stop"}'

            service.models.compile_hook = stop
            await service.message(initial["id"], owner_id="owner1", text="调整第一台")
            await settle(service)
            assert service.active.row["phase"] == "play"
            assert not service.devices.outputs["mock_a"] and service.devices.outputs["mock_b"]
            assert service.active.player.started["mock_b"] == started
    asyncio.run(scenario())


def test_restart_keeps_card_blind_until_explicit_owner_finish_and_reveal_is_durable(tmp_path):
    async def scenario():
        async with harness(tmp_path) as (service, clock, _):
            initial = await start(service)
            await settle(service)
            await service.close()
            service.retained.clear()
            await service.recover()
            result = await service.get(initial["id"])
            assert result["status"] == "ended" and "card" not in result
            assert (await service.history())[0]["card"] is None
            with pytest.raises(SceneOwnerMismatch):
                await service.action(initial["id"], owner_id="another", action="finish")
            with pytest.raises(SceneConflict):
                await service.feedback(initial["id"], rating="up", reason="")
            final = await service.action(initial["id"], owner_id="owner1", action="finish")
            assert final["card"]["raw_text"] == CARD and final["messages"] == []
            assert final["generation"] > result["generation"]
            clock.advance(10)
            again = await service.action(initial["id"], owner_id="owner1", action="finish")
            assert again["revealed_at"] == final["revealed_at"] and again["generation"] == final["generation"]
            assert (await service.history())[0]["card"]["raw_text"] == CARD
    asyncio.run(scenario())


def test_schema_upgrade_preserves_existing_reveals_but_not_new_interrupted_scenes(tmp_path):
    async def scenario():
        async with aiosqlite.connect(tmp_path / "old.db") as db:
            await db.execute("""CREATE TABLE scene_sessions (
                id TEXT PRIMARY KEY, status TEXT, created_at REAL, ended_at REAL)""")
            await db.execute("INSERT INTO scene_sessions VALUES ('old','ended',1,2)")
            await init_scene_tables(db)
            assert await (await db.execute("SELECT phase,revealed_at FROM scene_sessions")).fetchone() == ("play", 2)
            await db.execute("INSERT INTO scene_sessions(id,status,created_at,ended_at) VALUES ('new','ended',3,4)")
            await init_scene_tables(db)
            assert await (await db.execute("SELECT revealed_at FROM scene_sessions WHERE id='new'")).fetchone() == (None,)
    asyncio.run(scenario())


def test_prompt_describes_output_semantics_without_prescribing_aftercare():
    messages = turn_messages(**vars(IDENTITY), card=CARD,
        devices=[{**DEFAULT_DEVICES[0], "label": "toy1"}], history=[], now=1100, started_at=1000,
        last_wake_at=1050, arrangements="已停止", trigger="收到新反馈", phase="aftercare", paused=True)
    system, state = [message["content"] for message in messages]
    assert system.startswith("你是阿澈。")
    assert "什么时候真正结束并揭卡，由小栀在页面上决定" in system
    assert "再次写这个标签不会重复清空安排" in system
    assert "后续如何继续，由阿澈结合当下互动自己决定" in system
    assert "aftercare" not in system.lower() and "安抚" not in system
    assert "此前的安排已经收束" in state and "小栀尚未手动恢复设备" in state
