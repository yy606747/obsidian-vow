from contextlib import asynccontextmanager
from datetime import datetime
from zoneinfo import ZoneInfo
import asyncio
import json

import aiosqlite
import pytest

from app.daily_chat.schema import init_daily_chat_tables
from app.daily_chat.service import (
    DailyChatService, build_cross_day_background, calendar_day, inherited_messages_in_tx,
)


def timestamp(text):
    return datetime.fromisoformat(text).replace(tzinfo=ZoneInfo('Asia/Shanghai')).timestamp()


class Fixture:
    def __init__(self, path):
        self.path = path
        self.now = timestamp('2026-09-07T23:59:30')
        self.events = []
        self.service = DailyChatService(self.db, now=lambda: self.now,
                                       timezone_loader=lambda: 'Asia/Shanghai', broadcast=self.broadcast)

    @asynccontextmanager
    async def db(self):
        async with aiosqlite.connect(self.path) as db:
            db.row_factory = aiosqlite.Row
            yield db

    async def broadcast(self, event):
        self.events.append(event)

    async def initialize(self):
        async with self.db() as db:
            await db.executescript('''
                CREATE TABLE conversations(id TEXT PRIMARY KEY,title TEXT,model TEXT,created_at REAL,updated_at REAL);
                CREATE TABLE messages(id TEXT PRIMARY KEY,conv_id TEXT,role TEXT,content TEXT,created_at REAL,attachments TEXT);
                CREATE TABLE control_sessions(session_id TEXT,conv_id TEXT,status TEXT);
                CREATE TABLE self_wakes(id TEXT,conv_id TEXT,origin_ref TEXT,state TEXT);
                CREATE TABLE web_search_pending(id TEXT,conv_id TEXT,status TEXT);
                CREATE TABLE memory_pending_recalls(id TEXT,conv_id TEXT,status TEXT);
            ''')
            await init_daily_chat_tables(db)
            await db.execute('INSERT INTO conversations VALUES(?,?,?,?,?)', ('old', '上一窗', 'chosen-model', self.now-100, self.now))
            await db.commit()
        for index in range(40):
            await self.message(f'm{index:02}', 'old', at=self.now-40+index,
                               role='user' if index % 2 == 0 else 'assistant')
        return self

    async def message(self, id, conv, *, at=None, role='assistant'):
        async with self.db() as db:
            await db.execute('INSERT INTO messages VALUES(?,?,?,?,?,?)',
                             (id, conv, role, f'原话 {id}', self.now if at is None else at, '[]'))
            await db.commit()

    async def inherited(self, conv):
        async with self.db() as db:
            return await inherited_messages_in_tx(db, conv)

    async def cross_midnight(self):
        await self.service.ensure_current()
        self.now = timestamp('2026-09-08T00:00:00')
        return await self.service.ensure_current()


def run_case(tmp_path, case):
    async def run():
        fixture = await Fixture(tmp_path/'chat.db').initialize()
        await case(fixture)
    asyncio.run(run())


def test_adopts_existing_window_then_creates_exactly_one_at_midnight(tmp_path):
    async def case(f):
        initial = await f.service.ensure_current()
        assert initial['active_conv_id'] == 'old'
        assert initial['revision'] == 0
        f.now = timestamp('2026-09-08T00:00:00')
        states = await asyncio.gather(*(f.service.ensure_current() for _ in range(8)))
        assert len({s['active_conv_id'] for s in states}) == 1
        assert len(f.events) == 1
        state = states[0]
        assert state['day_key'] == '2026-09-08'
        assert f.events[0]['data']['conversation']['model'] == 'chosen-model'
        assert state['redirects']['old'] == state['active_conv_id']
    run_case(tmp_path, case)


