import asyncio
import json
import time
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest

import opportunity as opp
from app.chat.postprocess import PostProcessor
from app.chat.turn_profiles import (
    TurnProfile, opportunity_turn_profile, self_wake_turn_profile,
    classify_opportunity_control_output, classify_self_wake_control_output,
)
from app.stickers import autonomous as sticker_turn
from app.self_wake import trigger as wake_trigger
from app.schedule import trigger as schedule_trigger
from app.tools.schemas import ToolContext
from sentinel_core_wake_adapters import LegacyCoreWakePorts
from test_stickers import env, picture_bytes


NAMES = {'user_name': '小满', 'ai_name': '知秋'}


async def noop(*_args, **_kwargs):
    return {}


@pytest.fixture
def runtime(env, monkeypatch):
    async def setup():
        async with env.db() as db:
            for column in ('conv_id TEXT', 'content TEXT', 'created_at REAL'):
                await db.execute('ALTER TABLE messages ADD COLUMN ' + column)
            await db.execute('CREATE TABLE conversations (id TEXT PRIMARY KEY, model TEXT, updated_at REAL)')
            await db.execute("INSERT INTO conversations VALUES ('conv','core',1)")
            await db.commit()
        row = await env.service.store_bytes(picture_bytes())
        return await env.service.summarize(row)

    env.asset = asyncio.run(setup())
    env.broadcasts = []
    env.core_calls = []
    env.match_calls = []
    env.invitations = []

    async def broadcast(payload):
        env.broadcasts.append(payload)

    monkeypatch.setattr(sticker_turn, 'sticker_repository', env.repo)
    monkeypatch.setattr('app.stickers.service.sticker_service', env.service)
    monkeypatch.setattr('app.stickers.service.chat_capabilities', lambda: ('sticker.save', 'sticker.send'))
    monkeypatch.setattr('app.chat.worldbook.load_worldbook_names', lambda: tuple(NAMES.values()))
    monkeypatch.setattr('app.look_at_me.service.available', lambda: False)
    monkeypatch.setattr('app.look_at_me.service.inject_context', noop)
    monkeypatch.setattr('app.look_at_me.repository.repository.settle', noop)
    monkeypatch.setattr('routes.files.export_conversation', noop)
    monkeypatch.setattr(opp, 'get_db', env.db)
    monkeypatch.setattr(opp, '_broadcast_log', noop)
    monkeypatch.setattr(wake_trigger, 'get_db', env.db)
    monkeypatch.setattr(wake_trigger, 'export_conversation', noop)
    monkeypatch.setattr(wake_trigger, 'self_wake_repository', SimpleNamespace(finish_trigger=noop))
    monkeypatch.setattr(schedule_trigger, 'get_db', env.db)
    monkeypatch.setattr(schedule_trigger, 'export_conversation', noop)
    monkeypatch.setattr(schedule_trigger, '_log_monitor_success', noop)
    monkeypatch.setattr(schedule_trigger, '_log_monitor_failure', noop)
    monkeypatch.setattr(schedule_trigger, 'timeline_service', SimpleNamespace(
        start_background_refresh=lambda: None, record_injection_usage=noop))
    for module in (opp, wake_trigger, schedule_trigger):
        monkeypatch.setattr(module, 'manager', SimpleNamespace(broadcast=broadcast))
    env.broadcast = broadcast
    return env


