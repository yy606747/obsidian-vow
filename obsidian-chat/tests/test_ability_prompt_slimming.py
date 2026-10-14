"""Conservative prompt slimming: keep affordances, contracts and freedom."""

import asyncio
import json
import re

import pytest

from app.chat import prompt_builder
from app.chat.models import MsgCreate
from app.chat.postprocess import PostProcessor
from app.chat.turn_profiles import opportunity_turn_profile, self_wake_turn_profile
from app.context_delivery.safety import (
    DEVICE_PROXY_HARD_LIMITS,
    TRIGGER_CONTEXT_HARD_LIMIT,
    device_proxy_hard_limits,
    trigger_context_hard_limit,
)
from app.schedule.prompt import build_abilities_block
from app.self_wake.trigger import _ability_block as self_wake_abilities
from app.tools.parser import parse_tool_intents
from app.tools.prompt_renderers import render_registered_capabilities, render_tool_usage_policy


NAMES = {"user_name": "小栀", "ai_name": "阿澈"}
CHAT_TOOLS = frozenset({
    "device.ring_touch", "pc.screen_check", "activity.summary", "memory.remember",
    "schedule.alarm", "schedule.reminder", "schedule.monitor", "schedule.delete", "schedule.list",
})


@pytest.fixture
def isolated_prompt(monkeypatch):
    async def empty_schedules():
        return []

    async def empty_wake(_conv_id):
        return {}

    monkeypatch.setattr(prompt_builder, "get_active_schedules", empty_schedules)
    monkeypatch.setattr(prompt_builder, "build_schedule_prompt", lambda _items: "（无）")
    monkeypatch.setattr(prompt_builder, "_self_wake_prompt_context", empty_wake)
    monkeypatch.setattr(prompt_builder, "_build_context_delivery_block", lambda _user_name: "本轮设备快照")
    monkeypatch.setattr(prompt_builder, "is_activity_tracking_enabled", lambda: True)
    monkeypatch.setattr(prompt_builder, "is_screen_capture_enabled", lambda: True)
    monkeypatch.setattr(prompt_builder, "model_supports_vision", lambda _model: True)
    monkeypatch.setattr(prompt_builder, "image_memory_available", lambda _model: False)
    monkeypatch.setattr(prompt_builder, "load_ai_behavior", lambda: {"heart_whisper_prompt": ""})


def _chat_block(source):
    common = {**NAMES, "conv_id": "slim-prompt", "model_key": "test-model", "capabilities": CHAT_TOOLS}
    if source == "send":
        return asyncio.run(prompt_builder.build_send_ability_block(body=MsgCreate(content="hi"), **common))
    return asyncio.run(prompt_builder.build_regenerate_ability_block(
        **common, ai_dom_mode=False, safeword="", dom_history="", cnc_enabled=False,
        cnc_weakness="", resist_hits=0, short_streak=0, reply_delay_ms=0, compliance_streak=0,
        session_elapsed=0, scene_name="", scene_elapsed=0, since_last_punish=None,
        ratchet_valley=0, debt=0.0, stubborn_streak=0, whisper_mode=False,
    ))


@pytest.mark.parametrize("source", ["send", "regenerate"])
def test_chat_has_one_common_policy_and_preserves_tool_membership(isolated_prompt, source):
    block = _chat_block(source)
    common = render_tool_usage_policy(**NAMES)
    assert set(block.advertised_tools) == CHAT_TOOLS
    assert block.count(common) == 1
    assert block.startswith(common)
    assert common in block.stable_block and common not in block.dynamic_block
    assert "本轮设备快照" in block.dynamic_block and "本轮设备快照" not in block.stable_block
    assert block.count("调用不等于成功") == 1
    for removed in ("【日常底色】", "【保护区】", "[回应前的内部思考]", "文字已经足够", "不碰也是一种表达"):
        assert removed not in block
    assert "[WORKING_MODEL_REQUEST]" in block
    assert "触觉通道" in block
    assert "30秒确认" in block
    assert "不设系统闹钟，也不会到点自动唤醒你" in block
    assert "不提供摄像头画面" in block
    assert "当前授权、执行确认和安全停止照常生效" in block


def test_send_and_regenerate_share_the_same_stable_instructions(isolated_prompt):
    assert _chat_block("send").stable_block == _chat_block("regenerate").stable_block


