"""Self-Wake entry adapters, prompt status and async scan runner."""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Collection, Mapping
from typing import Any

from app.tools.registry import registered_tools_for_surface
from app.tools.schemas import ToolContext, ToolIntent

from . import SELF_WAKE_SURFACE_CAPABILITIES
from .repository import (
    MAX_WAKE_CALLS_PER_DAY,
    SelfWakeRepositoryError,
    self_wake_repository,
)
from .time_policy import (
    SelfWakeTimeError,
    format_owner_time,
    owner_timezone_name,
    parse_wake_at,
    quiet_hours_snapshot,
    validate_not_quiet,
)


logger = logging.getLogger(__name__)


def _rejected(reason: str, **details: Any) -> dict[str, Any]:
    return {
        "ok": False,
        "status": "rejected",
        "reason": str(reason or "rejected"),
        **details,
    }


def _entry_source(context: ToolContext) -> str | None:
    raw = str(context.metadata.get("source") or "").strip()
    if raw in {"send", "regenerate"}:
        return "chat"
    if raw == "opportunity":
        return "opportunity"
    return None


def _requested_capabilities(value: Any) -> tuple[str, ...] | None:
    if value is None:
        return ()
    if isinstance(value, str):
        values: Collection[Any] = value.split(",")
    elif isinstance(value, Collection) and not isinstance(value, Mapping):
        values = value
    else:
        return None
    return tuple(sorted({str(item).strip() for item in values if str(item).strip()}))


def self_wake_surface_capabilities() -> frozenset[str]:
    registered = frozenset(registered_tools_for_surface("self_wake"))
    # Registry validation also checks each binding independently. This exact
    # equality protects the frozen V1 surface against accidental expansion.
    if registered != SELF_WAKE_SURFACE_CAPABILITIES:
        return SELF_WAKE_SURFACE_CAPABILITIES & registered
    return registered


async def execute_schedule_intent(
    intent: ToolIntent,
    context: ToolContext,
) -> dict[str, Any]:
    source = _entry_source(context)
    if source is None:
        return _rejected("unsupported_source")
    source_turn_id = str(context.msg_id or context.request_id or "").strip()
    if not source_turn_id:
        return _rejected("missing_source_turn_id")

    requested = _requested_capabilities(
        intent.arguments.get("requested_capabilities")
    )
    if requested is None:
        return _rejected("invalid_requested_capabilities")
    legal = self_wake_surface_capabilities()
    unknown = sorted(set(requested) - legal)
    if unknown:
        return _rejected("unknown_requested_capability", unknown_capabilities=unknown)

    now = time.time()
    timezone_name = owner_timezone_name()
    try:
        wake_at = parse_wake_at(
            str(intent.arguments.get("wake_at") or ""),
            now=now,
            timezone_name=timezone_name,
        )
        validate_not_quiet(wake_at, timezone_name=timezone_name)
        result = await self_wake_repository.schedule_or_replace(
            wake_at=wake_at,
            intent=str(intent.arguments.get("intent") or ""),
            requested_capabilities=requested,
            origin="relationship",
            origin_ref=context.conv_id,
            source=source,
            conv_id=context.conv_id,
            source_turn_id=source_turn_id,
            owner_timezone=timezone_name,
            now=now,
        )
    except (SelfWakeTimeError, SelfWakeRepositoryError) as exc:
        return _rejected(exc.reason)
    except Exception as exc:
        return _rejected("self_wake_storage_failed", error=type(exc).__name__)

    wake = dict(result["wake"])
    replaced = dict(result["replaced"] or {})
    return {
        "ok": True,
        "status": "succeeded",
        "wake_id": wake["id"],
        "wake_at": wake["wake_at"],
        "expires_at": wake["expires_at"],
        "intent": wake["intent"],
        "requested_capabilities": wake["requested_capabilities"],
        "replaced_wake_id": replaced.get("id"),
        "replaced_wake_at": replaced.get("wake_at"),
        "replaced_intent": replaced.get("intent"),
    }


