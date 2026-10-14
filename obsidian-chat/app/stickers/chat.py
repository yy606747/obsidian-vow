"""One foreground match, with one text-only recovery for an otherwise empty reply."""

from dataclasses import dataclass, field

from app.chat.error_text import looks_like_model_error_text
from app.tools.schemas import ToolStatus
from .prompts import supplement


@dataclass
class StickerDelivery:
    attachments: list[dict] = field(default_factory=list)
    supplement_text: str = ''
    supplemented_events: list[str] = field(default_factory=list)
    failed: bool = False


async def deliver(postprocessed, *, profile, context, history, model_key, temperature,
                  user_name: str, ai_name: str, execute_actions, stream_model, processor,
                  other_attachments=(), pending_followup: bool = False, execution=None, private_outlet=None) -> StickerDelivery:
    delivery = StickerDelivery()
    intents = list(getattr(postprocessed, 'tool_intents', ()) or ())
    if not any(i.tool_name in {'sticker.save', 'sticker.send'} for i in intents):
        return delivery
    if execution is None:
        execution = await execute_actions(postprocessed, profile=profile, context=context,
                                         only_capabilities=frozenset({'sticker.save', 'sticker.send'}))
    results = execution.results_for('sticker.send')
    for result in results:
        payload = dict(result.result or {})
        if result.status is ToolStatus.EXECUTED and payload.get('attachment'):
            delivery.attachments.append(payload['attachment'])
            break
    first = next((i for i in intents if i.tool_name == 'sticker.send'
                  and str(i.arguments.get('expression') or '').strip()), None)
    if (not profile.allows_tool('sticker.send') or not any(r.status is not ToolStatus.SKIPPED for r in results)
            or delivery.attachments or other_attachments or pending_followup
            or (private_outlet and private_outlet.intent.strip())
            or postprocessed.content.strip() or first is None):
        return delivery
    missed = any((r.result or {}).get('status') == 'miss' for r in results)
    reason = '没有找到合适表情' if missed else '表情发送未完成'
    messages = [*history, {'role': 'assistant', 'content': first.raw_text},
                {'role': 'user', 'content': supplement(user_name=user_name, ai_name=ai_name,
                 expression=first.arguments.get('expression', ''), reason=reason, allow_work=bool(private_outlet))}]
    try:
        raw = ''
        async for chunk in stream_model(messages, model_key, {}, temperature):
            raw += str(chunk)
        if private_outlet:
            private_outlet.snapshot['messages'].append(dict(messages[-1]))
            raw = private_outlet.extract(raw)
        # Parse only for visible text. No follow-up tools or state writes are executed.
        cleaned = await processor.process(raw, conv_id=context.conv_id, memory_eval_mode=True,
                                          enabled_commands=frozenset())
        if (not cleaned.content.strip() and not (private_outlet and private_outlet.intent.strip())) or looks_like_model_error_text(cleaned.content):
            raise ValueError('empty_or_failed_supplement')
        delivery.supplement_text = cleaned.content
        delivery.supplemented_events = [f'sticker:send:{context.msg_id or context.request_id}']
    except Exception:
        delivery.failed = True
    return delivery
