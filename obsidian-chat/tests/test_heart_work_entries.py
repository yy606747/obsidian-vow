"""Exercise real autonomous callers with a work outbox substitute."""
import asyncio
import json

import pytest

from app.heart_works.entry import WorkEntry
from test_autonomous_expressions import runtime, run_route
from test_stickers import env
from test_heart_work_memory_api import archive

INTENT = '[心语创作]\n暂叫《雨》。想留一幅画和一段故事。\n[/心语创作]'


@pytest.fixture
def entries(monkeypatch):
    calls = []
    async def vows():
        return '', ''
    monkeypatch.setattr('app.vows.service.vow_service.load_vow_prompt_context', vows)
    async def enqueue(origin, intent_raw, context_snapshot):
        calls.append((origin, intent_raw, context_snapshot))
        return {'id': 'saved-work', 'status': 'queued'}
    monkeypatch.setattr('app.heart_works.service.heart_work_service.enqueue_from_turn', enqueue)
    return calls


@pytest.mark.parametrize('route', ['idle', 'look_at_me_accept', 'self_wake', 'schedule', 'sentinel'])
def test_autonomous_real_pipeline_accepts_intent_only(runtime, monkeypatch, entries, route):
    result = asyncio.run(run_route(route, runtime, monkeypatch, raw=INTENT, matched=False))
    assert len(entries) == 1, result
    assert entries[0][1] == '\n暂叫《雨》。想留一幅画和一段故事。\n'
    assert len(runtime.core_calls) == 1
    assert '[心语创作]' in str(runtime.core_calls[0])
    assert '暂叫' not in str(runtime.broadcasts)


@pytest.mark.parametrize('route', ['idle', 'look_at_me_accept', 'self_wake', 'schedule', 'sentinel'])
def test_sticker_failure_followup_can_leave_work_on_same_origin(runtime, monkeypatch, entries, route):
    result = asyncio.run(run_route(route, runtime, monkeypatch, raw='[发图:雨夜的猫]',
                                  matched=False, supplement=INTENT))
    assert len(entries) == 1, result
    assert len(runtime.core_calls) == 2 and '心语创作出口' in runtime.core_calls[-1][-1]['content']
    assert '暂叫' not in json.dumps(runtime.broadcasts, ensure_ascii=False)


@pytest.mark.parametrize('kind', ['night', 'summon'])
def test_exclusive_autonomous_outlets_accept_work_without_device(monkeypatch, entries, kind):
    import opportunity as opp
    from test_opportunity import _stub_prepared_turn, _stub_broadcast
    _stub_prepared_turn(monkeypatch, kind=kind)
    broadcasts = _stub_broadcast(monkeypatch)
    async def core(*args, **kwargs):
        return INTENT
    monkeypatch.setattr(opp, 'call_opportunity_core', core)
    result = asyncio.run(opp.run_autonomous_turn(kind=kind, now=1000,
        target={'conv_id': 'conv1', 'model_key': 'core', 'last_user_ts': 1}))
    assert result['status'] == 'acted' and result['round_branch'] == 'heart_work'
    assert len(entries) == 1 and '暂叫' not in str(broadcasts)


@pytest.mark.parametrize('kind', ['dom', 'whisper'])
def test_initiative_sse_never_streams_intent(monkeypatch, entries, kind):
    from app.chat import initiative_routes
    from app.chat.models import DomInitiativeBody, WhisperInitiativeBody
    from app.control import ControlPromptContext
    from test_chat_initiative_sse import _patch_initiative_io, _collect_sse
    _patch_initiative_io(monkeypatch, control_context=ControlPromptContext())
    async def stream(*args, **kwargs):
        for character in INTENT:
            yield character
    monkeypatch.setattr(initiative_routes, 'stream_ai', stream)
    async def scenario():
        route, body = ((initiative_routes.dom_initiative, DomInitiativeBody()) if kind == 'dom'
                       else (initiative_routes.whisper_initiative, WhisperInitiativeBody()))
        return await _collect_sse(await route('conv', body))
    output = asyncio.run(scenario())
    assert len(entries) == 1 and '暂叫' not in json.dumps(output, ensure_ascii=False)


def test_sentinel_legacy_no_empty_retry_or_chat_publication(monkeypatch, tmp_path, entries):
    import ai_providers
    import sentinel_runtime
    from test_sentinel_runtime import _patch_core_env, _fetch_all
    path, logs, broadcasts = _patch_core_env(monkeypatch, tmp_path)
    calls = []
    async def stream(messages, *args, **kwargs):
        calls.append(messages)
        yield INTENT
    monkeypatch.setattr(ai_providers, 'stream_ai', stream)
    asyncio.run(sentinel_runtime.SentinelRuntime()._call_core('空闲', 0, core_reason='回来看看', cached_logs=[]))
    assert len(entries) == len(calls) == 1
    assert not _fetch_all(path, "SELECT * FROM messages WHERE role='assistant'")


def test_together_awake_uses_existing_turn_and_hides_intent(monkeypatch, tmp_path, entries):
    from test_together import harness, Models, start, settle, ready
    async def scenario():
        model = Models('<self_awake>{"after_seconds":30,"intent":"回来"}</self_awake>', INTENT)
        async with harness(tmp_path, models=model) as (service, clock):
            row = await start(service)
            await settle(service)
            clock.advance(30)
            await ready(service, row['id'])
            await service.tick()
            await settle(service)
            assert len(entries) == 1
            view = await service.get(row['id'])
            assert '暂叫' not in json.dumps(view, ensure_ascii=False)
            assert '[心语创作]' not in str(model.calls[0])
            assert '[心语创作]' in str(model.calls[1])
    asyncio.run(scenario())