async def run_route(route, env, monkeypatch, *, raw, matched, supplement='偷偷看看。'):
    async def matcher(_slot, messages, **_kwargs):
        env.match_calls.append(json.loads(messages[1]['content'])['request'])
        return json.dumps({'id': env.asset['id'] if matched else None})

    async def core(model_key, messages, **_kwargs):
        env.core_calls.append(messages)
        return raw if len(env.core_calls) == 1 else supplement

    async def stream(messages, model_key, temperature=None):
        yield await core(model_key, messages)

    env.service.caller = matcher
    if route in {'idle', 'look_at_me_accept'}:
        prepared = opp.PreparedOpportunityTurn(
            messages=[{'role': 'user', 'content': '一个空闲的瞬间'}],
            profile=opportunity_turn_profile(runtime_capabilities={'sticker.send'},
                reflection_allowed=False, kind=route), model_key='core',
            identity_snapshot={}, reflection_context=None, mobile_screen_target=None,
            kind=route, advertised_tools=('sticker.send',), **NAMES)

        async def prepare(**_kwargs):
            return prepared

        monkeypatch.setattr(opp, '_prepare_opportunity_turn', prepare)
        monkeypatch.setattr(opp, 'call_opportunity_core', core)
        return await opp.OpportunityRunner()._run_kind(route, time.time(),
            {'conv_id': 'conv', 'model_key': 'core', 'last_user_ts': 1})
    if route == 'self_wake':
        caps = frozenset({'sticker.send'})
        prepared = wake_trigger.PreparedSelfWakeTurn(
            messages=[{'role': 'user', 'content': '回来看看'}],
            profile=self_wake_turn_profile(caps), model_key='core', advertised_tools=tuple(caps),
            requested_capabilities=frozenset(), effective_capabilities=caps,
            unavailable_capabilities=frozenset(), mobile_screen_target=None, identity_snapshot={})

        async def prepare(*_args, **_kwargs):
            return prepared

        monkeypatch.setattr(wake_trigger, 'prepare_self_wake_turn', prepare)
        monkeypatch.setattr(wake_trigger, 'call_self_wake_core', core)
        return await wake_trigger.fire_claimed_wake(
            {'id': 'wake', 'conv_id': 'conv', 'intent': '回来看看', 'requested_capabilities': []})
    if route == 'schedule':
        monkeypatch.setattr(schedule_trigger, 'stream_ai', stream)
        context = ToolContext(conv_id='conv', request_id='schedule:test', model_key='core',
            capabilities=('sticker.send',), metadata={'sticker_context': {'message': '到吃饭时间了'}})
        return await schedule_trigger._deliver_reply(
            [{'id': 's1', 'type': 'monitor', 'content': '吃饭', 'trigger_at': '2026-09-12 19:00'}],
            conv_id='conv', model_key='core', messages=[{'role': 'user', 'content': '到吃饭时间了'}],
            trigger_prompt='到吃饭时间了', context_meta={}, evidence_errors=[],
            tool_context=context, advertised_tools=('sticker.send',), invocation_id='schedule-test',
            sticker_feedback={}, **NAMES)
    if route == 'sentinel':
        from app.sentinel import run_core_wake_orchestrator_test_execute
        from test_sentinel_core_wake_adapters import _wake_package

        async def empty_context():
            return '', ''

        ports = LegacyCoreWakePorts(db_factory=env.db, broadcaster=env.broadcast,
            core_streamer=stream, monitor_log_writer=noop, sleeper=noop)
        ports.load_vow_prompt_context = empty_context
        ports.load_working_model_prompt_context = empty_context
        ports.load_timeline_prompt_context = noop
        ports.record_timeline_injection_usage = noop
        ports.prepare_web_search_turn = noop
        ports.finalize_web_search_turn = noop
        return await run_core_wake_orchestrator_test_execute(wake_package=_wake_package(),
            execution_context={'conv_id': 'conv', 'model_key': 'core', **NAMES},
            ports=ports, request_id='sentinel:test')
    raise AssertionError(route)


@pytest.mark.parametrize('route', ['idle', 'look_at_me_accept', 'self_wake', 'schedule', 'sentinel'])
@pytest.mark.parametrize('body,matched,broken', [('', True, False), ('来了。', True, False),
    ('来了。', False, False), ('', False, False), ('', False, True)])
