import asyncio
import json

import aiosqlite
from fastapi import FastAPI
import httpx
import pytest

from app.stickers.attachments import InvalidStickerAttachment
from app.stickers.repository import init_tables
from app.tools.schemas import ToolContext, ToolIntent
from routes import stickers as sticker_routes
from test_stickers import env, picture_bytes


def test_night_sticker_stays_private_until_sent_and_explicitly_collected(env, monkeypatch):
    app = FastAPI()
    app.include_router(sticker_routes.router)
    monkeypatch.setattr(sticker_routes, 'sticker_service', env.service)
    monkeypatch.setattr('app.stickers.service.configured', lambda _slot: False)
    raw = picture_bytes(animated=True)

    async def draw(_expression):
        return raw

    monkeypatch.setattr('app.stickers.image_provider.generate_sticker', draw)

    async def run():
        created = await env.service.execute(
            ToolIntent(id='night', tool_name='sticker.create', raw_text='【做表情：一只挥爪子的猫】',
                       arguments={'expression': '一只挥爪子的猫'}),
            ToolContext(conv_id='chat', msg_id='night', metadata={'round_kind': 'night'}))
        assert created['status'] == 'ready'
        sid = created['id']
        assert [row['id'] for row in await env.repo.library()] == [sid]
        with pytest.raises(InvalidStickerAttachment, match='先收藏'):
            await env.service.resolve_outgoing([{'type': 'sticker', 'id': sid}])
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
            assert (await client.get('/api/stickers')).json() == {'stickers': []}
            assert (await client.post('/api/stickers/collect', json={'message_id': sid})).status_code == 400
            async with env.db() as db:
                for mid, role, attachments in (
                    ('sent', 'assistant', [created]),
                    ('photo', 'user', [created['url']]),
                    ('internal', 'system', [created]),
                ):
                    await db.execute('INSERT INTO messages VALUES (?,?,?)', (mid, role, json.dumps(attachments)))
                await db.commit()
            # A received sticker stays in chat, outside the owner's collection.
            assert (await client.get('/api/stickers')).json() == {'stickers': []}
            for mid in ('photo', 'internal', 'missing'):
                assert (await client.post('/api/stickers/collect', json={'message_id': mid})).status_code == 400
            for _ in range(2):
                response = await client.post('/api/stickers/collect', json={'message_id': 'sent'})
                assert response.status_code == 200
                item = response.json()['sticker']
                assert item['id'] == sid
            assert (await client.get('/api/stickers')).json()['stickers'] == [item]
            assert await env.service.resolve_outgoing([{'type': 'sticker', 'id': sid}]) == [item]
        stored = await env.repo.get(sid)
        assert stored['ai_saved'] == stored['user_saved'] == 1
        assert stored['creation_prompt'] == '一只挥爪子的猫'
        assert (env.uploads / item['url'].rsplit('/', 1)[1]).read_bytes() == raw
        assert len(env.calls) == 1  # Collection reuses the image and its summary.
    asyncio.run(run())


def test_adding_same_image_merges_membership_without_gifting_owner_imports_to_core(env, monkeypatch):
    monkeypatch.setattr('app.stickers.service.configured', lambda _slot: False)

    async def run():
        raw = picture_bytes()
        owner = await env.service.add_to_panel(raw)
        await env.service.summarize(await env.repo.get(owner['id']))
        assert await env.service.match('想发一只猫') is None
        assert [call[0] for call in env.calls] == ['sticker_summary']
        stolen = await env.service.store_bytes(raw, source_message_id='owner-message')
        assert stolen['user_saved'] == stolen['ai_saved'] == 1
        assert len(await env.repo.library()) == len(await env.repo.panel_library()) == 1

        other_raw = picture_bytes(animated=True)
        private = await env.service.store_bytes(other_raw, creation_prompt='夜间创作')
        assert private['user_saved'] == 0
        imported = await env.service.add_to_panel(other_raw)
        assert imported['id'] == private['id']
        again = await env.repo.get(imported['id'])
        assert again['user_saved'] == again['ai_saved'] == 1
        assert again['creation_prompt'] == '夜间创作'
        assert len(list(env.uploads.glob('sticker_*'))) == 2
    asyncio.run(run())


def test_collection_migration_uses_origin_and_owner_sends_only_once(tmp_path):
    async def run():
        async with aiosqlite.connect(tmp_path / 'legacy.db') as db:
            await db.execute('''CREATE TABLE stickers (id TEXT PRIMARY KEY, file_hash TEXT UNIQUE,
                url TEXT, mime_type TEXT, summary_json TEXT, source_message_id TEXT, creation_prompt TEXT,
                status TEXT, error TEXT, created_at REAL, updated_at REAL, ai_saved INTEGER)''')
            await db.execute('CREATE TABLE messages (id TEXT, role TEXT, attachments TEXT)')
            cases = [('imported', 0, '', ''), ('stolen', 1, 'u1', ''), ('private', 1, '', 'night'),
                     ('shown', 1, '', 'night'), ('used', 1, '', 'night'), ('photo', 1, '', 'night')]
            for sid, ai_saved, source, prompt in cases:
                await db.execute('INSERT INTO stickers VALUES (?,?,?,?,?,?,?,?,?,?,?,?)',
                    (sid, sid, f'/uploads/{sid}.png', 'image/png', '{}', source, prompt, 'ready', '', 1, 1, ai_saved))
            for mid, role, payload in (
                ('a1', 'assistant', [{'type': 'sticker', 'id': 'shown'}]),
                ('u2', 'user', [{'type': 'sticker', 'id': 'used'}]),
                ('u3', 'user', [{'type': 'image/png', 'id': 'photo'}]),
            ):
                await db.execute('INSERT INTO messages VALUES (?,?,?)', (mid, role, json.dumps(payload)))
            await db.execute("INSERT INTO messages VALUES ('broken','user','not json sticker')")
            await init_tables(db)
            rows = await (await db.execute('SELECT id,ai_saved,user_saved FROM stickers')).fetchall()
            assert {sid for sid, _, owned in rows if owned} == {'imported', 'stolen', 'used'}
            assert {sid for sid, owned, _ in rows if owned} == {c[0] for c in cases} - {'imported'}
            # A later owner send must never make init_tables rerun historical inference.
            await db.execute('INSERT INTO messages VALUES (?,?,?)',
                             ('later', 'user', json.dumps([{'type': 'sticker', 'id': 'private'}])))
            await init_tables(db)
            assert await (await db.execute('SELECT id,ai_saved,user_saved FROM stickers')).fetchall() == rows
    asyncio.run(run())
