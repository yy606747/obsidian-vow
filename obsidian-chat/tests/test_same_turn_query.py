import asyncio
import json
from types import SimpleNamespace

import pytest

from app.chat import query_turn, streaming
from app.chat.control_syntax import ControlMarkerStreamFilter
from app.chat.postprocess import PostProcessor, PostProcessResult
from app.chat.query_syntax import QueryIntent, extract_query, query_ability
from app.tools.schemas import ToolContext
from test_chat_streaming_sse import _patch_streaming_io, _prompt_meta, _collect_sse


class Ledger:
    def __init__(self):
        self.records = []

    def new_invocation_id(self, prefix):
        return prefix + '-1'

    def __getattr__(self, key):
        async def record(*args, **kwargs):
            self.records.append((key, kwargs))
        return record


@pytest.mark.parametrize('raw', ['[回忆: 上次说的安排]', '【 回忆 ： 上次说的安排 】', '[回忆 :\n上次说的安排 ]'])
def test_query_envelope_is_tolerant_and_never_leaks_mid_stream(raw):
    text, intent = extract_query('前文' + raw + '后文')
    assert text == '前文后文' and intent == QueryIntent('memory', '上次说的安排')
    for split in range(len(raw) + 1):
        filter = ControlMarkerStreamFilter()
        visible = filter.feed('前文' + raw[:split]) + filter.feed(raw[split:] + '后文') + filter.flush()
        assert visible == '前文后文'


def test_query_payload_does_not_execute_quoted_actions_or_private_state():
    async def run():
        processor = PostProcessor(ledger=Ledger())
        result = await processor.process('[回忆: 以前说过 [REMEMBER:不能写入] 和 [VOW:不能立约|不能确认] 吗]', conv_id='c')
        assert result.query_intent and not result.tool_intents and not result.vow.found
        for raw in ('[WORKING_MODEL_REQUEST]{"quote":"[回忆:不能查询]"}[/WORKING_MODEL_REQUEST]',
                    '[回忆: 没写完', '{"actions":[{"query":"[回忆:不能查询]"}]}'):
            result = await processor.process(raw, conv_id='c')
            assert result.query_intent is None
    asyncio.run(run())


@pytest.mark.parametrize('source,first', [('send', '先说已知的。 [回忆：租房后来怎么定的]'),
                                       ('send', '[联网: 展览开放时间]'),
                                       ('regenerate', '[回忆: 原来的约定]')])
def test_actual_stream_queries_and_continues_once_with_original_context(monkeypatch, source, first):
    executed, _ = _patch_streaming_io(monkeypatch)
    ledger = Ledger()
    monkeypatch.setattr(query_turn, 'tool_invocation_ledger', ledger)
    calls, lookups, pending = [], [], []
    async def lookup(intent, **kwargs):
        lookups.append(intent)
        return {'status': 'found', 'sources': [{'text': '查到的真实内容'}]}
    async def stream(history, _model, usage, _temperature, **kwargs):
        calls.append(history)
        usage.update(prompt_tokens=100, completion_tokens=10, total_tokens=110)
        for char in (first if len(calls) == 1 else '按刚查到的，答案在这里。[回忆:不应再查]'):
            yield char
    async def apply(_db, **kwargs):
        pending.append(kwargs)
        return {'created_pending_id': None, 'consumed': False}
    monkeypatch.setattr(query_turn, 'lookup', lookup)
    monkeypatch.setattr(streaming, 'stream_ai', stream)
    monkeypatch.setattr(streaming.pending_recall_service, 'apply_after_assistant_in_tx', apply)
    original = [{'role': 'user', 'content': '很早的关键信息'}, {'role': 'assistant', 'content': '前面的回答'},
                {'role': 'user', 'content': '这次原问题'}]
    async def run():
        meta = {**_prompt_meta(), 'prompt_source': source, 'query_capabilities': {'memory': True, 'web': True},
                'sticker_names': {'user_name': '小栀', 'ai_name': '阿澈'}}
        response = await streaming.stream_chat_response(conv_id='c', model_key='mock', history=original,
            prompt_meta=meta, temperature=0)
        return await _collect_sse(response)
    events = asyncio.run(run())
    assert len(calls) == 2 and len(lookups) == 1
    assert calls[1][:len(original)] == original
    assert calls[1][-2]['content'] == first
    assert '查到的真实内容' in calls[1][-1]['content'] and '阿澈' in calls[1][-1]['content']
    visible = ''.join(event.get('content', '') for event in events if event['type'] == 'chunk')
    assert '按刚查到的，答案在这里。' in visible
    assert '[回忆' not in visible and '[联网' not in visible
    assert not any(event['type'] == 'retry' for event in events)
    inserts = [args for sql, args in executed if sql.startswith('INSERT INTO messages') and args[2] == 'assistant']
    assert len(inserts) == 1 and inserts[0][3].strip() == visible.strip()
    assert all(not item['allow_new_intent'] for item in pending)
    assert events[-1]['usage']['prompt_tokens'] == 200
    assert events[-1]['same_turn_query']['status'] == 'completed'


