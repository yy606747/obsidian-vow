from __future__ import annotations

import asyncio

from app.chat.action_executor import execute_postprocessed_actions
from app.chat.postprocess import PostProcessResult
from app.chat.turn_profiles import (
    chat_turn_profile,
    classify_opportunity_control_output,
    opportunity_turn_profile,
)
from app.tools.schemas import ToolContext, ToolIntent, ToolStatus


class _NoopLedger:
    async def record_execution(self, *_args, **_kwargs):
        return 0


_NOOP_LEDGER = _NoopLedger()


def _profile(*, reflect=False):
    return opportunity_turn_profile(
        runtime_capabilities={"memory.remember"},
        reflection_allowed=reflect,
    )


def test_opportunity_control_markers_are_exact_and_profile_gated():
    assert classify_opportunity_control_output(
        "[OPPORTUNITY_NONE]", profile=_profile()
    ) == "none"
    assert classify_opportunity_control_output(
        "[OPPORTUNITY_REFLECT]", profile=_profile(reflect=True)
    ) == "reflect"
    assert classify_opportunity_control_output(
        "【opportunity_none】", profile=_profile()
    ) == "none"
    assert classify_opportunity_control_output(
        "【OPPORTUNITY_REFLECT】", profile=_profile(reflect=True)
    ) == "reflect"
    assert classify_opportunity_control_output(
        "[OPPORTUNITY_REFLECT]", profile=_profile(reflect=False)
    ) == "invalid"
    assert classify_opportunity_control_output(
        "正文 [OPPORTUNITY_NONE]", profile=_profile()
    ) == "invalid"
    assert classify_opportunity_control_output(
        "[OPPORTUNITY_BROKEN", profile=_profile()
    ) == "invalid"
    assert not opportunity_turn_profile(
        runtime_capabilities={"schedule.delete"},
        reflection_allowed=False,
    ).allows_tool("schedule.delete")


def test_presence_bootstrap_profile_rejects_none_and_reflection():
    profile = opportunity_turn_profile(
        runtime_capabilities={"desktop.presence.draw"},
        reflection_allowed=True,
        kind="night",
        presence_bootstrap_required=True,
    )
    assert profile.allowed_tool_capabilities == frozenset({"desktop.presence.draw"})
    assert profile.allowed_markers == frozenset()
    assert classify_opportunity_control_output(
        "[OPPORTUNITY_NONE]", profile=profile
    ) == "invalid"
    assert classify_opportunity_control_output(
        "[OPPORTUNITY_REFLECT]", profile=profile
    ) == "invalid"


def test_executor_denies_capability_before_adapter_and_reuses_outer_msg_id():
    denied_calls = []
    remember_msg_ids = []

    async def denied_adapter(intent, context):
        denied_calls.append((intent, context))
        return {"ok": True}

    async def remember_adapter(_intent, context):
        remember_msg_ids.append(context.msg_id)
        return {"stored": True}

    postprocessed = PostProcessResult(
        content="",
        tool_intents=[
            ToolIntent(
                id="schedule",
                tool_name="schedule.delete",
                raw_text="[SCHEDULE_DEL:x]",
                arguments={"schedule_id": "x"},
                side_effect_level="write",
            ),
            ToolIntent(
                id="remember",
                tool_name="memory.remember",
                raw_text="[REMEMBER:x]",
                arguments={"content": "x"},
                side_effect_level="write",
            ),
        ],
    )
    execution = asyncio.run(
        execute_postprocessed_actions(
            postprocessed,
            profile=_profile(),
            context=ToolContext(
                conv_id="conv",
                msg_id="outer-msg-id",
                request_id="outer-msg-id",
                capabilities=("memory.remember", "schedule.delete"),
            ),
            adapter_overrides={
                "schedule.delete": denied_adapter,
                "memory.remember": remember_adapter,
            },
            ledger_override=_NOOP_LEDGER,
        )
    )
    by_tool = {result.tool_name: result for result in execution.results}
    assert by_tool["schedule.delete"].status is ToolStatus.SKIPPED
    assert by_tool["schedule.delete"].error == "turn_profile_not_allowed"
    assert denied_calls == []
    assert by_tool["memory.remember"].status is ToolStatus.EXECUTED
    assert remember_msg_ids == ["outer-msg-id"]


