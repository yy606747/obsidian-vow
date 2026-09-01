import asyncio
import json

from camera import CAM_CHECK_CMD
from app.chat import side_effects
from app.chat.postprocess import PostProcessor
from app.chat.streaming import (
    _RecallIntentStreamFilter,
    _RingTouchStreamFilter,
    _TideIntentStreamFilter,
    _UpdateModelStreamFilter,
    _WebSearchIntentStreamFilter,
)


def test_postprocessor_extracts_command_plan_without_music_lookup():
    async def fail_schedule_processor(_text, _conv_id):
        raise AssertionError("postprocess should not execute schedule commands")

    processor = PostProcessor(schedule_processor=fail_schedule_processor)

    result = asyncio.run(processor.process(
        (
            "播放 [MUSIC:夜曲 周杰伦] [TOY:SCENE:warmup] "
            f"{CAM_CHECK_CMD} [查看动态:99] [SCREEN_CHECK:确认她在做什么] [POI_SEARCH:咖啡] "
            "[HEART:想你] [REMEMBER:用户喜欢冷萃] <meta>hidden</meta> 完成"
        ),
        conv_id="conv_post",
    ))

    assert result.music_cards == []
    assert result.music_attachments == []
    assert result.toy_commands == ["SCENE:warmup"]
    assert result.cam_triggered is True
    assert result.activity_n == 12
    assert result.screen_check_reasons == ["确认她在做什么"]
    assert result.poi_categories == ["咖啡"]
    assert result.heart_whispers == ["想你"]
    assert result.remember_notes == ["用户喜欢冷萃"]
    assert [intent.tool_name for intent in result.tool_intents] == [
        "music.search",
        "device.toy",
        "monitor.camera",
        "activity.summary",
        "pc.screen_check",
        "location.poi_search",
        "heart.whisper",
        "memory.remember",
    ]
    assert result.tool_intent_payloads[0]["arguments"] == {"query": "夜曲 周杰伦"}
    assert result.tool_intent_payloads[3]["arguments"] == {"raw_window": "99", "n": 12}
    assert result.tool_intent_payloads[4]["arguments"] == {"reason": "确认她在做什么"}
    assert [item["status"] for item in result.tool_result_payloads] == [
        "pending",
        "skipped",
        "pending",
        "pending",
        "pending",
        "pending",
        "pending",
        "pending",
    ]
    assert result.tool_result_payloads[1]["error"] == "mode_not_allowed"
    assert "hidden" not in result.content
    for marker in ("[MUSIC:", "[TOY:", CAM_CHECK_CMD, "[查看动态:", "[SCREEN_CHECK:", "[POI_SEARCH:", "[HEART:", "[REMEMBER:"):
        assert marker not in result.content


def test_postprocessor_handles_mixed_width_ring_and_heart_in_one_message():
    processor = PostProcessor()

    result = asyncio.run(
        processor.process(
            "正文 [RING：指尖轻叩两下] 【HEART：其实一直在数。】",
            conv_id="conv-mixed-width-ring-heart",
        )
    )

    assert result.content == "正文"
    assert result.ring_touch_descriptions == ["指尖轻叩两下"]
    assert result.heart_whispers == ["其实一直在数。"]


def test_postprocessor_strips_schedule_commands_without_executing_them():
    async def fail_schedule_processor(_text, _conv_id):
        raise AssertionError("schedule execution should happen in ToolService")

    processor = PostProcessor(schedule_processor=fail_schedule_processor)

    result = asyncio.run(processor.process(
        "我会提醒你 [ALARM:2026-05-13 08:00|起床]",
        conv_id="conv_schedule",
    ))

    assert result.content == "我会提醒你"
    assert result.music_cards == []
    assert result.toy_commands == []
    assert result.heart_whispers == []
    assert result.remember_notes == []
    assert [intent.tool_name for intent in result.tool_intents] == ["schedule.alarm"]
    assert result.tool_intents[0].arguments == {
        "raw_datetime": "2026-05-13 08:00",
        "content": "起床",
    }
    assert result.tool_result_payloads[0]["status"] == "pending"


