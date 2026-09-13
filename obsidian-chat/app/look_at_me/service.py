"""Daily-window admission and shared Core context boundary."""
from dataclasses import replace
import re

from config import SETTINGS, save_settings
from .repository import repository


def configuration():
    return {'enabled': SETTINGS.get('look_at_me_enabled') is True,
            'device_id': str(SETTINGS.get('look_at_me_device_id') or '')}


def configure(*, enabled, device_id):
    device_id = str(device_id).strip()
    if enabled and not device_id:
        raise ValueError('device_required')
    SETTINGS.update(look_at_me_enabled=bool(enabled),look_at_me_device_id=device_id)
    save_settings(SETTINGS)
    return configuration()


def available():
    cfg = configuration()
    return cfg['enabled'] and bool(cfg['device_id'])


def parse_expression(raw):
    pieces = re.split(r'[|｜]',str(raw),maxsplit=1)
    if len(pieces) != 2 or not re.fullmatch(r'[0-9]+',pieces[0].strip()):
        return {'parse_error':'expected_seconds_and_expression'}
    seconds = int(pieces[0].strip())
    if seconds < 1 or seconds > 2147483647 or not pieces[1].strip():
        return {'parse_error':'invalid_duration_or_expression'}
    return {'duration_seconds':seconds,'expression':pieces[1].strip()}


async def is_current(conv_id):
    from app.daily_chat.service import daily_chat
    state = await daily_chat.ensure_current()
    return bool(state.get('enabled') and state.get('active_conv_id') == daily_chat.resolve(conv_id))


async def scoped_snapshot(snapshot, conv_id):
    capabilities = set(snapshot.capabilities)
    if not available() or not await is_current(conv_id):
        capabilities.discard('mobile.look_at_me')
    return replace(snapshot, capabilities=tuple(sorted(capabilities)))


async def execute_invitation(intent, context, *, origin_conv_id=None):
    from app.daily_chat.service import daily_chat
    if not available():
        raise ValueError('look_at_me_disabled')
    if not await is_current(context.conv_id):
        raise ValueError('daily_window_required')
    args = intent.arguments
    seconds = args.get('duration_seconds')
    if args.get('parse_error') or type(seconds) is not int or not 0 < seconds <= 2147483647 or not str(args.get('expression') or '').strip():
        raise ValueError('invalid_duration_or_expression')
    row = await repository.create(
        conv_id=daily_chat.state['active_conv_id'], origin_conv_id=origin_conv_id or context.conv_id,
        model_key=context.model_key or '', device_id=configuration()['device_id'],
        source_turn=context.request_id or context.msg_id or '', source_intent=intent.id,
        expression=str(args['expression']).strip(), duration_seconds=seconds)
    from ws import manager
    await manager.broadcast({'type':'look_at_me','data':public_request(row)})
    return {'ok':True, **public_request(row)}


def public_request(row):
    return {key:row.get(key) for key in ('id','conv_id','expression','duration_seconds','status','decision','outcome','reject_reason','actual_ms','expires_at','choice_deadline','expected_end_at')}


async def context_for_turn(*, conv_id, turn_id, user_name, ai_name, replay_from=''):
    try:
        if not await is_current(conv_id):
            return {}
        from app.daily_chat.service import daily_chat
        return await repository.claim(turn_id=turn_id,user_name=user_name,ai_name=ai_name,
                                      timezone=daily_chat.timezone(),replay_from=replay_from)
    except Exception:
        import logging
        logging.getLogger(__name__).warning('Companionship context unavailable', exc_info=True)
        return {}


async def inject_context(history, *, conv_id, turn_id, user_name, ai_name, replay_from=''):
    meta = await context_for_turn(conv_id=conv_id,turn_id=turn_id,user_name=user_name,ai_name=ai_name,replay_from=replay_from)
    if meta.get('block'):
        history.extend([{'role':'user','content':meta['block']},
                        {'role':'assistant','content':f'（{ai_name}知道这次陪伴实际进行到了哪里。）'}])
    return meta