def test_look_at_me_midnight_moves_only_window_not_event_or_deadline(tmp_path):
    async def case(f):
        from app.look_at_me.repository import init_tables, LookAtMeRepository
        from app.look_at_me.prompts import fact
        repo=LookAtMeRepository(f.db,lambda:f.now)
        async with f.db() as db:
            await init_tables(db)
            await db.commit()
        r=await repo.create(conv_id='old',origin_conv_id='old',model_key='m',device_id='phone',
            source_turn='turn',source_intent='intent',expression='跨过午夜一起聊',duration_seconds=300)
        async with f.db() as db:
            await db.execute("UPDATE look_at_me_requests SET status='active',decision='accepted',started_at=?,expected_end_at=?,core_state='done',response_day='2026-09-07' WHERE id=?",(f.now,f.now+300,r['id']))
            await db.commit()
        deadline=f.now+300
        target=(await f.cross_midnight())['active_conv_id']
        moved=await repo.get(r['id'])
        assert moved['conv_id']==target and moved['origin_conv_id']=='old'
        assert moved['expected_end_at']==deadline and moved['core_state']=='done'
        assert moved['response_day']=='2026-09-07'
        assert '约好的腻歪时间' in fact(moved,user_name='青禾',ai_name='临川',now=f.now,timezone='Asia/Shanghai')
    run_case(tmp_path,case)


def test_fixed_background_is_independent_of_today_messages_and_setting_changes(tmp_path):
    async def case(f):
        state = await f.cross_midnight()
        target = state['active_conv_id']
        original = await f.inherited(target)
        assert [m['id'] for m in original] == [f'm{i:02}' for i in range(10, 40)]
        await f.service.configure(inherit_count=5)
        for index in range(45):
            await f.message(f'today{index:02}', target, at=f.now+index+1)
        assert await f.inherited(target) == original
        block = build_cross_day_background(original, user_name='青禾', ai_name='临川', timezone='Asia/Shanghai')
        assert block.count('原话 ') == 30
        assert '青禾' in block and '临川' in block
        assert '[2026-09-07 23:' in block
    run_case(tmp_path, case)


def test_rebuilding_a_deleted_daily_window_preserves_the_previous_real_chat(tmp_path):
    async def case(f):
        first = (await f.cross_midnight())['active_conv_id']
        async with f.db() as db:
            await db.execute('DELETE FROM conversations WHERE id=?', (first,))
            await db.execute('INSERT INTO conversations VALUES(?,?,?,?,?)',
                             ('background-only', '后台窗口', 'gemini-3-flash', f.now+10, f.now+20))
            await db.commit()
        # Restart too: retaining the model must not depend on a browser or cache.
        restarted = DailyChatService(f.db, now=lambda: f.now, timezone_loader=lambda: 'Asia/Shanghai', broadcast=f.broadcast)
        state = await restarted.ensure_current()
        target = state['active_conv_id']
        assert target != first
        assert f.events[-1]['data']['conversation']['model'] == 'chosen-model'
        assert len(await f.inherited(target)) == 30
        assert state['redirects'][first] == target
        async with f.db() as db:
            row = await (await db.execute('SELECT previous_conv_id FROM daily_chat_windows WHERE conv_id=?', (target,))).fetchone()
            assert row[0] == 'old'
    run_case(tmp_path, case)


@pytest.mark.parametrize('after_midnight', [False, True])
def test_model_selection_at_midnight_follows_the_live_window(tmp_path, monkeypatch, after_midnight):
    from app.chat import crud_routes
    from app.chat.models import ConvUpdate
    async def case(f):
        monkeypatch.setattr(crud_routes, 'get_db', f.db)
        monkeypatch.setattr(crud_routes, 'daily_chat', f.service)
        monkeypatch.setattr(crud_routes.manager, 'broadcast', f.broadcast)
        async def export(_):
            pass
        monkeypatch.setattr(crud_routes, 'export_conversation', export)
        await f.service.ensure_current()
        if after_midnight:
            await f.cross_midnight()
        result = await crud_routes.update_conversation('old', ConvUpdate(model='selected-non-default-model'))
        state = await f.cross_midnight()
        target = state['active_conv_id']
        async with f.db() as db:
            row = await (await db.execute('SELECT model FROM conversations WHERE id=?', (target,))).fetchone()
            assert row[0] == 'selected-non-default-model'
        if after_midnight:
            assert result['conv_id'] == target
            event = next(e for e in reversed(f.events) if e['type'] == 'conv_updated')
            assert event['data'] == {'id': target, 'model': 'selected-non-default-model'}
    run_case(tmp_path, case)


