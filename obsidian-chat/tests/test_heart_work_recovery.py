import asyncio
import io

import pytest
from PIL import Image

from app.heart_works import protocol, tools
from app.heart_works.service import HeartWorkService
from test_heart_works import repo, enqueue, worker, work


def test_private_and_quoted_intentions_are_not_actions():
    intent = '[心语创作]留一幅画[/心语创作]'
    for raw in ('`' + intent + '`', '> ' + intent, '```\n' + intent + '\n```',
                '[VOW:一段引用' + intent + ']', '<think>' + intent + '</think>',
                '【发图：画着' + intent + '的纸】', '[WEB_SEARCH_INTENT]' + intent + '[/WEB_SEARCH_INTENT]',
                '[WORKING_MODEL_REQUEST]' + intent + '[/WORKING_MODEL_REQUEST]'):
        assert protocol.extract_intent(raw) == (raw, '')
    for tool in ('[SCREEN_CHECK:桌面]', '[REMEMBER:秘密]', '[VOW:秘密]', '[IMAGE:没结束'):
        with pytest.raises(ValueError):
            protocol.parse_work(work(tool))


def test_tool_adapters_use_raw_image_and_stable_music_without_chat(tmp_path, monkeypatch):
    import music
    from app.presence.image_provider import ImageGenerationTransport
    from app.web_search.provider import TavilySearchProvider
    monkeypatch.setattr(tools, 'material_root', lambda: tmp_path)
    monkeypatch.setattr(tools, 'capabilities', lambda: {'IMAGE', 'MUSIC', 'MUSIC_SEARCH', 'WEB_SEARCH_INTENT'})
    monkeypatch.setattr(tools, 'image_config', lambda: {'slot': 'configured-endpoint'})
    calls = []
    async def image(_self, prompt, *, request_config):
        calls.append((prompt, request_config))
        data = io.BytesIO()
        Image.new('RGB', (37, 23), 'orange').save(data, format='PNG')
        return data.getvalue()
    def songs(query, limit):
        return [{'id': 123, 'name': '雨夜', 'artist': '歌手'}]
    async def search(_self, query):
        calls.append(query)
        return {'results': [{'title': '材料', 'url': 'https://example.test', 'content': '摘要'}]}
    monkeypatch.setattr(ImageGenerationTransport, 'generate_raw', image)
    monkeypatch.setattr(music, 'search_songs', songs)
    monkeypatch.setattr(music, 'get_audio_url', lambda song_id: 'https://temporary.test/expires')
    monkeypatch.setattr(TavilySearchProvider, 'search', search)
    async def scenario():
        first = await tools.execute(work_id='work', task_id='image', tool='IMAGE', query='一张普通海报')
        second = await tools.execute(work_id='work', task_id='image', tool='IMAGE', query='一张普通海报')
        assert first == second and len(calls) == 1
        with Image.open(first['asset']['path']) as image:
            assert image.size == (37, 23) and image.mode == 'RGB'
        assert calls == [('一张普通海报', {'slot': 'configured-endpoint'})]
        selected = await tools.execute(work_id='work', task_id='music', tool='MUSIC', query='雨夜 歌手')
        assert selected['block']['url'] == '/api/music/stream/123' and 'temporary' not in str(selected)
        candidates = await tools.execute(work_id='work', task_id='candidates', tool='MUSIC_SEARCH', query='雨夜')
        assert candidates['candidates'][0]['id'] == 123 and 'block' not in candidates
        result = await tools.execute(work_id='work', task_id='web', tool='WEB_SEARCH_INTENT', query='海报')
        assert result['results'][0]['content'] == '摘要'
        monkeypatch.setattr(music, 'get_audio_url', lambda song_id: None)
        with pytest.raises(ValueError, match='music_unplayable'):
            await tools.execute(work_id='work', task_id='bad', tool='MUSIC', query='雨夜')
        monkeypatch.setattr(tools, 'capabilities', lambda: set())
        assert await tools.execute(work_id='work', task_id='image', tool='IMAGE', query='一张普通海报') == first
        with pytest.raises(ValueError, match='capability_unavailable'):
            await tools.execute(work_id='work', task_id='new', tool='IMAGE', query='另一张')
    asyncio.run(scenario())


