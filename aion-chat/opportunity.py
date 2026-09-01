"""System-triggered opportunity turns executed by the target core model.

An opportunity is a normal core turn with no new real user message. The only
two-step action is reflection, whose second call follows deterministic harness
preparation. Synthetic triggers live only in provider input and are never
persisted.
"""

from __future__ import annotations

import random
import time
from dataclasses import dataclass
from typing import Any, Mapping

import aiosqlite

from ai_providers import call_core_chat_once
from app.chat.action_executor import ActionExecution, execute_postprocessed_actions
from app.chat.autonomous_capabilities import (
    autonomous_context_delivery_enabled,
    build_autonomous_runtime_context,
    load_autonomous_context_delivery,
    resolve_autonomous_capabilities,
)
from app.chat.error_text import looks_like_model_error_text
from app.chat.history import prepare_chat_history
from app.chat.postprocess import PostProcessor, looks_like_structured_reply
from app.chat.prompt_builder import (
    build_desire_block,
    build_opportunity_ability_block,
    build_v2_working_model_block,
    insert_prompt_ack,
)
from app.chat.worldbook import resolve_worldbook_names
from app.chat.turn_profiles import (
    OPPORTUNITY_REFLECT_TOKEN,
    TurnProfile,
    classify_opportunity_control_output,
    opportunity_turn_profile,
)
from app.memory_v3.recall_intent import strip_recall_intent_markers
from app.modes import mode_service  # compatibility injection seam for runtime tests
from app.presence.prompt_context import (
    build_presence_identity_block,
    presence_identity_head,
)
from app.web_search import web_search_service
from app.web_search.intent import strip_web_search_intent_markers, web_search_ability_block
from app.web_search.prompt import render_opportunity_pending_hint
from app.reflection.service import (
    CapturedReflectionContext,
    capture_reflection_context,
    run_reflection,
)
from app.tools.schemas import ToolContext
from app.tools.ledger import tool_invocation_ledger
from app.self_wake import SELF_WAKE_ENTRY_TOOLS
from app.self_wake.service import load_prompt_context as load_self_wake_prompt_context
from app.vows.service import VowReadError, vow_service
from app.working_model import service as working_model_service
from app.working_model.runtime import working_model_v2_injection_enabled
from app.working_model.writer import build_writer_identity_snapshot
from config import (
    load_ai_behavior,
    load_worldbook,  # compatibility seam retained for existing tests/extensions
)
from database import get_db
from ws import manager


FIBONACCI_INTERVALS_MIN = (21, 34, 55, 89)
OPPORTUNITY_SCHEMA_VERSION = "opportunity.turn.v2"

_CHAT_SILENCE_MIN_SEC = 20 * 60
_COOLDOWN_SEC = 10 * 60
_MAX_PER_HOUR = 2
_HOUR_SEC = 3600
_OPPORTUNITY_MAX_TOKENS = 4096
_OPPORTUNITY_RETRY_MAX_TOKENS = 8192
_LENGTH_FINISH_REASONS = frozenset({"length", "max_tokens", "max_output_tokens"})

_post_processor = PostProcessor()

# Narrow injection seam for the system-triggered target-core turn. Keeping a
# core-specific name makes it impossible to mistake this for a static slot.
call_opportunity_core = call_core_chat_once


def _length_limited(usage_meta: Mapping[str, Any]) -> bool:
    reason = str(usage_meta.get("finish_reason") or "").strip().lower()
    return reason in _LENGTH_FINISH_REASONS


@dataclass(frozen=True)
class PreparedOpportunityTurn:
    messages: list[dict]
    profile: TurnProfile
    model_key: str
    identity_snapshot: Mapping[str, str]
    reflection_context: CapturedReflectionContext | None
    mobile_screen_target: Mapping[str, str] | None
    kind: str = "idle"
    advertised_tools: tuple[str, ...] = ()
    user_name: str = ""
    ai_name: str = ""


def _configured_intervals_min() -> tuple[int, ...]:
    raw = load_ai_behavior().get("opportunity_intervals_min", FIBONACCI_INTERVALS_MIN)
    if not isinstance(raw, (list, tuple)):
        return FIBONACCI_INTERVALS_MIN
    values: list[int] = []
    for item in raw:
        try:
            value = int(item)
        except (TypeError, ValueError):
            continue
        if value > 0:
            values.append(value)
    return tuple(values) or FIBONACCI_INTERVALS_MIN