def test_reply_started_in_old_window_commits_once_in_new_window(tmp_path):
    async def case(f):
        state = await f.cross_midnight()
        await f.message('unfinished-reply', 'old')
        async with f.db() as db:
            cursor = await db.execute("SELECT conv_id,content FROM messages WHERE id='unfinished-reply'")
            row = await cursor.fetchone()
            assert row['conv_id'] == state['active_conv_id']
            cursor = await db.execute("SELECT origin_conv_id FROM daily_chat_deliveries WHERE message_id='unfinished-reply'")
            assert (await cursor.fetchone())[0] == 'old'
            cursor = await db.execute('SELECT COUNT(*) FROM messages')
            assert (await cursor.fetchone())[0] == 41
        assert all(m['id'] != 'unfinished-reply' for m in await f.inherited(state['active_conv_id']))
    run_case(tmp_path, case)


def test_next_day_flattens_references_without_copying_messages(tmp_path):
    async def case(f):
        first = await f.cross_midnight()
        await f.message('day-two', first['active_conv_id'], at=f.now+1)
        f.now = timestamp('2026-09-09T00:00:00')
        second = await f.service.ensure_current()
        inherited = await f.inherited(second['active_conv_id'])
        assert len(inherited) == len({m['id'] for m in inherited}) == 30
        assert inherited[-1]['id'] == 'day-two'
        assert inherited[0]['created_at'] < timestamp('2026-09-08T00:00:00')
        await f.message('late-old-tool-result', 'old')
        assert await f.service.message_window('late-old-tool-result', 'old') == second['active_conv_id']
        assert f.service.resolve(first['active_conv_id']) == second['active_conv_id']
    run_case(tmp_path, case)


def test_restart_catches_up_to_today_without_duplicate_empty_days(tmp_path):
    async def case(f):
        await f.service.ensure_current()
        f.now = timestamp('2026-09-12T08:00:00')
        restarted = DailyChatService(f.db, now=lambda: f.now, timezone_loader=lambda: 'Asia/Shanghai', broadcast=f.broadcast)
        state = await restarted.ensure_current()
        assert state['day_key'] == '2026-09-12'
        async with f.db() as db:
            cursor = await db.execute('SELECT COUNT(*) FROM daily_chat_windows')
            assert (await cursor.fetchone())[0] == 1
        assert len(await f.inherited(state['active_conv_id'])) == 30
    run_case(tmp_path, case)


def test_live_control_and_pending_results_follow_without_changing_source_ids(tmp_path):
    async def case(f):
        async with f.db() as db:
            await db.executescript('''
                INSERT INTO control_sessions VALUES('session-1','old','active');
                INSERT INTO control_sessions VALUES('past-session','old','ended');
                INSERT INTO self_wakes VALUES('wake-1','old','old','pending');
                INSERT INTO web_search_pending VALUES('search-1','old','ready');
                INSERT INTO memory_pending_recalls VALUES('recall-1','old','queued');
            ''')
            await db.commit()
        target = (await f.cross_midnight())['active_conv_id']
        async with f.db() as db:
            for table in ('control_sessions', 'self_wakes', 'web_search_pending', 'memory_pending_recalls'):
                cursor = await db.execute(f'SELECT conv_id FROM {table} LIMIT 1')
                assert (await cursor.fetchone())[0] == target
            cursor = await db.execute("SELECT origin_ref FROM self_wakes WHERE id='wake-1'")
            assert (await cursor.fetchone())[0] == 'old'
            cursor = await db.execute("SELECT conv_id FROM control_sessions WHERE session_id='past-session'")
            assert (await cursor.fetchone())[0] == 'old'
    run_case(tmp_path, case)


def test_deleted_or_edited_source_is_not_frozen_as_a_second_message(tmp_path):
    async def case(f):
        target = (await f.cross_midnight())['active_conv_id']
        async with f.db() as db:
            await db.execute("DELETE FROM messages WHERE id='m39'")
            await db.execute("UPDATE messages SET content='改过的原话' WHERE id='m38'")
            await db.commit()
        inherited = await f.inherited(target)
        assert len(inherited) == 29
        assert inherited[-1]['content'] == '改过的原话'
    run_case(tmp_path, case)


