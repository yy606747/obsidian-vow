"""Dedicated provider turn for an atomically consumed Self-Wake row."""

from __future__ import annotations

import asyncio
import json
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
    build_v2_working_model_block,
    insert_prompt_ack,
)
from app.chat.worldbook import resolve_worldbook_names
from app.chat.turn_profiles import (
    SELF_WAKE_NONE_TOKEN,
    TurnProfile,
    classify_self_wake_control_output,
    self_wake_turn_profile,
)
from app.memory_v3.recall_intent import strip_recall_intent_markers
from app.presence.prompt_context import (
    build_presence_identity_block,
    presence_identity_head,
)
from app.tools.ledger import execution_outcome, tool_invocation_ledger
from app.tools.prompt_renderers import render_registered_capabilities
from app.tools.registry import registered_tools_for_surface, validate_turn_advertisement
from app.tools.schemas import ToolContext
from app.vows.service import vow_service
from app.web_search.intent import strip_web_search_intent_markers
from app.working_model import service as working_model_service
from app.working_model.runtime import working_model_v2_injection_enabled
from app.working_model.writer import build_writer_identity_snapshot
from config import DEFAULT_MODEL, load_ai_behavior
from database import get_db
from routes.files import export_conversation
from ws import manager

from . import SELF_WAKE_SURFACE_CAPABILITIES
from .repository import self_wake_repository
from .time_policy import format_owner_time, owner_timezone_name


SELF_WAKE_PROVIDER_TIMEOUT_SECONDS = 120.0
SELF_WAKE_PROVIDER_MAX_TOKENS = 4096

_post_processor = PostProcessor()
call_self_wake_core = call_core_chat_once


@dataclass(frozen=True)
class PreparedSelfWakeTurn:
    messages: list[dict]
    profile: TurnProfile
    model_key: str
    advertised_tools: tuple[str, ...]
    requested_capabilities: frozenset[str]
    effective_capabilities: frozenset[str]
    unavailable_capabilities: frozenset[str]
    mobile_screen_target: Mapping[str, str] | None
    identity_snapshot: Mapping[str, str]


class SelfWakeTriggerError(RuntimeError):
    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


def _requested_capabilities(wake: Mapping[str, Any]) -> frozenset[str]:
    raw = wake.get("requested_capabilities")
    if raw is None:
        raw = wake.get("requested_capabilities_json") or "[]"
        if isinstance(raw, str):
            try:
                raw = json.loads(raw)
            except (TypeError, json.JSONDecodeError):
                raw = ()
    if isinstance(raw, str):
        raw = (raw,)
    try:
        return frozenset(str(item).strip() for item in raw if str(item).strip())
    except TypeError:
        return frozenset()


async def _load_target(conv_id: str) -> dict[str, Any]:
    async with get_db() as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT c.model, (SELECT MAX(m.created_at) FROM messages m "
            "WHERE m.conv_id=c.id AND m.role='user') AS last_user_ts "
            "FROM conversations c WHERE c.id=?",
            (conv_id,),
        )
        row = await cursor.fetchone()
    if row is None:
        raise SelfWakeTriggerError("origin_not_found")
    return {
        "model_key": str(row["model"] or DEFAULT_MODEL),
        "last_user_ts": (
            float(row["last_user_ts"]) if row["last_user_ts"] is not None else None
        ),
    }


async def _autonomous_mobile_screen_target(
    *,
    model_key: str,
    now: float,
) -> dict | None:
    try:
        from app.mobile_screen.autonomous import autonomous_mobile_screen_target

        return await autonomous_mobile_screen_target(model_key=model_key, now=now)
    except Exception:
        return None


