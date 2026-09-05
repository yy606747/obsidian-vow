import asyncio

from app.tools.parser import parse_tool_intents
from app.tools.schemas import ToolContext, ToolEventType, ToolStatus
from app.tools.service import ToolService


def test_tool_service_plans_pending_results_without_side_effects():
    service = ToolService()
    intents = parse_tool_intents("[MUSIC:夜曲] [REMEMBER:用户喜欢冷萃]")

    plan = service.plan(intents, context=ToolContext(conv_id="conv_tools"))

    assert [result.tool_name for result in plan.results] == ["music.search", "memory.remember"]
    assert [result.status for result in plan.results] == [ToolStatus.PENDING, ToolStatus.PENDING]
    assert [event.event_type for event in plan.events] == [
        ToolEventType.INTENT_PARSED,
        ToolEventType.INTENT_PARSED,
    ]
    assert plan.results[0].result == {"dry_run": True, "policy": "pending"}
    assert plan.results[0].metadata == {"service": "tool_service", "phase": "dry_run"}
    payload = plan.to_dict()
    assert payload["context"]["conv_id"] == "conv_tools"
    assert payload["results"][0]["status"] == "pending"


def test_tool_service_skips_everything_in_memory_eval_mode():
    service = ToolService()
    intents = parse_tool_intents("[MUSIC:夜曲] [TOY:1] [REMEMBER:不要落库]")

    results = service.execute(
        intents,
        context=ToolContext(conv_id="conv_eval", memory_eval_mode=True),
    )

    assert [result.status for result in results] == [
        ToolStatus.SKIPPED,
        ToolStatus.SKIPPED,
        ToolStatus.SKIPPED,
    ]
    assert {result.error for result in results} == {"memory_eval_mode"}
    assert all(result.events[0].event_type is ToolEventType.POLICY_SKIPPED for result in results)


def test_tool_service_device_policy_is_dry_run_only_and_capability_aware():
    service = ToolService()
    toy_intent = parse_tool_intents("[TOY:1]")[0]

    normal_result = service.execute(
        [toy_intent],
        context=ToolContext(conv_id="conv_normal", mode="normal"),
    )[0]
    assert normal_result.status is ToolStatus.SKIPPED
    assert normal_result.error == "mode_not_allowed"
    assert normal_result.events[0].payload["allowed_modes"] == ["intimate", "device_control"]

    capable_result = service.execute(
        [toy_intent],
        context=ToolContext(
            conv_id="conv_capable",
            mode="normal",
            capabilities=("device.toy",),
        ),
    )[0]
    assert capable_result.status is ToolStatus.PENDING
    assert capable_result.error is None

    intimate_result = service.execute(
        [toy_intent],
        context=ToolContext(conv_id="conv_intimate", mode="intimate"),
    )[0]
    assert intimate_result.status is ToolStatus.PENDING


def test_tool_service_execute_async_runs_adapter_for_migrated_tool():
    service = ToolService()
    intent = parse_tool_intents("[HEART:悄悄话]")[0]
    calls = []

    async def heart_adapter(intent_arg, context):
        calls.append((intent_arg.arguments["content"], context.conv_id, context.msg_id))
        return {
            "type": "heart_whisper",
            "id": "hw_test",
            "msg_id": context.msg_id,
            "content": intent_arg.arguments["content"],
            "created_at": 123.0,
        }

    results = asyncio.run(service.execute_async(
        [intent],
        context=ToolContext(conv_id="conv_heart", msg_id="msg_heart"),
        adapters={"heart.whisper": heart_adapter},
    ))

    assert calls == [("悄悄话", "conv_heart", "msg_heart")]
    assert len(results) == 1
    assert results[0].status is ToolStatus.EXECUTED
    assert results[0].result["type"] == "heart_whisper"
    assert results[0].result["content"] == "悄悄话"
    assert results[0].metadata == {"service": "tool_service", "phase": "adapter"}
    assert [event.event_type for event in results[0].events] == [
        ToolEventType.EXECUTION_STARTED,
        ToolEventType.EXECUTION_FINISHED,
    ]


def test_tool_service_execute_async_keeps_policy_before_adapter():
    service = ToolService()
    intent = parse_tool_intents("[HEART:不要写入]")[0]

    async def fail_adapter(_intent, _context):
        raise AssertionError("adapter should not run")

    results = asyncio.run(service.execute_async(
        [intent],
        context=ToolContext(conv_id="conv_eval", msg_id="msg_eval", memory_eval_mode=True),
        adapters={"heart.whisper": fail_adapter},
    ))

    assert len(results) == 1
    assert results[0].status is ToolStatus.SKIPPED
    assert results[0].error == "memory_eval_mode"
    assert results[0].events[0].event_type is ToolEventType.POLICY_SKIPPED