def test_postprocessor_strips_known_markers_even_when_groups_are_disabled():
    processor = PostProcessor()
    result = asyncio.run(
        processor.process(
            (
                "正文 [MUSIC:夜曲] [TOY:1] "
                f"{CAM_CHECK_CMD} [RING:轻碰] "
                "[ALARM:2026-08-12 08:00|起床]"
            ),
            conv_id="conv-disabled-markers",
            enabled_commands=frozenset(),
        )
    )

    assert result.content == "正文"
    assert result.tool_intents == []
    assert result.ring_touch_descriptions == []


def test_postprocessor_extracts_one_bounded_recall_intent_and_strips_it():
    processor = PostProcessor()
    result = asyncio.run(
        processor.process(
            "我先陪你把现在说完。[RECALL_INTENT]找她以前提过的那次南京出差[/RECALL_INTENT]",
            conv_id="conv-recall",
        )
    )

    assert result.content == "我先陪你把现在说完。"
    assert result.recall_intent == "找她以前提过的那次南京出差"

    duplicated = asyncio.run(
        processor.process(
            "正文[RECALL_INTENT]一[/RECALL_INTENT][RECALL_INTENT]二[/RECALL_INTENT]",
            conv_id="conv-recall",
        )
    )
    assert duplicated.content == "正文"
    assert duplicated.recall_intent == ""


def test_memory_eval_strips_recall_intent_without_returning_it():
    processor = PostProcessor()
    result = asyncio.run(
        processor.process(
            "正文[RECALL_INTENT]不应落盘[/RECALL_INTENT]",
            conv_id="conv-eval-recall",
            memory_eval_mode=True,
        )
    )

    assert result.content == "正文"
    assert result.recall_intent == ""


def test_recall_intent_stream_filter_never_leaks_split_or_unfinished_marker():
    stream_filter = _RecallIntentStreamFilter()
    assert stream_filter.feed("正文[RECALL_") == "正文"
    assert stream_filter.feed("INTENT]找南京") == ""
    assert stream_filter.feed("旧事[/RECALL_INTENT]继续") == "继续"
    assert stream_filter.flush() == ""

    unfinished = _RecallIntentStreamFilter()
    assert unfinished.feed("可见[RECALL_INTENT]半截") == "可见"
    assert unfinished.flush() == ""


def test_postprocessor_extracts_and_stream_hides_web_search_intent():
    processor = PostProcessor()
    result = asyncio.run(processor.process(
        "我先陪你说完。[WEB_SEARCH_INTENT]查今天的新消息[/WEB_SEARCH_INTENT]",
        conv_id="conv-web-search",
    ))

    assert result.content == "我先陪你说完。"
    assert result.web_search_intent == "查今天的新消息"

    stream_filter = _WebSearchIntentStreamFilter()
    assert stream_filter.feed("正文[WEB_SEARCH_IN") == "正文"
    assert stream_filter.flush() == ""


def test_postprocessor_strips_private_reasoning_blocks():
    processor = PostProcessor()

    result = asyncio.run(processor.process(
        "开头 <think>这里是内部推理</think> 中间 <analysis>不要展示</analysis> 结尾",
        conv_id="conv_private",
    ))

    assert result.content == "开头  中间  结尾"
    assert "内部推理" not in result.content
    assert "analysis" not in result.content


def test_postprocessor_strips_unfinished_private_reasoning_block():
    processor = PostProcessor()

    result = asyncio.run(processor.process(
        "可见内容 <think>未闭合的内部推理",
        conv_id="conv_private_unfinished",
    ))

    assert result.content == "可见内容"
    assert "未闭合" not in result.content