def test_capabilities_describe_only_configured_tools(monkeypatch):
    import music
    from app.web_search import web_search_service
    from app.presence import image_provider
    from app.heart_works.prompts import intention_prompt, creation_prompt
    slots = []
    def request_config(slot):
        slots.append(slot)
        if slot == 'sticker_image':
            raise image_provider.PresenceImageProviderError('unconfigured')
        return ('endpoint', 'model', 30)
    monkeypatch.setattr(image_provider, '_request_config', request_config)
    monkeypatch.setattr(music, 'get_music_dependency_status', lambda: {'available': False})
    monkeypatch.setattr(web_search_service, 'enabled', lambda: False)
    caps = tools.capabilities()
    assert caps == {'IMAGE'} and slots == ['sticker_image', 'presence_image']
    prompt = creation_prompt(ai_name='临川', user_name='青禾', intent_raw='一个念头', capabilities=caps)
    assert '[IMAGE:' in prompt and '[MUSIC:' not in prompt and '[WEB_SEARCH_INTENT]' not in prompt
    def unavailable(slot):
        raise image_provider.PresenceImageProviderError('unconfigured')
    monkeypatch.setattr(image_provider, '_request_config', unavailable)
    assert tools.capabilities() == set()
    prompt = intention_prompt(ai_name='临川', user_name='青禾', capabilities=set())
    assert '后续创作当前可用：文字。' in prompt


def test_failure_explicit_retry_then_multistep_search_retains_material(repo):
    async def scenario():
        attempts = []
        async def execute(**args):
            attempts.append(args['tool'])
            if args['tool'] == 'IMAGE':
                if attempts.count('IMAGE') == 1:
                    raise ValueError('unavailable')
                return {'block': {'type': 'image', 'description': '灯塔', 'url': '/image'}}
            if args['tool'] == 'MUSIC_SEARCH':
                return {'candidates': [{'name': '灯塔曲', 'artist': '歌手'}]}
            if args['tool'] == 'MUSIC':
                return {'block': {'type': 'music', 'title': '灯塔曲', 'artist': '歌手', 'url': '/music'}}
            return {'results': [{'content': '第一份搜索材料'}]}
        row = await enqueue(repo)
        service, calls = worker(repo, [
            work('[IMAGE:灯塔]\n[WEB_SEARCH_INTENT]灯塔[/WEB_SEARCH_INTENT]', end='待续'),
            work('[IMAGE:灯塔]\n[MUSIC_SEARCH:灯塔曲]', end='待续'),
            work('[IMAGE:灯塔]\n[MUSIC:灯塔曲 歌手]')], execute=execute)
        await service.run(row['id'])
        assert (await repo.get(row['id']))['status'] == 'published'
        assert len(calls) == 3 and attempts == ['IMAGE', 'WEB_SEARCH_INTENT', 'IMAGE', 'MUSIC_SEARCH', 'MUSIC']
        assert '第一份搜索材料' in calls[-1][-1]['content']
        assert '灯塔曲' in calls[-1][-1]['content']
        assert (await repo.read("SELECT attempt FROM heart_work_tasks WHERE tool='IMAGE'"))['attempt'] == 2
    asyncio.run(scenario())


def test_unknown_image_is_returned_to_core_without_blind_regeneration(repo, tmp_path, monkeypatch):
    monkeypatch.setattr(tools, 'material_root', lambda: tmp_path)
    async def scenario():
        row = await enqueue(repo)
        token = await repo.claim(row['id'])
        raw = work('[IMAGE:失联图片]')
        parsed = protocol.parse_work(raw)
        await repo.save_revision(row['id'], token, raw, parsed)
        task = await repo.task(row['id'], parsed['body'][0], 1)
        await repo.execute("UPDATE heart_work_tasks SET status='running',attempt=1 WHERE id=?", (task['id'],))
        await repo.execute("UPDATE heart_works SET lease_until=0,lease_token='' WHERE id=?", (row['id'],))
        async def forbidden(**args):
            pytest.fail('unknown paid request must not be retried without a decision')
        service, calls = worker(repo, [work('换成文字。')], execute=forbidden)
        await service.run(row['id'])
        assert (await repo.get(row['id']))['status'] == 'published'
        assert '结果未知' in calls[0][-1]['content']
        assert (await repo.read('SELECT status FROM heart_work_tasks'))['status'] == 'unknown'
    asyncio.run(scenario())


@pytest.mark.parametrize('terminal', ['delete', 'abandon'])
def test_concurrent_terminal_work_cannot_be_published_by_late_material(repo, terminal):
    async def scenario():
        row = await enqueue(repo)
        async def execute(**args):
            if terminal == 'delete':
                await repo.delete(row['id'])
            else:
                await repo.abandon(row['id'], '不留了')
            return {'block': {'type': 'image', 'url': '/late'}}
        service, _ = worker(repo, [work('[IMAGE:晚到]')], execute=execute)
        await service.run(row['id'])
        assert (await repo.get(row['id']))['status'] == {'delete': 'deleted', 'abandon': 'abandoned'}[terminal]
        assert await repo.read('SELECT * FROM heart_work_jobs', many=True) == []
        assert await repo.read('SELECT * FROM heart_work_events', many=True) == []
        assert (await enqueue(repo))['id'] == row['id']
    asyncio.run(scenario())