def _pick_interval_sec() -> int:
    return random.choice(_configured_intervals_min()) * 60


async def _runtime_capabilities(
    *,
    model_key: str,
    mobile_screen_target: Mapping[str, str] | None,
) -> frozenset[str]:
    return await resolve_autonomous_capabilities(
        model_key=model_key,
        mobile_screen_target=mobile_screen_target,
    )


def _runtime_context_text(
    *,
    now: float,
    last_user_ts: float,
    capabilities: frozenset[str],
    user_name: str,
    ai_name: str,
    context_delivery_text: str | None = None,
) -> str:
    return build_autonomous_runtime_context(
        now=now,
        last_user_ts=last_user_ts,
        capabilities=capabilities,
        user_name=user_name,
        ai_name=ai_name,
        context_delivery_text=context_delivery_text,
    )


async def _prepare_opportunity_turn(
    *,
    target: Mapping[str, Any],
    now: float,
    kind: str = "idle",
) -> PreparedOpportunityTurn:
    if kind not in {"idle", "summon", "night"}:
        raise ValueError("invalid_autonomous_kind")
    conv_id = str(target["conv_id"])
    model_key = str(target["model_key"])
    history_ctx = await prepare_chat_history(
        conv_id,
        context_limit=15,
        attachment_policy="last_user",
    )
    history = list(history_ctx.history)
    cap_idx = history_ctx.cap_idx
    inject_offset = 0

    vow_block, _vow_ability = await vow_service.load_vow_prompt_context()
    identity_snapshot = build_writer_identity_snapshot(
        history_ctx.wb,
        vow_block=vow_block,
    )
    inject_working_model = working_model_v2_injection_enabled()
    user_name, ai_name = resolve_worldbook_names(history_ctx.wb)
    reflection_enabled = kind != "summon" and bool(
        load_ai_behavior().get("working_model_reflection_enabled", False)
    )
    working_model_head: Mapping[str, Any] = {}
    desire_head: Mapping[str, Any] = {}
    if inject_working_model:
        working_model_head, desire_head = (
            await working_model_service.load_v2_prompt_heads()
        )

    reflection_context = None
    if reflection_enabled:
        try:
            reflection_head = working_model_head
            if not inject_working_model:
                reflection_head, _unused_desire_head = (
                    await working_model_service.load_v2_prompt_heads()
                )
            reflection_context = await capture_reflection_context(
                target_conv_id=conv_id,
                model_key=model_key,
                identity_snapshot=identity_snapshot,
                require_feature_flag=False,
                working_model_head=reflection_head,
            )
        except Exception:
            # Reflection is independently gated; inability to expose REFLECT
            # must not remove the rest of an otherwise valid opportunity turn.
            reflection_context = None

    mobile_screen_target = (
        await _autonomous_mobile_screen_target(model_key=model_key, now=now)
        if kind == "idle"
        else None
    )
    capabilities = set(await _runtime_capabilities(
        model_key=model_key,
        mobile_screen_target=mobile_screen_target,
    ))
    if kind == "idle":
        capabilities.update(SELF_WAKE_ENTRY_TOOLS)
    elif kind == "summon":
        capabilities.intersection_update({"desktop.presence.show"})
    else:
        capabilities.intersection_update({"desktop.presence.draw"})
    capabilities = frozenset(capabilities)
    web_search_snapshot = {"count": 0, "full": True, "recent_intent": ""}
    web_search_allowed = False
    if kind == "idle" and web_search_service.enabled():
        web_search_snapshot = await web_search_service.capacity_snapshot(conv_id)
        web_search_allowed = not bool(web_search_snapshot.get("full"))
    presence_requires_human = False
    if kind == "night" and "desktop.presence.draw" in capabilities:
        from app.presence import sprite_library

        presence_requires_human = not await sprite_library.has_non_seed_sprites()
        if presence_requires_human:
            # The empty-library bootstrap has exactly one exit: the first
            # self-chosen human image.  Reflection returns on later nights.
            reflection_context = None
    profile = opportunity_turn_profile(
        runtime_capabilities=capabilities,
        reflection_allowed=reflection_context is not None,
        web_search_allowed=web_search_allowed,
        kind=kind,
        presence_bootstrap_required=presence_requires_human,
    )
    ability_block = build_opportunity_ability_block(
        profile=profile,
        user_name=user_name,
        ai_name=ai_name,
        model_key=model_key,
        kind=kind,
        mobile_screen_target=mobile_screen_target,
        self_wake_context=(
            await load_self_wake_prompt_context(conv_id, now=now)
            if kind == "idle"
            else None
        ),
        presence_requires_human=presence_requires_human,
    )
    opportunity_web_blocks = []
    if web_search_allowed:
        opportunity_web_blocks.append(web_search_ability_block(allow_silent=True))
    pending_hint = render_opportunity_pending_hint(web_search_snapshot)
    if pending_hint:
        opportunity_web_blocks.append(pending_hint)
    ability_text = "\n\n".join([str(ability_block), *opportunity_web_blocks])

    if vow_block:
        inject_offset = insert_prompt_ack(
            history,
            cap_idx=cap_idx,
            inject_offset=inject_offset,
            content=vow_block,
            ack="（嗯，这些一直都算数。）",
        )
    if kind != "night":
        try:
            presence_identity = build_presence_identity_block(
                await presence_identity_head(),
                user_name=user_name,
                ai_name=ai_name,
            )
        except Exception:
            presence_identity = ""
        if presence_identity:
            inject_offset = insert_prompt_ack(
                history,
                cap_idx=cap_idx,
                inject_offset=inject_offset,
                content=presence_identity,
                ack="（嗯，这是我为自己留下的连续性基准。）",
            )
    inject_offset = insert_prompt_ack(
        history,
        cap_idx=cap_idx,
        inject_offset=inject_offset,
        content=ability_text,
        ack="（我知道自己现在能做什么。）",
    )
    if inject_working_model:
        inject_offset = insert_prompt_ack(
            history,
            cap_idx=cap_idx,
            inject_offset=inject_offset,
            content=build_v2_working_model_block(dict(working_model_head)),
            ack="（嗯，这是我此刻对她的认识。）",
        )
        inject_offset = insert_prompt_ack(
            history,
            cap_idx=cap_idx,
            inject_offset=inject_offset,
            content=build_desire_block(dict(desire_head)),
            ack="（嗯，这是我此刻想带进这段关系里的姿态。）",
        )
    context_delivery_text = None
    if autonomous_context_delivery_enabled():
        try:
            context_delivery_text = await load_autonomous_context_delivery(
                user_name=user_name,
                ai_name=ai_name,
                conv_id=conv_id,
                reference_time=now,
                exclude_summon_id=(
                    str(target.get("exclude_summon_id") or "") or None
                ),
            )
        except Exception:
            # An enabled shared projection fails closed; never revive legacy
            # sensing/location text on a durable-read failure.
            context_delivery_text = ""
    runtime_text = _runtime_context_text(
        now=now,
        last_user_ts=float(target["last_user_ts"]),
        capabilities=capabilities,
        user_name=user_name,
        ai_name=ai_name,
        context_delivery_text=context_delivery_text,
    )
    history.append({"role": "user", "content": runtime_text, "attachments": []})
    history.append({"role": "assistant", "content": "（嗯，知道了。）", "attachments": []})
    if kind == "night":
        if presence_requires_human:
            trigger_text = (
                f"现在是{ai_name}第一次为自己留下正式形象的夜间初始化。"
                f"{ai_name}不需要向{user_name}说话，任何正文都不会送达。\n"
                f"{ai_name}本轮只画一张自己选择的人形形象；不要解释，也不要输出其他出口。"
            )
        else:
            trigger_text = (
                f"现在是{ai_name}自己的夜间整理时刻。{ai_name}不需要向{user_name}说话，"
                "任何正文都不会送达。\n"
                f"{ai_name}只能单独选择一个出口：画一个形象、私下反思，或者精确输出 "
                "[OPPORTUNITY_NONE]。不要解释选择，也不要混用出口。"
            )
    elif kind == "summon":
        trigger_text = (
            f"{user_name}刚刚希望{ai_name}此刻出现。{user_name}没有附带消息。\n"
            f"本轮只有两种选择：使用桌面出现能力，或者精确、单独输出 [OPPORTUNITY_NONE]。\n"
            f"是否出现完全由{ai_name}自己决定。不要向{user_name}提到按钮、召唤或这段内部机制。"
        )
    else:
        trigger_text = (
            f"现在没人找{ai_name}，{ai_name}有一个空闲的瞬间。\n"
            f"选择的依据只有一个：{ai_name}现在想不想。不需要理由，不需要分析，不需要评分。"
            "做了就做了，不做就不做。\n"
            f"不要暴露这个机制的存在。不要对{user_name}说“突然想到”或“来看看”之类的话。"
        )
    history.append({"role": "user", "content": trigger_text, "attachments": []})
    return PreparedOpportunityTurn(
        kind=kind,
        messages=history,
        profile=profile,
        model_key=model_key,
        identity_snapshot=identity_snapshot,
        reflection_context=reflection_context,
        mobile_screen_target=mobile_screen_target,
        advertised_tools=tuple(ability_block.advertised_tools),
        user_name=user_name,
        ai_name=ai_name,
    )