def test_postprocessor_memory_eval_mode_strips_all_side_effect_commands():
    async def fail_schedule_processor(_text, _conv_id):
        raise AssertionError("schedule side effects must be skipped in memory_eval_mode")

    processor = PostProcessor(schedule_processor=fail_schedule_processor)

    result = asyncio.run(processor.process(
        (
            "hello [MUSIC:歌] [TOY:1] "
            f"{CAM_CHECK_CMD} [查看动态:6] [SCREEN_CHECK:看屏幕] [POI_SEARCH:餐厅] "
            "[HEART:悄悄话] [RING:轻轻碰一下] [REMEMBER:记住这个] "
            "[ALARM:2026-05-13 08:00|起床] [REMINDER:2026-05-14|交材料] "
            "[Monitor:2026-05-15 20:00|看一眼] [SCHEDULE_DEL:sch_1] [SCHEDULE_LIST] "
            "<meta>debug</meta> done"
        ),
        conv_id="conv_eval",
        memory_eval_mode=True,
    ))

    assert result.content.startswith("hello")
    assert result.content.endswith("done")
    for marker in (
        "[MUSIC:",
        "[TOY:",
        CAM_CHECK_CMD,
        "[查看动态:",
        "[SCREEN_CHECK:",
        "[POI_SEARCH:",
        "[HEART:",
        "[RING:",
        "[REMEMBER:",
        "[ALARM:",
        "[REMINDER:",
        "[Monitor:",
        "[SCHEDULE_DEL:",
        "[SCHEDULE_LIST]",
        "debug",
    ):
        assert marker not in result.content
    assert result.music_cards == []
    assert result.toy_commands == []
    assert result.cam_triggered is False
    assert result.activity_n == 0
    assert result.screen_check_reasons == []
    assert result.poi_categories == []
    assert result.heart_whispers == []
    assert result.ring_touch_descriptions == []
    assert result.remember_notes == []
    assert [intent.tool_name for intent in result.tool_intents] == [
        "music.search",
        "device.toy",
        "monitor.camera",
        "activity.summary",
        "pc.screen_check",
        "location.poi_search",
        "heart.whisper",
        "memory.remember",
        "schedule.alarm",
        "schedule.reminder",
        "schedule.monitor",
        "schedule.delete",
        "schedule.list",
    ]
    assert {item["status"] for item in result.tool_result_payloads} == {"skipped"}
    assert {item["error"] for item in result.tool_result_payloads} == {"memory_eval_mode"}


def test_postprocessor_hides_legacy_working_model_update_without_executing_it():
    processor = PostProcessor()

    result = asyncio.run(processor.process(
        "我懂了。[UPDATE_MODEL:用户最近在意记忆系统不要被硬规则控制。]",
        conv_id="conv_model",
    ))

    assert result.content == "我懂了。"
    assert result.working_model_update == ""
    assert result.working_model_request is None


def test_update_model_stream_filter_hides_chunked_command():
    stream_filter = _UpdateModelStreamFilter()

    assert stream_filter.feed("我懂了。[UP") == "我懂了。"
    assert stream_filter.feed("DATE_MODEL:隐藏更新") == ""
    assert stream_filter.feed("内容]继续说") == "继续说"
    assert stream_filter.flush() == ""


def test_ring_touch_stream_filter_hides_chunked_command():
    stream_filter = _RingTouchStreamFilter()

    assert stream_filter.feed("晚安。[RI") == "晚安。"
    assert stream_filter.feed("NG:轻轻碰一下") == ""
    assert stream_filter.feed("]明天见") == "明天见"
    assert stream_filter.flush() == ""


def test_ring_touch_stream_filter_drops_unclosed_marker_on_flush():
    stream_filter = _RingTouchStreamFilter()

    assert stream_filter.feed("我在。[RING:轻轻") == "我在。"
    assert stream_filter.flush() == ""


def test_postprocessor_extracts_tide_intent_unconditionally_without_toy_command():
    processor = PostProcessor()

    result = asyncio.run(processor.process(
        (
            "靠近一点。[TIDE_INTENT:先轻一点，别急[/TIDE_INTENT]"
            "继续说。[TIDE_INTENT:再慢慢加深[/TIDE_INTENT]"
        ),
        conv_id="conv_tide",
        enabled_commands=frozenset({"remember"}),
    ))

    assert result.content == "靠近一点。继续说。"
    assert result.tide_intent == "再慢慢加深"
    assert result.toy_commands == []
    assert "[TIDE_INTENT:" not in result.content