def test_autonomous_delivery_persists_one_sticker_or_one_text_recovery(runtime, monkeypatch, route, body, matched, broken):
    async def run():
        await run_route(route, runtime, monkeypatch, raw=body + '【发图：猫举着[OK]的牌子】',
            matched=matched, supplement='【发图：继续找】' if broken else '偷偷看看。')
        async with runtime.db() as db:
            messages = await (await db.execute("SELECT content,attachments FROM messages WHERE role='assistant'")).fetchall()
            events = await (await db.execute("SELECT feedback_state,consumed_by,expression FROM sticker_events WHERE action='send'")).fetchall()
        return messages, events

    messages, events = asyncio.run(run())
    recovery = not body and not matched
    assert len(runtime.core_calls) == (2 if recovery else 1)
    assert runtime.match_calls == ['猫举着[OK]的牌子']
    assert len(events) == 1 and events[0][2] == '猫举着[OK]的牌子'
    if broken:
        assert messages == []
        assert events[0][0] == 'pending'
    else:
        assert len(messages) == 1
        text, attachments = messages[0][0], json.loads(messages[0][1])
        assert text == ('偷偷看看。' if recovery else body)
        assert bool(attachments) is matched
        if attachments:
            assert len(attachments) == 1 and attachments[0]['id'] == runtime.asset['id']
        visible = [e['data'] for e in runtime.broadcasts if e['type'] == 'msg_created' and e['data']['role'] == 'assistant']
        assert len(visible) == 1 and visible[0]['attachments'] == attachments
        assert '发图' not in visible[0]['content']
        assert events[0][0] == ('consumed' if recovery else 'none' if matched else 'pending')
        if recovery:
            prompt = runtime.core_calls[-1][-1]['content']
            assert all(name in prompt for name in NAMES.values())


@pytest.mark.parametrize('kind', ['idle', 'look_at_me_accept', 'night', 'summon'])
def test_sticker_profile_excludes_silent_special_turns(kind):
    profile = opportunity_turn_profile(runtime_capabilities={'sticker.send', 'sticker.create',
        'desktop.presence.show', 'desktop.presence.draw'}, reflection_allowed=True, kind=kind)
    assert profile.allows_tool('sticker.send') is (kind in {'idle', 'look_at_me_accept'})
    assert not profile.allows_tool('sticker.save')


@pytest.mark.parametrize('mode', ['normal', 'work', 'voice_call', 'device_control', 'intimate', 'control_session'])
def test_control_modes_do_not_advertise_or_execute_sticker_send(monkeypatch, mode):
    from app.modes import mode_service
    from app.chat.action_executor import execute_postprocessed_actions
    from app.chat.turn_profiles import chat_turn_profile
    monkeypatch.setattr('app.stickers.service.chat_capabilities', lambda: ('sticker.save', 'sticker.send'))
    snapshot = mode_service.snapshot(mode)
    allowed = mode not in {'device_control', 'intimate', 'control_session'}
    assert ('sticker.send' in snapshot.capabilities) is allowed
    if allowed:
        return

    async def run():
        context = ToolContext(conv_id='conv', mode=mode, capabilities=snapshot.capabilities)
        reply = await PostProcessor().process('文字【发图：探头】', conv_id='conv', tool_context=context)
        result = await execute_postprocessed_actions(reply, profile=chat_turn_profile('send'),
            context=context, only_capabilities=frozenset({'sticker.send'}))
        assert reply.content == '文字' and not result.executed
    asyncio.run(run())


@pytest.mark.parametrize('tag', ['[OPPORTUNITY_NONE]', '[SELF_WAKE_NONE]', '[RING:敲三下]', '[WEB_SEARCH_INTENT]外部查询[/WEB_SEARCH_INTENT]'])
def test_free_sticker_expression_cannot_activate_quoted_controls(tag):
    raw = '【发图：举着' + tag + '的牌子】'
    profile = opportunity_turn_profile(runtime_capabilities={'sticker.send'}, reflection_allowed=True)
    assert classify_opportunity_control_output(raw, profile=profile) == 'ordinary'
    assert classify_self_wake_control_output(raw, profile=self_wake_turn_profile({'sticker.send'})) == 'ordinary'

    async def run():
        reply = await PostProcessor().process(raw, conv_id='conv')
        assert reply.content == ''
        assert [i.tool_name for i in reply.tool_intents] == ['sticker.send']
        assert reply.tool_intents[0].arguments['expression'] == '举着' + tag + '的牌子'
        assert not reply.ring_touch_descriptions and not reply.web_search_intent
    asyncio.run(run())