def _ability_block(
    *,
    profile: TurnProfile,
    user_name: str,
    mobile_screen_target: Mapping[str, str] | None,
) -> tuple[str, tuple[str, ...]]:
    capabilities = set(profile.allowed_tool_capabilities)
    behavior = load_ai_behavior()
    rendered = render_registered_capabilities(
        "self_wake",
        capabilities=capabilities,
        context={
            "user_name": user_name,
            "pc_screen_available": "pc.screen_check" in capabilities,
            "mobile_screen_available": (
                "mobile.screen_check" in capabilities
                and mobile_screen_target is not None
            ),
            "mobile_screen_target": mobile_screen_target,
            "ring_available": "device.ring_touch" in capabilities,
            "heart_prompt": str(behavior.get("heart_whisper_prompt") or "").format(
                user_name=user_name
            ),
            "poi_available": "location.poi_search" in capabilities,
            "presence_draw_available": "desktop.presence.draw" in capabilities,
            "presence_show_available": "desktop.presence.show" in capabilities,
        },
    )
    advertised = tuple(tool_name for tool_name, _prose in rendered)
    validate_turn_advertisement(capabilities, advertised)
    abilities = [
        f"你可以直接写一句自然的话给{user_name}看，也可以只执行后台动作。",
        *(prose for _tool_name, prose in rendered),
        (
            f"{SELF_WAKE_NONE_TOKEN} — 如果按原意图判断此刻不需要任何动作或文字，"
            "精确、单独输出这个标记。"
        ),
    ]
    block = "[Self-Wake 本轮可用能力]\n" + "\n".join(
        f"{index}. {ability}" for index, ability in enumerate(abilities, 1)
    )
    block += (
        "\n\n[Self-Wake 硬边界]\n"
        "这是一轮已消费的单次触发；不得设置或取消 self-wake，不得增删日程，"
        "不得输出 [OPPORTUNITY_NONE]、[VOW:]、[WORKING_MODEL_REQUEST] 或 [RECALL_INTENT]。"
        f"{SELF_WAKE_NONE_TOKEN} 只能单独、精确输出；与正文或工具标记混用会让整轮作废。"
        "工具结果由执行层决定，不要伪造结果或解释标记语法。"
    )
    return block, advertised


async def prepare_self_wake_turn(
    wake: Mapping[str, Any],
    *,
    now: float | None = None,
) -> PreparedSelfWakeTurn:
    current = time.time() if now is None else float(now)
    conv_id = str(wake.get("conv_id") or "").strip()
    if not conv_id:
        raise SelfWakeTriggerError("missing_conv_id")
    target = await _load_target(conv_id)
    history_ctx = await prepare_chat_history(
        conv_id,
        context_limit=15,
        attachment_policy="last_user",
    )
    model_key = str(target.get("model_key") or history_ctx.model_key or DEFAULT_MODEL)
    history = list(history_ctx.history)
    cap_idx = history_ctx.cap_idx
    inject_offset = 0

    mobile_screen_target = await _autonomous_mobile_screen_target(
        model_key=model_key,
        now=current,
    )
    runtime = await resolve_autonomous_capabilities(
        model_key=model_key,
        mobile_screen_target=mobile_screen_target,
        now=current,
    )
    registered = frozenset(registered_tools_for_surface("self_wake"))
    requested = _requested_capabilities(wake)
    effective = frozenset(
        requested & runtime & registered & SELF_WAKE_SURFACE_CAPABILITIES
    )
    unavailable = frozenset(requested - effective)
    profile = self_wake_turn_profile(effective)
    user_name, ai_name = resolve_worldbook_names(history_ctx.wb)
    ability_block, advertised = _ability_block(
        profile=profile,
        user_name=user_name,
        mobile_screen_target=mobile_screen_target,
    )

    vow_block, _vow_ability = await vow_service.load_vow_prompt_context()
    identity_snapshot = build_writer_identity_snapshot(
        history_ctx.wb,
        vow_block=vow_block,
    )
    if vow_block:
        inject_offset = insert_prompt_ack(
            history,
            cap_idx=cap_idx,
            inject_offset=inject_offset,
            content=vow_block,
            ack="（嗯，这些一直都算数。）",
        )
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
        content=ability_block,
        ack="（我知道这次实际能做什么。）",
    )
    if working_model_v2_injection_enabled():
        working_model_head, desire_head = await working_model_service.load_v2_prompt_heads()
        inject_offset = insert_prompt_ack(
            history,
            cap_idx=cap_idx,
            inject_offset=inject_offset,
            content=build_v2_working_model_block(dict(working_model_head)),
            ack="（嗯，这是我此刻对她的认识。）",
        )
        insert_prompt_ack(
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
                reference_time=current,
            )
        except Exception:
            context_delivery_text = ""
    runtime_text = build_autonomous_runtime_context(
        now=current,
        last_user_ts=target.get("last_user_ts"),
        capabilities=effective,
        user_name=user_name,
        ai_name=ai_name,
        heading="Self-Wake 实时状态",
        context_delivery_text=context_delivery_text,
    )
    history.append({"role": "user", "content": runtime_text, "attachments": []})
    history.append({"role": "assistant", "content": "（嗯，知道了。）", "attachments": []})
    timezone_name = str(wake.get("owner_timezone") or owner_timezone_name())
    wake_at = float(wake.get("wake_at") or current)
    history.append(
        {
            "role": "user",
            "content": (
                "[Self-Wake 单次触发]\n"
                f"原定意图：{str(wake.get('intent') or '').strip()}\n"
                f"原定时间：{format_owner_time(wake_at, timezone_name=timezone_name)}\n"
                f"当前时间：{format_owner_time(current, timezone_name=timezone_name)}\n"
                "请求能力：" + ("、".join(sorted(requested)) or "无") + "\n"
                "实际挂载能力：" + ("、".join(sorted(effective)) or "无") + "\n"
                "当前不可用能力：" + ("、".join(sorted(unavailable)) or "无") + "\n"
                "当时想做的事只是你自己安排的念头，不是她现在的状态，也压不过最近聊天里她亲口说的话。\n"
                "照着当时的念头和眼下的真实情况自然决定；不要解释你为什么这时候出现。"
            ),
            "attachments": [],
        }
    )
    return PreparedSelfWakeTurn(
        messages=history,
        profile=profile,
        model_key=model_key,
        advertised_tools=advertised,
        requested_capabilities=requested,
        effective_capabilities=effective,
        unavailable_capabilities=unavailable,
        mobile_screen_target=mobile_screen_target,
        identity_snapshot=identity_snapshot,
    )