def test_scene_timed_return_has_work_outlet(monkeypatch, tmp_path, entries):
    from test_scenes import harness, Models, start, settle
    async def scenario():
        model = Models()
        model.outputs.clear()
        model.outputs.extend(['<self_awake>30</self_awake>', INTENT])
        async with harness(tmp_path, models=model) as (service, clock, events):
            await start(service)
            await settle(service)
            # Keep the existing device lease alive; trigger only the autonomous deadline.
            service.active.due_at = clock()
            await service.tick()
            await settle(service)
            assert len(entries) == 1
            assert '暂叫' not in json.dumps(events, ensure_ascii=False)
    asyncio.run(scenario())


@pytest.mark.parametrize('kind', ['screen', 'poi', 'activity'])
def test_autonomous_tool_followups_preserve_origin_and_hide_intention(archive, monkeypatch, entries, kind):
    import database
    import location
    import activity
    from app.chat import side_effects
    from test_vows_phase2 import _screen_request, _screen_ops
    monkeypatch.setattr(side_effects, 'get_db', database.get_db)
    monkeypatch.setattr(side_effects, 'load_worldbook', lambda: {'user_name': '青禾', 'ai_name': '临川'})
    async def no_background(*args, **kwargs):
        return []
    monkeypatch.setattr('app.daily_chat.service.cross_day_prompt', no_background)
    monkeypatch.setattr('app.daily_chat.service.resolve_conversation_id', lambda value: value)
    calls, broadcasts = [], []
    async def stream(messages, *args, **kwargs):
        calls.append(messages)
        yield INTENT
    async def broadcast(event):
        broadcasts.append(event)
    monkeypatch.setattr(side_effects, 'stream_ai', stream)
    monkeypatch.setattr(side_effects.manager, 'broadcast', broadcast)
    monkeypatch.setattr(location, 'load_location_config', lambda: {'amap_key': 'local', 'poi_types': {'咖啡': 'code'}})
    monkeypatch.setattr(location, 'load_location_status', lambda: {'lng': 1, 'lat': 1})
    monkeypatch.setattr(location, 'save_location_status', lambda value: None)
    monkeypatch.setattr(location, 'format_location_for_prompt', lambda: '已知地点')
    async def search(*args):
        return [{'name': '咖啡馆', 'distance': 100}]
    monkeypatch.setattr(location, 'amap_regeo', no_background)
    monkeypatch.setattr(location, 'amap_poi_search', search)
    monkeypatch.setattr(activity, 'get_activity_summary_for_prompt', lambda *args: '在读书')
    origin = WorkEntry({'source': 'self_wake', 'turn_id': 'original-turn', 'conv_id': 'conv1', 'model_key': 'core'},
        {'user_name': '青禾', 'ai_name': '临川', 'messages': [{'role': 'user', 'content': '你是临川。已绑定身份。'}]})
    async def scenario():
        if kind == 'screen':
            request = _screen_request()
            request.status, request.reject_reason = 'rejected', 'denied'
            request.heart_work_origin = origin.metadata()
            request._done_event.set()
            ops, _ = _screen_ops()
            await side_effects._run_screen_followup(request, ops=ops, screen_label='电脑', reject_text={})
        elif kind == 'poi':
            await side_effects.perform_poi_check('conv1', 'core', ['咖啡'], heart_work_origin=origin.metadata())
        else:
            await side_effects.perform_activity_check('conv1', 'core', 1, heart_work_origin=origin.metadata())
    asyncio.run(scenario())
    assert len(calls) == len(entries) == 1
    assert entries[0][0]['turn_id'] == 'original-turn' and entries[0][0]['source'] == 'self_wake'
    assert entries[0][2]['ai_name'] == '临川' and entries[0][2]['user_name'] == '青禾'
    assert '暂叫' not in json.dumps(broadcasts, ensure_ascii=False)
    assert not any(event['type'] == 'msg_created' for event in broadcasts)


def test_interrupted_together_intention_does_not_commit(monkeypatch, tmp_path, entries):
    from test_together import harness, Models, start, settle, say
    async def scenario():
        model = Models('<self_awake>{"after_seconds":30}</self_awake>')
        async with harness(tmp_path, models=model) as (service, clock):
            row = await start(service)
            await settle(service)
            started = asyncio.Event()
            async def blocked(messages):
                if len(model.calls) == 2:
                    yield INTENT
                    started.set()
                    await asyncio.Event().wait()
                else:
                    yield '收到。'
            model.hook = blocked
            clock.advance(30)
            await service.tick()
            await asyncio.wait_for(started.wait(), 2)
            await say(service, row['id'], '现在聊这个')
            await settle(service)
            assert entries == []
    asyncio.run(scenario())


def test_late_scene_intention_after_pause_does_not_commit(monkeypatch, tmp_path, entries):
    from test_scenes import harness, Models, start, settle
    async def scenario():
        model = Models()
        model.outputs.clear()
        model.outputs.append('<self_awake>30</self_awake>')
        async with harness(tmp_path, models=model) as (service, clock, events):
            row = await start(service)
            await settle(service)
            started, gate = asyncio.Event(), asyncio.Event()
            async def late(messages):
                started.set()
                try:
                    await gate.wait()
                except asyncio.CancelledError:
                    await gate.wait()
                yield INTENT
            model.turn_hook = late
            service.active.due_at = clock()
            await service.tick()
            await asyncio.wait_for(started.wait(), 2)
            await service.action(row['id'], owner_id='owner1', action='pause')
            gate.set()
            await settle(service)
            assert entries == [] and '暂叫' not in json.dumps(events, ensure_ascii=False)
    asyncio.run(scenario())
