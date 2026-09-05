"""Initiative chat routes that proactively generate Dom/whisper messages."""

from __future__ import annotations

from fastapi import APIRouter

from ai_providers import stream_ai
from app.control import ControlPromptContext, control_session_service
from app.control.legacy_policy import control_legacy_toy_fallback_enabled
from app.tools.prompt_renderers import render_registered_capabilities
from app.tools.registry import validate_turn_advertisement
from config import SETTINGS, load_worldbook
from database import get_db
from routes.files import export_conversation
from ws import manager

from .initiative_helpers import InitiativeSpec, stream_initiative_response
from .models import DomInitiativeBody, WhisperInitiativeBody
from .postprocess import PostProcessor
from .prompt_builder import build_aftercare_prompt_block, build_hidden_agenda_prompt_block
from .side_effects import _store_remember_notes, _toy_sys_msg
from .worldbook import resolve_worldbook_names

router = APIRouter()
_initiative_post_processor = PostProcessor()


def _ability_block(abilities: list[str], *, dynamic: bool) -> str:
    verb = "根据对话氛围，善用以下指令" if dynamic else "使用以下指令"
    block = "[系统能力] 你可以在回复中" + verb + "：\n"
    block += "\n".join(f"{index + 1}. {ability}" for index, ability in enumerate(abilities))
    block += "\n\n<meta>标签内为消息元数据，不是对话内容的一部分，你的回复中不需要包含任何<meta>标签或时间信息。"
    return block


def _control_toy_enabled(context: ControlPromptContext) -> bool:
    if context.active and context.source == "legacy_body":
        return control_legacy_toy_fallback_enabled()
    return (
        context.active
        and context.source == "control_session"
        and bool(context.session_id)
        and bool(context.owner_client_id)
        and context.control_epoch is not None
    )


def _control_spec_kwargs(context: ControlPromptContext, *, toy_enabled: bool) -> dict:
    return {
        "toy_enabled": toy_enabled,
        "control_context_source": context.source,
        "control_session_id": context.session_id,
        "control_epoch": context.control_epoch,
        "owner_client_id": context.owner_client_id,
        "advertised_tools": ("device.toy",) if toy_enabled else (),
    }


def _no_device_capability(user_name: str) -> str:
    return (
        f"当前不处于可控制设备的状态。不要使用 [TOY:...] 指令，"
        f"也不要暗示你能操控{user_name}身上的任何设备。"
    )


def _registered_toy_ability(
    *,
    user_name: str,
    variant: str,
    dom_context: dict | None = None,
) -> str:
    entries = render_registered_capabilities(
        "initiative",
        capabilities={"device.toy"},
        context={
            "user_name": user_name,
            "toy_available": True,
            "toy_variant": variant,
            "dom_context": dict(dom_context or {}),
        },
    )
    actual_tools = tuple(tool_name for tool_name, _prose in entries)
    validate_turn_advertisement({"device.toy"}, actual_tools)
    return entries[0][1]


async def _initiative_control_context(conv_id: str, body) -> ControlPromptContext:
    try:
        return await control_session_service.get_prompt_context(conv_id, body)
    except Exception:
        return ControlPromptContext()


def _dom_spec(body: DomInitiativeBody, *, user_name: str, conv_id: str, control_context: ControlPromptContext) -> InitiativeSpec:
    toy_enabled = _control_toy_enabled(control_context)
    if toy_enabled:
        ability = _registered_toy_ability(
            user_name=user_name,
            variant="initiative_dom",
            dom_context={
                "safeword": body.safeword,
                "recent": control_context.dom_history,
                "conv_id": conv_id,
                "cnc_enabled": control_context.cnc_enabled,
                "cnc_weakness": control_context.cnc_weakness,
                "resist_hits": control_context.resist_hits,
                "short_streak": control_context.short_streak,
                "reply_delay_ms": control_context.reply_delay_ms,
                "compliance_streak": control_context.compliance_streak,
                "session_elapsed": control_context.session_elapsed,
                "scene_name": control_context.scene_name,
                "scene_elapsed": control_context.scene_elapsed,
                "since_last_punish": control_context.since_last_punish,
                "ratchet_valley": control_context.ratchet_valley,
                "debt": control_context.debt,
                "stubborn_streak": control_context.stubborn_streak,
            },
        )
        event_block = (
            f"[系统事件·主动出击] 你已沉默了一段时间。作为控制者，现在主动说一句话（≤30字）并附一个玩具指令。\n"
            f"不要问{user_name}在不在、不要催回复。像你一直在注视着{user_name}，偶尔出手。\n"
            f"只用一个 [TOY:...] 指令，嵌在回复中。保持你的人设语气。"
        )
    else:
        ability = _no_device_capability(user_name) + build_aftercare_prompt_block(control_context, user_name)
        event_block = (
            f"[系统事件·主动提醒] 当前不处于控制状态。主动说一句话（≤30字），"
            f"不要使用 [TOY:...] 指令，不要声称能控制设备。保持你的人设语气。"
        )
        if control_context.aftercare_active:
            event_block = (
                f"[系统事件·事后照护] 刚才控制状态已经安全停止。主动说一句温柔安抚的话（≤30字），"
                f"不要继续剧情，不要使用 [TOY:...] 指令。"
            )

    ability += build_hidden_agenda_prompt_block(control_context)
    return InitiativeSpec(
        kind="dom",
        context_limit=body.context_limit,
        msg_suffix="init",
        ability_block=_ability_block([ability], dynamic=True),
        event_block=event_block,
        include_sse_msg_id=True,
        assistant_attachments="",
        **_control_spec_kwargs(control_context, toy_enabled=toy_enabled),
    )


