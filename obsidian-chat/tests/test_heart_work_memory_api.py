import asyncio
import importlib
import json

import pytest

import database
from app.heart_works.repository import Repository
from app.memory_v2 import embedding
from app.memory_v3.pending_recall import selection_prompt_items
from test_heart_works import enqueue, worker, work

hybrid = importlib.import_module('app.memory_v2.hybrid_recall')


@pytest.fixture
def archive(tmp_path, monkeypatch):
    monkeypatch.setattr(database, 'DB_PATH', tmp_path / 'archive.db')
    asyncio.run(database.init_db())
    embedded = []
    async def vector(text):
        embedded.append(text)
        return [1., 0.]
    monkeypatch.setattr(embedding, 'get_document_embedding', vector)
    monkeypatch.setattr(embedding, 'get_embedding', vector)
    monkeypatch.setattr('app.memory_v2.repository.get_document_embedding', vector)
    hybrid.clear_full_corpus_cache()
    yield Repository(), embedded
    hybrid.clear_full_corpus_cache()


@pytest.mark.parametrize('compaction,full_corpus', [(False, False), (False, True), (True, True)])
def test_event_only_reaches_ordinary_pending_and_deleted_snapshot(archive, monkeypatch, compaction, full_corpus):
    from app.heart_works.memory import write_event, valid_items
    from app.memory_v2.compaction_config import load_config as load_compaction_config
    repo, embedded = archive
    policy = load_compaction_config()
    monkeypatch.setattr(hybrid, 'load_compaction_config', lambda: {**policy, 'enabled': compaction})
    async def scenario():
        row = await enqueue(repo)
        service, _ = worker(repo, [work('FULL_BODY_NEVER_INDEXED', note='临川留下了一则灯塔故事。')])
        await service.run(row['id'])
        saved = await repo.get(row['id'])
        await write_event(repo, saved)
        await write_event(repo, saved)
        assert len(await repo.read('SELECT id FROM memories', many=True)) == 1
        candidates = await hybrid.wide_chunk_recall('灯塔故事', top_k=8, candidate_limit=50,
            as_of_ts=repo.now() + 1, full_corpus_enabled=full_corpus)
        assert len(candidates) == 1 and candidates[0]['source_type'] == 'heart_work'
        assert '灯塔故事' in candidates[0]['raw_content']
        candidate_id = candidates[0]['candidate_id']
        items = selection_prompt_items({'selected_candidate_ids': [candidate_id], 'needs_raw_detail_ids': [candidate_id]}, candidates)
        assert items[0]['kind'] == 'event' and not items[0]['needs_raw_detail']
        ordinary = await hybrid.hybrid_recall('灯塔故事', full_corpus=full_corpus, min_score=0)
        events = [item for item in ordinary['selected'] if item.get('work_id') == row['id']]
        assert len(events) == 1
        # The ordinary event note and pending projection have different row IDs
        # but must occupy one prompt slot for the same work.
        note = {**events[0], 'id': 'ordinary-note', 'candidate_id': 'ordinary-note'}
        merged = hybrid.merge_pending_items({'selected': [note]}, items, top_k=8)
        assert len(merged['selected']) == 1
        assert merged['selected'][0]['retrieval_routes'] == ['ordinary', 'pending']
        assert all('FULL_BODY_NEVER_INDEXED' not in text for text in embedded)
        assert 'FULL_BODY_NEVER_INDEXED' not in json.dumps([candidates, items, ordinary])
        assert await valid_items(items)
        await repo.delete(row['id'])
        service.invalidate()
        assert await valid_items(items) == []
        assert await hybrid.wide_chunk_recall('灯塔故事', top_k=8, candidate_limit=50, as_of_ts=repo.now() + 1) == []
    asyncio.run(scenario())