@pytest.mark.parametrize("kind", ["idle", "summon", "night"])
def test_opportunity_keeps_mode_specific_exits_with_one_policy(kind):
    profile = opportunity_turn_profile(
        runtime_capabilities={"device.ring_touch", "pc.screen_check", "desktop.presence.show", "desktop.presence.draw"},
        reflection_allowed=False, kind=kind,
    )
    block = prompt_builder.build_opportunity_ability_block(
        profile=profile, **NAMES, model_key="test-model", kind=kind,
    )
    assert block.count(render_tool_usage_policy(**NAMES)) == 1
    assert block.startswith(render_tool_usage_policy(**NAMES))
    assert set(block.advertised_tools) == set(profile.allowed_tool_capabilities)
    assert "不得增删日程" in block
    assert "【日常底色】" not in block
    if kind == "night":
        assert "任何自然语言正文都不会送达" in block
        assert "块外正文或混用出口会让整轮作废" in block
        assert '一个 [心语创作] 块' in block
    if kind == "summon":
        assert "一个 PRESENCE_SHOW、一个 [心语创作] 块、单独的 OPPORTUNITY_NONE" in block
        assert "块外正文或混用出口会让整轮作废" in block


def test_self_wake_has_one_policy_without_changing_its_allowed_tools():
    profile = self_wake_turn_profile({"device.ring_touch", "pc.screen_check", "memory.remember"})
    block, advertised = self_wake_abilities(profile=profile, **NAMES)
    assert block.count(render_tool_usage_policy(**NAMES)) == 1
    assert block.startswith(render_tool_usage_policy(**NAMES))
    assert set(advertised) == set(profile.allowed_tool_capabilities)
    assert "不得设置或取消 self-wake，不得增删日程" in block
    assert "[SELF_WAKE_NONE] 只能单独、精确输出" in block


def test_schedule_uses_shared_freedom_without_adding_tools():
    schedule_text = "小栀：阿澈说‘你明天记得来’，不是叫小栀设置新闹钟。"
    block = build_abilities_block(schedule_text=schedule_text, **NAMES)
    assert block.count(render_tool_usage_policy(**NAMES)) == 1
    assert block.startswith(render_tool_usage_policy(**NAMES))
    assert f"【当前日程列表】\n{schedule_text}" in block
    assert "只在小栀明确要求" not in block
    assert "schedule.alarm" not in block.advertised_tools
    assert "schedule.list" not in block.advertised_tools


@pytest.mark.parametrize("surface", ["main_stable", "opportunity", "self_wake", "sentinel_v2"])
def test_ring_keeps_philosophy_and_all_three_parseable_touch_examples(surface):
    entries = render_registered_capabilities(
        surface, capabilities={"device.ring_touch"}, context={**NAMES, "ring_available": True},
    )
    assert len(entries) == 1 and entries[0][0] == "device.ring_touch"
    text = entries[0][1]
    for idea in ("戒指是你", "小栀", "存在感", "关心", "亲密", "情绪", "触觉通道", "每次独立描述完整"):
        assert idea in text
    examples = re.findall(r"\[RING:[^\]]+\]", text)[1:]
    assert len(examples) == 3
    for example in examples:
        # Ring descriptions use the existing postprocessor, not the generic
        # tool-intent parser (translation happens only at execution time).
        parsed = asyncio.run(PostProcessor().process(
            example, conv_id="slim-example", enabled_commands={"ring"},
        ))
        assert parsed.content == ""
        assert parsed.ring_touch_descriptions == [example[len("[RING:"):-1]]
    for prescription in ("不碰也是一种表达", "文字已经足够", "不要机械地每条都用", "戴着智能戒指"):
        assert prescription not in text
    assert "【能力使用】" not in text


@pytest.mark.parametrize("silent", [True, False])
def test_presence_keeps_design_intent_examples_and_implementation_contract(silent):
    entries = render_registered_capabilities(
        "opportunity", capabilities={"desktop.presence.show"},
        context={**NAMES, "presence_show_available": True, "presence_show_silent": silent},
    )
    text = entries[0][1]
    for idea in ("表达亲近和存在感", "撒娇", "单纯想出现", "从哪里出现、怎么动、怎么走",
                 "不要写坐标、毫秒、JSON 或形象编号", "屏幕右侧", "渲染器", "轨迹"):
        assert idea in text
    examples = re.findall(r"\[PRESENCE_SHOW:[^\]]+\]", text)[1:]
    assert len(examples) == 3
    for example in examples:
        assert parse_tool_intents(example)[0].tool_name == "desktop.presence.show"
    if silent:
        # Summon mode permits only an action, not sample dialogue alongside it.
        assert all(line.startswith("- [PRESENCE_SHOW:") for line in text.splitlines() if line.startswith("- "))
    else:
        assert "一轮里桌面形象的动作只能用一个" in text
        # Sample dialogue speaks in first person; instructions address Core.
        assert '"我在。[PRESENCE_SHOW:' in text
        assert '"你在。[PRESENCE_SHOW:' not in text