class OpportunityRunner:
    def __init__(self):
        self._next_fire_at: float = 0
        self._attempt_timestamps: list[float] = []
        # Existing success counters remain behavioral/cooldown state; they are
        # deliberately not used as the new model-call budget.
        self._action_timestamps: list[float] = []
        self._last_action_at: float = 0
        self.enabled = False

    def start(self):
        self.enabled = True
        self._schedule_next()

    def stop(self):
        self.enabled = False
        self._next_fire_at = 0

    def status(self) -> dict:
        remaining = max(0, self._next_fire_at - time.time()) if self._next_fire_at else 0
        return {
            "enabled": self.enabled,
            "next_fire_in": round(remaining),
            "attempts_last_hour": self._count_recent_attempts(),
            "actions_last_hour": self._count_recent_actions(),
        }

    def _schedule_next(self):
        self._next_fire_at = time.time() + _pick_interval_sec()

    @staticmethod
    def _trim(values: list[float], now: float | None = None) -> list[float]:
        cutoff = (time.time() if now is None else now) - _HOUR_SEC
        return [value for value in values if value > cutoff]

    def _count_recent_attempts(self, now: float | None = None) -> int:
        self._attempt_timestamps = self._trim(self._attempt_timestamps, now)
        return len(self._attempt_timestamps)

    def _count_recent_actions(self, now: float | None = None) -> int:
        self._action_timestamps = self._trim(self._action_timestamps, now)
        return len(self._action_timestamps)

    async def maybe_fire(self) -> dict | None:
        if not self.enabled:
            if load_ai_behavior().get("opportunity_enabled", False):
                self.start()
            else:
                return None
        now = time.time()
        if now < self._next_fire_at:
            return None
        self._schedule_next()

        gate, target = await self._check_gates(now)
        if gate:
            return {"status": "gated", "reason": gate}
        result = await self._run(now, target or {})
        if result.get("status") == "acted":
            self._action_timestamps.append(now)
            self._last_action_at = now
        return result

    async def _check_gates(self, now: float) -> tuple[str | None, dict | None]:
        if not load_ai_behavior().get("opportunity_enabled", False):
            return "disabled", None
        if self._count_recent_attempts(now) >= _MAX_PER_HOUR:
            return "rate_limit", None
        if self._count_recent_actions(now) >= _MAX_PER_HOUR:
            return "rate_limit", None
        if self._last_action_at and now - self._last_action_at < _COOLDOWN_SEC:
            return "cooldown", None
        target = await _resolve_target_conv()
        if not target:
            return "no_conversation", None
        if float(target["last_user_ts"] or 0) <= 0:
            return "no_user_history", None
        if now - float(target["last_user_ts"]) < _CHAT_SILENCE_MIN_SEC:
            return "recent_chat", None
        if _is_quiet_hours(_load_cam_cfg()):
            return "quiet_hours", None
        return None, target

    async def _run(self, now: float, target: Mapping[str, Any]) -> dict:
        return await run_autonomous_turn(
            kind="idle",
            now=now,
            target=target,
            idle_runner=self,
        )

    async def _run_kind(
        self,
        kind: str,
        now: float,
        target: Mapping[str, Any],
    ) -> dict:
        try:
            prepared = await _prepare_opportunity_turn(
                target=target,
                now=now,
                kind=kind,
            )
        except VowReadError as exc:
            return await self._finish(
                now,
                kind=kind,
                branch="invalid",
                status="action_failed",
                error=f"vow_read_failed:{exc}",
            )
        except Exception as exc:
            return await self._finish(
                now,
                kind=kind,
                branch="invalid",
                status="action_failed",
                error=f"prompt_build_failed:{exc}",
            )

        msg_suffix = "opp" if kind == "idle" else kind
        msg_id = f"msg_{int(now * 1000)}_{msg_suffix}"
        invocation_id = tool_invocation_ledger.new_invocation_id(
            f"{kind}_core"
        )
        source_chain = "opportunity" if kind == "idle" else kind
        context = ToolContext(
            conv_id=str(target["conv_id"]),
            msg_id=msg_id,
            request_id=msg_id,
            model_key=prepared.model_key,
            mode="normal",
            capabilities=tuple(prepared.profile.allowed_tool_capabilities),
            metadata={
                "source": source_chain,
                "source_chain": source_chain,
                "invocation_id": invocation_id,
                "advertised_tools": prepared.advertised_tools,
                "wake_id": msg_id,
                "round_kind": kind,
                "mobile_target_device_id": (
                    prepared.mobile_screen_target or {}
                ).get("device_id"),
            },
        )

        async def finish_turn(
            status: str,
            *,
            turn_outcome: str,
            round_branch: str,
            **details: Any,
        ) -> dict:
            await tool_invocation_ledger.record_turn(
                context,
                prompt_source="opportunity",
                advertised_tools=prepared.advertised_tools,
                turn_outcome=turn_outcome,
                metadata={
                    "opportunity_status": status,
                    "visible_persisted": bool(details.get("visible")),
                    "round_kind": kind,
                    "round_branch": round_branch,
                },
            )
            return await self._finish(
                now,
                kind=kind,
                branch=round_branch,
                status=status,
                **details,
            )

        # Cost guard: one rolling-window attempt is recorded only after every
        # other gate and immediately before the target core is called.
        if kind == "idle":
            self._attempt_timestamps.append(now)
        usage_meta: dict[str, Any] = {}
        raw = ""
        for call_index, max_tokens in enumerate(
            (_OPPORTUNITY_MAX_TOKENS, _OPPORTUNITY_RETRY_MAX_TOKENS),
            1,
        ):
            usage_meta = {}
            try:
                raw = await call_opportunity_core(
                    prepared.model_key,
                    prepared.messages,
                    expect_json=False,
                    timeout=120.0,
                    temperature=0.9,
                    scope=f"{kind}:core_turn",
                    usage_meta=usage_meta,
                    max_tokens=max_tokens,
                )
            except Exception as exc:
                return await finish_turn(
                    status="provider_failed",
                    turn_outcome="provider_failed",
                    round_branch="provider_failed",
                    error=str(exc),
                )
            if not _length_limited(usage_meta):
                break
            if call_index == 2:
                return await finish_turn(
                    status="provider_failed",
                    turn_outcome="provider_failed",
                    round_branch="provider_failed",
                    raw_response=raw,
                    error="output_truncated_after_retry",
                    finish_reason=str(usage_meta.get("finish_reason") or ""),
                )
        raw = str(raw or "")
        if not raw.strip():
            last_call = usage_meta.get("provider_last") or {}
            status = "empty" if last_call.get("ok") else "provider_failed"
            return await finish_turn(
                status,
                turn_outcome=(
                    "invalid_output" if status == "empty" else "provider_failed"
                ),
                round_branch=(
                    "invalid" if status == "empty" else "provider_failed"
                ),
                raw_response=raw,
            )
        if looks_like_model_error_text(raw):
            return await finish_turn(
                status="provider_failed",
                turn_outcome="provider_failed",
                round_branch="provider_failed",
                raw_response=raw,
            )

        control = classify_opportunity_control_output(
            raw,
            profile=prepared.profile,
        )
        if control == "invalid":
            return await finish_turn(
                status="invalid_control_output",
                turn_outcome="invalid_output",
                round_branch="invalid",
                raw_response=raw,
            )
        if control == "none":
            return await finish_turn(
                status="none_explicit",
                turn_outcome="succeeded",
                round_branch="none",
                raw_response=raw,
            )
        if control == "reflect":
            if prepared.reflection_context is None:
                return await finish_turn(
                    status="invalid_control_output",
                    turn_outcome="invalid_output",
                    round_branch="invalid",
                    raw_response=raw,
                )
            try:
                reflection = await run_reflection(prepared.reflection_context)
            except Exception as exc:
                return await finish_turn(
                    status="action_failed",
                    turn_outcome="succeeded",
                    round_branch="reflect",
                    raw_response=raw,
                    error=f"reflection_runtime_failed:{exc}",
                )
            reflection_log = reflection.get("log") or {}
            return await finish_turn(
                status="acted" if reflection.get("entered") else "action_failed",
                turn_outcome="succeeded",
                round_branch="reflect",
                raw_response=raw,
                reflection={
                    "entered": bool(reflection.get("entered")),
                    "status": reflection.get("status"),
                    "log_id": reflection_log.get("id"),
                    "outcome": reflection_log.get("outcome"),
                },
            )

        try:
            try:
                postprocessed = await _post_processor.process(
                    raw,
                    conv_id=str(target["conv_id"]),
                    enabled_commands=prepared.profile.enabled_commands,
                    tool_context=context,
                )
            except TypeError as exc:
                if "tool_context" not in str(exc):
                    raise
                postprocessed = await _post_processor.process(
                    raw,
                    conv_id=str(target["conv_id"]),
                    enabled_commands=prepared.profile.enabled_commands,
                )
        except Exception as exc:
            return await finish_turn(
                status="action_failed",
                turn_outcome="postprocess_failed",
                round_branch="invalid",
                raw_response=raw,
                error=f"postprocess_failed:{exc}",
            )

        # WM/VOW/Recall candidates are intentionally ignored here. They are
        # private channels outside this profile and no corresponding scheduler
        # is called. Tool enforcement happens again inside the shared executor.
        visible_text = strip_web_search_intent_markers(
            strip_recall_intent_markers(postprocessed.content)
        ).strip()
        if looks_like_model_error_text(visible_text):
            visible_text = ""
        if looks_like_structured_reply(visible_text):
            # A malformed/actions-only envelope must never become a user-visible
            # JSON blob in an autonomous turn.
            visible_text = ""

        if kind in {"summon", "night"}:
            expected_tool = (
                "desktop.presence.show"
                if kind == "summon"
                else "desktop.presence.draw"
            )
            strict_intents = list(postprocessed.tool_intents)
            if (
                visible_text
                or len(strict_intents) != 1
                or strict_intents[0].tool_name != expected_tool
            ):
                return await finish_turn(
                    status="invalid_control_output",
                    turn_outcome="invalid_output",
                    round_branch="invalid",
                    raw_response=raw,
                )

        try:
            execution = await execute_postprocessed_actions(
                postprocessed,
                profile=prepared.profile,
                context=context,
            )
        except Exception as exc:
            execution = ActionExecution(())
            execution_error = str(exc)
        else:
            execution_error = ""

        if kind == "idle":
            await _broadcast_action_results(execution)
        web_search_result = {"status": "not_requested", "search_id": None}
        web_search_detected = kind == "idle" and bool(
            prepared.profile.allows_marker("web_search_intent")
            and str(getattr(postprocessed, "web_search_intent", "") or "").strip()
        )
        if web_search_detected:
            try:
                web_search_result = await web_search_service.enqueue_opportunity(
                    conv_id=str(target["conv_id"]),
                    origin_turn_id=msg_id,
                    intent_text=str(postprocessed.web_search_intent).strip(),
                )
            except Exception as exc:
                web_search_result = {
                    "status": "failed",
                    "search_id": None,
                    "error": type(exc).__name__,
                }
        web_search_queued = web_search_result.get("status") == "queued"
        visible_persisted = False
        if kind == "idle" and visible_text:
            visible_persisted = await _send_message(
                visible_text,
                conv_id=str(target["conv_id"]),
                user_name=prepared.user_name,
                ai_name=prepared.ai_name,
                msg_id=msg_id,
            )
        tool_executed = any(
            result.status.value == "executed"
            and not (
                isinstance(result.result, Mapping)
                and result.result.get("ok") is False
            )
            for result in execution.results
        )
        had_tool_intent = bool(postprocessed.tool_intents or postprocessed.ring_touch_descriptions)
        failed_results = [
            result for result in execution.results
            if result.status.value == "failed"
        ]
        provider_tool_failure = any(
            any(
                token in str(result.error or "")
                for token in (
                    "image_provider_",
                    "image_generation_",
                    "image_download_",
                )
            )
            for result in failed_results
        )
        if web_search_detected and not web_search_queued:
            status = "action_failed"
        elif visible_persisted or tool_executed or web_search_queued:
            status = "acted"
        elif had_tool_intent or visible_text or execution_error or web_search_detected:
            status = "action_failed"
        else:
            status = "empty"
        if provider_tool_failure:
            round_branch = "provider_failed"
        elif kind == "night" and tool_executed:
            round_branch = "draw"
        elif kind == "summon" and tool_executed:
            round_branch = "show"
        elif kind != "idle":
            round_branch = "invalid"
        elif visible_persisted:
            round_branch = "message"
        elif tool_executed:
            round_branch = "tool"
        elif web_search_queued:
            round_branch = "web_search"
        else:
            round_branch = "invalid"
        return await finish_turn(
            status=status,
            turn_outcome="succeeded",
            round_branch=round_branch,
            raw_response=raw,
            visible=visible_persisted,
            tool_results=[result.to_dict() for result in execution.results],
            web_search_queued=web_search_queued,
            web_search_status=web_search_result.get("status"),
            error=execution_error,
        )

    async def _finish(
        self,
        now: float,
        *,
        kind: str,
        branch: str,
        status: str,
        **details: Any,
    ) -> dict:
        entry = {
            "schema_version": OPPORTUNITY_SCHEMA_VERSION,
            "timestamp": now,
            "time": time.strftime("%H:%M:%S", time.localtime(now)),
            "status": status,
            "executed": status == "acted",
            "round_kind": kind,
            "round_branch": branch,
            **details,
        }
        if "raw_response" in entry:
            entry["raw_response"] = strip_web_search_intent_markers(
                strip_recall_intent_markers(str(entry["raw_response"] or ""))
            )[:500]
        if kind == "idle":
            await _broadcast_log(entry)
        return entry


