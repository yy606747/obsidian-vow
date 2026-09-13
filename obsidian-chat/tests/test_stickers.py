import asyncio
import io
import json
from contextlib import asynccontextmanager
from types import SimpleNamespace

import aiosqlite
import pytest
from PIL import Image

from app.chat.control_syntax import ControlMarkerStreamFilter, canonicalize_control_markers
from app.chat.postprocess import PostProcessResult
from app.chat.action_executor import ActionExecution
from app.chat.turn_profiles import chat_turn_profile, opportunity_turn_profile
from app.stickers import prompts
from app.stickers.chat import deliver
from app.stickers.repository import StickerRepository, init_tables
from app.stickers.service import StickerService
from app.tools.parser import parse_tool_intents
from app.tools.schemas import ToolContext, ToolIntent, ToolResult, ToolStatus


SUMMARY = {'visual': '白猫探头', 'visible_text': '', 'intent': '偷偷看看动静', 'tone': '轻松', 'not_for': '拒绝'}


def picture_bytes(animated=False):
    output = io.BytesIO()
    first = Image.new('RGB', (18, 12), 'white')
    if animated:
        first.save(output, format='GIF', save_all=True,
                   append_images=[Image.new('RGB', (18, 12), color) for color in ('red', 'blue')],
                   duration=100, loop=0)
    else:
        first.save(output, format='PNG')
    return output.getvalue()


@pytest.fixture
def env(tmp_path):
    @asynccontextmanager
    async def db_factory():
        async with aiosqlite.connect(tmp_path / 'stickers.sqlite') as db:
            yield db

    async def setup():
        async with db_factory() as db:
            await init_tables(db)
            await db.execute('CREATE TABLE messages (id TEXT PRIMARY KEY,role TEXT,attachments TEXT)')
            await db.commit()

    asyncio.run(setup())
    calls = []

    async def caller(slot, messages, **kwargs):
        calls.append((slot, messages, kwargs))
        return json.dumps(SUMMARY) if slot == 'sticker_summary' else '{"id":null}'

    repo = StickerRepository(get_db_factory=db_factory, now=lambda: 100_000)
    service = StickerService(repository=repo, caller=caller, uploads=tmp_path / 'uploads')
    return SimpleNamespace(repo=repo, service=service, calls=calls, db=db_factory, uploads=service.uploads)


def test_collection_preserves_gif_and_summarizes_once(env):
    async def run():
        data = picture_bytes(animated=True)
        row = await env.service.store_bytes(data, source_message_id='u1')
        again = await env.service.store_bytes(data, source_message_id='u2')
        assert row['id'] == again['id']
        first, second = await asyncio.gather(env.service.summarize(row), env.service.summarize(again))
        assert first['status'] == second['status'] == 'ready'
        assert (env.uploads / row['url'].rsplit('/', 1)[1]).read_bytes() == data
        assert len(env.calls) == 1
        assert len(env.calls[0][1][1]['attachments']) == 3
        assert not list(env.uploads.glob('sticker_frame_*'))
    asyncio.run(run())


def test_match_uses_summaries_and_strict_candidate_enum(env):
    async def run():
        row = await env.service.store_bytes(picture_bytes())
        row = await env.service.summarize(row)

        async def matcher(slot, messages, **kwargs):
            assert slot == 'sticker_matcher'
            assert all('attachments' not in message for message in messages)
            body = json.loads(messages[1]['content'])
            assert body['request'] == '偷偷探头看看'
            assert body['library'][0]['visual'] == SUMMARY['visual']
            assert kwargs['response_schema']['properties']['id']['enum'] == [row['id'], None]
            return json.dumps({'id': row['id']})

        env.service.caller = matcher
        selected = await env.service.match('偷偷探头看看')
        assert selected['type'] == 'sticker' and selected['id'] == row['id']

        async def bad(*args, **kwargs):
            return '{"id":"invented"}'

        env.service.caller = bad
        with pytest.raises(ValueError, match='候选集合'):
            await env.service.match('anything')
    asyncio.run(run())


def test_null_is_a_wish_but_service_failure_is_not(env):
    async def run():
        row = await env.service.store_bytes(picture_bytes())
        await env.service.summarize(row)
        request = ToolIntent(id='i', tool_name='sticker.send', raw_text='【发图：猫猫装凶】', arguments={'expression': '猫猫装凶'})
        result = await env.service.execute(request, ToolContext(conv_id='c1', msg_id='a1'))
        assert result['status'] == 'miss'

        async def broken(*args, **kwargs):
            raise RuntimeError('private upstream response')

        env.service.caller = broken
        failed = await env.service.execute(request, ToolContext(conv_id='c1', msg_id='a2'))
        assert failed['status'] == 'failed'
        assert len(await env.repo.wishes()) == 1
        claim = await env.repo.claim(conv_id='c1', turn_id='t2', user_name='小满', ai_name='知秋')
        assert '没有找到合适表情' in claim['block']
        assert '发送服务未能完成请求' in claim['block']
        assert 'private upstream' not in claim['block']
    asyncio.run(run())


