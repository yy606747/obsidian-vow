import asyncio
from copy import deepcopy
import json

from app.scenes.config import validate_config
from app.scenes.hardware import HARDWARE, _OLD_INTROS, devices
from app.scenes.prompts import card_messages, turn_messages
from test_scenes import harness, settle, start
from test_scene_hardware import hardware_models


def test_all_five_profiles_and_only_shipped_copy_upgrade_without_resetting_owner_limits():
    profiles = devices()
    assert [d["id"] for d in profiles] == ["muse", "sosexy", "sk30", "sk40", "cx492b"]
    assert validate_config({"enabled": True, "devices": profiles})["devices"] == profiles
    for driver, (name, intro) in _OLD_INTROS.items():
        device = deepcopy(HARDWARE[driver])
        device.update(name=name, description=intro + " 本次开放的取值范围以设备能力配置为准，保留多通道编排。")
        channel = next(iter(device["capabilities"]["channels"].values()))
        channel["max"] = channel["min"]
        result = validate_config({"enabled": False, "devices": [device]})
        assert result["enabled"] is False
        assert result["devices"][0]["description"] == HARDWARE[driver]["description"]
        assert result["devices"][0]["capabilities"]["channels"] == device["capabilities"]["channels"]
        device.update(name="自己起的名字", description="自己写的介绍")
        kept = validate_config({"enabled": True, "devices": [device]})["devices"][0]
        assert kept["name"] == "自己起的名字" and kept["description"] == "自己写的介绍"


def test_core_gets_physical_introductions_not_numeric_wire_fields():
    profiles = devices()
    identity = dict(user_name="小栀", ai_name="阿澈", ai_persona="", working_model="")
    card = card_messages(**identity, notes="", devices=profiles, count=1)[0]["content"]
    for i, device in enumerate(profiles, 1):
        device["label"] = f"toy{i}"
    scene = turn_messages(**identity, card="准备卡", devices=profiles, history=[], now=100,
                          started_at=0, last_wake_at=50, arrangements="", trigger="按时回来")[0]["content"]
    for device in profiles:
        assert device["description"] in card and device["description"] in scene
        assert f"<{device['label']}>" in scene
        for field in device["capabilities"]["channels"]:
            assert field not in card and field not in scene
    for key in ("sk30_ble", "sk40_ble"):
        intro = HARDWARE[key]["description"]
        assert "脉冲" in intro and "吮吸" in intro


def test_five_device_scene_compiles_independently_and_native_exchange_is_fenced(tmp_path):
    async def scenario():
        models = hardware_models()
        models.card_output = "## 卡1：五台联调\n分别编排。\n<devices>muse,sosexy,sk30,sk40,cx492b</devices>"
        models.outputs.clear()
        models.outputs.append("".join(f"<toy{i}>本台通道交替运行</toy{i}>" for i in range(1, 6)))
        profiles = devices()
        ids = [d["id"] for d in profiles]
        async with harness(tmp_path, models=models) as (service, clock, _):
            await service.configure({"enabled": True, "devices": profiles})
            await service.report_bridge(owner_id="owner1", connected=ids)
            state = await start(service, devices=ids)
            await settle(service)
            sid, generation = state["id"], state["generation"]
            async def output(connected=ids):
                return await service.output(sid, owner_id="owner1", generation=generation,
                                            connected=connected, adopted={})
            first = await output()
            assert len(first["devices"]) == len(models.compilations) == 5
            assert [d["version"] for d in first["devices"]] == [1]*5
            assert [d["driver"] for d in first["devices"]] == list(HARDWARE)
            models.outputs.append("<toy4>仅改变这一台的脉冲，吮吸继续原来的节奏</toy4>")
            await service.message(sid, owner_id="owner1", text="调一下第四台")
            await settle(service)
            second = await output()
            assert [d["version"] for d in second["devices"]] == [1, 1, 1, 2, 1]
            payload = json.loads(models.compilations[-1][1]["content"])
            assert set(payload["capabilities"]["channels"]) == {"suction_on", "suction_level", "pulse_gear"}
            await service.action(sid, owner_id="owner1", action="pause")
            assert (await output())["devices"] == []
            assert not any(service.devices.outputs.values())
    asyncio.run(scenario())