async def _broadcast_action_results(execution: ActionExecution) -> None:
    for result in execution.executed:
        payload = dict(result.result or {})
        if result.tool_name == "heart.whisper" and payload:
            await manager.broadcast({"type": "heart_whisper", "data": payload})
        elif result.tool_name == "location.poi_search" and payload:
            await manager.broadcast({"type": "poi_search", "data": payload})
        elif result.tool_name in {"pc.screen_check", "mobile.screen_check"} and payload:
            event_type = str(payload.get("type") or "screen_check_pending")
            await manager.broadcast({"type": event_type, "data": payload})
            if result.tool_name == "mobile.screen_check" and event_type == "screen_check_pending":
                try:
                    from app.mobile_screen.autonomous import (
                        record_autonomous_mobile_screen_request,
                    )

                    record_autonomous_mobile_screen_request()
                except Exception:
                    pass


async def _persist_visible_message(
    *,
    conv_id: str,
    msg_id: str,
    content: str,
    created_at: float,
) -> bool:
    cleaned = str(content or "").strip()
    if not cleaned:
        return False
    try:
        async with get_db() as db:
            await db.execute("BEGIN IMMEDIATE")
            try:
                await db.execute(
                    "INSERT INTO messages "
                    "(id,conv_id,role,content,created_at,attachments) "
                    "VALUES (?,?, 'assistant', ?,?, '[]')",
                    (msg_id, conv_id, cleaned, created_at),
                )
                cursor = await db.execute(
                    "UPDATE conversations SET updated_at=? WHERE id=?",
                    (created_at, conv_id),
                )
                if int(cursor.rowcount or 0) != 1:
                    raise SelfWakeTriggerError("origin_not_found")
                await db.commit()
            except BaseException:
                await db.rollback()
                raise
    except Exception:
        return False
    await manager.broadcast(
        {
            "type": "msg_created",
            "data": {
                "id": msg_id,
                "conv_id": conv_id,
                "role": "assistant",
                "content": cleaned,
                "created_at": created_at,
                "attachments": [],
            },
        }
    )
    try:
        await export_conversation(conv_id)
    except Exception:
        pass
    return True


