from camera import CAM_CHECK_CMD
from app.tools.parser import parse_structured_tool_intents, parse_tool_intents, tool_intents_payload
from app.tools.schemas import SideEffectLevel


def test_parse_tool_intents_preserves_legacy_command_order_and_arguments():
    text = (
        "开场 [MUSIC:夜曲 周杰伦] [TOY:SCENE:warmup] "
        f"{CAM_CHECK_CMD} [查看动态:99] [SCREEN_CHECK:确认她是不是还在写代码] [POI_SEARCH:咖啡] "
        "[ALARM:2026-05-13 08:00|起床] [REMINDER:2026-05-14|交材料] "
        "[Monitor:2026-05-15 20:00|看一眼] [SCHEDULE_DEL:sch_1] [SCHEDULE_LIST] "
        "[HEART:想你] [REMEMBER:用户喜欢冷萃]"
    )

    intents = parse_tool_intents(text)

    assert [intent.tool_name for intent in intents] == [
        "music.search",
        "device.toy",
        "monitor.camera",
        "activity.summary",
        "pc.screen_check",
        "location.poi_search",
        "schedule.alarm",
        "schedule.reminder",
        "schedule.monitor",
        "schedule.delete",
        "schedule.list",
        "heart.whisper",
        "memory.remember",
    ]
    assert [intent.id for intent in intents[:3]] == [
        "intent_001_music_search",
        "intent_002_device_toy",
        "intent_003_monitor_camera",
    ]
    assert intents[0].arguments == {"query": "夜曲 周杰伦"}
    assert intents[1].arguments == {"command": "SCENE:warmup"}
    assert intents[3].arguments == {"raw_window": "99", "n": 12}
    assert intents[4].arguments == {"reason": "确认她是不是还在写代码"}
    assert intents[5].arguments == {"category": "咖啡"}
    assert intents[6].arguments == {"raw_datetime": "2026-05-13 08:00", "content": "起床"}
    assert intents[9].arguments == {"schedule_id": "sch_1"}
    assert intents[10].arguments == {}
    assert intents[11].arguments == {"content": "想你"}
    assert intents[12].arguments == {"content": "用户喜欢冷萃"}
    assert intents[0].side_effect_level is SideEffectLevel.EXTERNAL
    assert intents[1].side_effect_level is SideEffectLevel.DEVICE
    assert intents[1].allowed_modes == ("intimate", "device_control")
    assert intents[6].side_effect_level is SideEffectLevel.WRITE


def test_parse_tool_intents_accepts_fullwidth_toy_marker():
    intents = parse_tool_intents("继续 【TOY：SIEGE:10:10】", enabled_commands={"toy"})

    assert len(intents) == 1
    assert intents[0].tool_name == "device.toy"
    assert intents[0].arguments == {"command": "SIEGE:10:10"}
    assert intents[0].raw_text == "【TOY：SIEGE:10:10】"


def test_parse_tool_intents_respects_enabled_command_groups():
    text = (
        "突袭 [TOY:1] [REMEMBER:用户喜欢突然袭击] "
        "[MUSIC:歌] [ALARM:2026-05-13 08:00|起床] [HEART:悄悄话]"
    )

    intents = parse_tool_intents(text, enabled_commands={"toy", "remember"})

    assert [intent.tool_name for intent in intents] == ["device.toy", "memory.remember"]
    assert intents[0].arguments == {"command": "1"}
    assert intents[1].arguments == {"content": "用户喜欢突然袭击"}


def test_tool_intents_payload_matches_contract_shape():
    intents = parse_tool_intents("[POI_SEARCH:咖啡] [查看动态:0]")

    payload = tool_intents_payload(intents)

    assert payload[0]["tool_name"] == "location.poi_search"
    assert payload[0]["arguments"] == {"category": "咖啡"}
    assert payload[0]["metadata"]["legacy_marker"] == "POI_SEARCH"
    assert payload[1]["tool_name"] == "activity.summary"
    assert payload[1]["arguments"] == {"raw_window": "0", "n": 6}


def test_parse_structured_tool_intents_respects_enabled_groups_and_aliases():
    intents = parse_structured_tool_intents(
        [
            {"type": "toy", "command": "SCENE:warmup"},
            {"tool_name": "memory.remember", "arguments": {"content": "用户喜欢结构化动作"}},
            {"tool_name": "music.search", "arguments": {"query": "夜曲"}},
            {"type": "unknown", "value": "ignored"},
        ],
        enabled_commands={"toy", "remember"},
    )

    assert [intent.tool_name for intent in intents] == ["device.toy", "memory.remember"]
    assert [intent.id for intent in intents] == ["intent_001_device_toy", "intent_002_memory_remember"]
    assert intents[0].arguments == {"command": "SCENE:warmup"}
    assert intents[0].source == "structured_action"
    assert intents[0].metadata["schema"] == "assistant_actions_v1"
    assert intents[1].arguments == {"content": "用户喜欢结构化动作"}


def test_parse_screen_check_intent_from_legacy_and_structured_actions():
    legacy = parse_tool_intents("[SCREEN_CHECK:看一眼她是不是在学习]", enabled_commands={"screen"})
    structured = parse_structured_tool_intents(
        [{"type": "screen_check", "reason": "确认当前电脑状态"}],
        enabled_commands={"screen"},
    )

    assert legacy[0].tool_name == "pc.screen_check"
    assert legacy[0].arguments == {"reason": "看一眼她是不是在学习"}
    assert structured[0].tool_name == "pc.screen_check"
    assert structured[0].arguments == {"reason": "确认当前电脑状态"}


def test_parse_structured_ring_touch_intent():
    intents = parse_structured_tool_intents(
        [{
            "tool_name": "device.ring_touch",
            "arguments": {
                "touch": "急促地连敲三下",
                "reason": "提醒她回来",
                "haptics": {"taps": 3, "interval_ms": 800},
            },
        }],
        enabled_commands={"ring"},
    )

    assert len(intents) == 1
    assert intents[0].tool_name == "device.ring_touch"
    assert intents[0].arguments == {
        "touch": "急促地连敲三下",
        "reason": "提醒她回来",
        "haptics": {"taps": 3, "interval_ms": 800},
    }
    assert intents[0].side_effect_level is SideEffectLevel.DEVICE


def test_parse_structured_ring_touch_reads_top_level_haptics():
    intents = parse_structured_tool_intents(
        [{
            "tool_name": "device.ring_touch",
            "touch": "轻轻碰你两下",
            "haptics": {"taps": 2, "interval_ms": 2000},
        }],
        enabled_commands={"ring"},
    )

    assert len(intents) == 1
    assert intents[0].arguments["touch"] == "轻轻碰你两下"
    assert intents[0].arguments["haptics"] == {"taps": 2, "interval_ms": 2000}
