"""Shared, narrowly scoped execution layer for post-processed turn actions.

Provider calls, message persistence, SSE, broadcasts, and follow-up scheduling
remain owned by each outer turn. This module only converts the existing
``PostProcessResult`` surfaces to existing adapters, enforces the turn profile,
and returns auditable ``ToolResult`` objects.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
import time
from typing import Any

from app.tools.schemas import (
    ToolContext,
    ToolEvent,
    ToolEventType,
    ToolIntent,
    ToolResult,
    ToolStatus,
)
from app.tools.ledger import ToolInvocationLedger, tool_invocation_ledger
from app.tools.service import ToolAdapter, ToolService, tool_service

from .turn_profiles import TurnProfile


_SCHEDULE_GROUP = "__schedule__"
_SELF_WAKE_GROUP = "__self_wake__"
_SCHEDULE_CAPABILITIES = frozenset(
    {
        "schedule.alarm",
        "schedule.reminder",
        "schedule.monitor",
        "schedule.delete",
        "schedule.list",
    }
)
_SELF_WAKE_CAPABILITIES = frozenset({"self_wake.schedule", "self_wake.cancel"})

_EXECUTION_ORDER = (
    "music.search",
    _SCHEDULE_GROUP,
    _SELF_WAKE_GROUP,
    "heart.whisper",
    "memory.remember",
    "desktop.presence.show",
    "desktop.presence.draw",
    "device.toy",
    "device.ring_touch",
    "monitor.camera",
    "location.poi_search",
    "activity.summary",
    "pc.screen_check",
    "mobile.screen_check",
)

EXECUTOR_TOOL_CAPABILITIES = frozenset(
    {
        *(_SCHEDULE_CAPABILITIES),
        *(
            tool_name
            for tool_name in _EXECUTION_ORDER
            if tool_name not in {_SCHEDULE_GROUP, _SELF_WAKE_GROUP, "monitor.camera"}
        ),
        *(_SELF_WAKE_CAPABILITIES),
    }
)


@dataclass(frozen=True)
class ActionExecution:
    results: tuple[ToolResult, ...]

    def results_for(self, *tool_names: str) -> list[ToolResult]:
        wanted = set(tool_names)
        return [result for result in self.results if result.tool_name in wanted]

    @property
    def executed(self) -> list[ToolResult]:
        return [result for result in self.results if result.status is ToolStatus.EXECUTED]


def _profile_denied(intent: ToolIntent, profile: TurnProfile) -> ToolResult:
    event = ToolEvent(
        event_type=ToolEventType.POLICY_SKIPPED,
        tool_name=intent.tool_name,
        intent_id=intent.id,
        message="turn_profile_not_allowed",
        payload={"prompt_source": profile.prompt_source},
        created_at=time.time(),
    )
    return ToolResult.from_intent(
        intent,
        status=ToolStatus.SKIPPED,
        error="turn_profile_not_allowed",
        events=[event],
        metadata={"service": "action_executor", "phase": "profile"},
    )


def _extra_alarm_denied(intent: ToolIntent) -> ToolResult:
    event = ToolEvent(
        event_type=ToolEventType.POLICY_SKIPPED,
        tool_name=intent.tool_name,
        intent_id=intent.id,
        message="one_alarm_per_turn",
        payload={},
        created_at=time.time(),
    )
    return ToolResult.from_intent(
        intent,
        status=ToolStatus.SKIPPED,
        error="one_alarm_per_turn",
        events=[event],
        metadata={"service": "action_executor", "phase": "turn_limit"},
    )


def _extra_presence_denied(intent: ToolIntent) -> ToolResult:
    event = ToolEvent(
        event_type=ToolEventType.POLICY_SKIPPED,
        tool_name=intent.tool_name,
        intent_id=intent.id,
        message="one_presence_intent_per_turn",
        payload={},
        created_at=time.time(),
    )
    return ToolResult.from_intent(
        intent,
        status=ToolStatus.SKIPPED,
        error="one_presence_intent_per_turn",
        events=[event],
        metadata={"service": "action_executor", "phase": "turn_limit"},
    )


def _extra_self_wake_denied(intent: ToolIntent) -> ToolResult:
    event = ToolEvent(
        event_type=ToolEventType.POLICY_SKIPPED,
        tool_name=intent.tool_name,
        intent_id=intent.id,
        message="one_self_wake_action_per_turn",
        payload={},
        created_at=time.time(),
    )
    return ToolResult.from_intent(
        intent,
        status=ToolStatus.SKIPPED,
        error="one_self_wake_action_per_turn",
        events=[event],
        metadata={"service": "action_executor", "phase": "turn_limit"},
    )


async def _standard_bindings(
    postprocessed: Any,
    context: ToolContext,
    *,
    profile: TurnProfile,
    selected: set[str] | None,
    allow_toy_fallback: bool,
    has_error: bool,
) -> dict[str, tuple[list[ToolIntent], ToolAdapter]]:
    # Imported lazily because streaming owns the already-shipped adapters and
    # itself imports this execution layer. At call time both modules are fully
    # initialized; no second adapter implementation is introduced here.
    from . import streaming
    from app.presence.renderer import execute_presence_show
    from app.presence.sprites import execute_presence_draw
    from app.self_wake.service import (
        execute_cancel_intent,
        execute_schedule_intent,
    )

    async def execute_self_wake(intent: ToolIntent, tool_context: ToolContext):
        if intent.tool_name == "self_wake.schedule":
            return await execute_schedule_intent(intent, tool_context)
        return await execute_cancel_intent(intent, tool_context)

    toy_intents = streaming._toy_command_intents(postprocessed)
    if allow_toy_fallback:
        toy_intents = streaming._with_control_toy_fallback(
            toy_intents,
            context,
            has_error=has_error,
        )
    build_ring = (
        profile.allows_tool("device.ring_touch")
        and (selected is None or "device.ring_touch" in selected)
    )
    # Translation is itself a paid harness call, so even intent construction
    # must happen after the profile/phase gate.
    ring_intents = (
        await streaming._ring_touch_intents(postprocessed, context)
        if build_ring
        else []
    )
    schedule_intents = streaming._schedule_intents(postprocessed)

    bindings: dict[str, tuple[list[ToolIntent], ToolAdapter]] = {
        "music.search": (
            streaming._music_search_intents(postprocessed),
            streaming._execute_music_search,
        ),
        "heart.whisper": (
            streaming._heart_whisper_intents(postprocessed),
            streaming._execute_heart_whisper,
        ),
        "memory.remember": (
            streaming._remember_intents(postprocessed),
            streaming._execute_remember_note,
        ),
        "desktop.presence.draw": (
            [
                intent
                for intent in postprocessed.tool_intents
                if intent.tool_name == "desktop.presence.draw"
            ],
            execute_presence_draw,
        ),
        "desktop.presence.show": (
            [
                intent
                for intent in postprocessed.tool_intents
                if intent.tool_name == "desktop.presence.show"
            ],
            execute_presence_show,
        ),
        "device.toy": (toy_intents, streaming._execute_toy_command),
        "device.ring_touch": (ring_intents, streaming._execute_ring_touch),
        "monitor.camera": (
            streaming._camera_check_intents(postprocessed),
            streaming._execute_camera_check,
        ),
        "location.poi_search": (
            streaming._poi_search_intents(postprocessed),
            streaming._execute_poi_search,
        ),
        "activity.summary": (
            streaming._activity_summary_intents(postprocessed),
            streaming._execute_activity_summary,
        ),
        "pc.screen_check": (
            streaming._screen_check_intents(postprocessed),
            streaming._execute_screen_check,
        ),
        "mobile.screen_check": (
            streaming._mobile_screen_check_intents(postprocessed),
            streaming._execute_mobile_screen_check,
        ),
    }
    # Schedules are one group because their original executor preserved marker
    # order across alarm/reminder/delete types. Splitting by tool name would be
    # a subtle ordinary-chat behavior regression.
    bindings[_SCHEDULE_GROUP] = (
        schedule_intents,
        streaming._execute_schedule_command,
    )
    bindings[_SELF_WAKE_GROUP] = (
        [
            intent
            for intent in postprocessed.tool_intents
            if intent.tool_name in _SELF_WAKE_CAPABILITIES
        ],
        execute_self_wake,
    )
    return bindings


async def execute_postprocessed_actions(
    postprocessed: Any,
    *,
    profile: TurnProfile,
    context: ToolContext,
    only_capabilities: frozenset[str] | None = None,
    adapter_overrides: Mapping[str, ToolAdapter] | None = None,
    tool_service_override: ToolService | None = None,
    ledger_override: ToolInvocationLedger | None = None,
    allow_toy_fallback: bool = False,
    has_error: bool = False,
) -> ActionExecution:
    """Execute selected action groups after a strict source-profile check."""

    selected = set(only_capabilities) if only_capabilities is not None else None
    bindings = await _standard_bindings(
        postprocessed,
        context,
        profile=profile,
        selected=selected,
        allow_toy_fallback=allow_toy_fallback,
        has_error=has_error,
    )
    overrides = dict(adapter_overrides or {})
    executor_service = tool_service_override or tool_service
    results: list[ToolResult] = []
    intents_by_id: dict[str, ToolIntent] = {}
    presence_seen = False

    for tool_name in _EXECUTION_ORDER:
        schedule_group = tool_name == _SCHEDULE_GROUP
        self_wake_group = tool_name == _SELF_WAKE_GROUP
        if selected is not None and (
            (
                not (selected & _SCHEDULE_CAPABILITIES)
                if schedule_group
                else not (selected & _SELF_WAKE_CAPABILITIES)
                if self_wake_group
                else tool_name not in selected
            )
        ):
            continue
        intents, default_adapter = bindings.get(tool_name, ([], None))
        if not intents:
            continue
        intents_by_id.update({intent.id: intent for intent in intents})
        allowed: list[ToolIntent] = []
        for intent in intents:
            if not profile.allows_tool(intent.tool_name):
                results.append(_profile_denied(intent, profile))
            elif intent.tool_name.startswith("desktop.presence.") and presence_seen:
                results.append(_extra_presence_denied(intent))
            else:
                allowed.append(intent)
                if intent.tool_name.startswith("desktop.presence."):
                    presence_seen = True
        if schedule_group:
            limited: list[ToolIntent] = []
            alarm_seen = False
            for intent in allowed:
                if intent.tool_name == "schedule.alarm":
                    if alarm_seen:
                        results.append(_extra_alarm_denied(intent))
                        continue
                    alarm_seen = True
                limited.append(intent)
            allowed = limited
        if self_wake_group and len(allowed) > 1:
            for extra in allowed[1:]:
                results.append(_extra_self_wake_denied(extra))
            allowed = allowed[:1]
        if not allowed:
            continue
        if schedule_group or self_wake_group:
            adapters = {
                intent.tool_name: overrides.get(intent.tool_name) or default_adapter
                for intent in allowed
                if overrides.get(intent.tool_name) or default_adapter
            }
        else:
            adapter = overrides.get(tool_name) or default_adapter
            adapters = {tool_name: adapter} if adapter is not None else {}
        if not adapters:
            # A profile may never turn absence of an adapter into execution.
            results.extend(
                await executor_service.execute_async(allowed, context=context, adapters={})
            )
            continue
        results.extend(
            await executor_service.execute_async(
                allowed,
                context=context,
                adapters=adapters,
            )
        )

    ledger = ledger_override if ledger_override is not None else tool_invocation_ledger
    await ledger.record_execution(
        context,
        results=results,
        intents_by_id=intents_by_id,
    )
    return ActionExecution(tuple(results))


__all__ = [
    "ActionExecution",
    "EXECUTOR_TOOL_CAPABILITIES",
    "execute_postprocessed_actions",
]
