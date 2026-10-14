import asyncio

import httpx
import pytest

import ai_providers
import provider_http


def test_streams_reuse_client_and_keep_request_credentials_separate(monkeypatch):
    clients, options, requests = [], [], []
    real_client = httpx.AsyncClient

    def handler(request):
        requests.append(request)
        return httpx.Response(200, text=(
            'data: {"choices":[{"delta":{"content":"hello"}}]}\n\n'
            'data: [DONE]\n\n'
        ))

    def factory(**kwargs):
        options.append(kwargs)
        client = real_client(**kwargs, transport=httpx.MockTransport(handler))
        clients.append(client)
        return client

    monkeypatch.setattr(provider_http.httpx, "AsyncClient", factory)
    monkeypatch.setattr(ai_providers, "record_provider_event", lambda event: event)

    async def scenario():
        async with provider_http.provider_http_lifespan():
            for key, timeout in (("first-key", 7.98), ("updated-key", 6.52)):
                text = "".join([chunk async for chunk in ai_providers._stream_openai(
                    base_url="https://example.invalid/v1", api_key=key,
                    messages=[{"role": "user", "content": "hello"}], model="fixture",
                    endpoint={"timeout_sec": timeout},
                )])
                assert text == "hello"
            assert len(clients) == 1 and not clients[0].is_closed
        assert clients[0].is_closed

    asyncio.run(scenario())
    assert [r.headers["Authorization"] for r in requests] == [
        "Bearer first-key", "Bearer updated-key",
    ]
    assert options[0]["limits"].keepalive_expiry == 60.0
    assert [r.extensions["timeout"]["read"] for r in requests] == [7.98, 6.52]


def test_pool_separates_transport_options_and_closes_on_failure():
    clients = []

    async def scenario():
        with pytest.raises(RuntimeError, match="startup failed"):
            async with provider_http.provider_http_lifespan():
                for options in (
                    {"proxy": None},
                    {"proxy": None, "trust_env": False},
                    {"proxy": "http://proxy.invalid:8080"},
                ):
                    async with provider_http.provider_client(**options) as client:
                        clients.append(client)
                raise RuntimeError("startup failed")
        assert len({id(c) for c in clients}) == 3
        assert all(c.is_closed for c in clients)
        assert asyncio.get_running_loop() not in provider_http._pools

    asyncio.run(scenario())


def test_clients_do_not_cross_event_loops_and_one_shot_calls_close():
    clients = []

    async def scenario():
        async with provider_http.provider_http_lifespan():
            async with provider_http.provider_client(proxy=None) as client:
                clients.append(client)
        assert client.is_closed
        async with provider_http.provider_client(proxy=None) as standalone:
            assert standalone is not client
        assert standalone.is_closed

    asyncio.run(scenario())
    asyncio.run(scenario())
    assert clients[0] is not clients[1]


def test_cancelling_stream_closes_response_but_preserves_shared_client(monkeypatch):
    real_client = httpx.AsyncClient

    async def scenario():
        started, closed = asyncio.Event(), asyncio.Event()

        class WaitingBody(httpx.AsyncByteStream):
            async def __aiter__(self):
                started.set()
                await asyncio.Event().wait()
                yield b"unused"

            async def aclose(self):
                closed.set()

        def handler(request):
            return (httpx.Response(200, stream=WaitingBody()) if request.url.path == "/slow"
                    else httpx.Response(200, text="ok"))

        monkeypatch.setattr(provider_http.httpx, "AsyncClient", lambda **kwargs: real_client(
            **kwargs, transport=httpx.MockTransport(handler),
        ))

        async with provider_http.provider_http_lifespan():
            async with provider_http.provider_client() as client:
                async def stream():
                    async with client.stream("GET", "https://example.invalid/slow") as response:
                        await response.aread()

                task = asyncio.create_task(stream())
                await started.wait()
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task
                assert closed.is_set() and not client.is_closed
                assert (await client.get("https://example.invalid/next")).text == "ok"
            assert not client.is_closed
        assert client.is_closed

    asyncio.run(scenario())
