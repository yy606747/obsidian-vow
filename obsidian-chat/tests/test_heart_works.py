import asyncio
from contextlib import asynccontextmanager

import aiosqlite
import pytest

from app.heart_works import protocol
from app.heart_works.repository import Repository, init_tables
from app.heart_works.service import HeartWorkService


@pytest.fixture
def repo(tmp_path):
    @asynccontextmanager
    async def db():
        async with aiosqlite.connect(tmp_path / 'works.db') as connection:
            yield connection
    async def initialize():
        async with db() as connection:
            await init_tables(connection)
            await connection.execute('CREATE TABLE memories (id TEXT PRIMARY KEY, embedding BLOB)')
            await connection.execute('CREATE TABLE memory_items (id TEXT,legacy_memory_id TEXT,status TEXT,visibility TEXT)')
            await connection.commit()
    asyncio.run(initialize())
    return Repository(db)


async def enqueue(repo, intent='想写一份夜间故事'):
    return await repo.enqueue(origin={'source': 'idle', 'turn_id': 'turn-1', 'conv_id': 'conv', 'model_key': 'core'},
        intent_raw=intent, context_snapshot={'ai_name': '临川', 'user_name': '青禾',
            'messages': [{'role': 'user', 'content': '你是临川。'}]})


def work(body='今晚请早点睡。', cover='今晚给你看点刺激的', note='留下了一封劝早睡的信。', end='完成'):
    return f'[封面]\n{cover}\n[正文]\n{body}\n[记下]\n{note}\n[{end}]'


def worker(repo, replies, **kwargs):
    calls = []
    async def model(key, messages):
        calls.append(messages)
        return replies.pop(0)
    service = HeartWorkService(repo, model=model, capabilities=lambda: {'IMAGE', 'MUSIC', 'MUSIC_SEARCH', 'WEB_SEARCH_INTENT'}, **kwargs)
    async def no_jobs():
        pass
    service.run_jobs = no_jobs
    return service, calls


def test_intent_stream_preserves_natural_intent_and_quoted_examples():
    raw = '先说一句。\n[心语创作]\n暂叫雨。\n  留一首歌。\n[/心语创作]\n之后。'
    visible, intent = protocol.extract_intent(raw)
    assert intent == '\n暂叫雨。\n  留一首歌。\n'
    for size in range(1, 13):
        stream = protocol.IntentStreamFilter()
        output = ''.join(stream.feed(raw[i:i + size]) for i in range(0, len(raw), size)) + stream.flush()
        assert output == visible
    assert protocol.parse_work(work('```\n[IMAGE:只是一条例子]\n```'))['body'][0]['type'] == 'text'
    nested = protocol.parse_work(work('[IMAGE:猫举着[OK]牌子]\n后文'))['body']
    assert nested[0]['query'] == '猫举着[OK]牌子' and nested[1]['text'].strip() == '后文'
    with pytest.raises(ValueError):
        protocol.parse_work(work('[IMAGE:没结束'))


def test_text_idempotence_and_private_note(repo):
    async def scenario():
        row = await enqueue(repo)
        assert (await enqueue(repo))['id'] == row['id']
        service, calls = worker(repo, [work()])
        await service.run(row['id'])
        await service.run(row['id'])
        saved = await repo.get(row['id'])
        assert saved['status'] == 'published' and len(calls) == 1
        assert saved['body'] == [{'type': 'text', 'text': '今晚请早点睡。'}]
        assert '劝早睡' in saved['event_text'] and '刺激' not in saved['event_text']
        assert len(await repo.read('SELECT * FROM heart_work_events', many=True)) == 1
    asyncio.run(scenario())


def test_materials_in_place_without_reassembly_call(repo):
    async def scenario():
        executed = []
        async def tool(**args):
            executed.append(args)
            return {'block': {'type': 'image' if args['tool'] == 'IMAGE' else 'music', 'url': '/material',
                              'description': args['query']}}
        row = await enqueue(repo)
        service, calls = worker(repo, [work('前文\n[IMAGE:猫]\n中段\n[MUSIC:雨]\n后文', '[IMAGE:猫]')], execute=tool)
        await service.run(row['id'])
        saved = await repo.get(row['id'])
        assert saved['status'] == 'published'
        assert [b['type'] for b in saved['body']] == ['text', 'image', 'text', 'music', 'text']
        assert len(calls) == 1 and len(executed) == 2
    asyncio.run(scenario())


def test_search_and_failure_continue_same_draft_and_update_note(repo):
    async def scenario():
        async def tool(**args):
            if args['tool'] == 'IMAGE':
                raise ValueError('image_unavailable')
            return {'results': [{'title': '章鱼睡眠', 'url': 'https://example.test/octopus', 'content': '研究材料'}]}
        row = await enqueue(repo)
        service, calls = worker(repo, [
            work('[IMAGE:章鱼]\n[WEB_SEARCH_INTENT]睡眠[/WEB_SEARCH_INTENT]', note='旧简记', end='待续'),
            work('[研究](https://example.test/octopus)让我想到一个故事。', note='留下了睡眠研究的链接与感想。')], execute=tool)
        await service.run(row['id'])
        saved = await repo.get(row['id'])
        assert saved['status'] == 'published' and len(calls) == 2
        assert 'image_unavailable' in calls[-1][-1]['content'] and '研究材料' in calls[-1][-1]['content']
        assert '旧简记' not in saved['event_text'] and '睡眠研究' in saved['event_text']
    asyncio.run(scenario())


def test_missing_note_fallback_and_abandon(repo):
    async def scenario():
        row = await enqueue(repo)
        service, calls = worker(repo, ['[封面]\n谜面\n[正文]\n真实正文\n[完成]'])
        await service.run(row['id'])
        saved = await repo.get(row['id'])
        assert '正文节选：真实正文' in saved['event_text'] and len(calls) == 1
        await repo.delete(row['id'])
        assert not await repo.read('SELECT * FROM heart_work_events', many=True)
        await service.run(row['id'])
        assert (await repo.get(row['id']))['status'] == 'deleted'
    asyncio.run(scenario())


def test_restart_resumes_saved_revision_and_completed_material(repo):
    async def scenario():
        row = await enqueue(repo)
        token = await repo.claim(row['id'])
        raw = work('[IMAGE:猫]')
        parsed = protocol.parse_work(raw)
        await repo.save_revision(row['id'], token, raw, parsed)
        task = await repo.task(row['id'], parsed['body'][0], 1)
        await repo.task_result(task, {'block': {'type': 'image', 'url': '/saved', 'description': '猫'}})
        await repo.execute("UPDATE heart_works SET lease_until=0,lease_token='' WHERE id=?", (row['id'],))
        async def forbidden(**args):
            raise AssertionError('completed image requested twice')
        service, calls = worker(repo, [], execute=forbidden)
        await service.run(row['id'])
        assert (await repo.get(row['id']))['status'] == 'published' and not calls
    asyncio.run(scenario())
