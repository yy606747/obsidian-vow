import asyncio
import base64
import json
from types import SimpleNamespace

import httpx
import pytest

from app.chat import streaming
from app.daily_chat.service import DailyChatService
from app.presence.db import init_presence_tables
from app.stickers.image_provider import StickerImageProvider
from app.stickers.repository import init_tables
from app.tools.schemas import ToolContext
from test_chat_streaming_sse import _collect_sse, _patch_streaming_io, _prompt_meta
from test_stickers import env, picture_bytes


@pytest.mark.parametrize('body,match,broken_supplement,core_calls', [
    ('正文', False, False, 1), ('', True, False, 1), ('', False, False, 2), ('', False, True, 2),
])
def test_real_chat_delivery_and_sql_attachment(env, monkeypatch, body, match, broken_supplement, core_calls):
    executed, broadcasts = _patch_streaming_io(monkeypatch)
    monkeypatch.setattr('app.stickers.service.sticker_service', env.service)
    monkeypatch.setattr(streaming, 'sticker_repository', env.repo)
    monkeypatch.setattr(streaming, '_tool_context', lambda **kw: ToolContext(
        conv_id=kw['conv_id'], msg_id=kw['msg_id'], capabilities=('sticker.send',), model_key='core'))
    calls = []

    async def core(messages, *_args):
        calls.append(messages)
        text = body + '【发图：偷偷探头】' if len(calls) == 1 else ('【发图：又一张】' if broken_supplement else '偷偷看看。')
        for char in text:
            yield char

    monkeypatch.setattr(streaming, 'stream_ai', core)

    async def run():
        row = await env.service.store_bytes(picture_bytes())
        await env.service.summarize(row)

        async def matcher(*_args, **_kwargs):
            return json.dumps({'id': row['id'] if match else None})

        env.service.caller = matcher
        meta = {**_prompt_meta(), 'sticker_names': {'user_name': '小满', 'ai_name': '知秋'}}
        response = await streaming.stream_chat_response(conv_id='c', model_key='core', history=[], prompt_meta=meta, temperature=0.7)
        return await _collect_sse(response)

    events = asyncio.run(run())
    assert len(calls) == core_calls
    visible = ''.join(event.get('content', '') for event in events)
    assert '发图' not in visible
    inserts = [params for sql, params in executed if sql.startswith('INSERT INTO messages') and params[2] == 'assistant']
    if broken_supplement:
        assert not inserts and '[请求出错:' in visible
    else:
        assert len(inserts) == 1
        saved = json.loads(inserts[0][5] or '[]')
        assert bool(saved) == match
        if match:
            event = next(event for event in events if event['type'] == 'message')
            assert event['message']['content'] == ''
            assert event['message']['attachments'] == saved
            assert saved[0]['type'] == 'sticker'
        if core_calls == 2:
            assert inserts[0][3] == '偷偷看看。'
            assert '小满' in calls[-1][-1]['content'] and '知秋' in calls[-1][-1]['content']


@pytest.mark.parametrize('raw,core_calls,expect_error', [
    ('【偷图】【发图：探头】', 2, False),
    ('【发图：探头】【偷图】', 2, False),
    ('【发图：】', 1, True), ('【发图：   】', 1, True),
    ('【发图：探头', 1, True), ('【发图：举着[OK]的牌子', 1, True),
    ('【偷图】', 1, True), ('', 1, True),
])
def test_empty_reply_is_recovered_once_or_rejected_before_sql(env, monkeypatch, raw, core_calls, expect_error):
    executed, broadcasts = _patch_streaming_io(monkeypatch)
    monkeypatch.setattr('app.stickers.service.sticker_service', env.service)
    monkeypatch.setattr(streaming, 'sticker_repository', env.repo)
    monkeypatch.setattr(streaming, '_tool_context', lambda **kw: ToolContext(
        conv_id=kw['conv_id'], msg_id=kw['msg_id'], capabilities=('sticker.save', 'sticker.send'), model_key='core'))
    calls = []

    async def core(messages, *_args):
        calls.append(messages)
        yield raw if len(calls) == 1 else '偷偷看看。'

    monkeypatch.setattr(streaming, 'stream_ai', core)

    async def run():
        response = await streaming.stream_chat_response(conv_id='c', model_key='core', history=[],
            prompt_meta={**_prompt_meta(), 'sticker_names': {'user_name': '小满', 'ai_name': '知秋'}}, temperature=0)
        return await _collect_sse(response)

    events = asyncio.run(run())
    assert len(calls) == core_calls
    inserts = [params for sql, params in executed if sql.startswith('INSERT INTO messages') and params[2] == 'assistant']
    assert len(inserts) == (0 if expect_error else 1)
    assert all(params[3].strip() or json.loads(params[5] or '[]') for params in inserts)
    assert events[-1]['has_error'] is expect_error
    if expect_error:
        assert '[请求出错:' in ''.join(event.get('content', '') for event in events)
    else:
        assert inserts[0][3] == '偷偷看看。'