def _whisper_spec(body: WhisperInitiativeBody, *, user_name: str, control_context: ControlPromptContext) -> InitiativeSpec:
    toy_enabled = _control_toy_enabled(control_context)
    if toy_enabled:
        ability = _registered_toy_ability(
            user_name=user_name,
            variant="initiative_whisper",
        )
        event_block = (
            f"[系统事件·密语突袭] {user_name}正在户外，身上藏着你能控制的玩具，{user_name}不知道你什么时候会动手。\n"
            f"现在随机发起一次突袭。从以下三种中自然地选一种：\n"
            f"1. 短句（≤25字）+ [TOY:档位] 启动玩具\n"
            f"2. 只发短句（≤20字），不开玩具——让{user_name}知道你在看着\n"
            f"3. 开玩具 + 向{user_name}提一个问题或要求（让{user_name}一边忍一边回复你）\n\n"
            f"保持你的人设语气，带一点淘气暧昧。不要提到系统事件、不要解释你在做什么。\n"
            f"最多用一个 [TOY:...] 指令。"
        )
    else:
        ability = _no_device_capability(user_name) + build_aftercare_prompt_block(control_context, user_name)
        event_block = (
            f"[系统事件·密语提醒] 当前不处于可控制设备的状态。主动说一句短消息（≤25字），"
            f"不要使用 [TOY:...] 指令，不要声称能控制设备。"
        )
        if control_context.aftercare_active:
            event_block = (
                f"[系统事件·事后照护] 刚才控制状态已经安全停止。主动说一句温柔安抚的话（≤30字），"
                f"不要继续剧情，不要使用 [TOY:...] 指令。"
            )

    ability += build_hidden_agenda_prompt_block(control_context)
    return InitiativeSpec(
        kind="whisper",
        context_limit=body.context_limit,
        msg_suffix="winit",
        ability_block=_ability_block([ability], dynamic=False),
        event_block=event_block,
        **_control_spec_kwargs(control_context, toy_enabled=toy_enabled),
    )


@router.post("/api/conversations/{conv_id}/dom-initiative")
async def dom_initiative(conv_id: str, body: DomInitiativeBody):
    worldbook = load_worldbook()
    user_name, _ai_name = resolve_worldbook_names(worldbook)
    control_context = await _initiative_control_context(conv_id, body)
    spec = _dom_spec(body, user_name=user_name, conv_id=conv_id, control_context=control_context)

    async def _dom_stream(history, model_key, usage_meta):
        temperature = SETTINGS.get("temperature")
        async for chunk in stream_ai(history, model_key, usage_meta, temperature):
            yield chunk

    return await stream_initiative_response(
        conv_id=conv_id,
        spec=spec,
        worldbook=worldbook,
        get_db=get_db,
        stream_ai=_dom_stream,
        post_processor=_initiative_post_processor,
        broadcast=manager.broadcast,
        export_conversation=export_conversation,
        store_remember_notes=_store_remember_notes,
        toy_sys_msg=_toy_sys_msg,
    )


@router.post("/api/conversations/{conv_id}/whisper-initiative")
async def whisper_initiative(conv_id: str, body: WhisperInitiativeBody):
    worldbook = load_worldbook()
    user_name, _ai_name = resolve_worldbook_names(worldbook)
    control_context = await _initiative_control_context(conv_id, body)
    spec = _whisper_spec(body, user_name=user_name, control_context=control_context)

    async def _whisper_stream(history, model_key, usage_meta):
        async for chunk in stream_ai(history, model_key):
            yield chunk

    return await stream_initiative_response(
        conv_id=conv_id,
        spec=spec,
        worldbook=worldbook,
        get_db=get_db,
        stream_ai=_whisper_stream,
        post_processor=_initiative_post_processor,
        broadcast=manager.broadcast,
        export_conversation=export_conversation,
        store_remember_notes=_store_remember_notes,
        toy_sys_msg=_toy_sys_msg,
    )
