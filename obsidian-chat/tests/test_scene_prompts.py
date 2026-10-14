from copy import deepcopy
import json
import re

import pytest

from app.scenes.config import DEFAULT_DEVICES
from app.scenes.compiler import validate_sequence
from app.scenes.hardware import devices as hardware_devices
from app.scenes.prompts import card_messages, turn_messages, compiler_messages


IDENTITY = dict(user_name="小栀", ai_name="阿澈", ai_persona="固定人设", working_model="当前认识")


def scene_messages(devices):
    return turn_messages(**IDENTITY, card="准备卡", devices=devices, history=[], now=100,
                         started_at=0, last_wake_at=50, arrangements="", trigger="按时回来")


@pytest.mark.parametrize("count", [1, 2, 16])
def test_scene_prompt_explains_every_bound_device_with_complete_tags(count):
    devices = [
        {**deepcopy(DEFAULT_DEVICES[i % 2]), "id": f"device_{i + 1}", "label": f"toy{i + 1}",
         "name": f"模拟设备 {i + 1}", "description": f"第 {i + 1} 台设备的独立简介。"}
        for i in range(count)
    ]
    original = deepcopy(devices)
    system = scene_messages(devices)[0]["content"]
    assert re.findall(r"<(toy\d+)>", system) == [d["label"] for d in devices]
    assert re.findall(r"</(toy\d+)>", system) == [d["label"] for d in devices]
    for device in devices:
        assert f"【{device['label']}：{device['name']}】" in system
        assert device["description"] in system
        assert f"<{device['label']}>希望这台设备怎样动作的自然语言描述</{device['label']}>" in system
        assert f"这里的安排只控制{device['name']}。" in system
    assert devices == original
    assert "等对应标签" not in system
    assert "未提及的设备继续原安排" in system
    assert "暂时停止某台，也在它自己的标签里说明" in system
    assert all(field not in system for field in ("capabilities", "max_step_ms", "max_cycle_ms", '"min"', '"max"'))


def test_scene_prompt_uses_supplied_bindings_without_renumbering():
    devices = [
        {**DEFAULT_DEVICES[1], "label": "toy7"},
        {**DEFAULT_DEVICES[0], "label": "toy2"},
    ]
    system = scene_messages(devices)[0]["content"]
    assert "【toy7：模拟设备 B】" in system
    assert "【toy2：模拟设备 A】" in system
    assert re.findall(r"<(toy\d+)>", system) == ["toy7", "toy2"]


def test_scene_prompt_keeps_unfilled_description_explicit():
    devices = [{**DEFAULT_DEVICES[0], "label": "toy1", "description": ""}]
    system = scene_messages(devices)[0]["content"]
    assert "简介尚未填写。" in system
    assert DEFAULT_DEVICES[0]["description"] not in system
    assert "<toy1>" in system and "</toy1>" in system


def test_scene_prompt_separates_visible_expression_from_natural_language_control():
    system = scene_messages([{**DEFAULT_DEVICES[0], "label": "toy1"}])[0]["content"]
    assert "标签外的内容，是阿澈想让小栀看到的" in system
    assert "说什么、说多少，由阿澈自己决定" in system
    assert "自然语言" in system and "不需要填写档位、代码或 JSON" in system
    assert "标签内的内容不会展示给小栀" in system
    assert "分别由阿澈决定，不必互相解释" in system
    assert "也可以只说话，不必改变所有设备" in system
    assert "<self_awake>延迟秒数</self_awake>" in system
    assert "<scene_end/>" in system


def test_card_prompt_keeps_candidate_ids_without_scene_control_instructions():
    messages = card_messages(**IDENTITY, notes="", devices=DEFAULT_DEVICES, count=1)
    text = "\n".join(message["content"] for message in messages)
    for device in DEFAULT_DEVICES:
        assert device["id"] in text and device["description"] in text
    assert "<devices>" in text
    assert "<toy" not in text and "【表达与设备】" not in text
    assert "<remember>" not in text


def test_scene_remember_is_voluntary_and_explicitly_about_intimacy():
    system = scene_messages([{**DEFAULT_DEVICES[0], "label": "toy1"}])[0]["content"]
    assert system.startswith("你是阿澈。")
    assert "与小栀的这段亲密互动" in system
    assert "<remember>想记住的内容</remember>" in system
    assert "记什么、什么时候记，由你决定" in system
    assert "不要求每场留下笔记" in system
    assert "不作为本轮台词展示给小栀" in system
    assert "【这段正在发生的相处】" not in system


@pytest.mark.parametrize("device", [*DEFAULT_DEVICES, *hardware_devices(), *hardware_devices(bench=True)])
def test_compiler_example_is_valid_for_every_device_and_includes_disabled_channels(device):
    caps = device["capabilities"]
    original = deepcopy(caps)
    messages = compiler_messages(intent="按已授权范围执行", capabilities=caps,
                                 previous=None, position={"playing": False})
    example = json.loads(messages[0]["content"].splitlines()[1])
    assert validate_sequence(example, caps) == example
    assert set(example["steps"][0]["values"]) == set(caps["channels"])
    assert all(example["steps"][0]["values"][key] == 0
               for key, bounds in caps["channels"].items() if bounds["max"] == 0)
    assert json.dumps(list(caps["channels"]), ensure_ascii=False) in messages[0]["content"]
    assert caps == original