@pytest.mark.parametrize('failure', ['provider', 'commit'])
def test_failed_regenerate_restores_replayed_feedback(env, monkeypatch, failure):
    from test_chat_streaming_sse import _FakeDB
    executed, _broadcasts = _patch_streaming_io(monkeypatch)
    monkeypatch.setattr(streaming, 'sticker_repository', env.repo)

    class FailingDB(_FakeDB):
        async def commit(self):
            raise RuntimeError('test_commit_failure')

    if failure == 'commit':
        monkeypatch.setattr(streaming, 'get_db', lambda: FailingDB(executed))

    async def core(*_args):
        yield '[请求出错: test_provider_failure]' if failure == 'provider' else '收到。'

    monkeypatch.setattr(streaming, 'stream_ai', core)

    async def run():
        names = {'user_name': '小满', 'ai_name': '知秋'}
        await env.repo.begin('e', action='send', conv_id='c', expression='探头')
        await env.repo.finish('e', status='miss')
        claim = await env.repo.claim(conv_id='c', turn_id='old-turn', **names)
        async with env.db() as db:
            await env.repo.finalize_in_tx(db, turn_id='old-turn', message_id='deleted-answer')
            await db.commit()
        replay = await env.repo.replay('deleted-answer', **names)
        assert replay['block'] == claim['block'] and not replay['bound_turn_id']
        response = await streaming.stream_chat_response(conv_id='c', model_key='core', history=[],
            prompt_meta={**_prompt_meta(), 'prompt_source': 'regenerate', 'sticker_feedback': replay,
                         'sticker_names': names}, temperature=0)
        await _collect_sse(response)
        # No receipt is left pointing at the removed answer, and retry can claim it.
        assert not (await env.repo.replay('deleted-answer', **names))['block']
        retry = await env.repo.claim(conv_id='c', turn_id='regenerate:source', **names)
        assert retry['block'] == claim['block']
        await env.repo.release(retry['bound_turn_id'])
        assert (await env.repo.claim(conv_id='c', turn_id='next-natural-chat', **names))['block'] == claim['block']
    asyncio.run(run())


def test_pending_feedback_moves_across_daily_window(env):
    async def run():
        for event_id, conv in [('pending', 'old'), ('finished', 'old'), ('unrelated', 'other')]:
            await env.repo.begin(event_id, action='send', conv_id=conv, expression='猫猫')
            await env.repo.finish(event_id, status='miss', notify=event_id != 'finished')
        service = DailyChatService(env.db)
        async with env.db() as db:
            await service._carry_live_state(db, ['old'], 'new')
            await db.commit()
            rows = dict(await (await db.execute('SELECT id,conv_id FROM sticker_events')).fetchall())
        assert rows == {'pending': 'new', 'finished': 'old', 'unrelated': 'other'}
        claim = await env.repo.claim(conv_id='new', turn_id='morning', user_name='小满', ai_name='知秋')
        assert claim['block']
    asyncio.run(run())


def test_restart_records_interrupted_work_without_provider_calls(env):
    async def run():
        row = await env.service.store_bytes(picture_bytes())
        await env.repo.begin('save', action='save', conv_id='c')
        async with env.db() as db:
            await init_tables(db)
            await db.commit()
        assert (await env.repo.get(row['id']))['status'] == 'failed'
        assert not env.calls
        assert '重启中断' in (await env.repo.claim(conv_id='c', turn_id='t', user_name='小满', ai_name='知秋'))['block']
    asyncio.run(run())


def test_old_night_rows_survive_branch_migration(env):
    async def run():
        async with env.db() as db:
            await db.execute("""CREATE TABLE presence_night_rounds (
                night_key TEXT PRIMARY KEY, timezone TEXT NOT NULL, status TEXT NOT NULL,
                branch TEXT CHECK(branch IN ('draw','reflect','none','invalid','provider_failed')),
                started_at REAL, finished_at REAL, error TEXT, updated_at REAL)""")
            await db.execute("INSERT INTO presence_night_rounds VALUES ('old','UTC','completed','draw',1,2,'',2)")
            await init_presence_tables(db)
            await init_presence_tables(db)
            assert (await (await db.execute("SELECT branch FROM presence_night_rounds WHERE night_key='old'")).fetchone())[0] == 'draw'
            await db.execute("INSERT INTO presence_night_rounds VALUES ('new','UTC','completed','sticker',3,4,'',4)")
            await db.commit()
    asyncio.run(run())