def test_inherit_count_seed_is_used_once_and_disabled_rollover_stays_put(tmp_path):
    async def case(f):
        state = await f.service.configure(initial_count=18)
        assert state['inherit_count'] == 18
        assert (await f.service.configure(initial_count=70))['inherit_count'] == 18
        await f.service.configure(enabled=False)
        f.now = timestamp('2026-09-08T00:00:00')
        assert (await f.service.ensure_current())['active_conv_id'] == 'old'
    run_case(tmp_path, case)


def test_calendar_midnight_respects_timezone_and_dst():
    zone = ZoneInfo('America/Los_Angeles')
    now = datetime(2026, 3, 8, 0, 0, tzinfo=zone).timestamp()
    day, next_at = calendar_day(now, 'America/Los_Angeles')
    assert day == '2026-03-08'
    assert next_at-now == 23*3600
    assert calendar_day(timestamp('2026-09-08T00:00:00'), 'Asia/Shanghai')[0] == '2026-09-08'


def test_inherited_messages_only_enter_prompt_and_never_the_http_page(tmp_path, monkeypatch):
    from app.chat import history, crud_routes
    from app.daily_chat import service
    async def case(f):
        target = (await f.cross_midnight())['active_conv_id']
        monkeypatch.setattr(service, 'daily_chat', f.service)
        monkeypatch.setattr(history, 'get_db', f.db)
        monkeypatch.setattr(crud_routes, 'get_db', f.db)
        monkeypatch.setattr(history, 'load_worldbook', lambda: {'user_name': '青禾', 'ai_name': '临川'})
        assert await crud_routes.list_messages(target, limit=50, before=None) == []
        empty_ctx = await history.prepare_chat_history(target, context_limit=10, attachment_policy='last_user')
        assert len(empty_ctx.cross_day_message_ids) == 30
        for i in range(12):
            await f.message(f'today{i:02}', target, at=f.now+i+1, role='user' if i % 2 == 0 else 'assistant')
        for limit in (5, 10):
            ctx = await history.prepare_chat_history(target, context_limit=limit, attachment_policy='last_user')
            assert len(ctx.cross_day_message_ids) == 30
            assert len(ctx.visible_message_ids) == 30 + limit
            block = next(m['content'] for m in ctx.history if '[跨日背景' in m['content'])
            assert block.count('原话 m') == 30 and '原话 today' not in block
            page = await crud_routes.list_messages(target, limit=limit, before=None)
            assert len(page) == limit
            assert all(m['conv_id'] == target and m['id'].startswith('today') for m in page)
            assert not any(m.get('inherited') or 'origin_conv_id' in m for m in page)
        old_page = await crud_routes.list_messages('old', limit=50, before=None)
        assert len(old_page) == 40
        async with f.db() as db:
            assert (await (await db.execute('SELECT COUNT(*) FROM messages')).fetchone())[0] == 52
    run_case(tmp_path, case)


def test_actual_provider_stream_spans_midnight_without_restart(tmp_path, monkeypatch):
    import database
    from app.chat import streaming
    from app.daily_chat import service
    from app.background_tasks import begin_task_lifecycle
    monkeypatch.setattr(database, 'DB_PATH', tmp_path/'actual-stream.db')
    calls = []
    broadcasts = []
    async def noop(*args, **kwargs):
        return None
    async def broadcast(event, **kwargs):
        broadcasts.append(event)
    monkeypatch.setattr(streaming.manager, 'broadcast', broadcast)
    monkeypatch.setattr(streaming, 'export_conversation', noop)
    monkeypatch.setattr(streaming, '_schedule_working_model_pipeline_after_commit', lambda **kwargs: None)
    monkeypatch.setattr(streaming, '_schedule_chunk_index_update', lambda *args, **kwargs: {})
    monkeypatch.setattr(streaming, '_maybe_auto_digest', noop)
    async def case():
        await database.init_db()
        begin_task_lifecycle()
        now = [timestamp('2026-09-07T23:59:59')]
        async with database.get_db() as db:
            await db.execute("INSERT INTO conversations(id,title,model,created_at,updated_at) VALUES ('old','昨天','test',1,1)")
            await db.execute("INSERT INTO messages(id,conv_id,role,content,created_at) VALUES ('user-last','old','user','还在聊',?)", (now[0],))
            await db.commit()
        daily = DailyChatService(now=lambda: now[0], timezone_loader=lambda: 'Asia/Shanghai', broadcast=broadcast)
        monkeypatch.setattr(service, 'daily_chat', daily)
        await daily.ensure_current()
        async def provider(*args, **kwargs):
            calls.append('provider')
            yield '前半句，'
            now[0] += 1
            await daily.ensure_current()
            yield '接着说完后半句。'
        monkeypatch.setattr(streaming, 'stream_ai', provider)
        history = [{'role': 'user', 'content': '还在聊'}]
        response = await streaming.stream_chat_response(
            conv_id='old', model_key='test', history=history, temperature=None, memory_eval_mode=True,
            prompt_meta={'prompt_source': 'send', 'current_user_message_id': 'user-last',
                         'recalled_memories': [], 'debug_top6': [], 'prompt_messages': history, 'prompt_count': 1},
        )
        events = [json.loads(raw[6:].strip()) async for raw in response.body_iterator]
        async with database.get_db() as db:
            rows = await (await db.execute("SELECT id,conv_id,content FROM messages WHERE role='assistant'")).fetchall()
        assert len(rows) == 1
        assert rows[0][1] == daily.state['active_conv_id']
        assert rows[0][2] == '前半句，接着说完后半句。'
        assert events[0]['id'] == rows[0][0]
        assert ''.join(e.get('content', '') for e in events if e['type'] == 'chunk') == rows[0][2]
        assert len([e for e in events if e['type'] == 'start']) == 1
        created = next(e['data'] for e in broadcasts if e['type'] == 'msg_created')
        assert created['conv_id'] == rows[0][1]
    asyncio.run(asyncio.wait_for(case(), timeout=20))
    assert calls == ['provider']


