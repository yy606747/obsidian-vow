from app.tools.schemas import (
    KNOWN_TOOL_DEFINITIONS,
    SideEffectLevel,
    ToolContext,
    ToolEvent,
    ToolEventType,
    ToolIntent,
    ToolResult,
    ToolStatus,
    get_tool_definition,
    tool_definitions_payload,
)


def test_tool_intent_contract_normalizes_and_serializes():
    intent = ToolIntent(
        id="intent_1",
        tool_name="music.search",
        raw_text="[MUSIC:夜曲 周杰伦]",
        arguments={"query": "夜曲 周杰伦"},
        side_effect_level="external",
        allowed_modes=["normal", "work"],
        metadata={"legacy_marker": "MUSIC"},
    )

    assert intent.side_effect_level is SideEffectLevel.EXTERNAL
    assert intent.allowed_modes == ("normal", "work")
    assert intent.to_dict() == {
        "id": "intent_1",
        "tool_name": "music.search",
        "raw_text": "[MUSIC:夜曲 周杰伦]",
        "arguments": {"query": "夜曲 周杰伦"},
        "requires_confirmation": False,
        "side_effect_level": "external",
        "allowed_modes": ["normal", "work"],
        "source": "model_output",
        "confidence": 1.0,
        "metadata": {"legacy_marker": "MUSIC"},
    }


def test_tool_result_keeps_result_error_events_and_attachments_separate():
    intent = ToolIntent(
        id="intent_2",
        tool_name="device.toy",
        raw_text="[TOY:1]",
        side_effect_level=SideEffectLevel.DEVICE,
        allowed_modes=("intimate", "device_control"),
    )
    event = ToolEvent(
        event_type=ToolEventType.POLICY_SKIPPED,
        tool_name=intent.tool_name,
        intent_id=intent.id,
        message="capability_missing",
        payload={"mode": "normal"},
    )

    result = ToolResult.from_intent(
        intent,
        status=ToolStatus.SKIPPED,
        error="capability_missing",
        events=[event],
        attachments=[{"type": "debug", "reason": "not_allowed"}],
        metadata={"policy": "mode_capability"},
    )

    assert result.to_dict() == {
        "tool_name": "device.toy",
        "intent_id": "intent_2",
        "status": "skipped",
        "result": None,
        "error": "capability_missing",
        "events": [
            {
                "event_type": "policy_skipped",
                "tool_name": "device.toy",
                "intent_id": "intent_2",
                "message": "capability_missing",
                "payload": {"mode": "normal"},
                "created_at": None,
            }
        ],
        "user_visible_message": None,
        "attachments": [{"type": "debug", "reason": "not_allowed"}],
        "followup_required": False,
        "metadata": {"policy": "mode_capability"},
    }


def test_tool_context_contract_records_mode_capabilities_and_eval_guard():
    context = ToolContext(
        conv_id="conv_1",
        msg_id="msg_1",
        request_id="req_1",
        model_key="mock-model",
        mode="normal",
        capabilities=["music.search", "memory.remember"],
        memory_eval_mode=True,
    )

    payload = context.to_dict()
    assert payload["conv_id"] == "conv_1"
    assert payload["capabilities"] == ["music.search", "memory.remember"]
    assert payload["memory_eval_mode"] is True


def test_known_tool_definitions_cover_phase5_preflight_inventory():
    expected_tools = {
        "music.search",
        "schedule.alarm",
        "schedule.reminder",
        "schedule.monitor",
        "schedule.delete",
        "schedule.list",
        "location.poi_search",
        "activity.summary",
        "heart.whisper",
        "memory.remember",
        "device.toy",
        "device.ring_touch",
    }

    assert expected_tools <= set(KNOWN_TOOL_DEFINITIONS)
    assert "monitor.camera" not in KNOWN_TOOL_DEFINITIONS
    assert get_tool_definition("device.toy").side_effect_level is SideEffectLevel.DEVICE
    assert get_tool_definition("device.toy").allowed_modes == ("intimate", "device_control")
    assert get_tool_definition("device.ring_touch").side_effect_level is SideEffectLevel.DEVICE
    assert get_tool_definition("device.ring_touch").allowed_modes == ("ring_touch_enabled",)
    assert get_tool_definition("memory.remember").side_effect_level is SideEffectLevel.WRITE
    assert len(tool_definitions_payload()) >= len(expected_tools)
