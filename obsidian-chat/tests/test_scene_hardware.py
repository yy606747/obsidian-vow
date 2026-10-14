import asyncio
from copy import deepcopy
import json

import pytest

from app.scenes.compiler import validate_sequence
from app.scenes.config import validate_config
from app.scenes.hardware import devices
from app.scenes.session import SceneConflict, SceneOwnerMismatch
from test_scenes import harness, Models, settle, start


def test_real_profiles_are_opt_in_bounded_and_not_linear_muse_intensity():
    profiles = devices(bench=True, single_channel=True)
    value = {"enabled": True, "devices": profiles}
    assert validate_config(value) == value
    assert profiles[0]["capabilities"]["channels"]["thrust_pattern"]["max"] == 0
    assert profiles[1]["capabilities"]["channels"]["electro_speed"]["max"] == 0
    assert "离散" in profiles[0]["capabilities"]["description"]
    invalid = deepcopy(value)
    invalid["devices"][0]["capabilities"]["channels"]["vib_pattern"]["max"] = 10
    with pytest.raises(ValueError, match="hardware_range"):
        validate_config(invalid)
    invalid = deepcopy(value)
    invalid["devices"].append({**deepcopy(profiles[0]), "id": "second_muse"})
    with pytest.raises(ValueError, match="transport_already_used"):
        validate_config(invalid)


def test_compiler_enforces_native_integer_channels_and_transport_timing():
    caps = devices()[0]["capabilities"]
    sequence = {"action": "replace", "steps": [{"duration_ms": 5000, "values": {"vib_pattern": 1, "thrust_pattern": 0}}]}
    assert validate_sequence(sequence, caps) == sequence
    sequence["steps"][0]["values"]["vib_pattern"] = 1.5
    with pytest.raises(ValueError, match="integer"):
        validate_sequence(sequence, caps)
    sequence["steps"][0]["values"]["vib_pattern"] = 1
    sequence["steps"][0]["duration_ms"] = 1000
    with pytest.raises(ValueError, match="duration"):
        validate_sequence(sequence, caps)


def hardware_models():
    models = Models()
    models.card_output = "## 卡1：桌面\n只验证控制通道。\n<devices>muse,sosexy</devices>"
    async def compile(messages):
        caps = json.loads(messages[1]["content"])["capabilities"]
        return json.dumps({"action": "replace", "steps": [{"duration_ms": caps["min_step_ms"],
            "values": {key: bounds["max"] for key, bounds in caps["channels"].items()}}]})
    models.compile_hook = compile
    return models


def test_bridge_is_owner_scoped_and_mock_connect_cannot_fake_real_readiness(tmp_path):
    async def scenario():
        async with harness(tmp_path, models=hardware_models()) as (service, clock, _):
            await service.configure({"enabled": True, "devices": devices(bench=True)})
            with pytest.raises(SceneConflict, match="native_bridge_required"):
                await service.connect("muse")
            with pytest.raises(SceneConflict, match="device_unavailable"):
                await start(service, devices=["muse", "sosexy"])
            await service.report_bridge(owner_id="other", connected=["muse", "sosexy"])
            with pytest.raises(SceneConflict, match="native_bridge_required"):
                await start(service, devices=["muse", "sosexy"])
            await service.report_bridge(owner_id="owner1", connected=["muse", "sosexy"])
            clock.advance(21)
            assert not any(d["connected"] for d in service.device_list())
            with pytest.raises(SceneConflict, match="native_bridge_required"):
                await start(service, devices=["muse", "sosexy"])
    asyncio.run(scenario())


