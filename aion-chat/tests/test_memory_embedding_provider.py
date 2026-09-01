import asyncio
import json

import httpx
import pytest

from app.memory_v2 import embedding


def _gemini_config(**updates):
    config = {
        "provider": "gemini",
        "model": "gemini-embedding-2",
        "dimensions": 3,
        "batch_size": 32,
        "request_interval_sec": 0.0,
        "timeout_sec": 10.0,
        "max_retries": 2,
        "initial_backoff_sec": 1.0,
        "max_backoff_sec": 10.0,
        "jitter_ratio": 0.0,
        "profile": "retrieval-v1",
        "base_url": embedding.GEMINI_BASE,
        "signature": "gemini:gemini-embedding-2:3:retrieval-v1",
    }
    config.update(updates)
    return config


def test_gemini_defaults_are_tier_safe_and_memory_efficient(monkeypatch):
    monkeypatch.setitem(embedding.SETTINGS, "memory_embedding", {"provider": "gemini"})
    monkeypatch.delenv("AION_MEMORY_EMBEDDING_PROVIDER", raising=False)
    monkeypatch.delenv("AION_MEMORY_EMBEDDING_MODEL", raising=False)
    monkeypatch.delenv("AION_MEMORY_EMBEDDING_DIMENSIONS", raising=False)

    config = embedding.load_embedding_config()

    assert config["model"] == "gemini-embedding-2"
    assert config["dimensions"] == 768
    assert config["batch_size"] == 32
    assert config["request_interval_sec"] == 1.0
    assert config["max_retries"] == 8
    assert config["signature"] == "gemini:gemini-embedding-2:768:retrieval-v1"


def test_gemini_query_and_document_use_asymmetric_retrieval_format(monkeypatch):
    monkeypatch.setattr(embedding, "get_key", lambda provider: "test-key")
    query_path, query_body, query_headers = embedding._request_parts(
        ["南京那次"], purpose="query", config=_gemini_config()
    )
    doc_path, doc_body, _ = embedding._request_parts(
        ["在南京坐了很久高铁"], purpose="document", config=_gemini_config()
    )

    assert query_path.endswith(":batchEmbedContents")
    assert query_headers == {"x-goog-api-key": "test-key"}
    assert query_body["requests"][0]["content"]["parts"][0]["text"] == (
        "task: search result | query: 南京那次"
    )
    assert doc_body["requests"][0]["content"]["parts"][0]["text"] == (
        "title: none | text: 在南京坐了很久高铁"
    )
    assert query_body["requests"][0]["outputDimensionality"] == 3
    assert doc_path == query_path


def test_429_honors_retry_after_then_recovers(monkeypatch):
    request = httpx.Request("POST", "https://example.invalid")
    responses = [
        httpx.Response(429, headers={"Retry-After": "3"}, request=request),
        httpx.Response(
            200,
            json={"embeddings": [{"values": [3.0, 4.0, 0.0]}]},
            request=request,
        ),
    ]
    posted = []

    class FakeClient:
        def __init__(self, *args, **kwargs):
            assert "proxy" in kwargs

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def post(self, url, *, json, headers):
            posted.append((url, json, headers))
            return responses.pop(0)

    sleeps = []

    async def fake_sleep(seconds):
        sleeps.append(seconds)

    async def no_throttle(_config):
        return None

    monkeypatch.setattr(embedding.httpx, "AsyncClient", FakeClient)
    monkeypatch.setattr(embedding, "get_key", lambda provider: "test-key")
    monkeypatch.setattr(embedding, "record_provider_event", lambda event: None)
    monkeypatch.setattr(embedding, "_wait_for_rate_slot", no_throttle)
    monkeypatch.setattr(embedding.asyncio, "sleep", fake_sleep)

    result = asyncio.run(
        embedding._request_batch_once(
            ["旧事"], purpose="document", config=_gemini_config()
        )
    )

    assert result.retries == 1
    assert result.rate_limited == 1
    assert result.requests == 2
    assert sleeps == [3.0]
    assert len(posted) == 2
    assert result.vectors[0] == pytest.approx([0.6, 0.8, 0.0])


def test_siliconflow_default_keeps_original_text_and_payload(monkeypatch):
    monkeypatch.setattr(embedding, "get_key", lambda provider: "sf-key")
    config = embedding.load_embedding_config(
        {"provider": "siliconflow", "model": "BAAI/bge-m3", "dimensions": 1024}
    )

    path, body, headers = embedding._request_parts(
        ["原始正文"], purpose="document", config=config
    )

    assert path == "/embeddings"
    assert body == {"model": "BAAI/bge-m3", "input": ["原始正文"]}
    assert headers == {"Authorization": "Bearer sf-key"}
