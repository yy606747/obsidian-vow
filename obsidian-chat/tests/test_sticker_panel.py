import asyncio
import base64
import hashlib
import io
import json
from contextlib import asynccontextmanager

import aiosqlite
from fastapi import FastAPI
from fastapi.responses import StreamingResponse
import httpx
import pytest
from PIL import Image

from app.chat import history as history_mod, image_history
from app.stickers.repository import init_tables
from app.tools.schemas import ToolContext, ToolIntent
from routes import chat as chat_routes, stickers as sticker_routes
from test_stickers import env, picture_bytes


def test_picker_import_send_history_and_steal_are_distinct(env, monkeypatch):
    app = FastAPI()
    app.include_router(sticker_routes.router)
    app.include_router(chat_routes.router)
    monkeypatch.setattr(sticker_routes, 'sticker_service', env.service)
    monkeypatch.setattr('app.stickers.service.sticker_service', env.service)
    monkeypatch.setattr('app.stickers.service.configured', lambda _slot: False)
    monkeypatch.setattr(chat_routes, 'get_db', env.db)
    monkeypatch.setattr(history_mod, 'get_db', env.db)
    monkeypatch.setattr(history_mod, 'load_worldbook', lambda: {'user_name': '小满', 'ai_name': '知秋'})
    monkeypatch.setattr(image_history, 'UPLOADS_DIR', env.uploads)
    captured, tasks, broadcasts = [], [], []

    def track(coro, **kwargs):
        task = asyncio.create_task(coro)
        tasks.append(task)
        return task

    monkeypatch.setattr('app.stickers.service.create_tracked_task', track)

    async def prepare(conv_id, body, **kwargs):
        context = await history_mod.prepare_chat_history(conv_id, context_limit=30, attachment_policy='last_message')
        captured.append((body, context, kwargs['current_user_message_id']))
        return context.model_key, context.history, {}

    async def stream(**kwargs):
        return StreamingResponse(iter(['data: {"type":"done"}\n\n']), media_type='text/event-stream')

    async def broadcast(event):
        broadcasts.append(event)

    monkeypatch.setattr(chat_routes, 'prepare_send_prompt', prepare)
    monkeypatch.setattr(chat_routes, 'stream_chat_response', stream)
    monkeypatch.setattr(chat_routes.manager, 'broadcast', broadcast)

    async def run():
        async with env.db() as db:
            await db.execute('ALTER TABLE messages ADD COLUMN conv_id TEXT')
            await db.execute("ALTER TABLE messages ADD COLUMN content TEXT DEFAULT ''")
            await db.execute('ALTER TABLE messages ADD COLUMN created_at REAL')
            await db.execute('CREATE TABLE conversations (id TEXT, title TEXT, model TEXT, updated_at REAL)')
            await db.execute("INSERT INTO conversations VALUES ('chat','表情','vision',0)")
            await db.commit()
        raw = picture_bytes(animated=True)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
            response = await client.post('/api/stickers', files={'file': ('cat.gif', raw, 'image/gif')})
            assert response.status_code == 200
            item = response.json()['sticker']
            assert item['type'] == 'sticker' and item['mime_type'] == 'image/gif'
            assert (env.uploads / item['url'].rsplit('/', 1)[1]).read_bytes() == raw
            duplicate = await client.post('/api/stickers', files={'file': ('again.gif', raw, 'image/gif')})
            assert duplicate.json()['sticker']['id'] == item['id']
            assert len((await client.get('/api/stickers')).json()['stickers']) == 1
            assert not await env.repo.library()  # Importing is not Core choosing to steal it.

            # Canonical metadata comes from the library even if a client submits a forged URL/summary.
            response = await client.post('/api/conversations/chat/send', json={
                'content': '', 'memory_eval_mode': True,
                'attachments': [{**item, 'url': '/uploads/other.png', 'summary': {'visual': 'forged'}}],
            })
            assert response.status_code == 200
            body, context, source_id = captured[0]
            assert body.attachments == [item]
            assert broadcasts[0]['data']['attachments'] == [item]
            assert '小满向知秋发送的聊天表情' in context.history[-1]['content']
            assert context.history[-1]['attachments'] == [item]
            async with env.db() as db:
                saved = await (await db.execute('SELECT attachments FROM messages WHERE id=?', (source_id,))).fetchone()
            assert json.loads(saved[0]) == [item]
            assert not await env.repo.library()

            result = await env.service.execute(ToolIntent(id='save', tool_name='sticker.save', raw_text='【偷图】'),
                ToolContext(conv_id='chat', msg_id='a1', metadata={'current_user_message_id': source_id}))
            assert result['status'] == 'running'
            await asyncio.gather(*tasks)
            library = await env.repo.library()
            assert len(library) == 1 and library[0]['id'] == item['id']
            assert library[0]['source_message_id'] == source_id
            again = await client.post('/api/stickers', files={'file': ('again.gif', raw, 'image/gif')})
            assert again.status_code == 200 and len(await env.repo.library()) == 1
            assert len(env.calls) == 1

            # The exact same image sent as a normal photo stays a photo and cannot be stolen.
            response = await client.post('/api/conversations/chat/send', json={
                'content': '这是照片', 'attachments': [item['url']], 'memory_eval_mode': True,
            })
            assert response.status_code == 200
            _, normal_context, photo_id = captured[-1]
            assert '普通图片' in normal_context.history[-1]['content']
            assert '[已发表情]' not in normal_context.history[-1]['content']
            result = await env.service.execute(ToolIntent(id='save', tool_name='sticker.save', raw_text='【偷图】'),
                ToolContext(conv_id='chat', msg_id='a2', metadata={'current_user_message_id': photo_id}))
            assert result['status'] == 'failed'
            async with env.db() as db:
                error = await (await db.execute('SELECT error FROM sticker_events WHERE id=?', (result['event_id'],))).fetchone()
            assert '普通图片' in error[0]
    asyncio.run(run())