def _tool_succeeded(execution: ActionExecution) -> bool:
    return any(
        execution_outcome(result) in {"succeeded", "dispatched", "pending"}
        for result in execution.results
    )


async def fire_claimed_wake(wake: Mapping[str, Any]) -> dict[str, Any]:
    """Execute one already-consumed wake. This function never retries it."""

    wake_id = str(wake.get("id") or "").strip()
    conv_id = str(wake.get("conv_id") or "").strip()
    if not wake_id or not conv_id:
        raise SelfWakeTriggerError("invalid_claimed_wake")
    started_at = time.time()
    msg_id = f"msg_{time.time_ns()}_wake"
    requested = _requested_capabilities(wake)
    invocation_id = tool_invocation_ledger.new_invocation_id("self_wake_core")
    context = ToolContext(
        conv_id=conv_id,
        msg_id=msg_id,
        request_id=f"self_wake:{wake_id}",
        model_key=None,
        mode="normal",
        capabilities=(),
        metadata={
            "source": "self_wake",
            "source_chain": "self_wake",
            "invocation_id": invocation_id,
            "advertised_tools": (),
            "wake_id": wake_id,
            "origin": str(wake.get("origin") or "relationship"),
            "origin_ref": str(wake.get("origin_ref") or conv_id),
            "origin_source": str(wake.get("source") or ""),
            "intent": str(wake.get("intent") or ""),
            "requested_capabilities": tuple(sorted(requested)),
            "effective_capabilities": (),
            "unavailable_capabilities": tuple(sorted(requested)),
        },
    )
    prepared: PreparedSelfWakeTurn | None = None
    final_outcome = "pipeline_failed"
    final_error = ""
    assistant_persisted = False

    async def conclude(outcome: str, *, error: str = "", **details: Any) -> dict[str, Any]:
        nonlocal final_outcome, final_error
        final_outcome = str(outcome or "unknown")
        final_error = str(error or "")
        await self_wake_repository.finish_trigger(
            wake_id,
            outcome=final_outcome,
            error=final_error,
        )
        return {
            "wake_id": wake_id,
            "status": final_outcome,
            "error": final_error,
            **details,
        }

    try:
        try:
            prepared = await prepare_self_wake_turn(wake, now=started_at)
        except SelfWakeTriggerError as exc:
            return await conclude("prompt_build_failed", error=exc.reason)
        except Exception as exc:
            return await conclude(
                "prompt_build_failed",
                error=f"{type(exc).__name__}:{exc}",
            )

        context = ToolContext(
            conv_id=conv_id,
            msg_id=msg_id,
            request_id=f"self_wake:{wake_id}",
            model_key=prepared.model_key,
            mode="normal",
            capabilities=tuple(sorted(prepared.effective_capabilities)),
            metadata={
                **dict(context.metadata),
                "advertised_tools": prepared.advertised_tools,
                "effective_capabilities": tuple(
                    sorted(prepared.effective_capabilities)
                ),
                "unavailable_capabilities": tuple(
                    sorted(prepared.unavailable_capabilities)
                ),
                "mobile_target_device_id": (
                    prepared.mobile_screen_target or {}
                ).get("device_id"),
            },
        )
        await tool_invocation_ledger.record_model_request(
            context,
            invocation_id=invocation_id,
            request_snapshot=prepared.messages,
            advertised_tools=prepared.advertised_tools,
            metadata={
                "wake_id": wake_id,
                "origin": context.metadata.get("origin"),
                "source": context.metadata.get("origin_source"),
                "intent": context.metadata.get("intent"),
                "requested_capabilities": tuple(sorted(requested)),
                "effective_capabilities": tuple(
                    sorted(prepared.effective_capabilities)
                ),
                "unavailable_capabilities": tuple(
                    sorted(prepared.unavailable_capabilities)
                ),
            },
        )
        usage_meta: dict[str, Any] = {}
        try:
            raw = await call_self_wake_core(
                prepared.model_key,
                prepared.messages,
                expect_json=False,
                timeout=SELF_WAKE_PROVIDER_TIMEOUT_SECONDS,
                temperature=0.9,
                scope=f"self_wake:{wake_id}",
                usage_meta=usage_meta,
                max_tokens=SELF_WAKE_PROVIDER_MAX_TOKENS,
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            await tool_invocation_ledger.record_model_output(
                context,
                invocation_id=invocation_id,
                raw_output="",
                outcome="failed",
                error=str(exc),
                metadata={"wake_id": wake_id},
            )
            return await conclude("provider_failed", error=str(exc))
        raw = str(raw or "")
        provider_last = usage_meta.get("provider_last") or {}
        if not raw.strip():
            outcome = "invalid_output" if provider_last.get("ok", True) else "provider_failed"
            await tool_invocation_ledger.record_model_output(
                context,
                invocation_id=invocation_id,
                raw_output=raw,
                outcome="unknown" if outcome == "invalid_output" else "failed",
                error="",
                metadata={"wake_id": wake_id, "turn_outcome": outcome},
            )
            return await conclude(outcome)
        if looks_like_model_error_text(raw):
            await tool_invocation_ledger.record_model_output(
                context,
                invocation_id=invocation_id,
                raw_output=raw,
                outcome="failed",
                error="model_error_text",
                metadata={"wake_id": wake_id},
            )
            return await conclude("provider_failed", error="model_error_text")
        await tool_invocation_ledger.record_model_output(
            context,
            invocation_id=invocation_id,
            raw_output=raw,
            outcome="succeeded",
            metadata={"wake_id": wake_id},
        )

        control = classify_self_wake_control_output(raw, profile=prepared.profile)
        if control == "invalid":
            return await conclude("invalid_control_output")
        if control == "none":
            return await conclude("none_explicit")

        try:
            postprocessed = await _post_processor.process(
                raw,
                conv_id=conv_id,
                enabled_commands=prepared.profile.enabled_commands,
                tool_context=context,
            )
        except Exception as exc:
            return await conclude("postprocess_failed", error=str(exc))
        visible_text = strip_web_search_intent_markers(
            strip_recall_intent_markers(postprocessed.content)
        ).strip()
        if looks_like_model_error_text(visible_text) or looks_like_structured_reply(
            visible_text
        ):
            visible_text = ""

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
        await _broadcast_action_results(execution)
        tool_success = _tool_succeeded(execution)
        had_tool_intent = bool(postprocessed.tool_intents)

        if visible_text:
            assistant_persisted = await _persist_visible_message(
                conv_id=conv_id,
                msg_id=msg_id,
                content=visible_text,
                created_at=time.time(),
            )
            if not assistant_persisted:
                return await conclude("message_persist_failed")
            await tool_invocation_ledger.record_visible_message(
                context,
                invocation_id=invocation_id,
                cleaned_content=visible_text,
                message_id=msg_id,
                metadata={"wake_id": wake_id, "persisted": True},
            )
            return await conclude(
                "succeeded",
                error=execution_error,
                visible=True,
                tool_results=[result.to_dict() for result in execution.results],
            )
        if tool_success:
            return await conclude(
                "tool_only",
                error=execution_error,
                tool_results=[result.to_dict() for result in execution.results],
            )
        if had_tool_intent or execution_error:
            return await conclude(
                "all_tools_rejected",
                error=execution_error,
                tool_results=[result.to_dict() for result in execution.results],
            )
        return await conclude("invalid_output")
    except asyncio.CancelledError:
        await conclude("cancelled_on_shutdown")
        raise
    except Exception as exc:
        return await conclude("trigger_failed", error=f"{type(exc).__name__}:{exc}")
    finally:
        advertised = prepared.advertised_tools if prepared is not None else ()
        await tool_invocation_ledger.record_turn(
            context,
            prompt_source="self_wake",
            advertised_tools=advertised,
            turn_outcome=final_outcome,
            metadata={
                "wake_id": wake_id,
                "assistant_persisted": assistant_persisted,
                "trigger_error": final_error,
            },
        )


__all__ = [
    "PreparedSelfWakeTurn",
    "SELF_WAKE_PROVIDER_MAX_TOKENS",
    "SELF_WAKE_PROVIDER_TIMEOUT_SECONDS",
    "SelfWakeTriggerError",
    "call_self_wake_core",
    "fire_claimed_wake",
    "prepare_self_wake_turn",
]