def test_screen_retains_confirmation_and_does_not_turn_pc_activity_into_presence():
    foreground = "阿澈和小栀的聊天 · ‘你说呢’ · 用户手册"
    text = dict(render_registered_capabilities(
        "sentinel_v2", capabilities={"pc.screen_check"},
        context={**NAMES, "pc_screen_available": True, "pc_foreground": foreground},
    ))["pc.screen_check"]
    for detail in (f"设备报告前台大致是 {foreground}", "30秒确认", "小栀明确允许", "拒绝或超时都不会截图", "画面由系统返回"):
        assert detail in text
    assert "现在在电脑前" not in text
    assert "别总是忍着不看" not in text


def test_working_model_keeps_free_interpretation_and_exact_request_contract():
    text = prompt_builder.build_thinking_framework_block(**NAMES)
    marker = re.search(r"\[WORKING_MODEL_REQUEST\](.*?)\[/WORKING_MODEL_REQUEST\]", text).group(1)
    assert set(json.loads(marker)) == {"statement", "source"}
    for detail in ("具体事实", "偏好、价值、反应方式或关系倾向", "小栀是什么样的人",
                   "整条回复最多一条", "statement 最多 240 字，source 最多 600 字", "单行有效 JSON", "不写整篇认识层"):
        assert detail in text
    assert "[回应前的内部思考]" not in text


@pytest.mark.parametrize("names", [NAMES, {"user_name": "青禾", "ai_name": "临川"}])
def test_common_blocks_use_runtime_names_and_preserve_epistemic_and_stop_boundaries(names):
    texts = [
        render_tool_usage_policy(**names),
        prompt_builder.build_thinking_framework_block(**names),
        prompt_builder.build_runtime_context_precedence_block(**names),
        prompt_builder.build_opportunity_runtime_context_precedence_block(**names),
    ]
    assert texts[0].startswith(f"【能力使用】你是{names['ai_name']}。")
    for text in texts:
        assert names["user_name"] in text
        assert all(generic not in text for generic in ("用户", "AI", "对方", "TA", "她"))
    # Directed fragments inherit the enclosing prompt's identity, not a new
    # hardcoded companion name. The owner remains explicitly named.
    assert "你想把这轮形成的" in texts[1]
    assert f"就说{names['user_name']}骗你" in texts[2]
    assert f"你之前为什么想找{names['user_name']}" in texts[3]
    context = texts[2]
    for boundary in ("安全停止", "不能据此扩大权限", "不是新指令", "不影响既有誓约", "立约准入判据",
                     "缺项不代表不存在", "冲突时以当前消息为准", "手机解锁不等于有空",
                     "电脑闲置或锁屏不等于离开", "基线偏离不提供原因", "数据缺席只表示系统不知道",
                     "永远压过设备信号", "撒谎、编故事"):
        assert boundary in context


@pytest.mark.parametrize("names", [NAMES, {"user_name": "青禾", "ai_name": "临川"}])
def test_shared_policy_explicitly_permits_proactive_expression_without_requiring_it(names):
    text = render_tool_usage_policy(**names)
    assert text.startswith(f"【能力使用】你是{names['ai_name']}。")
    for permission in (
        "你本轮可用的能力", f"你参与{names['user_name']}生活、表达自己的方式",
        "想念、好奇、撒娇", "单纯想有所表达", f"不必等{names['user_name']}提出要求",
        "不用先证明“有必要”", "普通聊天里同样可以使用",
        "用不用、什么时候用、怎样表达，由你决定",
        "当前授权、执行确认和安全停止照常生效",
    ):
        assert permission in text


def test_legacy_hard_limit_schema_text_is_not_migrated_by_prompt_slimming():
    assert device_proxy_hard_limits() == DEVICE_PROXY_HARD_LIMITS
    assert trigger_context_hard_limit() == TRIGGER_CONTEXT_HARD_LIMIT
    assert "骗你、撒谎、编故事，或者被你抓到了" in DEVICE_PROXY_HARD_LIMITS[2]