def test_tide_intent_stream_filter_hides_chunked_closed_block():
    stream_filter = _TideIntentStreamFilter()

    assert stream_filter.feed("靠近。[TIDE") == "靠近。"
    assert stream_filter.feed("_INTENT:藏起来") == ""
    assert stream_filter.feed("[/TIDE_INTENT]继续") == "继续"
    assert stream_filter.flush() == ""


def test_tide_intent_stream_filter_handles_split_end_marker():
    stream_filter = _TideIntentStreamFilter()

    assert stream_filter.feed("靠近。[TIDE_INTENT:藏起来[/TIDE") == "靠近。"
    assert stream_filter.feed("_INTENT]继续") == "继续"
    assert stream_filter.flush() == ""


def test_tide_intent_stream_filter_drops_unclosed_marker_on_flush():
    stream_filter = _TideIntentStreamFilter()

    assert stream_filter.feed("靠近。[TIDE_INTENT:半截") == "靠近。"
    assert stream_filter.flush() == ""


def test_store_working_model_update_rejects_overlong_without_saving(monkeypatch):
    broadcasts = []
    saved = []

    async def fake_broadcast(payload):
        broadcasts.append(payload)

    def fake_save(*args, **kwargs):
        saved.append((args, kwargs))

    monkeypatch.setattr(side_effects.manager, "broadcast", fake_broadcast)
    monkeypatch.setattr(side_effects, "WORKING_MODEL_MAX_CHARS", 8)
    monkeypatch.setattr(side_effects, "save_working_model", fake_save)

    result = asyncio.run(side_effects.store_working_model_update("太长了" * 4))

    assert result["ok"] is False
    assert result["reason"] == "too_long"
    assert saved == []
    assert broadcasts[-1]["type"] == "working_model_update_rejected"


def test_postprocessor_limits_and_hides_disabled_commands_for_initiative_routes():
    async def fail_schedule_processor(_text, _conv_id):
        raise AssertionError("initiative postprocess should not run schedule commands")

    processor = PostProcessor(schedule_processor=fail_schedule_processor)

    result = asyncio.run(processor.process(
        (
            "突袭 [TOY:1] [REMEMBER:用户喜欢突然袭击] "
            "[MUSIC:歌] [ALARM:2026-05-13 08:00|起床] <meta>hide</meta>"
        ),
        conv_id="conv_initiative",
        enabled_commands={"toy", "remember"},
    ))

    assert result.toy_commands == ["1"]
    assert result.remember_notes == ["用户喜欢突然袭击"]
    assert "[TOY:" not in result.content
    assert "[REMEMBER:" not in result.content
    assert "hide" not in result.content
    assert "[MUSIC:歌]" not in result.content
    assert "[ALARM:2026-05-13 08:00|起床]" not in result.content
    assert [intent.tool_name for intent in result.tool_intents] == ["device.toy", "memory.remember"]
    assert [item["status"] for item in result.tool_result_payloads] == ["skipped", "pending"]


def test_postprocessor_accepts_structured_assistant_actions():
    processor = PostProcessor()
    payload = {
        "assistant_text": "靠近一点",
        "actions": [
            {"type": "toy", "command": "2"},
            {"tool_name": "memory.remember", "arguments": {"content": "用户喜欢结构化突袭"}},
        ],
    }

    result = asyncio.run(processor.process(
        json.dumps(payload, ensure_ascii=False),
        conv_id="conv_structured",
        enabled_commands={"toy", "remember"},
    ))

    assert result.content == "靠近一点"
    assert result.toy_commands == ["2"]
    assert result.remember_notes == ["用户喜欢结构化突袭"]
    assert [intent.tool_name for intent in result.tool_intents] == ["device.toy", "memory.remember"]
    assert result.tool_intents[0].source == "structured_action"
    assert result.tool_intents[0].arguments == {"command": "2"}
    assert "actions" not in result.content