@pytest.mark.parametrize('provider', ['gemini', 'openai'])
def test_sticker_image_transport_preserves_text_background_without_rembg(monkeypatch, provider):
    png = picture_bytes()
    requests = []

    def handle(request):
        body = json.loads(request.content)
        requests.append(body)
        if provider == 'gemini':
            return httpx.Response(200, json={'candidates': [{'content': {'parts': [{'inlineData': {'mimeType': 'image/png', 'data': base64.b64encode(png).decode()}}]}}]})
        return httpx.Response(200, json={'data': [{'b64_json': base64.b64encode(png).decode()}]})

    from app.presence import image_provider
    original_client = httpx.AsyncClient
    monkeypatch.setattr(image_provider.httpx, 'AsyncClient', lambda **kwargs: original_client(transport=httpx.MockTransport(handle), **kwargs))
    monkeypatch.setattr(StickerImageProvider, '_request_config', staticmethod(lambda: (
        {'type': provider, 'base_url': 'https://image.test', 'api_key': 'test'}, 'configured-model', 30)))

    async def forbidden_rembg(*args):
        raise AssertionError('stickers must not use sprite cutout')

    monkeypatch.setattr(image_provider, '_rembg_session', forbidden_rembg)
    monkeypatch.setattr(image_provider, '_remove_background', forbidden_rembg)
    assert asyncio.run(StickerImageProvider().generate_raw('蓝色背景，写“收到”')) == png
    prompt = requests[0]['contents'][0]['parts'][0]['text'] if provider == 'gemini' else requests[0]['prompt']
    assert '蓝色背景，写“收到”' in prompt
    assert 'No scenery' not in prompt and 'desktop spirit' not in prompt
    if provider == 'openai':
        assert not requests[0]['negative_prompt']


@pytest.mark.parametrize('broken', [False, True])
def test_night_sticker_runs_real_parser_and_executor_without_chat(env, monkeypatch, broken):
    import opportunity as opp
    from app.chat import action_executor
    from test_opportunity import _stub_broadcast, _stub_prepared_turn

    _stub_prepared_turn(monkeypatch, capabilities={'sticker.create'}, kind='night')
    broadcasts = _stub_broadcast(monkeypatch)
    monkeypatch.setattr('app.stickers.service.sticker_service', env.service)

    async def core(*_args, **_kwargs):
        return '【做表情：白猫举着[OK]的牌子，蓝色背景，写“收到”】'

    async def generate(expression):
        assert expression == '白猫举着[OK]的牌子，蓝色背景，写“收到”'
        if broken:
            raise RuntimeError('upstream unavailable')
        return picture_bytes()

    class Ledger:
        def new_invocation_id(self, _name):
            return 'night-sticker'

        async def record_turn(self, *_args, **_kwargs):
            pass

        async def record_execution(self, *_args, **_kwargs):
            pass

    monkeypatch.setattr(opp, 'tool_invocation_ledger', Ledger())
    monkeypatch.setattr(action_executor, 'tool_invocation_ledger', Ledger())
    monkeypatch.setattr(opp, 'call_opportunity_core', core)
    monkeypatch.setattr('app.stickers.image_provider.generate_sticker', generate)

    async def run():
        result = await opp.run_autonomous_turn(kind='night', now=1000,
            target={'conv_id': 'c', 'model_key': 'core', 'last_user_ts': 1})
        assert result['round_branch'] == ('provider_failed' if broken else 'sticker')
        assert result['status'] == ('action_failed' if broken else 'acted')
        assert len(await env.repo.library()) == (0 if broken else 1)
        feedback = await env.repo.claim(conv_id='c', turn_id='morning', user_name='小满', ai_name='知秋')
        assert ('未完成' if broken else '已收藏') in feedback['block']
        assert not broadcasts
    asyncio.run(run())


def test_sticker_slots_copy_defaults_once_and_preserve_owner_choices():
    import config
    settings = {'endpoints': [
        {'id': 'gem', 'type': 'gemini', 'base_url': 'https://gem.test', 'api_key': 'test'},
        {'id': 'sf', 'type': 'openai', 'base_url': 'https://api.siliconflow.cn/v1', 'api_key': 'test'},
    ], 'slots': {'presence_image': {'endpoint': 'gem', 'model': 'chosen-image'}}}
    config._ensure_endpoints_and_slots(settings)
    slots = settings['slots']
    assert slots['sticker_matcher'] == {'endpoint': 'sf', 'model': 'Qwen/Qwen3.5-27B', 'enable_thinking': False}
    assert slots['sticker_summary']['model'] == 'gemini-2.5-flash'
    assert slots['sticker_image'] == slots['presence_image']
    slots['sticker_image']['model'] = 'owner-new-image'
    slots['sticker_matcher']['endpoint'] = ''
    config._ensure_endpoints_and_slots(settings)
    assert slots['sticker_image']['model'] == 'owner-new-image'
    assert slots['presence_image']['model'] == 'chosen-image'
    assert slots['sticker_matcher']['endpoint'] == ''


def test_cross_day_sticker_keeps_actual_summary_and_sender():
    from app.daily_chat.service import build_cross_day_context
    records = [{'id': 'a', 'role': 'assistant', 'content': '', 'created_at': 1,
        'attachments': json.dumps([{'type': 'sticker', 'mime_type': 'image/gif',
            'url': '/uploads/collected.gif', 'expression': '偷偷看看', 'summary': {'visual': '白猫探头'}}])}]
    block = build_cross_day_context(records, user_name='小满', ai_name='知秋', timezone='UTC')[0]
    assert not block['attachments']
    assert '知秋向小满' in block['content'] and '白猫探头' in block['content']
    assert '小满在这条记录中发过图片' not in block['content']
