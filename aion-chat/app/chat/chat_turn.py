"""Prompt orchestration for send/regenerate chat turns."""

from __future__ import annotations

import asyncio
from typing import Optional

from app.control import ControlPromptContext, control_session_service
from app.control.legacy_policy import control_legacy_toy_fallback_enabled
from app.modes import ChatMode, ModeSnapshot, mode_service

from app.memory_v2.digest import HANDOFF_NOTE_TIMEOUT, write_handoff_note
from app.memory_v3.config import load_memory_v3_config
from app.memory_v3.pending_recall import pending_recall_service
from app.memory_v3.recall_intent import recall_intent_ability_block
from app.memory_v3.timeline import timeline_service
from app.vows.service import vow_service
from app.working_model.writer import build_writer_identity_snapshot
from app.tools.feedback import build_previous_turn_feedback
from app.presence.outcomes import presence_outcome_inbox
from app.presence.prompt_context import (
    build_presence_identity_block,
    presence_identity_head,
)
from app.web_search import web_search_service
from app.web_search.intent import strip_web_search_intent_markers, web_search_ability_block
from config import load_ai_behavior

from .history import build_handoff_note_block, prepare_chat_history
from .memory_prompt import inject_memory_prompt, inject_working_model_prompt
from .models import MsgCreate
from .prompt_layout import latest_user_index, mark_cache_boundary
from .prompt_builder import (
    AbilityPrompt,
    build_regenerate_ability_block,
    build_send_ability_block,
    insert_prompt_ack,
    join_prompt_blocks,
    split_ability_prompt,
)
from .worldbook import resolve_worldbook_names


def _attach_prompt_debug_meta(history: list[dict], prompt_meta: dict) -> dict:
    prompt_meta["prompt_messages"] = [
        {
            "role": message["role"],
            "content": strip_web_search_intent_markers(message["content"])[:500],
        }
        for message in history
    ]
    prompt_meta["prompt_count"] = len(history)
    return prompt_meta


def _attach_mode_meta(prompt_meta: dict, *, snapshot: ModeSnapshot) -> dict:
    prompt_meta["chat_mode"] = snapshot.mode.value
    prompt_meta["mode_source"] = snapshot.source
    prompt_meta["capabilities"] = list(snapshot.capabilities)
    return prompt_meta


def _attach_control_meta(prompt_meta: dict, *, context: ControlPromptContext) -> dict:
    prompt_meta["control_context_source"] = context.source
    prompt_meta["control_session_id"] = context.session_id
    prompt_meta["control_kind"] = context.kind
    prompt_meta["control_status"] = "active" if context.active else ("aftercare" if context.aftercare_active else "none")
    prompt_meta["control_epoch"] = context.control_epoch
    prompt_meta["control_resource_id"] = context.control_resource_id
    prompt_meta["owner_client_id"] = context.owner_client_id
    prompt_meta["hidden_agenda_status"] = context.hidden_agenda_status
    prompt_meta["hidden_agenda_source_refs"] = list(context.hidden_agenda_source_refs)
    prompt_meta["aftercare_active"] = context.aftercare_active
    prompt_meta["safety_close_reason"] = context.safety_close_reason
    prompt_meta["safety_closed_at"] = context.safety_closed_at
    return prompt_meta


def _memory_mode_flags(*, snapshot: ModeSnapshot, whisper_mode: bool, ai_dom_mode: bool) -> tuple[bool, bool]:
    if snapshot.mode is ChatMode.NORMAL:
        return False, False
    return whisper_mode, ai_dom_mode


def _ability_sections(
    ability_block: str,
    *,
    user_name: str = "她",
    vow_ability: str = "",
    extra_stable: str = "",
) -> tuple[str, str]:
    """Keep compatibility fakes as one block while splitting real builders."""

    stable, dynamic = split_ability_prompt(ability_block)
    stable = join_prompt_blocks(stable, extra_stable)
    if vow_ability:
        if isinstance(ability_block, AbilityPrompt):
            vow_stable, vow_dynamic = _split_vow_ability(
                vow_ability,
                user_name=user_name,
            )
            stable = join_prompt_blocks(stable, vow_stable)
            dynamic = join_prompt_blocks(dynamic, vow_dynamic)
        else:
            # Legacy/custom test builders have no section metadata.  Preserve
            # their historical single-block behavior instead of guessing.
            stable = join_prompt_blocks(stable, vow_ability)
    return stable, dynamic


