"""
聊天核心路由：send_message、regenerate，以及 chat 子路由聚合。
"""

from __future__ import annotations

import json
import time
from typing import Optional

from fastapi import APIRouter
from fastapi.responses import JSONResponse

from camera import CAMERA_DISABLED_REASON
from config import DEFAULT_MODEL, SETTINGS, model_supports_audio_input
from database import get_db
from ws import manager

from app.chat.audio_input import (
    AUDIO_ATTACHMENT_UNAVAILABLE_CODE,
    AUDIO_ATTACHMENT_UNAVAILABLE_MESSAGE,
    AUDIO_INPUT_UNAVAILABLE_CODE,
    AUDIO_INPUT_UNAVAILABLE_MESSAGE,
    InvalidAudioAttachment,
    contains_audio_attachment,
    missing_audio_attachments,
    normalize_chat_attachments,
    parse_attachments,
)
from app.chat.chat_turn import prepare_regenerate_prompt, prepare_send_prompt
from app.chat.crud_routes import router as crud_router
from app.chat.initiative_routes import router as initiative_router
from app.chat.models import CamCheckTrigger, MsgCreate
from app.chat.side_effects import _schedule_chunk_index_update
from app.chat.streaming import (
    ReplacedMessageNotFound,
    replace_message_and_freeze_vow_context,
    stream_chat_response,
    vow_blocked_response,
)
from app.vows.service import VowReadError

router = APIRouter()
router.include_router(crud_router)
router.include_router(initiative_router)


def _chat_audio_error(code: str, message: str) -> JSONResponse:
    return JSONResponse(
        status_code=422,
        content={"ok": False, "code": code, "error": message, "message": message},
    )


def _audio_preflight_response(model_key: str, attachments: list) -> JSONResponse | None:
    if not contains_audio_attachment(attachments):
        return None
    if not model_supports_audio_input(model_key):
        return _chat_audio_error(
            AUDIO_INPUT_UNAVAILABLE_CODE,
            AUDIO_INPUT_UNAVAILABLE_MESSAGE,
        )
    if missing_audio_attachments(attachments):
        return _chat_audio_error(
            AUDIO_ATTACHMENT_UNAVAILABLE_CODE,
            AUDIO_ATTACHMENT_UNAVAILABLE_MESSAGE,
        )
    return None


@router.post("/api/conversations/{conv_id}/send")
async def send_message(conv_id: str, body: MsgCreate):
    try:
        body.attachments = normalize_chat_attachments(body.attachments)
    except InvalidAudioAttachment as exc:
        return _chat_audio_error("invalid_audio_attachment", str(exc))

    now = time.time()
    msg_id = f"msg_{int(now*1000)}"

    att_json = json.dumps(body.attachments, ensure_ascii=False) if body.attachments else "[]"
    async with get_db() as db:
        if contains_audio_attachment(body.attachments):
            cur = await db.execute("SELECT model FROM conversations WHERE id=?", (conv_id,))
            conv = await cur.fetchone()
            model_key = conv[0] if conv else DEFAULT_MODEL
            blocked = _audio_preflight_response(model_key, body.attachments)
            if blocked is not None:
                return blocked
        await db.execute(
            "INSERT INTO messages (id, conv_id, role, content, created_at, attachments) VALUES (?,?,?,?,?,?)",
            (msg_id, conv_id, "user", body.content, now, att_json),
        )
        await db.execute("UPDATE conversations SET updated_at=? WHERE id=?", (now, conv_id))
        await db.commit()

    user_msg = {
        "id": msg_id,
        "conv_id": conv_id,
        "role": "user",
        "content": body.content,
        "created_at": now,
        "attachments": body.attachments,
    }
    await manager.broadcast({"type": "msg_created", "data": user_msg})
    if not body.memory_eval_mode:
        _schedule_chunk_index_update(conv_id, reason="user_message")
        from app.image_memory.service import schedule_message as schedule_image_memory
        schedule_image_memory(msg_id)

    try:
        model_key, history, prompt_meta = await prepare_send_prompt(
            conv_id,
            body,
            current_user_message_id=msg_id,
        )
    except VowReadError:
        # fail-closed（§5.2）：誓约读取失败，不调模型，绝不以人格开口
        return await vow_blocked_response(conv_id)
    temperature = body.temperature if body.temperature is not None else SETTINGS.get("temperature")
    return await stream_chat_response(
        conv_id=conv_id,
        model_key=model_key,
        history=history,
        prompt_meta=prompt_meta,
        temperature=temperature,
        memory_eval_mode=body.memory_eval_mode,
    )


@router.post("/api/conversations/{conv_id}/regenerate")
async def regenerate_message(
    conv_id: str,
    context_limit: int = 30,
    whisper_mode: bool = False,
    fast_mode: bool = False,
    temperature: Optional[float] = None,
    ai_dom_mode: bool = False,
    safeword: str = "",
    dom_history: str = "",
    cnc_enabled: bool = False,
    cnc_weakness: str = "",
    resist_hits: int = 0,
    short_streak: int = 0,
    reply_delay_ms: int = 0,
    compliance_streak: int = 0,
    session_elapsed: int = 0,
    scene_name: str = "",
    scene_elapsed: int = 0,
    since_last_punish: Optional[int] = None,
    ratchet_valley: int = 0,
    debt: float = 0.0,
    stubborn_streak: int = 0,
    replaced_message_id: Optional[str] = None,
):
    # Gate before the regenerate transaction removes the previous answer.  A
    # model switch must never turn an existing voice message into transcript-only
    # input while the UI claims the model listened to it.
    async with get_db() as db:
        cur = await db.execute("SELECT model FROM conversations WHERE id=?", (conv_id,))
        conv = await cur.fetchone()
        model_key = conv[0] if conv else DEFAULT_MODEL
        cur = await db.execute(
            "SELECT attachments FROM messages "
            "WHERE conv_id=? AND role='user' ORDER BY created_at DESC LIMIT 1",
            (conv_id,),
        )
        latest_user = await cur.fetchone()
    latest_user_attachments = parse_attachments(latest_user[0] if latest_user else "[]")
    blocked = _audio_preflight_response(model_key, latest_user_attachments)
    if blocked is not None:
        return blocked

    # §4.5：先在单一事务里完成 撤约→删旧消息→冻结 vow snapshot，提交后才生成；
    # 事务任一步失败 → 回滚（旧消息与 vow 保留）+ fail-closed。
    vow_snapshot = None
    if replaced_message_id:
        try:
            vow_snapshot = await replace_message_and_freeze_vow_context(conv_id, replaced_message_id)
        except ReplacedMessageNotFound:
            return {"ok": False, "error": "replaced_message_not_found"}
        except Exception:
            return await vow_blocked_response(conv_id)
    try:
        model_key, history, prompt_meta = await prepare_regenerate_prompt(
            conv_id,
            context_limit=context_limit,
            whisper_mode=whisper_mode,
            fast_mode=fast_mode,
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
            vow_snapshot=vow_snapshot,
            replaced_message_id=replaced_message_id,
        )
    except VowReadError:
        return await vow_blocked_response(conv_id)
    return await stream_chat_response(
        conv_id=conv_id,
        model_key=model_key,
        history=history,
        prompt_meta=prompt_meta,
        temperature=temperature,
    )


@router.post("/api/cam-check-trigger")
async def cam_check_trigger(body: CamCheckTrigger):
    return {
        "ok": False,
        "error": CAMERA_DISABLED_REASON,
        "message": "[CAM_CHECK] 已禁用；摄像头输入后续会作为 Sentinel evidence adapter 接入。",
    }
