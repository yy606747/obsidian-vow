import asyncio
import copy
import json
import sqlite3

import pytest

import database
from app.chat import basic_actions, streaming
from app.stickers.chat import StickerDelivery
from test_chat_streaming_sse import _collect_sse, _patch_streaming_io, _prompt_meta


@pytest.fixture
def run_reply(monkeypatch):
    executed, broadcasts = _patch_streaming_io(monkeypatch)
    calls, outputs, requests, turns, notes = [], [], [], [], []

    async def output(_context, **kwargs):
        outputs.append(copy.deepcopy(kwargs))

    async def request(_context, **kwargs):
        requests.append(copy.deepcopy(kwargs))

    async def turn(_context, **kwargs):
        turns.append(copy.deepcopy(kwargs))

    async def remember(items, _conv_id):
        notes.extend(items)
        return len(items)

    monkeypatch.setattr(streaming.tool_invocation_ledger, 'record_model_output', output)
    monkeypatch.setattr(streaming.tool_invocation_ledger, 'record_model_request', request)
    monkeypatch.setattr(streaming.tool_invocation_ledger, 'record_turn', turn)
    monkeypatch.setattr(basic_actions, '_store_remember_notes', remember)
    monkeypatch.setattr(streaming, '_schedule_working_model_pipeline_after_commit', lambda **_: None)
    monkeypatch.setattr(streaming, '_schedule_chunk_index_update', lambda *_, **__: {})
    monkeypatch.setattr(streaming.timeline_service, 'start_background_refresh', lambda *_: None)

    def run(attempts, *, source='send', evaluation=False):
        history = [{'role': 'user', 'content': '晚安'}]

        async def provider(messages, model, usage, temperature, **options):
            index = len(calls)
            if index:
                assert options.get('retry_prohibited') is False
            calls.append((copy.deepcopy(messages), model, temperature))
            assert index < len(attempts), 'unexpected extra model request'
            raw, meta = attempts[index]
            usage.update(copy.deepcopy(meta))
            if isinstance(raw, BaseException):
                raise raw
            for chunk in (raw if isinstance(raw, list) else [raw]):
                yield chunk

        monkeypatch.setattr(streaming, 'stream_ai', provider)

        async def scenario():
            meta = {**_prompt_meta(), 'prompt_source': source}
            response = await streaming.stream_chat_response(
                conv_id='recovery', model_key='fixture', history=history,
                prompt_meta=meta, temperature=0.7, memory_eval_mode=evaluation,
            )
            return await _collect_sse(response)

        events = asyncio.run(scenario())
        return dict(events=events, calls=calls, outputs=outputs, requests=requests,
                    turns=turns, notes=notes, sql=executed, broadcasts=broadcasts)

    return run


def usage(reason, number):
    return dict(finish_reason=reason, prompt_tokens=10, completion_tokens=number,
                total_tokens=10 + number, reasoning_tokens=1, reasoning_metrics_reported=True,
                billable_output_tokens=number, cache_metrics_reported=True,
                cache_read_tokens=0, cache_write_tokens=0, cache_hit=False,
                provider_calls=[{'request_id': f'request_{number}'}])


@pytest.mark.parametrize('source', ['send', 'regenerate'])
@pytest.mark.parametrize('first', ['', '[错误] empty_response', '[错误] content_filter',
                                  '[RING:轻碰][REMEMBER:丢弃的尝试]', '【偷图】'])
def test_empty_reply_retries_same_input_once_before_actions(run_reply, first, source):
    result = run_reply([(first, usage('content_filter', 2)),
                        ('晚安。[REMEMBER:最终记下]', usage('stop', 3))], source=source)
    assert len(result['calls']) == 2 and result['calls'][0] == result['calls'][1]
    assert result['notes'] == ['最终记下']
    assert len({r['invocation_id'] for r in result['requests']}) == 2
    assert result['outputs'][0]['metadata']['retrying'] is True
    assert result['outputs'][1]['raw_output'] == '晚安。[REMEMBER:最终记下]'
    assert result['turns'][-1]['turn_outcome'] == 'succeeded'
    debug = result['events'][-1]
    assert debug['auto_retry_count'] == 1 and debug['has_error'] is False
    assert debug['usage']['prompt_tokens'] == 20 and debug['usage']['completion_tokens'] == 5
    assert len(debug['usage']['provider_calls']) == 2
    assert len([e for e in result['events'] if e['type'] == 'retry']) == 1
    assert not any('[错误]' in e.get('content', '') for e in result['events'])
    messages = [params for sql, params in result['sql'] if sql.startswith('INSERT INTO messages')]
    assert len(messages) == 1 and messages[0][3] == '晚安。'