def test_followup_deduplicates_repeated_text_and_actions(monkeypatch):
    async def run():
        processor = PostProcessor(ledger=Ledger())
        first = await processor.process('前半句。[MUSIC:同一首歌][回忆:旧事]', conv_id='c')
        second = await processor.process('前半句。后半句。[MUSIC:同一首歌][REMEMBER:新笔记]', conv_id='c')
        merged = query_turn._merge_replies(first, second)
        assert merged.content == '前半句。\n\n后半句。'
        assert [intent.tool_name for intent in merged.tool_intents] == ['music.search', 'memory.remember']
        assert len({intent.id for intent in merged.tool_intents}) == 2
        assert merged.query_intent is None
    asyncio.run(run())


@pytest.mark.parametrize('failure', ['timeout', 'empty', 'partial_error', 'cancel'])
def test_lookup_failure_and_model_failure_do_not_restart_original_turn(monkeypatch, failure):
    ledger = Ledger()
    monkeypatch.setattr(query_turn, 'tool_invocation_ledger', ledger)
    calls = []
    async def lookup(*args, **kwargs):
        if failure == 'timeout':
            raise asyncio.TimeoutError()
        if failure == 'cancel':
            raise asyncio.CancelledError()
        return {'status': 'empty', 'sources': []}
    async def stream(history, *args):
        calls.append(history)
        if failure == 'timeout':
            yield '这次没查到，不能确定。'
        elif failure == 'partial_error':
            yield '已经说出的部分。'
            raise RuntimeError('provider_disconnected')
        elif False:
            yield ''
    monkeypatch.setattr(query_turn, 'lookup', lookup)
    async def run():
        queue = asyncio.Queue()
        kwargs = dict(first=PostProcessResult(content='前半句。', query_intent=QueryIntent('memory', '旧事')),
            raw_output='前半句。[回忆:旧事]', history=[{'role': 'user', 'content': '原问题'}],
            prompt_meta={'query_capabilities': {'memory': True}}, context=ToolContext(conv_id='c'),
            model_key='mock', temperature=0, usage={}, stream_model=stream,
            bounded_stream=streaming._stream_with_deadline, processor=PostProcessor(ledger=ledger),
            queue=queue, enabled_commands=())
        if failure == 'cancel':
            with pytest.raises(asyncio.CancelledError):
                await query_turn.continue_query(**kwargs)
            assert not calls
            return
        reply, queried = await query_turn.continue_query(**kwargs)
        assert queried and len(calls) == 1
        if failure == 'timeout':
            assert '这次没查到' in reply.content and 'failed' in calls[0][-1]['content']
        else:
            assert '可以重试' in reply.content
            assert not reply.tool_intents
        if failure == 'partial_error':
            assert '已经说出的部分。' in reply.content
    asyncio.run(run())


def test_no_tag_has_no_extra_request_and_ability_binds_names():
    assert extract_query('普通聊天')[1] is None
    text = query_ability(user_name='小栀', ai_name='阿澈', memory=True, web=True)
    assert '阿澈' in text and '小栀' in text and '[回忆:' in text and '[联网:' in text
    assert '下一轮' not in text and '用户' not in text