def _vow_stable_policy(user_name: str) -> str:
    return (
        f"【立约能力纪律】只有本轮实时额度明确允许时，当你和{user_name}明确说定了一件"
        "关于你们之间、值得永远为真的事，才可以用 [VOW:誓约内容|确认语] 正式记下。"
        "誓约内容是约定本身（240 字以内）；确认语是你对这次立约说的一句话"
        "（120 字以内），只有系统确认写入成功后才会出现在你的回复里。\n"
        "准入判据：只收一年后你们仍希望它为真的东西；拿不准，不收。\n"
        "不要在正文里声称已经立约——确认语只写在标记内，由系统在写入成功后替你说出。"
    )


def _split_vow_ability(
    vow_ability: str,
    *,
    user_name: str = "她",
) -> tuple[str, str]:
    """Separate the stable VOW discipline from its live daily quota."""

    text = str(vow_ability or "").strip()
    if not text:
        return "", ""
    quota_lines = [
        line.strip()
        for line in text.splitlines()
        if line.strip().startswith(("今天你还可以主动立约", "今天的立约额度"))
    ]
    if not quota_lines:
        # Preserve custom/legacy service output if its schema is unknown.
        return text, ""
    return _vow_stable_policy(user_name), "【本轮立约额度】" + "\n".join(quota_lines)


def _start_runtime_tail(
    history: list[dict],
    *,
    conv_id: str,
    stable_inject_offset: int,
    stable_prefix_end_index: int,
) -> tuple[int, int, dict]:
    tail_index = latest_user_index(history)
    layout = mark_cache_boundary(
        history,
        # GPT-5.6 explicit caching matches the exact prefix ending at the
        # breakpoint.  Conversation history is a rolling window, so putting
        # the marker near the latest user makes every turn a new cache entry.
        # Keep it inside the truly stable injected section instead.
        before_index=stable_prefix_end_index,
        session_id=f"chat:{conv_id}",
    )
    # Retain the old cap+offset arithmetic for injected block helpers/tests,
    # while making its sum point at the latest user rather than history start.
    dynamic_cap_idx = tail_index - stable_inject_offset
    return dynamic_cap_idx, stable_inject_offset, layout


async def _control_prompt_context(conv_id: str, payload) -> ControlPromptContext:
    try:
        return await control_session_service.get_prompt_context(conv_id, payload)
    except Exception:
        return ControlPromptContext()


def _mode_snapshot_for_context(
    *,
    whisper_mode: bool,
    ai_dom_mode: bool,
    control_context: ControlPromptContext,
) -> ModeSnapshot:
    if control_context.aftercare_active or control_context.source == "safety_tombstone":
        return mode_service.snapshot(
            ChatMode.NORMAL,
            source="safety_tombstone",
            metadata={
                "control_session_id": control_context.session_id,
                "control_epoch": control_context.control_epoch,
                "owner_client_id": control_context.owner_client_id,
                "safety_close_reason": control_context.safety_close_reason,
            },
        )
    if (
        control_context.source == "none"
        and (whisper_mode or ai_dom_mode)
        and not control_legacy_toy_fallback_enabled()
    ):
        return mode_service.snapshot(
            ChatMode.NORMAL,
            source="legacy_disabled",
            metadata={"ai_dom_mode": bool(ai_dom_mode), "whisper_mode": bool(whisper_mode)},
        )
    if control_context.active and control_context.source == "control_session":
        return mode_service.snapshot(
            ChatMode.CONTROL_SESSION,
            source="control_session",
            metadata={
                "control_session_id": control_context.session_id,
                "control_kind": control_context.kind,
                "owner_client_id": control_context.owner_client_id,
            },
        )
    return mode_service.snapshot_from_flags(
        whisper_mode=whisper_mode,
        ai_dom_mode=ai_dom_mode,
    )