async def execute_cancel_intent(
    _intent: ToolIntent,
    context: ToolContext,
) -> dict[str, Any]:
    if _entry_source(context) is None:
        return _rejected("unsupported_source")
    try:
        result = await self_wake_repository.cancel_pending(
            origin="relationship",
            origin_ref=context.conv_id,
        )
    except SelfWakeRepositoryError as exc:
        return _rejected(exc.reason)
    except Exception as exc:
        return _rejected("self_wake_storage_failed", error=type(exc).__name__)
    if not result.get("ok"):
        return _rejected(str(result.get("reason") or "no_pending_wake"))
    wake = dict(result.get("wake") or {})
    return {
        "ok": True,
        "status": "succeeded",
        "cancelled_wake_id": wake.get("id"),
        "cancelled_wake_at": wake.get("wake_at"),
        "cancelled_intent": wake.get("intent"),
    }


async def load_prompt_context(
    conv_id: str,
    *,
    now: float | None = None,
) -> dict[str, Any]:
    current = time.time() if now is None else float(now)
    timezone_name = owner_timezone_name()
    try:
        status = await self_wake_repository.load_prompt_status(
            origin="relationship",
            origin_ref=conv_id,
            now=current,
            timezone_name=timezone_name,
        )
    except Exception:
        status = {
            "pending": None,
            "quota": {
                "limit": MAX_WAKE_CALLS_PER_DAY,
                "used": 0,
                "remaining": MAX_WAKE_CALLS_PER_DAY,
            },
            "recent_nonexecution": None,
            "owner_timezone": timezone_name,
        }
    quiet = quiet_hours_snapshot()
    return {
        "self_wake_status": status,
        "self_wake_now": current,
        "self_wake_now_local": format_owner_time(
            current,
            timezone_name=timezone_name,
        ),
        "self_wake_timezone": timezone_name,
        "self_wake_quiet_hours": quiet,
        "self_wake_legal_capabilities": tuple(sorted(self_wake_surface_capabilities())),
    }


def render_prompt_status(context: Mapping[str, Any]) -> str:
    status = dict(context.get("self_wake_status") or {})
    timezone_name = str(context.get("self_wake_timezone") or owner_timezone_name())
    quiet = dict(context.get("self_wake_quiet_hours") or {})
    quota = dict(status.get("quota") or {})
    pending = status.get("pending")
    recent = status.get("recent_nonexecution")
    legal = tuple(context.get("self_wake_legal_capabilities") or ())
    limit = quota.get("limit", MAX_WAKE_CALLS_PER_DAY)
    used = quota.get("used", 0)
    remaining = quota.get("remaining", MAX_WAKE_CALLS_PER_DAY)
    lines = [
        "【你留给未来自己的约定】",
        f"现在是 {context.get('self_wake_now_local')}（{timezone_name}）。",
        (
            f"今天系统允许你在所有对话中合计尝试主动回来 {limit} 次；"
            f"已经尝试 {used} 次，还能再尝试 {remaining} 次。"
        ),
        (
            "那时可以预留的动作名：" + ("、".join(legal) or "无") +
            "。也可以一个都不预留，只回来看看或说句话。"
        ),
    ]
    if quiet.get("enabled"):
        lines.insert(
            2,
            f"安静时段是 {quiet.get('start')}–{quiet.get('end')}；"
            "不要把回来时间留在这段时间里。",
        )
    else:
        lines.insert(2, "目前没有安静时段限制。")
    if isinstance(pending, Mapping):
        requested = pending.get("requested_capabilities") or ()
        lines.append(
            "你已经约好在 "
            f"{format_owner_time(float(pending.get('wake_at')), timezone_name=timezone_name)} "
            f"回来，留给那时自己的念头是：“{str(pending.get('intent') or '')[:300]}”。"
        )
        lines.append(
            "希望那时可用的动作："
            f"{','.join(str(item) for item in requested) or '没有预留动作'}。"
            "如果现在留下新的回来时间，之前这次约定会被覆盖。"
        )
    else:
        lines.append("你现在没有给未来的自己留下回来时间。")
    if isinstance(recent, Mapping):
        outcome_text = {
            "daily_quota_exhausted": "当天所有对话合计的主动回来次数已经用完",
            "expired": "系统醒来时已经比约定晚了两个小时以上",
            "provider_failed": "那次生成没有成功",
        }.get(str(recent.get("outcome") or ""), "那次没有完成")
        lines.append(
            "最近两天有一次没有回来：原本想在 "
            f"{format_owner_time(float(recent.get('wake_at')), timezone_name=timezone_name)} "
            f"回来做“{str(recent.get('intent') or '')[:300]}”，但{outcome_text}。"
        )
    return "\n".join(lines)


