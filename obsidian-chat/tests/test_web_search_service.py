import asyncio
import json

from app.web_search.organizer import organize_search_results
from app.web_search import provider as provider_module
from app.web_search.provider import TavilySearchProvider
from app.web_search.service import WebSearchService


class FakeRepository:
    def __init__(self):
        self.row = {
            "id": "web-1",
            "status": "queued",
            "intent_text": "查新东西",
            "expires_at": 99999999999,
        }
        self.ready = None
        self.failed = None

    async def get(self, _search_id):
        return self.row

    async def mark_ready(self, _search_id, *, result, now):
        self.ready = result
        return True

    async def mark_failed(self, _search_id, *, reason):
        self.failed = reason
        return True


class FakeProvider:
    def __init__(self):
        self.calls = 0

    async def search(self, _query):
        self.calls += 1
        return {
            "request_id": "req-1",
            "results": [{"title": "标题", "url": "https://real", "content": "摘要"}],
        }


def test_worker_uses_one_provider_and_one_organizer_call():
    repo = FakeRepository()
    calls = []
    provider = FakeProvider()

    async def organizer(intent, searched_at, results):
        calls.append((intent, results))
        return {
            "digest": "整理",
            "claims": [{"text": "事实", "source_ids": ["S1"]}],
            "uncertainties": [],
            "sources": [{"source_id": "S1", "title": "标题", "url": "https://real"}],
            "searched_at": searched_at,
        }

    service = WebSearchService(repository=repo, provider=provider, organizer=organizer)
    asyncio.run(service._run("web-1"))
    assert provider.calls == 1
    assert len(calls) == 1
    assert repo.failed is None
    assert repo.ready["provider_request_id"] == "req-1"


def test_organizer_rejects_forged_source_id(monkeypatch):
    async def fake_call(*_args, **_kwargs):
        return json.dumps({
            "digest": "整理",
            "claims": [{"text": "伪造", "source_ids": ["S9"]}],
            "uncertainties": [],
        })

    monkeypatch.setattr("app.web_search.organizer.call_slot_chat", fake_call)
    try:
        asyncio.run(organize_search_results(
            "查询",
            1,
            [{"title": "真", "url": "https://real", "content": "摘要"}],
        ))
    except ValueError as exc:
        assert str(exc) == "organizer_unknown_source"
    else:
        raise AssertionError("forged source id was accepted")


def test_organizer_code_fills_all_sources_even_without_claims(monkeypatch):
    async def fake_call(*_args, **_kwargs):
        return {
            "digest": "只有概括",
            "claims": [],
            "uncertainties": [],
            "sources": [{"source_id": "S9", "url": "https://forged"}],
        }

    monkeypatch.setattr("app.web_search.organizer.call_slot_chat", fake_call)
    result = asyncio.run(organize_search_results(
        "查询",
        1,
        [
            {"title": "一", "url": "https://one", "content": "摘要一"},
            {"title": "二", "url": "https://two", "content": "摘要二"},
        ],
    ))

    assert result["sources"] == [
        {"source_id": "S1", "title": "一", "url": "https://one", "published_date": ""},
        {"source_id": "S2", "title": "二", "url": "https://two", "published_date": ""},
    ]


def test_empty_provider_result_becomes_ready_without_organizer():
    repo = FakeRepository()

    class EmptyProvider:
        async def search(self, _query):
            return {"request_id": "req-empty", "results": []}

    async def fail_organizer(*_args):
        raise AssertionError("empty results must not call organizer")

    service = WebSearchService(
        repository=repo,
        provider=EmptyProvider(),
        organizer=fail_organizer,
    )
    asyncio.run(service._run("web-1"))

    assert repo.failed is None
    assert repo.ready["claims"] == []
    assert repo.ready["provider_request_id"] == "req-empty"


def test_tavily_provider_uses_fixed_v1_request_and_env_key(monkeypatch):
    calls = []

    class Response:
        status_code = 200

        def json(self):
            return {"request_id": "req", "results": []}

    class Client:
        def __init__(self, **kwargs):
            calls.append(("client", kwargs))

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

        async def post(self, url, *, headers, json):
            calls.append(("post", url, headers, json))
            return Response()

    monkeypatch.setitem(provider_module.SETTINGS, "tavily_api_key", "settings-key")
    monkeypatch.setenv("OBSIDIAN_TAVILY_API_KEY", "env-key")
    monkeypatch.setattr(provider_module.httpx, "AsyncClient", Client)

    result = asyncio.run(TavilySearchProvider().search("最近的新消息"))

    assert result == {"request_id": "req", "results": []}
    assert calls[1] == (
        "post",
        provider_module.TAVILY_URL,
        {"Authorization": "Bearer env-key"},
        {
            "query": "最近的新消息",
            "search_depth": "basic",
            "max_results": 5,
            "include_answer": False,
            "include_raw_content": False,
            "auto_parameters": False,
        },
    )