def test_event_embedding_refresh_and_delete_during_writeback(archive, monkeypatch):
    from app.heart_works.memory import write_event
    repo, _ = archive
    monkeypatch.setattr(embedding, 'embedding_signature', lambda: 'model-a')
    async def scenario():
        row = await enqueue(repo)
        service, _ = worker(repo, [work('正文档案', note='留下了一封信。')])
        await service.run(row['id'])
        saved = await repo.get(row['id'])
        await write_event(repo, saved)
        original = await repo.read('SELECT * FROM heart_work_events')
        async def vector(_text):
            return [0., 1.]
        monkeypatch.setattr(embedding, 'embedding_signature', lambda: 'model-b')
        monkeypatch.setattr(embedding, 'get_document_embedding', vector)
        await write_event(repo, saved)
        refreshed = await repo.read('SELECT * FROM heart_work_events')
        assert refreshed['embedding_signature'] == 'model-b'
        assert refreshed['embedding'] != original['embedding']
        memory = await repo.read('SELECT * FROM memories')
        note = await repo.read('SELECT * FROM memory_items')
        assert memory['embedding'] == note['embedding'] == refreshed['embedding']
        async def delete_while_embedding(_text):
            await repo.delete(row['id'])
            return [1., 1.]
        monkeypatch.setattr(embedding, 'embedding_signature', lambda: 'model-c')
        monkeypatch.setattr(embedding, 'get_document_embedding', delete_while_embedding)
        await write_event(repo, saved)
        assert not await repo.read('SELECT * FROM memories', many=True)
        assert not await repo.read('SELECT * FROM heart_work_events', many=True)
        assert not await repo.read("SELECT * FROM memory_items WHERE status='active'", many=True)
    asyncio.run(scenario())


def test_archive_api_only_published_cover_and_persisted_read(archive):
    import httpx
    from fastapi import FastAPI
    from routes.heart_whispers import router
    repo, _ = archive
    async def scenario():
        app = FastAPI()
        app.include_router(router)
        row = await enqueue(repo)
        await repo.execute("INSERT INTO heart_whispers VALUES ('old','conv','message','旧心语',1)")
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url='http://test') as client:
            listing = (await client.get('/api/heart-whispers?include_works=true')).json()
            assert len(listing['items']) == 1
            assert (await client.get('/api/heart-whispers/works/' + row['id'])).status_code == 404
            service, calls = worker(repo, [work('BODY_SECRET', 'COVER_ONLY', 'NOTE_PRIVATE')])
            await service.run(row['id'])
            listing = (await client.get('/api/heart-whispers?include_works=true')).json()
            assert 'COVER_ONLY' in str(listing) and 'BODY_SECRET' not in str(listing) and 'NOTE_PRIVATE' not in str(listing)
            legacy = (await client.get('/api/heart-whispers')).json()
            assert legacy['items'] == [{'id': 'old', 'conv_id': 'conv', 'msg_id': 'message', 'content': '旧心语', 'created_at': 1.}]
            detail = (await client.get('/api/heart-whispers/works/' + row['id'])).json()
            assert 'BODY_SECRET' in str(detail) and 'NOTE_PRIVATE' not in str(detail)
            assert len(calls) == 1
            await client.delete('/api/heart-whispers/works/' + row['id'])
            assert (await client.get('/api/heart-whispers/works/' + row['id'])).status_code == 404
    asyncio.run(scenario())


def test_archive_kind_filter_paginates_each_kind_independently(archive):
    import httpx
    from fastapi import FastAPI
    from routes.heart_whispers import router
    repo, _ = archive

    async def scenario():
        app = FastAPI()
        app.include_router(router)
        for index in range(23):
            await repo.execute('INSERT INTO heart_whispers VALUES (?,?,?,?,?)',
                               (f'short_{index:02}', 'conv', f'msg_{index}', '短心语', 100 + index))
        works = []
        for index, status in enumerate(('published', 'published', 'published', 'queued', 'deleted')):
            row = await repo.enqueue(
                origin={'source': 'idle', 'turn_id': str(index), 'conv_id': 'conv', 'model_key': 'core'},
                intent_raw='一封信', context_snapshot={})
            await repo.execute('UPDATE heart_works SET status=?,published_at=?,cover_json=?,body_json=? WHERE id=?',
                               (status, 10 + index, '[{"type":"text","text":"封面"}]',
                                '[{"type":"text","text":"BODY_PRIVATE"}]', row['id']))
            if status == 'published':
                works.insert(0, row['id'])
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url='http://test') as client:
            for include_works in ('false', 'true'):
                long_pages = [(await client.get('/api/heart-whispers', params={
                    'kind': 'work', 'page': page, 'page_size': 2, 'include_works': include_works,
                })).json() for page in (1, 2)]
                assert [item['id'] for page in long_pages for item in page['items']] == works
                assert all(page['total'] == 3 for page in long_pages)
                assert all(item['kind'] == 'work' for page in long_pages for item in page['items'])
                assert 'BODY_PRIVATE' not in str(long_pages)
                short_pages = [(await client.get('/api/heart-whispers', params={
                    'kind': 'legacy', 'page': page, 'page_size': 20, 'include_works': include_works,
                })).json() for page in (1, 2)]
                assert [len(page['items']) for page in short_pages] == [20, 3]
                assert all(page['total'] == 23 for page in short_pages)
                assert [item['id'] for page in short_pages for item in page['items']] == [
                    f'short_{index:02}' for index in reversed(range(23))]
                assert all(item['kind'] == 'legacy' for page in short_pages for item in page['items'])
            assert (await client.get('/api/heart-whispers?kind=unknown')).status_code == 422
    asyncio.run(scenario())