def test_claim_and_paused_repair_keep_one_draft(repo):
    async def scenario():
        row = await enqueue(repo)
        first, second = await asyncio.gather(repo.claim(row['id']), repo.claim(row['id']))
        assert bool(first) != bool(second)
        await repo.execute("UPDATE heart_works SET lease_until=0,lease_token='' WHERE id=?", (row['id'],))
        service, calls = worker(repo, ['半截正文'] * 3)
        await service.run(row['id'])
        assert (await repo.get(row['id']))['status'] == 'paused' and len(calls) == 3
        await repo.resume(row['id'])
        service, _ = worker(repo, [work('刻意留下的故事片段……')])
        await service.run(row['id'])
        assert (await repo.get(row['id']))['status'] == 'published'
        assert len(await repo.read('SELECT * FROM heart_works', many=True)) == 1
    asyncio.run(scenario())


def test_deliberate_continuation_is_not_capped_at_three_calls(repo):
    async def scenario():
        row = await enqueue(repo)
        service, calls = worker(repo, [work(f'第{i}段', end='待续') for i in range(4)] + [work('完整作品')])
        await service.run(row['id'])
        assert (await repo.get(row['id']))['status'] == 'published' and len(calls) == 5
    asyncio.run(scenario())


def test_provider_truncation_cannot_publish_even_with_a_finish_marker(repo, monkeypatch):
    import ai_providers
    from app.heart_works.service import core_call
    from app.tools.ledger import tool_invocation_ledger
    traces = []
    async def truncated(*args, **kwargs):
        kwargs['usage_meta']['finish_reason'] = 'MAX_TOKENS'
        return work()
    async def request(context, **kwargs):
        traces.append(dict(context.metadata))
    monkeypatch.setattr(ai_providers, 'call_core_chat_once', truncated)
    monkeypatch.setattr(tool_invocation_ledger, 'record_model_request', request)
    monkeypatch.setattr(tools, 'capabilities', lambda: set())
    async def scenario():
        row = await enqueue(repo)
        service = HeartWorkService(repo, model=core_call, capabilities=lambda: set())
        await service.run(row['id'])
        assert (await repo.get(row['id']))['status'] == 'paused'
        assert [trace['revision'] for trace in traces] == [1, 2, 3]
        assert all(trace['work_id'] == row['id'] for trace in traces)
        assert await repo.read('SELECT * FROM heart_work_events', many=True) == []
    asyncio.run(scenario())


def test_abandoned_work_has_no_memory_and_infrastructure_failure_can_resume(repo):
    async def scenario():
        row = await enqueue(repo)
        async def failed(*args):
            raise TimeoutError()
        service = HeartWorkService(repo, model=failed, capabilities=lambda: set())
        for _ in range(3):
            await service.run(row['id'])
            await repo.execute('UPDATE heart_works SET retry_at=0 WHERE id=?', (row['id'],))
        assert (await repo.get(row['id']))['status'] == 'paused'
        await repo.resume(row['id'])
        service, _ = worker(repo, ['[放弃:今天先不留了]'])
        await service.run(row['id'])
        assert (await repo.get(row['id']))['status'] == 'abandoned'
        assert not await repo.read('SELECT * FROM heart_work_events', many=True)
    asyncio.run(scenario())


def test_publish_notification_retries_without_republishing(repo):
    async def scenario():
        row = await enqueue(repo)
        service, _ = worker(repo, [work()])
        await service.run(row['id'])
        await repo.execute("UPDATE heart_work_jobs SET status='done' WHERE kind='memory'")
        events = []
        async def broadcast(event):
            events.append(event)
            if len(events) == 1:
                raise ConnectionError()
        outbox = HeartWorkService(repo, broadcast=broadcast)
        await outbox.run_jobs()
        assert (await repo.read("SELECT * FROM heart_work_jobs WHERE kind='notify'"))['attempts'] == 1
        await repo.execute("UPDATE heart_work_jobs SET retry_at=0 WHERE kind='notify'")
        await outbox.run_jobs()
        await outbox.run_jobs()
        assert len(events) == 2 and events[0] == events[1]
        assert set(events[0]['data']) == {'id', 'kind', 'created_at', 'cover'}
        assert len(await repo.read('SELECT * FROM heart_work_events', many=True)) == 1
    asyncio.run(scenario())
