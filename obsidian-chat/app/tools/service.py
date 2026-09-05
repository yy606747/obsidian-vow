"""ToolService execution boundary.

Batch 4.2 introduces the service boundary without moving any legacy side-effect
execution. Existing chat code can inspect ToolResult/ToolEvent objects, while
postprocess/streaming keep the current behavior.
"""

from __future__ import annotations

from dataclasses import dataclass
import time
from typing import Any, Awaitable, Callable, Iterable, Mapping, Optional

from .schemas import (
    ToolContext,
    ToolEvent,
    ToolEventType,
    ToolIntent,
    ToolResult,
    ToolStatus,
)


ToolAdapter = Callable[[ToolIntent, ToolContext], Awaitable[Optional[Mapping[str, Any]]]]


@dataclass(frozen=True)
class ToolPlan:
    context: ToolContext
    intents: tuple[ToolIntent, ...]
    results: tuple[ToolResult, ...]

    @property
    def events(self) -> tuple[ToolEvent, ...]:
        return tuple(event for result in self.results for event in result.events)

    def to_dict(self) -> dict:
        return {
            "context": self.context.to_dict(),
            "intents": [intent.to_dict() for intent in self.intents],
            "results": [result.to_dict() for result in self.results],
            "events": [event.to_dict() for event in self.events],
        }


class ToolService:
    """Plan tool execution and run explicitly wired adapters."""

    def __init__(self, *, adapters: Mapping[str, ToolAdapter] | None = None):
        self._adapters = dict(adapters or {})

    def plan(
        self,
        intents: Iterable[ToolIntent],
        *,
        context: ToolContext,
    ) -> ToolPlan:
        intent_tuple = tuple(intents or ())
        results = tuple(self._plan_one(intent, context=context) for intent in intent_tuple)
        return ToolPlan(context=context, intents=intent_tuple, results=results)

    def execute(
        self,
        intents: Iterable[ToolIntent],
        *,
        context: ToolContext,
    ) -> list[ToolResult]:
        """Dry-run execute.

        Real adapters will replace this method incrementally. For now it returns
        the same results as ``plan`` and never triggers external effects.
        """
        return list(self.plan(intents, context=context).results)

    async def execute_async(
        self,
        intents: Iterable[ToolIntent],
        *,
        context: ToolContext,
        adapters: Mapping[str, ToolAdapter] | None = None,
    ) -> list[ToolResult]:
        """Execute adapter-backed tools and dry-run everything else.

        Adapter migration is incremental: callers opt into the tools they are
        ready to execute, while all other known intents keep the Batch 4.2
        dry-run behavior.
        """
        adapter_map = {**self._adapters, **dict(adapters or {})}
        results: list[ToolResult] = []
        for intent in tuple(intents or ()):
            policy_result = self._policy_result(intent, context=context)
            if policy_result is not None:
                results.append(policy_result)
                continue

            adapter = adapter_map.get(intent.tool_name)
            if adapter is None:
                results.append(self._pending(intent, context=context))
                continue

            results.append(await self._execute_with_adapter(intent, context=context, adapter=adapter))
        return results

    def _plan_one(self, intent: ToolIntent, *, context: ToolContext) -> ToolResult:
        policy_result = self._policy_result(intent, context=context)
        if policy_result is not None:
            return policy_result
        return self._pending(intent, context=context)

    def _policy_result(self, intent: ToolIntent, *, context: ToolContext) -> ToolResult | None:
        if context.memory_eval_mode:
            return self._skipped(intent, reason="memory_eval_mode", context=context)

        if not self._mode_or_capability_allows(intent, context):
            return self._skipped(intent, reason="mode_not_allowed", context=context)

        return None

    def _pending(self, intent: ToolIntent, *, context: ToolContext) -> ToolResult:
        event = ToolEvent(
            event_type=ToolEventType.INTENT_PARSED,
            tool_name=intent.tool_name,
            intent_id=intent.id,
            message="dry_run_pending",
            payload={
                "mode": context.mode,
                "side_effect_level": intent.side_effect_level.value,
                "requires_confirmation": intent.requires_confirmation,
            },
            created_at=time.time(),
        )
        return ToolResult.from_intent(
            intent,
            status=ToolStatus.PENDING,
            result={"dry_run": True, "policy": "pending"},
            events=[event],
            metadata={"service": "tool_service", "phase": "dry_run"},
        )

    async def _execute_with_adapter(
        self,
        intent: ToolIntent,
        *,
        context: ToolContext,
        adapter: ToolAdapter,
    ) -> ToolResult:
        started = ToolEvent(
            event_type=ToolEventType.EXECUTION_STARTED,
            tool_name=intent.tool_name,
            intent_id=intent.id,
            message="adapter_started",
            payload={
                "mode": context.mode,
                "side_effect_level": intent.side_effect_level.value,
                "requires_confirmation": intent.requires_confirmation,
            },
            created_at=time.time(),
        )
        try:
            adapter_result = await adapter(intent, context)
        except Exception as exc:
            failed = ToolEvent(
                event_type=ToolEventType.EXECUTION_FAILED,
                tool_name=intent.tool_name,
                intent_id=intent.id,
                message="adapter_failed",
                payload={"error": str(exc)},
                created_at=time.time(),
            )
            return ToolResult.from_intent(
                intent,
                status=ToolStatus.FAILED,
                error=str(exc),
                events=[started, failed],
                metadata={"service": "tool_service", "phase": "adapter"},
            )

        finished = ToolEvent(
            event_type=ToolEventType.EXECUTION_FINISHED,
            tool_name=intent.tool_name,
            intent_id=intent.id,
            message="adapter_finished",
            payload={"has_result": adapter_result is not None},
            created_at=time.time(),
        )
        return ToolResult.from_intent(
            intent,
            status=ToolStatus.EXECUTED,
            result=adapter_result or {},
            events=[started, finished],
            metadata={"service": "tool_service", "phase": "adapter"},
        )

    def _skipped(self, intent: ToolIntent, *, reason: str, context: ToolContext) -> ToolResult:
        event = ToolEvent(
            event_type=ToolEventType.POLICY_SKIPPED,
            tool_name=intent.tool_name,
            intent_id=intent.id,
            message=reason,
            payload={
                "mode": context.mode,
                "allowed_modes": list(intent.allowed_modes),
                "capabilities": list(context.capabilities),
                "memory_eval_mode": context.memory_eval_mode,
            },
            created_at=time.time(),
        )
        return ToolResult.from_intent(
            intent,
            status=ToolStatus.SKIPPED,
            error=reason,
            events=[event],
            metadata={"service": "tool_service", "phase": "dry_run"},
        )

    def _mode_or_capability_allows(self, intent: ToolIntent, context: ToolContext) -> bool:
        if intent.tool_name in context.capabilities:
            return True
        if not intent.allowed_modes:
            return True
        return context.mode in intent.allowed_modes


tool_service = ToolService()


__all__ = ["ToolAdapter", "ToolPlan", "ToolService", "tool_service"]