def test_working_model_markers_inside_structured_actions_are_inert():
    processor = PostProcessor()
    payload = {
        "assistant_text": "正文",
        "actions": [
            {
                "tool_name": "memory.remember",
                "arguments": {
                    "content": (
                        "[WORKING_MODEL_REQUEST]"
                        '{"statement":"不应执行","source":"不应执行"}'
                        "[/WORKING_MODEL_REQUEST]"
                    )
                },
            },
            {"type": "toy", "command": "2"},
        ],
    }

    result = asyncio.run(processor.process(
        json.dumps(payload, ensure_ascii=False),
        conv_id="conv_wm_inert",
        enabled_commands={"toy", "remember"},
    ))

    assert result.content == "正文"
    assert result.remember_notes == []
    assert [intent.tool_name for intent in result.tool_intents] == ["device.toy"]


def test_postprocessor_accepts_fullwidth_toy_marker():
    processor = PostProcessor()

    result = asyncio.run(processor.process(
        "继续压住你。 【TOY：HOLD:6:4】",
        conv_id="conv_fullwidth_toy",
        enabled_commands={"toy"},
    ))

    assert result.content == "继续压住你。"
    assert result.toy_commands == ["HOLD:6:4"]
    assert result.tool_intents[0].tool_name == "device.toy"
    assert result.tool_intents[0].arguments == {"command": "HOLD:6:4"}


def test_postprocessor_infers_ring_touch_from_visible_claim():
    processor = PostProcessor()

    result = asyncio.run(processor.process(
        "我轻轻碰你一下，别分心。",
        conv_id="conv_ring_claim",
    ))

    assert result.content == "我轻轻碰你一下，别分心。"
    assert result.tool_intents == []
    assert result.ring_touch_descriptions == ["我轻轻碰你"]


def test_postprocessor_extracts_ring_marker_and_limits_to_one():
    processor = PostProcessor()

    result = asyncio.run(processor.process(
        "晚安。[RING:轻轻碰一下，像是把手放上去]明天见。[RING:再点两下]",
        conv_id="conv_ring_marker",
    ))

    assert result.content == "晚安。明天见。"
    assert result.tool_intents == []
    assert result.ring_touch_descriptions == ["轻轻碰一下，像是把手放上去"]


def test_postprocessor_converts_structured_ring_touch_to_description():
    processor = PostProcessor()
    payload = {
        "assistant_text": "我轻轻碰你一下。",
        "actions": [
            {
                "tool_name": "device.ring_touch",
                "touch": "轻轻碰你一下",
                "haptics": {"taps": 2, "interval_ms": 2000},
            },
        ],
    }

    result = asyncio.run(processor.process(
        json.dumps(payload, ensure_ascii=False),
        conv_id="conv_ring_structured",
    ))

    assert result.tool_intents == []
    assert result.ring_touch_descriptions == ["轻轻碰你一下"]


def test_postprocessor_prefers_ring_marker_over_structured_ring_touch():
    processor = PostProcessor()
    payload = {
        "assistant_text": "我在。[RING:慢慢点一下]",
        "actions": [
            {"tool": "ring", "text": "结构化旧触碰", "haptics": {"taps": 3, "interval_ms": 1000}},
        ],
    }

    result = asyncio.run(processor.process(
        json.dumps(payload, ensure_ascii=False),
        conv_id="conv_ring_priority",
    ))

    assert result.content == "我在。"
    assert result.tool_intents == []
    assert result.ring_touch_descriptions == ["慢慢点一下"]


def test_postprocessor_does_not_infer_ring_touch_from_coincidence():
    processor = PostProcessor()

    result = asyncio.run(processor.process(
        "碰巧你也在想这个。",
        conv_id="conv_no_ring_claim",
    ))

    assert result.tool_intents == []


def test_postprocessor_does_not_execute_generic_actions_json():
    processor = PostProcessor()
    payload = {
        "text": "这是一段普通 JSON 示例",
        "actions": [{"type": "toy", "command": "9"}],
    }

    result = asyncio.run(processor.process(
        json.dumps(payload, ensure_ascii=False),
        conv_id="conv_generic_json",
        enabled_commands={"toy", "remember"},
    ))

    assert json.loads(result.content) == payload
    assert result.toy_commands == []
    assert result.tool_intents == []