async def run_autonomous_turn(
    *,
    kind: str,
    now: float,
    target: Mapping[str, Any],
    idle_runner: OpportunityRunner | None = None,
) -> dict[str, Any]:
    """Run one Core turn with kind-frozen prompt, parser, and side effects."""

    if kind not in {"idle", "summon", "night"}:
        raise ValueError("invalid_autonomous_kind")
    executor = idle_runner or OpportunityRunner()
    return await executor._run_kind(kind, float(now), target)


async def _broadcast_action_results(execution: ActionExecution) -> None:
    for result in execution.executed:
        payload = dict(result.result or {})
        if result.tool_name == "heart.whisper" and payload:
            await manager.broadcast({"type": "heart_whisper", "data": payload})
        elif result.tool_name == "location.poi_search" and payload:
            await manager.broadcast({"type": "poi_search", "data": payload})
        elif result.tool_name in {"pc.screen_check", "mobile.screen_check"} and payload:
            event_type = payload.get("type", "screen_check_pending")
            await manager.broadcast({"type": event_type, "data": payload})
            if (
                result.tool_name == "mobile.screen_check"
                and event_type == "screen_check_pending"
            ):
                from app.mobile_screen.autonomous import record_autonomous_mobile_screen_request

                record_autonomous_mobile_screen_request()


