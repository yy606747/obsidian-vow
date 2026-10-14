import asyncio
from contextlib import asynccontextmanager
import time

import pytest

from app.provider_privacy import private_provider_call, private_provider_scope
from app.scenes.context import load_identity
from app.scenes.models import SceneModels


@pytest.mark.parametrize("v2", [False, True])
def test_scene_identity_loads_only_persona_names_and_current_understanding(monkeypatch, v2):
    import config
    import database
    from app.working_model import repository

    monkeypatch.setattr(config, "load_worldbook", lambda: {"user_name":"小栀", "ai_name":"阿澈",
        "ai_persona":"PERSONA", "user_persona":"NO_USER_PORTRAIT", "world_setting":"NO_WORLD"})
    monkeypatch.setattr(config, "load_ai_behavior", lambda: {"working_model_v2_injection_enabled":v2})
    monkeypatch.setattr(config, "load_working_model", lambda: {"content":"LEGACY_UNDERSTANDING", "desire":"NO_DESIRE"})

    @asynccontextmanager
    async def db():
        yield object()

    async def head(_db):
        return {"content":"CURRENT_UNDERSTANDING"}

    monkeypatch.setattr(database, "get_db", db)
    monkeypatch.setattr(repository, "get_head", head)
    identity = asyncio.run(load_identity())
    assert vars(identity) == {"user_name":"小栀", "ai_name":"阿澈", "ai_persona":"PERSONA",
        "working_model":"CURRENT_UNDERSTANDING" if v2 else "LEGACY_UNDERSTANDING"}


def test_scene_real_adapters_forward_the_isolated_context_and_scope(monkeypatch):
    import ai_providers
    import config
    observed = []
    messages = [{"role":"system", "content":"PRIVATE_SCENE_CONTEXT"}]

    async def once(model, context, **kwargs):
        observed.append(("once", model, context, kwargs, private_provider_scope.get()))
        return "result"

    async def stream(context, model, meta, **kwargs):
        try:
            observed.append(("stream", model, context, kwargs, private_provider_scope.get()))
            yield "visible"
            assert private_provider_scope.get() == "scene:turn"
            yield "<toy1>hidden</toy1>"
        finally:
            assert private_provider_scope.get() == "scene:turn"

    async def slot(name, context, **kwargs):
        observed.append(("compile", name, context, kwargs, private_provider_scope.get()))
        return '{"action":"hold"}'

    monkeypatch.setattr(ai_providers, "call_core_chat_once", once)
    monkeypatch.setattr(ai_providers, "stream_ai", stream)
    monkeypatch.setattr(ai_providers, "call_slot_chat", slot)
    monkeypatch.setattr(config, "get_slot", lambda name: {"model":"compiler-model", "endpoint":"mock"})

    async def scenario():
        models = SceneModels()
        assert await models.once("core-model", messages, scope="cards") == "result"
        assert private_provider_scope.get() is None
        output = []
        async for chunk in models.stream("core-model", messages):
            assert private_provider_scope.get() is None
            output.append(chunk)
        assert output == ["visible", "<toy1>hidden</toy1>"]
        assert await models.compile(messages) == '{"action":"hold"}'
        assert private_provider_scope.get() is None
        source = models.stream("core-model", messages)
        assert await anext(source) == "visible"
        await source.aclose()
        assert private_provider_scope.get() is None

    asyncio.run(scenario())
    assert [row[4] for row in observed] == ["scene:cards", "scene:turn", "scene:compiler", "scene:turn"]
    assert all(row[2] == messages for row in observed)
    assert observed[2][1] == "scene_compiler"
    assert observed[2][3]["slot_snapshot"]["model"] == "compiler-model"
    assert observed[2][3]["expect_json"] is True


@pytest.mark.parametrize("failure", ["length", "empty", "error", "prohibited"])
def test_incomplete_scene_streams_fail_instead_of_committing_partial_intents(monkeypatch, failure):
    import ai_providers

    async def stream(context, model, meta, **kwargs):
        if failure == "length":
            meta["finish_reason"] = "length"
        elif failure == "prohibited":
            meta["_prohibited"] = True
        yield "[错误] private body" if failure == "error" else "\x00" if failure == "empty" else "<toy1>partial</toy1>"

    monkeypatch.setattr(ai_providers, "stream_ai", stream)

    async def scenario():
        with pytest.raises(ValueError, match="scene_model"):
            async for _ in SceneModels().stream("core", []):
                pass
        assert private_provider_scope.get() is None

    asyncio.run(scenario())


def test_unconfigured_compiler_does_not_fall_back_to_a_different_slot(monkeypatch):
    import config
    monkeypatch.setattr(config, "get_slot", lambda _: None)
    with pytest.raises(ValueError, match="scene_compiler_not_configured"):
        asyncio.run(SceneModels().compile([]))


def test_private_provider_errors_keep_metadata_but_never_log_or_broadcast_scene_text(monkeypatch):
    import ai_providers as providers
    from ws import manager
    logged, broadcasts = [], []
    monkeypatch.setattr(providers, "record_provider_event", lambda event: logged.append(event) or event)

    async def broadcast(event):
        broadcasts.append(event)

    monkeypatch.setattr(manager, "broadcast", broadcast)

    async def scenario():
        with private_provider_call("scene:turn"):
            ctx = providers._provider_context(scope="core", provider_type="openai", label="test", model="test", base_url="https://example.invalid")
            providers._finish_provider_call({}, ctx, ok=False, start=time.monotonic(), error_type="http_error", message="PRIVATE_SCENE_BODY")
            assert providers._redact("PRIVATE_SCENE_BODY") == "[private provider detail]"
            await providers._broadcast_endpoint_error("core", "test", "PRIVATE_SCENE_BODY")
        assert private_provider_scope.get() is None
        assert providers._redact("ordinary error") == "ordinary error"
        await providers._broadcast_endpoint_error("core", "test", "ordinary error")

    asyncio.run(scenario())
    assert logged[0]["scope"] == "scene:turn"
    assert logged[0]["message"] == ""
    assert len(broadcasts) == 1
    assert broadcasts[0]["data"]["error"] == "ordinary error"
    assert "PRIVATE_SCENE_BODY" not in str(logged) + str(broadcasts)