def test_filtered_twice_stops_with_one_friendly_error_and_no_actions(run_reply):
    result = run_reply([('[RING:第一次][REMEMBER:第一次]', usage('content_filter', 1)),
                        ('[RING:第二次][REMEMBER:第二次]', usage('content_filter', 2))])
    assert len(result['calls']) == 2 and not result['notes']
    assert not any(sql.startswith('INSERT INTO messages') for sql, _ in result['sql'])
    assert result['turns'][-1]['turn_outcome'] == 'filtered_empty'
    errors = [e for e in result['events'] if '[错误]' in e.get('content', '')]
    assert len(errors) == 1 and 'content_filter' in errors[0]['content']


def test_partial_filter_preserves_text_without_retry_or_tool_actions(run_reply):
    result = run_reply([('还有一句没说完[REMEMBER:未完整生成的动作]', usage('content_filter', 3))])
    assert len(result['calls']) == 1 and not result['notes']
    assert result['outputs'][0]['outcome'] == 'partial'
    assert result['turns'][-1]['turn_outcome'] == 'filtered_partial'
    message = next(e['message'] for e in result['events'] if e['type'] == 'message')
    assert message['content'] == '还有一句没说完'
    assert message['response_status'] == 'filtered_partial' and message['persisted'] is True
    assert result['events'][-1]['has_error'] is False
    assert any(params[-1] == 'filtered_partial' for sql, params in result['sql'] if sql.startswith('INSERT INTO messages'))


@pytest.mark.parametrize('error', ['timeout', 'auth_error', 'gateway_error'])
def test_other_provider_errors_do_not_trigger_empty_retry(run_reply, error):
    result = run_reply([(f'[错误] {error}', {'provider_last': {'error_type': error}})])
    assert len(result['calls']) == 1
    assert result['turns'][-1]['turn_outcome'] == 'provider_failed'


def test_cancelled_retry_does_not_persist_or_execute_discarded_attempt(run_reply):
    result = run_reply([('[REMEMBER:丢弃]', usage('content_filter', 1)), (asyncio.CancelledError(), {})])
    assert len(result['calls']) == 2 and not result['notes']
    assert result['turns'][-1]['turn_outcome'] == 'cancelled'
    assert not any(sql.startswith('INSERT INTO messages') for sql, _ in result['sql'])


def test_evaluation_and_already_retried_provider_do_not_get_an_extra_retry(run_reply):
    result = run_reply([('', {})], evaluation=True)
    assert len(result['calls']) == 1


def test_native_provider_retry_budget_is_respected(run_reply):
    result = run_reply([('[错误] empty_response', {'provider_calls': [{}, {}]})])
    assert len(result['calls']) == 1


def test_native_retry_success_still_streams_text_and_clears_its_discarded_error(run_reply):
    result = run_reply([(['[错误] PROHIBITED_CONTENT', '\x00RETRY\x00', '晚安。'],
                         {'provider_calls': [{}, {}], 'finish_reason': 'STOP'})])
    assert len(result['calls']) == 1
    assert result['events'][-1]['has_error'] is False
    chunks = [e['content'] for e in result['events'] if e['type'] == 'chunk']
    assert chunks == ['晚安。']
    assert result['turns'][-1]['turn_outcome'] == 'succeeded'


@pytest.mark.parametrize('first', ['', '[错误] empty_response', '[RING:轻碰][REMEMBER:丢弃]'])
def test_unfiltered_empty_attempt_can_recover_without_losing_error_classification(run_reply, first):
    result = run_reply([(first, {}), ('收到了。', {})])
    assert len(result['calls']) == 2 and not result['notes']
    assert result['turns'][-1]['turn_outcome'] == 'succeeded'