def test_sticker_preflight_rejects_missing_files_ids_and_multiple_before_writes(env, monkeypatch):
    monkeypatch.setattr('app.stickers.service.sticker_service', env.service)

    @asynccontextmanager
    async def read_only_db():
        # The companionship gate may read before sticker validation; writes remain forbidden.
        async with env.db() as db:
            await db.execute('PRAGMA query_only=ON')
            yield db

    monkeypatch.setattr(chat_routes, 'get_db', read_only_db)
    from app.chat.models import MsgCreate

    async def run():
        row = await env.service.store_bytes(picture_bytes(), ai_saved=False)
        # Even a valid original cannot be sent from a stale/shared picker before collecting it.
        assert not row['user_saved']
        result = await chat_routes.send_message('chat', MsgCreate(content='', attachments=[{'type': 'sticker', 'id': row['id']}]))
        assert result.status_code == 422
        assert '先收藏' in json.loads(result.body)['error']
        (env.uploads / row['url'].rsplit('/', 1)[1]).unlink()
        for attachments in ([{'type': 'sticker', 'id': 'missing'}],
                            [{'type': 'sticker', 'id': row['id']}],
                            [{'type': 'sticker', 'id': row['id']}] * 2,
                            [{'type': 'sticker', 'url': '/uploads/photo.png'}]):
            result = await chat_routes.send_message('chat', MsgCreate(content='', attachments=attachments))
            assert result.status_code == 422
            assert json.loads(result.body)['code'] == 'invalid_sticker_attachment'
    asyncio.run(run())


def test_import_validates_bytes_and_missing_assets_do_not_appear(env, monkeypatch):
    monkeypatch.setattr(sticker_routes, 'sticker_service', env.service)
    monkeypatch.setattr('app.stickers.service.configured', lambda _slot: False)
    app = FastAPI()
    app.include_router(sticker_routes.router)

    async def run():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
            for data in (b'', b'not a png'):
                result = await client.post('/api/stickers', files={'file': ('bad.png', data, 'image/png')})
                assert result.status_code == 400
            assert not await env.repo.panel_library()
            item = await env.service.add_to_panel(picture_bytes())
            (env.uploads / item['url'].rsplit('/', 1)[1]).unlink()
            assert (await client.get('/api/stickers')).json() == {'stickers': []}
    asyncio.run(run())