def test_save_binds_source_and_repeated_execution_is_idempotent(env, monkeypatch):
    tasks = []

    def track(coro, **kwargs):
        task = asyncio.create_task(coro)
        tasks.append(task)
        return task

    monkeypatch.setattr('app.stickers.service.create_tracked_task', track)

    async def run():
        env.uploads.mkdir()
        data = picture_bytes(animated=True)
        (env.uploads / 'current.gif').write_bytes(data)
        async with env.db() as db:
            await db.execute('INSERT INTO messages VALUES (?,?,?)', ('current', 'user', json.dumps([
                {'type': 'sticker', 'url': '/uploads/current.gif', 'mime_type': 'image/gif'}])))
            await db.execute('INSERT INTO messages VALUES (?,?,?)', ('later', 'user', '["/uploads/wrong.png"]'))
            await db.commit()
        request = ToolIntent(id='i', tool_name='sticker.save', raw_text='【偷图】')
        context = ToolContext(conv_id='c', msg_id='a', metadata={'current_user_message_id': 'current'})
        await env.service.execute(request, context)
        await env.service.execute(request, context)
        await asyncio.gather(*tasks)
        assert len(tasks) == len(env.calls) == 1
        rows = await env.repo.library()
        assert len(rows) == 1 and rows[0]['source_message_id'] == 'current'
        (env.uploads / 'current.gif').unlink()
        assert (env.uploads / rows[0]['url'].rsplit('/', 1)[1]).read_bytes() == data
    asyncio.run(run())


def test_feedback_claim_release_commit_and_replay(env):
    async def run():
        await env.repo.begin('e', action='send', conv_id='c', expression='装凶')
        await env.repo.finish('e', status='miss')
        one = await env.repo.claim(conv_id='c', turn_id='t1', user_name='小满', ai_name='知秋')
        other = await env.repo.claim(conv_id='c', turn_id='t2', user_name='小满', ai_name='知秋')
        assert one['block'] and not other['block']
        await env.repo.release('t1')
        again = await env.repo.claim(conv_id='c', turn_id='t2', user_name='小满', ai_name='知秋')
        assert again['block'] == one['block']
        async with env.db() as db:
            await env.repo.finalize_in_tx(db, turn_id='t2', message_id='a2')
            await db.commit()
        assert not (await env.repo.claim(conv_id='c', turn_id='t3', user_name='小满', ai_name='知秋'))['block']
        assert (await env.repo.replay('a2', user_name='小满', ai_name='知秋'))['block'] == one['block']
        async with env.db() as db:
            await env.repo.finalize_in_tx(db, turn_id='', message_id='a3', replay_from='a2')
            await db.commit()
        assert (await env.repo.replay('a3', user_name='小满', ai_name='知秋'))['block'] == one['block']
        await env.repo.release('', replayed_from='a2')
        assert (await env.repo.replay('a3', user_name='小满', ai_name='知秋'))['block'] == one['block']
        assert len(await env.repo.wishes()) == 1  # Feedback consumption does not delete creative wishes.
    asyncio.run(run())


@pytest.mark.parametrize('raw', ['喵【偷图】哦', '喵【发图：偷偷探头】哦', '喵[发图：猫猫]哦', '喵【做表情：有字的猫\n写“收到”】哦',
    '喵【发图：白猫举着[OK]的牌子】哦', '喵[做表情:白猫举着【牌子[OK]】]哦'])
def test_sticker_tags_are_hidden_at_every_stream_boundary(raw):
    for split in range(len(raw) + 1):
        stream = ControlMarkerStreamFilter()
        visible = stream.feed(raw[:split]) + stream.feed(raw[split:]) + stream.flush()
        assert visible == '喵哦'
    parsed = parse_tool_intents(canonicalize_control_markers(raw))
    assert len(parsed) == 1 and parsed[0].tool_name.startswith('sticker.')
    if 'OK' in raw:
        assert parsed[0].arguments['expression'] == ('白猫举着[OK]的牌子' if '发图' in raw else '白猫举着【牌子[OK]】')
    stream = ControlMarkerStreamFilter()
    assert stream.feed('喵【发图：未闭合') + stream.flush() == '喵'