def test_normal_tool_only_sticker_is_delivered_without_replaying_model(run_reply, monkeypatch):
    async def deliver(*args, **kwargs):
        return StickerDelivery(attachments=[{'type': 'sticker', 'id': 'one', 'url': '/uploads/one.png'}])
    monkeypatch.setattr(streaming, 'deliver_sticker', deliver)
    result = run_reply([('【发图:晚安】', usage('stop', 2))])
    assert len(result['calls']) == 1
    assert result['turns'][-1]['turn_outcome'] == 'succeeded'


def test_response_status_migration_and_message_reload(tmp_path, monkeypatch):
    path = tmp_path / 'legacy.db'
    with sqlite3.connect(path) as db:
        db.execute('CREATE TABLE messages (id TEXT PRIMARY KEY, conv_id TEXT, role TEXT, content TEXT, created_at REAL)')
        db.execute("INSERT INTO messages VALUES ('old','c','assistant','原来的消息',1)")
    monkeypatch.setattr(database, 'DB_PATH', path)

    async def scenario():
        from app.chat.crud_routes import get_message, list_messages
        await database.init_db()
        await database.init_db()  # Existing deployments can restart repeatedly.
        async with database.get_db() as db:
            await db.execute("INSERT INTO messages (id,conv_id,role,content,created_at,response_status) VALUES ('partial','c','assistant','没说完',2,'filtered_partial')")
            await db.commit()
        single = await get_message('partial')
        history = await list_messages('c', limit=50, before=None)
        return single, history

    single, history = asyncio.run(scenario())
    assert single['content'] == '没说完' and single['response_status'] == 'filtered_partial'
    assert history[0]['content'] == '原来的消息' and history[0]['response_status'] == ''
    assert history[1]['response_status'] == 'filtered_partial'



@pytest.mark.parametrize('source', ['send', 'regenerate'])
def test_first_text_timeout_retries_once_without_duplicate_delivery(run_reply, source):
    result = run_reply([(TimeoutError('first_text_timeout'), {'stream_timeout_phase':'first_text'}),
                        ('终于收到啦。', usage('stop', 3))], source=source)
    assert len(result['calls']) == 2 and result['calls'][0] == result['calls'][1]
    assert result['turns'][-1]['turn_outcome'] == 'succeeded'
    assert len([sql for sql, _ in result['sql'] if sql.startswith('INSERT INTO messages')]) == 1
    retry = next(e for e in result['events'] if e['type'] == 'retry')
    assert retry['reason'] == 'timeout'
    assert not any('请求出错' in e.get('content', '') for e in result['events'])


def test_repeated_timeout_stops_after_two_attempts(run_reply):
    result = run_reply([(TimeoutError('first_text_timeout'), {'stream_timeout_phase':'first_text'})] * 2)
    assert len(result['calls']) == 2
    assert result['events'][-1]['has_error']
    assert not any(sql.startswith('INSERT INTO messages') for sql, _ in result['sql'])


@pytest.mark.parametrize('phase', ['first_text', 'idle'])
def test_stream_deadline_closes_stalled_connection(monkeypatch, phase):
    monkeypatch.setattr(streaming, 'CHAT_FIRST_TEXT_TIMEOUT_SEC', 0.01)
    monkeypatch.setattr(streaming, 'CHAT_STREAM_IDLE_TIMEOUT_SEC', 0.01)
    closed, chunks, meta = [], [], {}

    async def provider():
        try:
            if phase == 'idle':
                yield '已经开始回复'
            while True:
                await asyncio.sleep(0.003)
                yield ' '  # heartbeat/whitespace cannot extend the deadline
        finally:
            closed.append(True)

    async def scenario():
        with pytest.raises(TimeoutError, match=phase + '_timeout'):
            async for chunk in streaming._stream_with_deadline(provider(), meta):
                chunks.append(chunk)

    asyncio.run(scenario())
    assert closed == [True]
    assert meta['stream_timeout_phase'] == phase
    assert meta['provider_calls'][-1]['retryable'] is (phase == 'first_text')