def test_summary_failure_does_not_remove_human_sticker_or_make_it_matchable(env, monkeypatch):
    tasks = []

    def track(coro, **kwargs):
        task = asyncio.create_task(coro)
        tasks.append(task)
        return task

    async def broken(*args, **kwargs):
        raise RuntimeError('provider failed')

    env.service.caller = broken
    monkeypatch.setattr('app.stickers.service.configured', lambda _slot: True)
    monkeypatch.setattr('app.stickers.service.create_tracked_task', track)

    async def run():
        item = await env.service.add_to_panel(picture_bytes())
        await asyncio.gather(*tasks)
        assert (await env.repo.get(item['id']))['status'] == 'failed'
        assert len(await env.service.panel_library()) == 1
        assert await env.service.resolve_outgoing([{'type': 'sticker', 'id': item['id']}]) == [item]
        assert await env.service.match('anything') is None
    asyncio.run(run())


def test_existing_library_migration_preserves_core_collection(tmp_path):
    async def run():
        async with aiosqlite.connect(tmp_path / 'old.db') as db:
            await db.execute('''CREATE TABLE stickers (id TEXT PRIMARY KEY, file_hash TEXT UNIQUE,
                url TEXT, mime_type TEXT, summary_json TEXT, source_message_id TEXT, creation_prompt TEXT,
                status TEXT, error TEXT, created_at REAL, updated_at REAL)''')
            await db.execute("INSERT INTO stickers VALUES ('old','hash','/uploads/a.gif','image/gif','{}','','','ready','',1,1)")
            await init_tables(db)
            await init_tables(db)
            row = await (await db.execute('SELECT id,status,ai_saved FROM stickers')).fetchone()
            assert row == ('old', 'ready', 1)
    asyncio.run(run())


def test_image_memory_processes_only_ordinary_photos_in_a_mixed_message(env, monkeypatch):
    from app.image_memory import service as memory_service
    monkeypatch.setattr(memory_service, 'get_db', env.db)
    processed = []

    async def process(message_id, url):
        processed.append((message_id, url))

    monkeypatch.setattr(memory_service, 'process_image', process)

    async def run():
        async with env.db() as db:
            attachments = ['/uploads/photo.png', {'type': 'sticker', 'url': '/uploads/cat.gif'}]
            await db.execute('INSERT INTO messages VALUES (?,?,?)', ('mixed', 'user', json.dumps(attachments)))
            await db.commit()
        await memory_service.process_message('mixed')
        assert processed == [('mixed', '/uploads/photo.png')]
    asyncio.run(run())


@pytest.mark.parametrize('structured', [True, False])
def test_gif_model_input_uses_ordered_png_samples_and_keeps_original(tmp_path, monkeypatch, structured):
    import ai_providers
    raw = picture_bytes(animated=True)
    path = tmp_path / 'cat.gif'
    path.write_bytes(raw)
    monkeypatch.setattr(ai_providers, 'UPLOADS_DIR', tmp_path)
    item = {'type': 'sticker', 'id': 'cat', 'url': '/uploads/cat.gif', 'mime_type': 'image/gif',
            'expected_sha256': hashlib.sha256(raw).hexdigest()} if structured else '/uploads/cat.gif'
    history = [{'role': 'user', 'content': '动图表情', 'attachments': [item]}]
    native = ai_providers.build_gemini_contents(history)
    frames = [part['inline_data'] for part in native[0]['parts'] if 'inline_data' in part]
    assert len(frames) == 3 and all(frame['mime_type'] == 'image/png' for frame in frames)
    assert [Image.open(io.BytesIO(base64.b64decode(frame['data']))).getpixel((0, 0)) for frame in frames] == [
        (255, 255, 255), (255, 0, 0), (0, 0, 255)]
    compatible = ai_providers.build_multimodal_messages(history)
    urls = [part['image_url']['url'] for part in compatible[0]['content'] if part['type'] == 'image_url']
    assert urls == ['data:image/png;base64,' + frame['data'] for frame in frames]
    assert path.read_bytes() == raw
    if structured:
        item['expected_sha256'] = 'changed'
        with pytest.raises(ValueError, match='图片来源'):
            ai_providers.build_gemini_contents(history)