def test_unclosed_outer_sticker_is_not_executed_after_an_inner_closing_bracket():
    raw = '【做表情：白猫举着[OK]的牌子'
    assert not parse_tool_intents(canonicalize_control_markers(raw))
    stream = ControlMarkerStreamFilter()
    assert ''.join(stream.feed(char) for char in raw) + stream.flush() == ''


@pytest.mark.parametrize('marker', ['【发图：白猫举着[OK]的牌子】', '[做表情:猫【牌子[OK]】]', '【做表情：白猫举着[OK]'])
def test_shared_command_sanitizer_hides_balanced_and_incomplete_stickers(marker):
    from app.chat.side_effects import _sanitize_working_model_update
    assert _sanitize_working_model_update('正文' + marker) == '正文'


@pytest.mark.parametrize('body,matched,other_attachments,expected_calls', [
    ('正文', False, [], 0), ('', True, [], 0), ('', False, [], 1),
    ('', False, [{'type': 'music', 'id': 'song'}], 0),
])
def test_only_empty_sticker_reply_receives_one_supplement(body, matched, other_attachments, expected_calls):
    calls = []
    intent = ToolIntent(id='i', tool_name='sticker.send', raw_text='【发图：探头】', arguments={'expression': '探头'})

    async def run():
        async def execute(*args, **kwargs):
            return ActionExecution((ToolResult.from_intent(intent, status=ToolStatus.EXECUTED,
                result={'status': 'matched' if matched else 'miss', 'attachment': {'type': 'sticker'} if matched else None}),))

        async def stream(messages, *args):
            calls.append(messages)
            yield '偷偷看看。'

        async def process(raw, **kwargs):
            assert kwargs['memory_eval_mode'] is True and kwargs['enabled_commands'] == frozenset()
            return PostProcessResult(content=raw)

        result = await deliver(PostProcessResult(content=body, tool_intents=[intent]), profile=chat_turn_profile('send'),
            context=ToolContext(conv_id='c', msg_id='a'), history=[], model_key='core', temperature=0.7,
            user_name='小满', ai_name='知秋', execute_actions=execute, stream_model=stream,
            processor=SimpleNamespace(process=process), other_attachments=other_attachments)
        assert len(calls) == expected_calls
        assert bool(result.attachments) == matched
        assert bool(result.supplemented_events) == bool(expected_calls)
        if calls:
            assert '小满' in calls[0][-1]['content'] and '知秋' in calls[0][-1]['content']
    asyncio.run(run())


def test_night_can_create_before_desktop_bootstrap_and_summary_finishes(env, monkeypatch):
    seen = []

    async def generate(expression):
        seen.append(expression)
        return picture_bytes()

    monkeypatch.setattr('app.stickers.image_provider.generate_sticker', generate)

    async def run():
        profile = opportunity_turn_profile(runtime_capabilities={'desktop.presence.draw', 'sticker.create'},
            reflection_allowed=True, kind='night', presence_bootstrap_required=True)
        assert profile.allows_tool('sticker.create')
        assert profile.allows_marker('opportunity_none') and profile.allows_marker('opportunity_reflect')
        expression = '白猫探头，蓝色背景，写“收到”'
        result = await env.service.execute(ToolIntent(id='i', tool_name='sticker.create', raw_text='', arguments={'expression': expression}),
            ToolContext(conv_id='c', msg_id='night1', metadata={'round_kind': 'night'}))
        assert result['status'] == 'ready' and seen == [expression]
        assert len(await env.repo.library()) == 1
        assert (await env.repo.claim(conv_id='c', turn_id='morning', user_name='小满', ai_name='知秋'))['block']
    asyncio.run(run())


def test_all_sticker_core_prompts_use_runtime_names():
    names = {'user_name': '小满', 'ai_name': '知秋'}
    blocks = [prompts.save_ability(**names), prompts.send_ability(**names), prompts.create_ability(**names),
              prompts.night_materials([{'expression': '猫猫', 'context': {}}], **names),
              prompts.supplement(**names, expression='猫猫', reason='未找到'),
              prompts.history_note([{'type': 'sticker'}], **names)]
    for block in blocks:
        assert '小满' in block and '知秋' in block
        assert not any(word in block for word in ('用户', '对方', 'TA', '你', 'AI'))


def test_empty_expression_does_not_shadow_the_first_valid_request():
    intents = parse_tool_intents(canonicalize_control_markers('【发图：  】【发图：猫猫探头】'))
    assert len(intents) == 1 and intents[0].arguments['expression'] == '猫猫探头'