async def prepare_send_prompt(
    conv_id: str,
    body: MsgCreate,
    *,
    current_user_message_id: str = "",
) -> tuple[str, list[dict], dict]:
    history_ctx = await prepare_chat_history(
        conv_id,
        context_limit=body.context_limit,
        attachment_policy="last_message",
        retracted=body.retracted,
    )
    history = history_ctx.history
    user_name, ai_name = resolve_worldbook_names(history_ctx.wb)
    cap_idx = history_ctx.cap_idx
    inject_offset = 0
    control_context = await _control_prompt_context(conv_id, body)
    mode_snapshot = _mode_snapshot_for_context(
        whisper_mode=body.whisper_mode,
        ai_dom_mode=body.ai_dom_mode,
        control_context=control_context,
    )
    memory_v3_config = load_memory_v3_config()

    # 誓约常驻注入（§5.1）：vow block 先注入，之后才插入含 control/aftercare 的
    # ability block。读取失败抛 VowReadError，由路由层 fail-closed——绝不在缺失
    # 誓约上下文的情况下以人格开口。eval 是诊断路径，不注入（§5.1）。
    vow_block, vow_ability = "", ""
    if not body.memory_eval_mode:
        vow_block, vow_ability = await vow_service.load_vow_prompt_context()
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
    ability_prompt = await build_send_ability_block(
        conv_id=conv_id,
        body=body,
        user_name=user_name,
        capabilities=mode_snapshot.capabilities,
        control_context=control_context,
        model_key=history_ctx.model_key,
    )
    recall_ability = ""
    if (
        memory_v3_config["pending_recall_enabled"]
        and not body.fast_mode
        and not body.memory_eval_mode
    ):
        recall_ability = recall_intent_ability_block(user_name=user_name)
    stable_ability, runtime_ability = _ability_sections(
        ability_prompt,
        user_name=user_name,
        vow_ability=vow_ability,
        extra_stable=recall_ability,
    )
    inject_offset = insert_prompt_ack(
        history,
        cap_idx=cap_idx,
        inject_offset=inject_offset,
        content=stable_ability,
        ack="（我知道自己现在能做什么。）",
    )
    if not body.fast_mode:
        inject_offset = await inject_working_model_prompt(
            history,
            cap_idx=cap_idx,
            inject_offset=inject_offset,
        )
    runtime_cap_idx, inject_offset, cache_layout = _start_runtime_tail(
        history,
        conv_id=conv_id,
        stable_inject_offset=inject_offset,
        stable_prefix_end_index=cap_idx + inject_offset,
    )
    runtime_start_offset = inject_offset
    if runtime_ability:
        inject_offset = insert_prompt_ack(
            history,
            cap_idx=runtime_cap_idx,
            inject_offset=inject_offset,
            content=runtime_ability,
            ack="（嗯，这一刻的实际情况我清楚。）",
        )
    web_search_meta = {"status": "disabled", "block": "", "ids": []}
    if current_user_message_id and not body.memory_eval_mode:
        web_search_meta = await web_search_service.prepare_dialogue_turn(
            conv_id=conv_id,
            bound_turn_id=f"send:{current_user_message_id}",
        )
        if web_search_meta.get("status") != "disabled":
            web_runtime_block = join_prompt_blocks(
                web_search_ability_block(),
                str(web_search_meta.get("block") or ""),
            )
            inject_offset = insert_prompt_ack(
                history,
                cap_idx=runtime_cap_idx,
                inject_offset=inject_offset,
                content=web_runtime_block,
                ack="（嗯，查询能力和已经返回的资料我都清楚。）",
            )
    tool_result_feedback = ""
    tool_result_feedback_enabled = bool(
        load_ai_behavior().get("tool_result_feedback_enabled", True)
    )
    if tool_result_feedback_enabled:
        tool_result_feedback = await build_previous_turn_feedback(
            conv_id=conv_id,
            assistant_message_ids=getattr(
                history_ctx,
                "previous_turn_assistant_ids",
                (),
            ),
        )
        if tool_result_feedback:
            inject_offset = insert_prompt_ack(
                history,
                cap_idx=runtime_cap_idx,
                inject_offset=inject_offset,
                content=tool_result_feedback,
                ack="（嗯，刚才实际执行到了哪里、结果是什么，我心里有数。）",
            )
    presence_outcome_meta = {
        "status": "skipped",
        "bound_turn_id": "",
        "outcome_ids": [],
        "injected": False,
    }
    if current_user_message_id and not body.memory_eval_mode:
        bound_turn_id = f"send:{current_user_message_id}"
        try:
            claimed_outcomes = await presence_outcome_inbox.claim_for_turn(
                conv_id=conv_id,
                bound_turn_id=bound_turn_id,
            )
        except Exception:
            claimed_outcomes = {
                "status": "error",
                "bound_turn_id": bound_turn_id,
                "outcome_ids": [],
                "block": "",
            }
        outcome_block = str(claimed_outcomes.get("block") or "")
        if outcome_block:
            inject_offset = insert_prompt_ack(
                history,
                cap_idx=runtime_cap_idx,
                inject_offset=inject_offset,
                content=outcome_block,
                ack="（嗯，桌面上实际发生到哪一步，我知道了。）",
            )
        presence_outcome_meta = {
            "status": str(claimed_outcomes.get("status") or "empty"),
            "bound_turn_id": bound_turn_id,
            "outcome_ids": list(claimed_outcomes.get("outcome_ids") or ()),
            "injected": bool(outcome_block),
        }
    handoff_note = ""
    handoff_block = ""
    handoff_skip_reason: str | None = None
    timeline_meta = {"status": "disabled", "block": "", "entries": []}
    if (
        memory_v3_config["timeline_enabled"]
        and not body.fast_mode
        and not body.memory_eval_mode
    ):
        try:
            timeline_meta = await timeline_service.prompt_context(
                visible_messages=[
                    {"id": message_id}
                    for message_id in getattr(
                        history_ctx,
                        "visible_message_ids",
                        [
                            str(message.get("id") or "")
                            for message in history_ctx.actual_recent
                            if str(message.get("id") or "")
                        ],
                    )
                ],
                config_snapshot=memory_v3_config,
            )
        except Exception:
            # 时间线是柔性持有层，读取失败不得拖垮真实回复。
            timeline_meta = {"status": "error", "block": "", "entries": []}
        handoff_skip_reason = "timeline_enabled"
    elif memory_v3_config["timeline_enabled"]:
        handoff_skip_reason = "disabled"
    else:
        previous_conv_id = getattr(history_ctx, "previous_conversation_id", None)
        if not previous_conv_id:
            handoff_skip_reason = "no_previous"
        elif body.fast_mode or body.memory_eval_mode:
            handoff_skip_reason = "disabled"
        else:
            # fail-open：续点是锦上添花，任何失败都不能拖垮/弄崩这条真实回复。
            # asyncio.wait_for 是真正的硬上限——provider 配置的 timeout_sec 可能远大于
            # 我们传给 _call_flash_lite 的值，这里兜底保证不会卡住首条回复。
            try:
                note = await asyncio.wait_for(
                    write_handoff_note(previous_conv_id), timeout=HANDOFF_NOTE_TIMEOUT
                )
            except asyncio.TimeoutError:
                note, handoff_skip_reason = None, "timeout"
            except Exception:
                note, handoff_skip_reason = None, "exception"
            else:
                if note is None:
                    handoff_skip_reason = "failed"
                elif note == "":
                    handoff_skip_reason = "empty"
            if note:
                handoff_note = note
                handoff_block = build_handoff_note_block(
                    handoff_note,
                    user_name=user_name,
                    source=getattr(history_ctx, "previous_conversation_source", None),
                )
    timeline_block = str(timeline_meta.get("block") or "")
    if timeline_block:
        inject_offset = insert_prompt_ack(
            history,
            cap_idx=runtime_cap_idx,
            inject_offset=inject_offset,
            content=timeline_block,
            ack="（嗯，近几天的事我还记得。）",
        )
    if handoff_block:
        inject_offset = insert_prompt_ack(
            history,
            cap_idx=runtime_cap_idx,
            inject_offset=inject_offset,
            content=handoff_block,
            ack="（明白，我记在心里，不会主动提起。）",
        )
    memory_whisper_mode, memory_ai_dom_mode = _memory_mode_flags(
        snapshot=mode_snapshot,
        whisper_mode=body.whisper_mode,
        ai_dom_mode=body.ai_dom_mode,
    )
    pending_recall = {"status": "skipped", "items": []}
    if (
        current_user_message_id
        and not body.fast_mode
        and not body.memory_eval_mode
        and memory_v3_config["pending_recall_enabled"]
    ):
        pending_recall = await pending_recall_service.prepare_for_user(
            conv_id=conv_id,
            user_message_id=current_user_message_id,
            current_user_message=body.content,
            recent_messages=history_ctx.actual_recent,
            config_snapshot=memory_v3_config,
        )
    inject_offset, prompt_meta = await inject_memory_prompt(
        history,
        conv_id=conv_id,
        cap_idx=runtime_cap_idx,
        inject_offset=inject_offset,
        actual_recent=history_ctx.actual_recent,
        fast_mode=body.fast_mode,
        whisper_mode=memory_whisper_mode,
        ai_dom_mode=memory_ai_dom_mode,
        prompt_source="send",
        user_name=user_name,
        current_user_content=body.content,
        pending_items=pending_recall.get("items") or [],
        visible_message_ids=getattr(history_ctx, "visible_message_ids", []),
        include_working_model=False,
    )
    prompt_meta = _attach_mode_meta(
        prompt_meta,
        snapshot=mode_snapshot,
    )
    prompt_meta["handoff_note"] = {
        "injected": bool(handoff_block),
        "source": getattr(history_ctx, "previous_conversation_source", None),
        "note": handoff_note,
        "skip_reason": handoff_skip_reason,
    }
    prompt_meta["timeline"] = timeline_meta
    prompt_meta["prompt_source"] = "send"
    prompt_meta["tool_result_feedback"] = {
        "enabled": tool_result_feedback_enabled,
        "injected": bool(tool_result_feedback),
    }
    prompt_meta["advertised_tools"] = list(
        getattr(ability_prompt, "advertised_tools", ())
    )
    prompt_meta["current_user_message_id"] = current_user_message_id
    prompt_meta["working_model_writer_identity"] = build_writer_identity_snapshot(
        history_ctx.wb,
        vow_block=vow_block,
    )
    prompt_meta["pending_recall"] = pending_recall
    prompt_meta["memory_v3_config_snapshot"] = memory_v3_config
    prompt_meta["web_search"] = web_search_meta
    prompt_meta["presence_outcomes"] = presence_outcome_meta
    cache_layout["runtime_message_count"] = max(0, inject_offset - runtime_start_offset)
    cache_layout["runtime_insert_index"] = runtime_cap_idx + runtime_start_offset
    prompt_meta["cache_layout"] = cache_layout
    prompt_meta = _attach_control_meta(prompt_meta, context=control_context)
    return history_ctx.model_key, history, _attach_prompt_debug_meta(history, prompt_meta)


