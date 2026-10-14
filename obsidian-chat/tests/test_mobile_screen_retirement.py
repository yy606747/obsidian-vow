"""Retired control syntax remains invisible and cannot trigger device work."""

import asyncio

import pytest

from app.chat.postprocess import PostProcessor
from app.tools.parser import parse_structured_tool_intents, parse_tool_intents
from app.tools.registry import registered_tools_for_surface, validate_tool_registry
from app.tools.schemas import get_tool_definition


@pytest.mark.parametrize("marker", [
    "[MOBILE_SCREEN_CHECK:android_phone|看看]",
    "【MOBILE_SCREEN_CHECK：android_tablet|看看】",
])
def test_retired_marker_is_hidden_without_scheduling_capture(marker):
    text = "继续聊。" + marker
    assert parse_tool_intents(text) == []
    result = asyncio.run(PostProcessor().process(text, conv_id="retired-screen"))
    assert result.content == "继续聊。"
    assert result.tool_intents == []


@pytest.mark.parametrize("tool", ["mobile.screen_check", "mobile_screen_check", "mobile_screen"])
def test_retired_structured_action_does_not_hide_live_pc_action(tool):
    intents = parse_structured_tool_intents([
        {"type": tool, "target": "android_phone", "reason": "看看"},
        {"type": "screen_check", "reason": "看看电脑"},
    ])
    assert [intent.tool_name for intent in intents] == ["pc.screen_check"]


def test_retired_capability_cannot_be_readvertised_by_old_configuration():
    assert get_tool_definition("mobile.screen_check") is None
    validate_tool_registry()
    for surface in ("main_stable", "opportunity", "self_wake", "initiative", "sentinel_v2"):
        tools = registered_tools_for_surface(
            surface, capabilities={"mobile.screen_check", "pc.screen_check"},
        )
        assert "mobile.screen_check" not in tools
        if surface != "initiative":
            assert "pc.screen_check" in tools
