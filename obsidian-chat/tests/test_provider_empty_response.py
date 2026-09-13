import asyncio
import json

import httpx
import pytest

import ai_providers


@pytest.fixture
def transport(monkeypatch):
    events, notices, requests = [], [], []
    response = {'body': '', 'status': 200}

    def handler(request):
        requests.append(request)
        return httpx.Response(response['status'], content=response['body'],
                              headers={'content-type': 'text/event-stream'})

    real_client = httpx.AsyncClient
    monkeypatch.setattr(ai_providers.httpx, 'AsyncClient',
                        lambda **_kwargs: real_client(transport=httpx.MockTransport(handler)))
    monkeypatch.setattr(ai_providers, 'record_provider_event', lambda event: events.append(event) or event)

    async def notify(*args, **kwargs):
        notices.append((args, kwargs))

    monkeypatch.setattr(ai_providers, '_broadcast_endpoint_error', notify)

    def collect(body, *, status=200):
        response.update(body=body, status=status)
        meta = {}

        async def run():
            return ''.join([chunk async for chunk in ai_providers._stream_openai(
                base_url='https://provider.invalid/v1', api_key='synthetic',
                messages=[{'role': 'user', 'content': '晚安'}], model='test-model', meta=meta,
            )])

        return asyncio.run(run()), meta

    return collect, events, notices, requests


def frame(data, *, space=True):
    return 'data:' + (' ' if space else '') + json.dumps(data, ensure_ascii=False) + '\n\n'


@pytest.mark.parametrize('ending', ['data: [DONE]\n\n', ''])
def test_zero_usage_empty_stream_is_failed_once_and_keeps_finish_reason(transport, ending):
    collect, events, notices, requests = transport
    body = frame({'choices': [{'delta': {}, 'finish_reason': 'stop'}]})
    body += frame({'choices': [], 'usage': {'prompt_tokens': 0, 'completion_tokens': 0, 'total_tokens': 0}})
    text, meta = collect(body + ending)
    assert text.startswith('[错误]') and 'empty_response' in text
    assert len(events) == len(notices) == len(requests) == 1
    event = events[0]
    assert event['ok'] is False and event['error_type'] == 'empty_response'
    assert event['http_status'] == 200 and event['retryable'] is True
    assert event['meta']['finish_reason'] == 'stop'
    assert event['meta']['completion_tokens'] == 0
    assert 'frames=2' in event['message'] and 'parse_errors=0' in event['message']
    assert meta['provider_last'] == event


@pytest.mark.parametrize('body', [
    'data: [DONE]\n\n',
    frame({'choices': [{'delta': {'content': ' \n\t'}}]}),
    frame({'choices': [{'delta': {'reasoning_content': 'private reasoning'}, 'finish_reason': 'length'}]}),
    'data: malformed-event\n\ndata: [DONE]\n\n',
])
def test_missing_or_nontext_output_never_counts_as_success(transport, body):
    collect, events, notices, requests = transport
    text, _ = collect(body)
    assert text.strip().startswith('[错误]')
    assert len(events) == len(notices) == len(requests) == 1
    assert events[0]['error_type'] == 'empty_response'
    assert 'private reasoning' not in text
    assert 'malformed-event' not in events[0]['message']


@pytest.mark.parametrize('content', ['晚安。', '【发图：抱着被子说晚安】', '[VIEW_IMAGE:msg1|/uploads/photo.png]'])
def test_text_and_control_only_replies_remain_successful(transport, content):
    collect, events, notices, requests = transport
    body = frame({'choices': [{'delta': {'content': content}}]}, space=False)
    body += frame({'choices': [{'delta': {}, 'finish_reason': 'stop'}]}) + 'data:[DONE]\n\n'
    text, _ = collect(body)
    assert text == content
    assert len(events) == len(requests) == 1 and not notices
    assert events[0]['ok'] is True and events[0]['http_status'] == 200
    assert events[0]['meta']['finish_reason'] == 'stop'


def test_http_error_keeps_its_original_classification(transport):
    collect, events, notices, requests = transport
    text, _ = collect(json.dumps({'error': {'message': 'temporarily unavailable'}}), status=503)
    assert len(events) == len(notices) == len(requests) == 1
    assert events[0]['error_type'] == 'upstream_overloaded'
    assert 'empty_response' not in text


def test_error_inside_http_200_stream_is_not_swallowed(transport):
    collect, events, notices, requests = transport
    text, _ = collect(frame({'error': {'message': 'upstream unavailable'}}) + 'data: [DONE]\n\n')
    assert len(events) == len(notices) == len(requests) == 1
    assert events[0]['ok'] is False
    assert events[0]['message'] == 'upstream unavailable'
    assert 'upstream unavailable' in text and 'empty_response' not in text