class SelfWakeRunner:
    """Claim due rows quickly and trigger each in an independent task."""

    def __init__(self, *, repository=None, trigger=None):
        self.repository = repository or self_wake_repository
        self._trigger = trigger
        self.in_flight: set[asyncio.Task] = set()
        self._wake_by_task: dict[asyncio.Task, Mapping[str, Any]] = {}

    def _trigger_callable(self):
        if self._trigger is not None:
            return self._trigger
        from .trigger import fire_claimed_wake

        return fire_claimed_wake

    def _on_done(self, task: asyncio.Task) -> None:
        self.in_flight.discard(task)
        self._wake_by_task.pop(task, None)
        if task.cancelled():
            return
        try:
            error = task.exception()
        except Exception:
            logger.warning("self-wake trigger task failed", exc_info=True)
            return
        if error is not None:
            logger.warning("self-wake trigger task failed: %s", error)

    async def scan_once(self) -> int:
        claimed = await self.repository.claim_due_batch()
        trigger = self._trigger_callable()
        for wake in claimed:
            task = asyncio.create_task(
                trigger(wake),
                name=f"self_wake:{wake.get('id')}",
            )
            self.in_flight.add(task)
            self._wake_by_task[task] = wake
            task.add_done_callback(self._on_done)
        return len(claimed)

    async def shutdown(self) -> None:
        active = [task for task in self.in_flight if not task.done()]
        wakes = {task: self._wake_by_task.get(task, {}) for task in active}
        for task in active:
            task.cancel()
        if active:
            await asyncio.gather(*active, return_exceptions=True)
        # A task can be cancelled before its coroutine enters the trigger's
        # own CancelledError handler. Rewriting the same terminal outcome is
        # harmless and closes that small startup/shutdown race.
        for task in active:
            if not task.cancelled():
                continue
            wake_id = str(wakes[task].get("id") or "")
            if not wake_id:
                continue
            try:
                await self.repository.finish_trigger(
                    wake_id,
                    outcome="cancelled_on_shutdown",
                    error="",
                )
            except Exception:
                logger.warning(
                    "self-wake shutdown outcome write failed (%s)",
                    wake_id,
                    exc_info=True,
                )

    async def run_scan_loop(self, *, interval_sec: float = 10.0) -> None:
        interval = max(0.01, float(interval_sec))
        try:
            while True:
                try:
                    await self.scan_once()
                except asyncio.CancelledError:
                    raise
                except Exception:
                    logger.warning("self-wake scan failed", exc_info=True)
                await asyncio.sleep(interval)
        finally:
            await self.shutdown()


self_wake_runner = SelfWakeRunner()


async def run_scan_loop(interval_sec: float = 10.0) -> None:
    await self_wake_runner.run_scan_loop(interval_sec=interval_sec)


__all__ = [
    "execute_cancel_intent",
    "execute_schedule_intent",
    "load_prompt_context",
    "render_prompt_status",
    "run_scan_loop",
    "SelfWakeRunner",
    "self_wake_surface_capabilities",
    "self_wake_runner",
]