def test_tool_batch_spanning_midnight_keeps_ids_and_executes_each_once(tmp_path, monkeypatch):
    from app.daily_chat import service
    from app.tools.service import ToolService
    from app.tools.schemas import ToolContext, ToolIntent, ToolStatus
    async def case(f):
        await f.service.ensure_current()
        monkeypatch.setattr(service, 'daily_chat', f.service)
        calls = []
        async def adapter(intent, context):
            calls.append((intent.id, context.conv_id, context.request_id))
            if intent.id == 'first':
                await f.cross_midnight()
            return {'ok': True}
        tools = ToolService(adapters={'memory.remember': adapter})
        results = await tools.execute_async(
            [ToolIntent(id=id, tool_name='memory.remember', raw_text='') for id in ('first', 'second')],
            context=ToolContext(conv_id='old', request_id='same-turn', capabilities=('memory.remember',)),
        )
        assert all(result.status is ToolStatus.EXECUTED for result in results)
        assert calls == [('first', 'old', 'same-turn'), ('second', f.service.state['active_conv_id'], 'same-turn')]
    run_case(tmp_path, case)


def test_cross_day_media_keeps_voice_text_and_shares_existing_image_budget(tmp_path, monkeypatch):
    import config
    from app.chat import image_history
    from app.daily_chat.service import build_cross_day_context
    monkeypatch.setattr(config, 'UPLOADS_DIR', tmp_path)
    monkeypatch.setattr(image_history, 'image_history_config', lambda: {
        'image_history_user_turns': 4, 'image_history_max_images': 2, 'image_history_max_bytes': 100,
    })
    (tmp_path/'old.png').write_bytes(b'original-image')
    records = [{'id': 'source', 'role': 'user', 'content': '昨天的照片和语音', 'created_at': 1,
                'attachments': json.dumps([{'url': '/uploads/old.png', 'type': 'image/png'},
                                           {'type': 'voice', 'mime_type': 'audio/wav', 'transcript': '明天继续这个话题'}])}]
    first = build_cross_day_context(records, user_name='青禾', ai_name='临川', timezone='Asia/Shanghai',
                                    today_user_ids=['today'], image_budget={'retained_images': 1, 'retained_bytes': 10})
    assert len(first[0]['attachments']) == 1
    assert '明天继续这个话题' in first[0]['content']
    exhausted = build_cross_day_context(records, user_name='青禾', ai_name='临川', timezone='Asia/Shanghai',
                                        today_user_ids=['today'], image_budget={'retained_images': 2, 'retained_bytes': 20})
    assert not exhausted[0]['attachments']
    assert '本轮未加载' in exhausted[0]['content']
    assert records[0]['content'] == '昨天的照片和语音'