def test_shared_executor_preserves_mixed_schedule_marker_order():
    calls = []

    async def adapter(intent, _context):
        calls.append(intent.tool_name)
        return {"ok": True}

    intents = [
        ToolIntent(
            id="reminder",
            tool_name="schedule.reminder",
            raw_text="[REMINDER:x|first]",
            arguments={},
            side_effect_level="write",
        ),
        ToolIntent(
            id="alarm",
            tool_name="schedule.alarm",
            raw_text="[ALARM:y|second]",
            arguments={},
            side_effect_level="write",
        ),
    ]
    asyncio.run(
        execute_postprocessed_actions(
            PostProcessResult(content="", tool_intents=intents),
            profile=chat_turn_profile("send"),
            context=ToolContext(
                conv_id="conv",
                capabilities=("schedule.reminder", "schedule.alarm"),
            ),
            adapter_overrides={
                "schedule.reminder": adapter,
                "schedule.alarm": adapter,
            },
            ledger_override=_NOOP_LEDGER,
        )
    )
    assert calls == ["schedule.reminder", "schedule.alarm"]


def test_shared_executor_allows_only_one_alarm_per_user_turn():
    calls = []

    async def adapter(intent, _context):
        calls.append(intent.id)
        return {"ok": True}

    intents = [
        ToolIntent(
            id="alarm-first",
            tool_name="schedule.alarm",
            raw_text="[ALARM:x|first]",
            arguments={},
            side_effect_level="write",
        ),
        ToolIntent(
            id="alarm-second",
            tool_name="schedule.alarm",
            raw_text="[ALARM:y|second]",
            arguments={},
            side_effect_level="write",
        ),
    ]
    execution = asyncio.run(
        execute_postprocessed_actions(
            PostProcessResult(content="", tool_intents=intents),
            profile=chat_turn_profile("send"),
            context=ToolContext(
                conv_id="conv",
                capabilities=("schedule.alarm",),
            ),
            adapter_overrides={"schedule.alarm": adapter},
            ledger_override=_NOOP_LEDGER,
        )
    )

    assert calls == ["alarm-first"]
    by_id = {result.intent_id: result for result in execution.results}
    assert by_id["alarm-first"].status is ToolStatus.EXECUTED
    assert by_id["alarm-second"].status is ToolStatus.SKIPPED
    assert by_id["alarm-second"].error == "one_alarm_per_turn"


def test_shared_executor_allows_only_one_presence_intent_per_turn():
    calls = []

    async def adapter(intent, _context):
        calls.append(intent.tool_name)
        return {"ok": True, "status": "queued"}

    intents = [
        ToolIntent(
            id="show",
            tool_name="desktop.presence.show",
            raw_text="[PRESENCE_SHOW:peek]",
            arguments={"intent_text": "peek"},
            side_effect_level="external",
        ),
        ToolIntent(
            id="draw",
            tool_name="desktop.presence.draw",
            raw_text="[PRESENCE_DRAW:fog]",
            arguments={"description": "fog"},
            side_effect_level="external",
        ),
    ]
    profile = opportunity_turn_profile(
        runtime_capabilities={
            "desktop.presence.show",
            "desktop.presence.draw",
        },
        reflection_allowed=False,
    )
    execution = asyncio.run(
        execute_postprocessed_actions(
            PostProcessResult(content="", tool_intents=intents),
            profile=profile,
            context=ToolContext(
                conv_id="conv",
                capabilities=tuple(profile.allowed_tool_capabilities),
            ),
            adapter_overrides={
                "desktop.presence.show": adapter,
                "desktop.presence.draw": adapter,
            },
            ledger_override=_NOOP_LEDGER,
        )
    )
    assert calls == ["desktop.presence.show"]
    by_id = {result.intent_id: result for result in execution.results}
    assert by_id["show"].status is ToolStatus.EXECUTED
    assert by_id["draw"].status is ToolStatus.SKIPPED
    assert by_id["draw"].error == "one_presence_intent_per_turn"