def test_two_device_real_transport_independent_replacement_ack_and_stop_fences(tmp_path):
    async def scenario():
        models = hardware_models()
        async with harness(tmp_path, models=models) as (service, clock, _):
            await service.configure({"enabled": True, "devices": devices(bench=True)})
            await service.report_bridge(owner_id="owner1", connected=["muse", "sosexy"])
            state = await start(service, devices=["muse", "sosexy"])
            await settle(service)
            sid, generation = state["id"], state["generation"]
            async def output(**kw):
                return await service.output(sid, owner_id="owner1", generation=generation,
                    connected=["muse", "sosexy"], adopted=kw.get("adopted", {}))
            with pytest.raises(SceneOwnerMismatch):
                await service.output(sid, owner_id="other", generation=generation, connected=[], adopted={})
            first = await output()
            assert [d["driver"] for d in first["devices"]] == ["muse_ble", "sosexy_ble"]
            assert all(d["version"] == 1 for d in first["devices"])
            assert "PRIVATE_CONTROL" not in json.dumps(first)
            await output(adopted={d["id"]: {"version": d["version"], "elapsed_ms": 750} for d in first["devices"]})
            models.outputs.append("<toy2>换一下节奏</toy2>")
            await service.message(sid, owner_id="owner1", text="继续测试")
            await settle(service)
            second = await output()
            assert [d["version"] for d in second["devices"]] == [1, 2]
            payload = json.loads(models.compilations[-1][1]["content"])
            assert payload["previous_sequence"] == first["devices"][1]["sequence"]
            assert payload["playback_position"]["elapsed_ms"] == 750
            await service.action(sid, owner_id="owner1", action="pause")
            assert (await output())["devices"] == []
            assert not any(service.devices.outputs.values())
            ended = await service.action(sid, owner_id="owner1", action="finish")
            assert ended["card"] and not (await output())["devices"]
    asyncio.run(scenario())


def test_output_link_fails_closed_without_waiting_for_regular_heartbeat(tmp_path):
    async def scenario():
        async with harness(tmp_path, models=hardware_models()) as (service, clock, _):
            await service.configure({"enabled": True, "devices": devices(bench=True)})
            await service.report_bridge(owner_id="owner1", connected=["muse", "sosexy"])
            state = await start(service, devices=["muse", "sosexy"])
            await settle(service)
            clock.advance(5.1)
            await service.heartbeat(state["id"], owner_id="owner1")
            await service.tick()
            assert service.active.row["status"] == "paused"
            assert service.active.row["reason"] == "output_link_lost"
            assert not any(service.devices.outputs.values())
            late = await service.output(state["id"], owner_id="owner1", generation=state["generation"], connected=["muse", "sosexy"], adopted={})
            assert late["devices"] == [] and late["status"] == "paused"
    asyncio.run(scenario())


def test_multichannel_profiles_keep_all_motors_available_and_bench_limits_are_separate():
    for bench in (False, True):
        profiles = devices(bench=bench)
        assert validate_config({"enabled": True, "devices": profiles})["devices"] == profiles
        for device in profiles:
            assert all(bounds["max"] > 0 for bounds in device["capabilities"]["channels"].values())
            assert "错峰" in device["description"]
    assert devices()[0]["capabilities"]["channels"]["thrust_pattern"]["max"] == 9


def test_native_output_keeps_timed_scene_alive_without_page_or_chat_heartbeats(tmp_path):
    async def scenario():
        models = hardware_models()
        models.outputs.clear()
        models.outputs.extend(["<toy1>多通道交替</toy1><toy2>三个通道错峰</toy2><self_awake>180</self_awake>",
                              "<toy1>只调一路，另外一路继续</toy1><self_awake>180</self_awake>"])
        async with harness(tmp_path, models=models) as (service, clock, _):
            await service.configure({"enabled": True, "devices": devices(bench=True)})
            await service.report_bridge(owner_id="owner1", connected=["muse", "sosexy"])
            state = await start(service, devices=["muse", "sosexy"])
            await settle(service)
            for _ in range(181):
                clock.advance(1)
                await service.output(state["id"], owner_id="owner1", generation=state["generation"],
                                     connected=["muse", "sosexy"], adopted={})
                await service.tick()
                await settle(service)
            assert service.active.row["status"] == "active"
            assert len(models.turns) == 2
            assert all(v > 0 for key, v in service.devices.outputs["sosexy"].items() if key.endswith("_speed"))
    asyncio.run(scenario())


def test_late_native_notification_or_retry_cannot_pause_resumed_generation(tmp_path):
    async def scenario():
        async with harness(tmp_path, models=hardware_models()) as (service, clock, _):
            await service.configure({"enabled": True, "devices": devices(bench=True)})
            await service.report_bridge(owner_id="owner1", connected=["muse", "sosexy"])
            state = await start(service, devices=["muse", "sosexy"])
            await settle(service)
            await service.action(state["id"], owner_id="owner1", action="pause", generation=state["generation"])
            resumed = await service.action(state["id"], owner_id="owner1", action="resume")
            for action in ("pause", "end"):
                value = await service.action(state["id"], owner_id="owner1", action=action, generation=state["generation"])
                assert value["status"] == "active" and value["generation"] == resumed["generation"]
    asyncio.run(scenario())
