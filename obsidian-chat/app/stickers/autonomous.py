"""Shared sticker delivery and feedback for autonomous chat turns."""

import logging

from .chat import StickerDelivery, deliver
from .repository import sticker_repository

log = logging.getLogger(__name__)


async def extract_expressions(raw, *, profile, context):
    """Take free-expression controls out before a legacy parser sees their payloads."""
    from app.chat.control_syntax import canonicalize_control_markers, iter_value_control_markers
    from app.chat.postprocess import PostProcessor, _extract_structured_reply, _strip_meta_tags_outside_vow
    parsed = await PostProcessor().process(raw, conv_id=context.conv_id,
        enabled_commands=profile.enabled_commands, tool_context=context)
    text, _actions, _ok = _extract_structured_reply(canonicalize_control_markers(raw))
    text = _strip_meta_tags_outside_vow(text)
    for marker in reversed(list(iter_value_control_markers(text, names={'发图', '陪我'}))):
        text = text[:marker.start] + text[marker.end:]
    return text.strip(), parsed


async def inject_feedback(messages, *, conv_id, turn_id, user_name, ai_name):
    try:
        meta = await sticker_repository.claim(
            conv_id=conv_id, turn_id=turn_id, user_name=user_name, ai_name=ai_name)
    except Exception:
        log.warning("Autonomous sticker feedback unavailable", exc_info=True)
        return {}
    if meta.get('block'):
        messages[-1:-1] = [
            {'role': 'user', 'content': meta['block']},
            {'role': 'assistant', 'content': f'（{ai_name}记下了实际的表情处理结果。）'},
        ]
    return meta


async def complete_delivery(postprocessed, *, execution, profile, context, history,
                            user_name, ai_name, core_call=None, stream_model=None,
                            temperature=0.9, other_attachments=(), pending_followup=False, heart_entry=None):
    if not profile.allows_tool('sticker.send'):
        return StickerDelivery()
    from app.chat.postprocess import PostProcessor
    from app.chat.action_executor import execute_postprocessed_actions
    if stream_model is None:
        from ai_providers import call_core_chat_once
        caller = core_call or call_core_chat_once

        async def stream_model(messages, model_key, usage_meta, temp):
            yield await caller(model_key, messages, expect_json=False, timeout=120,
                               temperature=temp, max_tokens=1024,
                               scope='sticker:autonomous_supplement', usage_meta=usage_meta)

    return await deliver(
        postprocessed, profile=profile, context=context, history=history,
        model_key=context.model_key, temperature=temperature,
        user_name=user_name, ai_name=ai_name, execution=execution,
        execute_actions=execute_postprocessed_actions, stream_model=stream_model,
        processor=PostProcessor(), other_attachments=other_attachments,
        pending_followup=pending_followup, private_outlet=heart_entry)


async def finalize_in_tx(db, *, feedback, message_id, delivery):
    turn_id = str((feedback or {}).get('bound_turn_id') or '')
    supplemented = delivery.supplemented_events if delivery else ()
    if turn_id or supplemented:
        await sticker_repository.finalize_in_tx(
            db, turn_id=turn_id, message_id=message_id, supplemented=supplemented)


async def release_feedback(feedback):
    turn_id = str((feedback or {}).get('bound_turn_id') or '')
    if turn_id:
        await sticker_repository.release(turn_id)