def test_saved_image_route_and_unused_asset_cleanup(archive, tmp_path, monkeypatch):
    import io
    import httpx
    from PIL import Image
    from fastapi import FastAPI
    from routes.heart_whispers import router
    from app.heart_works import tools
    from app.presence.image_provider import ImageGenerationTransport
    repo, _ = archive
    monkeypatch.setattr(tools, 'material_root', lambda: tmp_path / 'materials')
    monkeypatch.setattr(tools, 'capabilities', lambda: {'IMAGE', 'WEB_SEARCH_INTENT'})
    monkeypatch.setattr(tools, 'image_config', lambda: ('endpoint', 'model', 30))
    async def generate(*args, **kwargs):
        output = io.BytesIO()
        Image.new('RGB', (12, 13), 'red').save(output, 'PNG')
        return output.getvalue()
    monkeypatch.setattr(ImageGenerationTransport, 'generate_raw', generate)
    async def execute(**args):
        if args['tool'] == 'WEB_SEARCH_INTENT':
            return {'results': []}
        return await tools.execute(**args)
    async def scenario():
        app = FastAPI()
        app.include_router(router)
        row = await enqueue(repo)
        service, _ = worker(repo, [
            work('[IMAGE:不用的草图]\n[WEB_SEARCH_INTENT]查点资料[/WEB_SEARCH_INTENT]', end='待续'),
            work('正文', cover='[IMAGE:采用的封面]')], execute=execute)
        await service.run(row['id'])
        assert len(list(tools.material_root().rglob('*.png'))) == 2
        await service.cleanup_assets()
        assert len(list(tools.material_root().rglob('*.png'))) == 1
        saved = await repo.get(row['id'])
        url = saved['cover'][0]['url']
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url='http://test') as client:
            response = await client.get(url)
            assert response.status_code == 200 and response.headers['content-type'] == 'image/png'
            assert Image.open(io.BytesIO(response.content)).size == (12, 13)
            await client.delete('/api/heart-whispers/works/' + row['id'])
            assert (await client.get(url)).status_code == 404
        await service.cleanup_assets()
        assert not list(tools.material_root().rglob('*.png'))
    asyncio.run(scenario())


def test_memory_writeback_retries_independently_of_publication(archive, monkeypatch):
    from app.heart_works import memory
    from app.heart_works.service import HeartWorkService
    repo, _ = archive
    original = memory.write_event
    attempts, events = [], []
    async def flaky(*args, **kwargs):
        attempts.append(1)
        if len(attempts) == 1:
            raise RuntimeError('embedding temporarily unavailable')
        return await original(*args, **kwargs)
    async def broadcast(event):
        events.append(event)
    monkeypatch.setattr(memory, 'write_event', flaky)
    async def scenario():
        row = await enqueue(repo)
        service, _ = worker(repo, [work()])
        await service.run(row['id'])
        jobs = HeartWorkService(repo, broadcast=broadcast)
        await jobs.run_jobs()
        assert (await repo.get(row['id']))['status'] == 'published'
        assert len(events) == 1 and not await repo.read('SELECT * FROM memories', many=True)
        await repo.execute("UPDATE heart_work_jobs SET retry_at=0 WHERE kind='memory'")
        await jobs.recover()
        await jobs.recover()
        assert len(attempts) == 2 and len(events) == 1
        assert len(await repo.read('SELECT * FROM memories', many=True)) == 1
        assert all(job['status'] == 'done' for job in await repo.read('SELECT * FROM heart_work_jobs', many=True))
    asyncio.run(scenario())
