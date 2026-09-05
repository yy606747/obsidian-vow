import asyncio

import ai_providers


def test_vertex_model_url_uses_project_location_publisher_prefix():
    base = "https://aiplatform.googleapis.com/v1/projects/p1/locations/global/publishers/google/"

    assert ai_providers._vertex_model_url(base, "gemini-3.1-pro-preview", "generateContent") == (
        "https://aiplatform.googleapis.com/v1/projects/p1/locations/global/"
        "publishers/google/models/gemini-3.1-pro-preview:generateContent"
    )
    assert ai_providers._vertex_model_url(base, "gemini-3.1-pro-preview", "streamGenerateContent", stream=True).endswith(
        "/models/gemini-3.1-pro-preview:streamGenerateContent?alt=sse"
    )


def test_vertex_headers_use_google_api_key_header():
    assert ai_providers._vertex_headers("AIza-test") == {
        "Content-Type": "application/json",
        "x-goog-api-key": "AIza-test",
    }


def test_stream_ai_dispatches_custom_vertex_endpoint(monkeypatch):
    calls = []

    monkeypatch.setattr(ai_providers, "resolve_core_model", lambda _key: {
        "_kind": "custom",
        "endpoint": {
            "id": "vertex",
            "name": "Vertex Gemini",
            "type": "vertex",
            "base_url": "https://aiplatform.googleapis.com/v1/projects/p1/locations/global/publishers/google",
            "api_key": "AIza-test",
        },
        "model": "gemini-3.1-pro-preview",
    })

    async def fake_call_vertex_compat(endpoint, model, messages, meta=None, temperature=None):
        calls.append({
            "endpoint": endpoint,
            "model": model,
            "messages": messages,
            "temperature": temperature,
        })
        yield "ok"

    monkeypatch.setattr(ai_providers, "call_vertex_compat", fake_call_vertex_compat)

    async def collect():
        return [chunk async for chunk in ai_providers.stream_ai(
            [{"role": "user", "content": "ping"}],
            "vertex-gemini-3.1-pro",
            temperature=0.2,
        )]

    assert asyncio.run(collect()) == ["ok"]
    assert calls[0]["model"] == "gemini-3.1-pro-preview"
    assert calls[0]["endpoint"]["type"] == "vertex"
    assert calls[0]["temperature"] == 0.2
