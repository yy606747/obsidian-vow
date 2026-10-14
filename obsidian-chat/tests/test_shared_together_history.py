import asyncio

import pytest

from app.together.shared_history import select_shared_window
from app.together.context import bounded_history, resolve_entry
from test_together import harness, start, settle, Models


def rows(prefix, count, *, offset=0, **extra):
    return [dict(id=f'{prefix}{i}', role='user' if i % 2 == 0 else 'assistant',
                 content=f'{prefix}{i}', created_at=offset + i, **extra) for i in range(count)]


@pytest.mark.parametrize('count,expected_chat,expected_room', [(12, 18, 12), (40, 0, 30)])
def test_shared_window_counts_utterances_without_duplicating_entry(count, expected_chat, expected_room):
    chat = rows('chat', 30)
    room = rows('room', count, offset=30, together_session_id='session', conv_id='conv')
    event = dict(id='end', role='system', content='ended', created_at=100)
    result, old = select_shared_window(chat + [event], chat[-5:], room, conv_id='conv', limit=30)
    all_rows = old + result
    assert len([row for row in all_rows if row['id'].startswith('chat')]) == expected_chat
    assert len([row for row in all_rows if row['id'].startswith('room')]) == expected_room
    assert len({row['id'] for row in all_rows}) == len(all_rows)
    assert event in result


def test_new_chat_naturally_evicts_together_and_window_is_configurable():
    room = rows('room', 12, together_session_id='session', conv_id='conv')
    chat = rows('chat', 5, offset=20)
    result, _ = select_shared_window(chat, [], room, conv_id='conv', limit=7)
    assert [row['id'] for row in result] == ['room10', 'room11'] + [f'chat{i}' for i in range(5)]
    history = bounded_history(rows('entry', 20), rows('room', 12), max_messages=15)
    assert [row['content'] for row in history] == [f'entry{i}' for i in range(17, 20)] + [f'room{i}' for i in range(12)]


def test_ending_preserves_text_before_cleanup_and_next_room_borrows_it_once(tmp_path, monkeypatch):
    from app.chat import history as history_mod
    from app.daily_chat.service import daily_chat

    async def run():
        async with harness(tmp_path, models=Models('相伴新回复'), entry=rows('borrowed', 5)) as (svc, clock):
            await svc.repo.execute("ALTER TABLE messages ADD COLUMN attachments TEXT")
            await svc.repo.execute("ALTER TABLE conversations ADD COLUMN title TEXT")
            await svc.repo.execute("INSERT INTO conversations(id,model) VALUES ('conv1','vision-model')")
            for row in rows('chat', 20):
                await svc.repo.execute("INSERT INTO messages(id,conv_id,role,content,created_at) VALUES (?,'conv1',?,?,?)",
                                       (row['id'], row['role'], row['content'], row['created_at']))
            sid = (await start(svc))['id']
            await settle(svc)
            await svc.action(sid, device_id='android_phone', action='end')
            await svc.repo.execute("UPDATE together_sessions SET event_message_id='durable' WHERE id=?", (sid,))
            await svc.repo.cleanup_transcript(sid)
            assert await svc.repo.messages(sid) == []
            saved = await svc.repo.read('SELECT * FROM together_context_messages', many=True)
            assert [row['content'] for row in saved] == ['相伴新回复']
            clock.advance(1)
            await svc.repo.execute("INSERT INTO messages(id,conv_id,role,content,created_at) VALUES ('question','conv1','user','刚刚说的呢',?)", (clock(),))
            monkeypatch.setattr(history_mod, 'get_db', svc.repo.db)
            monkeypatch.setattr(history_mod, 'load_worldbook', lambda: {'user_name': '小栀', 'ai_name': '阿澈'})
            monkeypatch.setattr(daily_chat, 'state', {})
            context = await history_mod.prepare_chat_history('conv1', context_limit=6, attachment_policy='last_message')
            assert context.visible_message_ids == ['chat16', 'chat17', 'chat18', 'chat19', saved[0]['id'], 'question']
            assert context.latest_user_message_id == 'question'
            text = '\n'.join(row['content'] for row in context.history)
            assert text.count('相伴新回复') == 1 and '已经结束的相伴' in text
            assert 'borrowed' not in text
            _, _, entry = await resolve_entry('conv1', 6, db_factory=svc.repo.db)
            assert len(entry) == 6
            assert sum('相伴新回复' in row['content'] for row in entry) == 1
    asyncio.run(run())


def test_cross_day_room_messages_share_the_same_budget():
    old = rows('yesterday', 30, conv_id='yesterday')
    room = rows('room', 12, offset=30, together_session_id='session', conv_id='yesterday')
    current = rows('today', 1, offset=50)
    result, inherited = select_shared_window(current, old, room, conv_id='today', limit=30)
    assert len(result) == 1 and len(inherited) == 29
    assert [row['id'] for row in inherited][-12:] == [f'room{i}' for i in range(12)]