async def _resolve_target_conv() -> dict | None:
    try:
        from config import DEFAULT_MODEL

        async with get_db() as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                "SELECT c.id, c.model FROM conversations c "
                "WHERE EXISTS (SELECT 1 FROM messages m WHERE m.conv_id=c.id AND m.role='user') "
                "ORDER BY c.updated_at DESC LIMIT 1"
            )
            conv = await cursor.fetchone()
            if not conv:
                return None
            cursor = await db.execute(
                "SELECT created_at FROM messages WHERE conv_id=? AND role='user' "
                "ORDER BY created_at DESC LIMIT 1",
                (conv["id"],),
            )
            row = await cursor.fetchone()
        return {
            "conv_id": conv["id"],
            "model_key": conv["model"] or DEFAULT_MODEL,
            "last_user_ts": row["created_at"] if row else 0,
        }
    except Exception:
        return None


async def _send_message(
    text: str,
    *,
    conv_id: str,
    user_name: str,
    ai_name: str,
    msg_id: str | None = None,
) -> bool:
    del user_name
    content = strip_web_search_intent_markers(
        strip_recall_intent_markers(str(text or ""))
    ).strip()
    if not content or looks_like_model_error_text(content):
        return False
    now = time.time()
    assistant_id = msg_id or f"msg_{int(now * 1000)}_opp"
    system_id = f"{assistant_id}_sys"
    system_content = f"💭 {ai_name}想起了什么"
    try:
        async with get_db() as db:
            await db.execute("BEGIN IMMEDIATE")
            await db.execute(
                "INSERT INTO messages (id, conv_id, role, content, created_at, attachments) "
                "VALUES (?,?,?,?,?,?)",
                (system_id, conv_id, "system", system_content, now, "[]"),
            )
            await db.execute(
                "INSERT INTO messages (id, conv_id, role, content, created_at, attachments) "
                "VALUES (?,?,?,?,?,?)",
                (assistant_id, conv_id, "assistant", content, now + 0.001, "[]"),
            )
            await db.execute(
                "UPDATE conversations SET updated_at=? WHERE id=?",
                (now + 0.001, conv_id),
            )
            await db.commit()
    except Exception:
        return False
    await manager.broadcast(
        {
            "type": "msg_created",
            "data": {
                "id": system_id,
                "conv_id": conv_id,
                "role": "system",
                "content": system_content,
                "created_at": now,
                "attachments": [],
            },
        }
    )
    await manager.broadcast(
        {
            "type": "msg_created",
            "data": {
                "id": assistant_id,
                "conv_id": conv_id,
                "role": "assistant",
                "content": content,
                "created_at": now + 0.001,
                "attachments": [],
            },
        }
    )
    try:
        from routes.files import export_conversation

        await export_conversation(conv_id)
    except Exception:
        pass
    return True