async def prepare_regenerate_prompt(
    conv_id: str,
    *,
    context_limit: int,
    whisper_mode: bool,
    fast_mode: bool,
    ai_dom_mode: bool,
    safeword: str,
    dom_history: str,
    cnc_enabled: bool,
    cnc_weakness: str,
    resist_hits: int,
    short_streak: int,
    reply_delay_ms: int,
    compliance_streak: int,
    session_elapsed: int,
    scene_name: str,
    scene_elapsed: int,
    since_last_punish: Optional[int],
    ratchet_valley: int,
    debt: float,
    stubborn_streak: int,
    vow_snapshot: Optional[tuple[str, str]] = None,
    replaced_message_id: Optional[str] = None,
) -> tuple[str, list[dict], dict]:
    history_ctx = await prepare_chat_history(
        conv_id,
        context_limit=context_limit,
        attachment_policy="last_user",
    )
    history = history_ctx.history
    user_name, ai_name = resolve_worldbook_names(history_ctx.wb)
    cap_idx = history_ctx.cap_idx
    inject_offset = 0
    control_payload = {
        "whisper_mode": whisper_mode,
        "ai_dom_mode": ai_dom_mode,
        "dom_history": dom_history,
        "cnc_enabled": cnc_enabled,
        "cnc_weakness": cnc_weakness,
        "resist_hits": resist_hits,
        "short_streak": short_streak,
        "reply_delay_ms": reply_delay_ms,
        "compliance_streak": compliance_streak,
        "session_elapsed": session_elapsed,
        "scene_name": scene_name,
        "scene_elapsed": scene_elapsed,
        "since_last_punish": since_last_punish,
        "ratchet_valley": ratchet_valley,
        "debt": debt,
        "stubborn_streak": stubborn_streak,
    }
    control_context = await _control_prompt_context(conv_id, control_payload)
    mode_snapshot = _mode_snapshot_for_context(
        whisper_mode=whisper_mode,
        ai_dom_mode=ai_dom_mode,
        control_context=control_context,
    )

    # 誓约常驻注入（§5.1）：同 send，先于 ability block；读取失败由路由层 fail-closed。
    # 携带 replaced_message_id 的 regenerate 用撤约事务内冻结的 snapshot（§4.5），
    # 不再单独读 vow——杜绝"已撤约删消息、却因读取失败无法生成"的中间态。
    if vow_snapshot is not None:
        vow_block, vow_ability = vow_snapshot
    else:
        vow_block, vow_ability = await vow_service.load_vow_prompt_context()
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
    ability_prompt = await build_regenerate_ability_block(
        conv_id=conv_id,
        user_name=user_name,
        ai_dom_mode=ai_dom_mode,
        safeword=safeword,
        dom_history=dom_history,
        cnc_enabled=cnc_enabled,
        cnc_weakness=cnc_weakness,
        resist_hits=resist_hits,
        short_streak=short_streak,
        reply_delay_ms=reply_delay_ms,
        compliance_streak=compliance_streak,
        session_elapsed=session_elapsed,
        scene_name=scene_name,
        scene_elapsed=scene_elapsed,
        since_last_punish=since_last_punish,
        ratchet_valley=ratchet_valley,
        debt=debt,
        stubborn_streak=stubborn_streak,
        whisper_mode=whisper_mode,
        capabilities=mode_snapshot.capabilities,
        control_context=control_context,
        model_key=history_ctx.model_key,
    )
    stable_ability, runtime_ability = _ability_sections(
        ability_prompt,
        user_name=user_name,
        vow_ability=vow_ability,
    )
    inject_offset = insert_prompt_ack(
        history,
        cap_idx=cap_idx,
        inject_offset=inject_offset,
        content=stable_ability,
        ack="（我知道自己现在能做什么。）",
    )
    if not fast_mode:
        inject_offset = await inject_working_model_prompt(
            history,
            cap_idx=cap_idx,
            inject_offset=inject_offset,
        )
    runtime_cap_idx, inject_offset, cache_layout = _start_runtime_tail(
        history,
        conv_id=conv_id,
        stable_inject_offset=inject_offset,
        stable_prefix_end_index=cap_idx + inject_offset,
    )
    runtime_start_offset = inject_offset
    if runtime_ability:
        inject_offset = insert_prompt_ack(
            history,
            cap_idx=runtime_cap_idx,
            inject_offset=inject_offset,
            content=runtime_ability,
            ack="（嗯，这一刻的实际情况我清楚。）",
        )
    web_search_replay = (
        await web_search_service.replay_for_assistant(
            replaced_message_id,
        )
        if replaced_message_id
        else {"status": "none", "assistant_message_id": "", "block": "", "rows": []}
    )
    if web_search_replay.get("block"):
        inject_offset = insert_prompt_ack(
            history,
            cap_idx=runtime_cap_idx,
            inject_offset=inject_offset,
            content=str(web_search_replay["block"]),
            ack="（嗯，这些是当时已经拿到的资料。）",
        )
    memory_whisper_mode, memory_ai_dom_mode = _memory_mode_flags(
        snapshot=mode_snapshot,
        whisper_mode=whisper_mode,
        ai_dom_mode=ai_dom_mode,
    )
    pending_replay = (
        await pending_recall_service.replay_for_assistant(replaced_message_id)
        if replaced_message_id
        else {"status": "none", "items": []}
    )
    inject_offset, prompt_meta = await inject_memory_prompt(
        history,
        conv_id=conv_id,
        cap_idx=runtime_cap_idx,
        inject_offset=inject_offset,
        actual_recent=history_ctx.actual_recent,
        fast_mode=fast_mode,
        whisper_mode=memory_whisper_mode,
        ai_dom_mode=memory_ai_dom_mode,
        prompt_source="regenerate",
        user_name=user_name,
        pending_items=pending_replay.get("items") or [],
        visible_message_ids=getattr(history_ctx, "visible_message_ids", []),
        include_working_model=False,
    )
    prompt_meta = _attach_mode_meta(
        prompt_meta,
        snapshot=mode_snapshot,
    )
    prompt_meta = _attach_control_meta(prompt_meta, context=control_context)
    prompt_meta["prompt_source"] = "regenerate"
    prompt_meta["advertised_tools"] = list(
        getattr(ability_prompt, "advertised_tools", ())
    )
    prompt_meta["pending_recall"] = pending_replay
    prompt_meta["current_user_message_id"] = (
        pending_replay.get("target_user_message_id")
        or getattr(history_ctx, "latest_user_message_id", None)
    )
    prompt_meta["working_model_writer_identity"] = build_writer_identity_snapshot(
        history_ctx.wb,
        vow_block=vow_block,
    )
    prompt_meta["memory_v3_config_snapshot"] = load_memory_v3_config()
    prompt_meta["web_search"] = web_search_replay
    cache_layout["runtime_message_count"] = max(0, inject_offset - runtime_start_offset)
    cache_layout["runtime_insert_index"] = runtime_cap_idx + runtime_start_offset
    prompt_meta["cache_layout"] = cache_layout
    return history_ctx.model_key, history, _attach_prompt_debug_meta(history, prompt_meta)