@pytest.mark.parametrize('current,success', [(True, True), (True, False), (False, False)])
def test_sentinel_invitation_only_is_scoped_and_never_saved_as_empty(runtime, monkeypatch, current, success):
    monkeypatch.setattr('app.look_at_me.service.available', lambda: True)

    async def is_current(_conv):
        return current

    async def invite(intent, context, **_kwargs):
        runtime.invitations.append((intent, context))
        if not success:
            raise ValueError('device_busy')
        return {'ok': True, 'id': 'invitation', 'status': 'queued'}

    monkeypatch.setattr('app.look_at_me.service.is_current', is_current)
    monkeypatch.setattr('app.look_at_me.service.execute_invitation', invite)

    async def run():
        result = await run_route('sentinel', runtime, monkeypatch,
            raw='【陪我：20｜一起看看[OK]这几个字】', matched=False)
        async with runtime.db() as db:
            messages = await (await db.execute('SELECT * FROM messages')).fetchall()
        assert not messages
        return result

    result = asyncio.run(run())
    assert len(runtime.core_calls) == 1
    assert len(runtime.invitations) == int(current)
    assert result['status'] == ('core_succeeded' if success else 'core_empty')
    prompt = '\n'.join(m['content'] for m in runtime.core_calls[0])
    assert ('【腻歪时间】' in prompt) is current
    assert all('陪我' not in str(event) for event in runtime.broadcasts)
    if success:
        assert runtime.invitations[0][0].arguments['expression'] == '一起看看[OK]这几个字'


@pytest.mark.parametrize('route', ['idle', 'self_wake', 'sentinel'])
def test_failed_persistence_releases_feedback_for_next_natural_turn(runtime, monkeypatch, route):
    db_factory = runtime.db

    @asynccontextmanager
    async def failing_db():
        async with db_factory() as db:
            execute = db.execute

            async def fail_assistant_insert(sql, params=()):
                if sql.startswith('INSERT INTO messages') and (
                        "'assistant'" in sql or 'assistant' in params):
                    raise RuntimeError('test_message_write_failed')
                return await execute(sql, params)

            db.execute = fail_assistant_insert
            yield db

    for module in (opp, wake_trigger):
        monkeypatch.setattr(module, 'get_db', failing_db)
    if route == 'sentinel':
        runtime.db = failing_db

    async def run():
        await runtime.repo.begin('previous', action='send', conv_id='conv', expression='昨天没找到的猫')
        await runtime.repo.finish('previous', status='miss')
        try:
            await run_route(route, runtime, monkeypatch, raw='【发图：猫】', matched=True)
        except RuntimeError as exc:
            assert str(exc) == 'test_message_write_failed'
        async with db_factory() as db:
            assert not await (await db.execute("SELECT id FROM messages WHERE role='assistant'")).fetchall()
        meta = await runtime.repo.claim(conv_id='conv', turn_id='next-chat', **NAMES)
        assert '昨天没找到的猫' in meta['block']

    asyncio.run(run())
    assert any('昨天没找到的猫' in m['content'] for m in runtime.core_calls[0])


def test_sentinel_feedback_does_not_hide_advertised_tools(runtime, monkeypatch):
    from sentinel_core_wake_adapters import _advertised_tools_from_messages

    async def inject(history, **_kwargs):
        history.extend([{'role': 'user', 'content': '此前陪伴已结束'},
                        {'role': 'assistant', 'content': '知道了。'}])
        return {}

    monkeypatch.setattr('app.look_at_me.service.inject_context', inject)
    asyncio.run(run_route('sentinel', runtime, monkeypatch, raw='【发图：探头】', matched=True))
    assert 'sticker.send' in _advertised_tools_from_messages(runtime.core_calls[0])


def test_sentinel_control_round_omits_stickers_without_removing_companionship(runtime, monkeypatch):
    monkeypatch.setattr('app.look_at_me.service.available', lambda: True)

    async def current(_conv):
        return True

    monkeypatch.setattr('app.look_at_me.service.is_current', current)

    async def run():
        ports = LegacyCoreWakePorts(db_factory=runtime.db, broadcaster=runtime.broadcast,
            core_streamer=lambda *_args, **_kwargs: None, monitor_log_writer=noop)
        messages = [{'role': 'user', 'content': '来看看'}]
        await ports.prepare_expression_turn(messages=messages, conv_id='conv', model_key='core',
            request_id='control-test', control_active=True)
        assert '【发图：' not in messages[-1]['content']
        assert '【陪我：' in messages[-1]['content']
        _, pending = await ports.extract_expression_reply('文字【发图：探头】')
        assert not pending
        await ports.release_expression_turn()

    asyncio.run(run())