async def _autonomous_mobile_screen_target(*, model_key: str, now: float) -> dict | None:
    try:
        from app.mobile_screen.autonomous import autonomous_mobile_screen_target

        return await autonomous_mobile_screen_target(model_key=model_key, now=now)
    except Exception:
        return None


async def _broadcast_log(entry: dict) -> None:
    try:
        payload = dict(entry)
        # Reflection is owner-auditable through reflection_log/wm_audit, but it
        # is not a chat/UI performance.  Do not put its marker, clue, evidence,
        # verdict, or downstream writer data onto the general websocket.
        if (
            payload.get("reflection") is not None
            or str(payload.get("raw_response") or "").strip()
            == OPPORTUNITY_REFLECT_TOKEN
        ):
            payload.pop("reflection", None)
            payload.pop("raw_response", None)
            payload.pop("error", None)
        await manager.broadcast({"type": "opportunity_log", "data": payload})
    except Exception:
        pass


def _load_cam_cfg() -> dict:
    from config import load_cam_config

    return load_cam_config()


def _is_quiet_hours(cam_cfg: dict) -> bool:
    if not cam_cfg.get("quiet_hours_enabled", False):
        return False
    start_str = cam_cfg.get("quiet_hours_start", "00:00")
    end_str = cam_cfg.get("quiet_hours_end", "09:00")
    try:
        sh, sm = map(int, start_str.split(":"))
        eh, em = map(int, end_str.split(":"))
    except (ValueError, AttributeError):
        return False
    now = time.localtime()
    current = now.tm_hour * 60 + now.tm_min
    start = sh * 60 + sm
    end = eh * 60 + em
    if start <= end:
        return start <= current < end
    return current >= start or current < end


opportunity_runner = OpportunityRunner()
