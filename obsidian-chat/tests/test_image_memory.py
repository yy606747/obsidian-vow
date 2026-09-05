"""图片派生记忆的真实数据库与兼容接口请求检查，不调用真实服务。"""

import asyncio
import base64
import importlib
import json
import sqlite3

import httpx
import pytest

import ai_providers
import config
import database
from app.image_memory import repository as repo, service
from app.memory_v2 import embedding, memory_service, prompt_block
from app.memory_v3.pending_recall import selection_prompt_items
from routes import image_memory as image_routes, settings as settings_routes

hybrid = importlib.import_module("app.memory_v2.hybrid_recall")
PNG = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aS2kAAAAASUVORK5CYII=")
DESCRIPTION = json.dumps({"scene": "桌上红色杯子", "text": "夏日", "uncertainties": "材质无法确认"}, ensure_ascii=False)


@pytest.fixture
def photo(tmp_path, monkeypatch):
    path = tmp_path / "images.db"
    monkeypatch.setattr(database, "DB_PATH", path)
    monkeypatch.setattr(repo, "UPLOADS_DIR", tmp_path)
    monkeypatch.setattr(ai_providers, "UPLOADS_DIR", tmp_path)
    monkeypatch.setitem(config.SETTINGS, "image_memory_enabled", True)
    monkeypatch.setitem(config.SETTINGS, "endpoints", [{"id": "vision", "type": "openai", "base_url": "https://vision.invalid/v4", "api_key": "test-only"}])
    monkeypatch.setitem(config.SETTINGS, "slots", {"vision_summary": {"endpoint": "vision", "model": "glm-4.6v-flash", "enabled": True}})
    asyncio.run(database.init_db())
    (tmp_path / "photo.png").write_bytes(PNG)
    with sqlite3.connect(path) as db:
        db.execute("INSERT INTO conversations (id,title,created_at,updated_at) VALUES ('conv','测试',1,1)")
        db.execute("INSERT INTO messages (id,conv_id,role,content,attachments,created_at) VALUES ('photo','conv','user','不应传给摘要的私密聊天',?,1)", (json.dumps(["/uploads/photo.png"]),))

    async def documents(texts):
        return [[1.0, 0.0] for _ in texts]

    async def query(_text):
        return [1.0, 0.0]

    async def no_broadcast(*_args, **_kwargs):
        return None

    monkeypatch.setattr(embedding, "get_embeddings_batch", documents)
    monkeypatch.setattr(embedding, "get_embedding", query)
    monkeypatch.setattr(ai_providers, "_broadcast_endpoint_error", no_broadcast)
    monkeypatch.setattr(service, "RATE_LIMIT_DELAY", 0)
    hybrid.clear_full_corpus_cache()
    yield path
    hybrid.clear_full_corpus_cache()


def row(path):
    with sqlite3.connect(path) as db:
        db.row_factory = sqlite3.Row
        raw = db.execute("SELECT * FROM image_observations").fetchone()
        return dict(raw) if raw else None


def transport(monkeypatch, handler):
    original = httpx.AsyncClient
    monkeypatch.setattr(ai_providers.httpx, "AsyncClient", lambda **kwargs: original(transport=httpx.MockTransport(handler), **kwargs))


def test_real_payload_has_only_image_and_instruction_and_independent_model(photo, monkeypatch):
    payloads = []

    def handle(request):
        payloads.append(json.loads(request.content))
        return httpx.Response(200, json={"choices": [{"message": {"content": DESCRIPTION}}]})

    transport(monkeypatch, handle)
    assert asyncio.run(service.process_image("photo", "/uploads/photo.png")) == "ready"
    monkeypatch.setitem(config.SETTINGS, "default_model", "changed-core-model")
    monkeypatch.setitem(config.SETTINGS["slots"]["vision_summary"], "model", "another-model")
    assert asyncio.run(service.process_image("photo", "/uploads/photo.png")) == "ready"
    assert len(payloads) == 1
    payload = payloads[0]
    assert payload["model"] == "glm-4.6v-flash"
    assert len(payload["messages"]) == 1
    parts = payload["messages"][0]["content"]
    assert [part["image_url"]["url"] for part in parts if part["type"] == "image_url"] == ["data:image/png;base64," + base64.b64encode(PNG).decode()]
    assert "私密聊天" not in json.dumps(payload, ensure_ascii=False)
    saved = row(photo)
    assert saved["model"] == "glm-4.6v-flash" and saved["attempts"] == 1
    assert "test-only" not in str(saved)
    assert "不确定处：材质无法确认" in saved["description"]


@pytest.mark.parametrize("state", ["disabled", "slot_off", "unconfigured", "unsupported"])
def test_unavailable_slot_never_calls_core_or_embedding(photo, monkeypatch, state):
    if state == "disabled":
        monkeypatch.setitem(config.SETTINGS, "image_memory_enabled", False)
    elif state == "slot_off":
        monkeypatch.setitem(config.SETTINGS["slots"]["vision_summary"], "enabled", False)
    elif state == "unconfigured":
        monkeypatch.setitem(config.SETTINGS["slots"]["vision_summary"], "endpoint", "missing")
    else:
        monkeypatch.setitem(config.SETTINGS["endpoints"][0], "type", "gemini")

    async def unexpected(*args, **kwargs):
        pytest.fail("未配置或关闭时不应调用模型")

    monkeypatch.setattr(service, "call_slot_chat", unexpected)
    monkeypatch.setattr(embedding, "get_embeddings_batch", unexpected)
    assert asyncio.run(service.process_image("photo", "/uploads/photo.png")) == "disabled_or_unconfigured"
    assert row(photo) is None


def test_retry_freezes_endpoint_and_model_and_stops_after_two(photo, monkeypatch):
    calls = []

    def handle(request):
        calls.append((str(request.url), json.loads(request.content)["model"]))
        config.SETTINGS["slots"]["vision_summary"]["model"] = "paid-should-not-use"
        config.SETTINGS["endpoints"][0]["base_url"] = "https://changed.invalid/v1"
        return httpx.Response(429, json={"error": "rate_limited"})

    transport(monkeypatch, handle)
    assert asyncio.run(service.process_image("photo", "/uploads/photo.png")) == "deferred"
    assert calls == [("https://vision.invalid/v4/chat/completions", "glm-4.6v-flash")] * 2
    assert row(photo)["attempts"] == 2


@pytest.mark.parametrize("body,status,attempts", [('{"refused":true}', 200, 1), ("", 401, 1), ("", 503, 2), ("not-json", 200, 2)])
def test_failures_are_bounded_and_leave_manual_retry_state(photo, monkeypatch, body, status, attempts):
    transport(monkeypatch, lambda req: httpx.Response(status, json={"choices": [{"message": {"content": body}}]}))
    assert asyncio.run(service.process_image("photo", "/uploads/photo.png")) == "deferred"
    assert row(photo)["attempts"] == attempts
    assert row(photo)["error_type"]


def test_embedding_retry_keeps_successful_description(photo, monkeypatch):
    calls = []

    async def describe(*args, **kwargs):
        calls.append(1)
        return DESCRIPTION

    async def fail(texts):
        return [None]

    monkeypatch.setattr(service, "call_slot_chat", describe)
    with monkeypatch.context() as scoped:
        scoped.setattr(embedding, "get_embeddings_batch", fail)
        assert asyncio.run(service.process_image("photo", "/uploads/photo.png")) == "deferred"
    assert row(photo)["description"]
    assert asyncio.run(service.process_image("photo", "/uploads/photo.png")) == "ready"
    assert calls == [1]


def test_long_term_recall_provenance_pending_and_deleted_source(photo, monkeypatch):
    async def describe(*args, **kwargs):
        return DESCRIPTION
    monkeypatch.setattr(service, "call_slot_chat", describe)

    async def scenario():
        await service.process_image("photo", "/uploads/photo.png")
        candidates = await hybrid.wide_chunk_recall("红色杯子", top_k=5, candidate_limit=1000, as_of_ts=100)
        assert len(candidates) == 1 and candidates[0]["source_type"] == "image"
        assert candidates[0]["source_end_ts"] == 1
        items = selection_prompt_items({"selected_candidate_ids": [candidates[0]["candidate_id"]], "needs_raw_detail_ids": [candidates[0]["candidate_id"]]}, candidates)
        assert not items[0]["needs_raw_detail"]
        assert await repo.valid_items(items) == items
        block = prompt_block.build_v2_memory_prompt_block({"selected": items}, user_name="小栀")
        assert "图片观察，非原话" in block["content"]
        assert "来源消息=photo" in block["content"] and "附件=/uploads/photo.png" in block["content"]
        config.SETTINGS["image_memory_enabled"] = False
        assert await repo.valid_items(items) == []
        config.SETTINGS["image_memory_enabled"] = True
        async with database.get_db() as db:
            await db.execute("DELETE FROM messages WHERE id='photo'")
            await memory_service.reconcile_conversation_chunks_in_tx(db, "conv")
            await db.commit()
        memory_service.invalidate_conversation_cache("conv")
        assert await repo.valid_items(items) == []
        assert await hybrid.wide_chunk_recall("红色杯子", top_k=5, candidate_limit=1000, as_of_ts=100) == []

    asyncio.run(scenario())
    assert row(photo)["status"] == "retired"


def test_delete_during_summary_does_not_resurrect_image(photo, monkeypatch):
    async def describe(*args, **kwargs):
        async with database.get_db() as db:
            await db.execute("DELETE FROM messages WHERE id='photo'")
            await db.commit()
        return DESCRIPTION
    monkeypatch.setattr(service, "call_slot_chat", describe)
    assert asyncio.run(service.process_image("photo", "/uploads/photo.png")) == "retired"


def test_settings_default_and_persistence_use_independent_slot(photo, monkeypatch, tmp_path):
    fresh = {"endpoints": [], "slots": {}}
    config._ensure_endpoints_and_slots(fresh)
    assert fresh["slots"]["vision_summary"] == {"endpoint": "", "model": "glm-4.6v-flash", "enabled": False}
    monkeypatch.setattr(config, "SETTINGS_PATH", tmp_path / "settings.json")
    monkeypatch.setattr(settings_routes, "save_settings", config.save_settings)
    asyncio.run(settings_routes.put_slot(settings_routes.SlotUpdate(name="vision_summary", endpoint="vision", model="glm-4.6v-flash", extras={"enabled": True})))
    reloaded = config.load_settings()
    assert reloaded["slots"]["vision_summary"]["enabled"] is True
    assert reloaded["slots"]["vision_summary"]["model"] == "glm-4.6v-flash"
    assert config.get_slot("vision_summary")["model"] == "glm-4.6v-flash"


def test_single_concurrency_and_schedule_deduplication(photo, monkeypatch):
    calls = []

    async def describe(*args, **kwargs):
        calls.append(1)
        await asyncio.sleep(0.01)
        return DESCRIPTION
    monkeypatch.setattr(service, "call_slot_chat", describe)

    async def scenario():
        first = service.schedule_message("photo")
        assert service.schedule_message("photo") is first
        await first
        await asyncio.gather(service.process_image("photo", "/uploads/photo.png"), service.process_image("photo", "/uploads/photo.png"))
    asyncio.run(scenario())
    assert calls == [1]


@pytest.fixture
def viewable(photo, monkeypatch):
    from app.image_memory import view

    async def describe(*args, **kwargs):
        return DESCRIPTION

    async def vows():
        return "[约定] 说定的事要算数。", {}

    async def broadcast(payload):
        pass

    monkeypatch.setattr(service, "call_slot_chat", describe)
    monkeypatch.setattr(view.vow_service, "load_vow_prompt_context", vows)
    monkeypatch.setattr(view, "load_worldbook", lambda: {
        "user_name": "小栀", "ai_name": "阿澈", "ai_persona": "既有性格不变", "user_persona": "原有介绍",
    })
    monkeypatch.setattr(view.manager, "broadcast", broadcast)
    monkeypatch.setitem(config.SETTINGS, "user_models", {"core-image": {
        "endpoint": "vision", "model": "owner-selected-model", "image_input": True,
    }})
    asyncio.run(service.process_image("photo", "/uploads/photo.png"))
    with sqlite3.connect(photo) as db:
        db.execute("INSERT INTO messages (id,conv_id,role,content,attachments,created_at) VALUES ('parent','conv','assistant','我再看看。','[]',10)")
    return photo


def view_context():
    from app.tools.schemas import ToolContext
    return ToolContext(conv_id="conv", msg_id="parent", request_id="parent", model_key="core-image",
                       capabilities=("memory.view_image",), metadata={"turn_id": "turn_image_test"})


def view_intent():
    from app.tools.parser import parse_tool_intents
    return parse_tool_intents("[VIEW_IMAGE:photo|/uploads/photo.png]")[0]


@pytest.mark.parametrize("provider", ["openai", "gemini"])
def test_review_reaches_real_core_visual_encoder_and_never_replays_tools(viewable, monkeypatch, provider):
    from app.image_memory import view
    payloads = []
    monkeypatch.setitem(config.SETTINGS["endpoints"][0], "type", provider)
    response_text = "杯子上写着夏日。[REMEMBER:不应写入][VIEW_IMAGE:photo|/uploads/photo.png]"

    def handle(request):
        payloads.append((str(request.url), json.loads(request.content)))
        event = {"choices": [{"delta": {"content": response_text}}]} if provider == "openai" else {
            "candidates": [{"content": {"parts": [{"text": response_text}]}, "finishReason": "STOP"}],
        }
        return httpx.Response(200, text="data: " + json.dumps(event) + "\n\ndata: [DONE]\n\n", headers={"content-type": "text/event-stream"})

    transport(monkeypatch, handle)

    async def scenario():
        request = await view.execute_view_image(view_intent(), view_context())
        return await view.followup(view_context(), request)

    result = asyncio.run(scenario())
    assert result["status"] == "succeeded"
    assert len(payloads) == 1
    url, payload = payloads[0]
    if provider == "openai":
        assert payload["model"] == "owner-selected-model"
        images = [part["image_url"]["url"] for message in payload["messages"] if isinstance(message["content"], list)
                  for part in message["content"] if part.get("type") == "image_url"]
        assert images == ["data:image/png;base64," + base64.b64encode(PNG).decode()]
    else:
        assert "owner-selected-model:streamGenerateContent" in url
        images = [part["inline_data"]["data"] for message in payload["contents"] for part in message["parts"] if "inline_data" in part]
        assert images == [base64.b64encode(PNG).decode()]
    prompt = json.dumps(payload, ensure_ascii=False)
    assert "小栀" in prompt and "阿澈" in prompt and "既有性格不变" in prompt
    assert "不是当前画面" in prompt
    with sqlite3.connect(viewable) as db:
        answer = db.execute("SELECT content FROM messages WHERE id=?", (result["message_id"],)).fetchone()[0]
        assert answer == "杯子上写着夏日。"
        assert db.execute("SELECT COUNT(*) FROM memory_items").fetchone()[0] == 0
        assert db.execute("SELECT COUNT(*) FROM messages WHERE role='assistant'").fetchone()[0] == 2


def test_view_tool_binding_parser_and_one_per_turn(viewable):
    from app.chat.postprocess import PostProcessor
    from app.chat.action_executor import execute_postprocessed_actions
    from app.chat.turn_profiles import chat_turn_profile, initiative_turn_profile
    from app.tools.parser import parse_structured_tool_intents
    from app.tools.registry import validate_tool_registry

    validate_tool_registry()
    intent = parse_structured_tool_intents([{"tool": "memory.view_image", "arguments": {"message_id": "photo", "attachment_url": "/uploads/photo.png"}}])[0]
    assert intent.arguments == view_intent().arguments

    async def scenario():
        raw = "再看看。[VIEW_IMAGE:photo|/uploads/photo.png][VIEW_IMAGE:photo|/uploads/photo.png]"
        processed = await PostProcessor().process(raw, conv_id="conv", tool_context=view_context())
        assert processed.content == "再看看。"
        executed = await execute_postprocessed_actions(processed, profile=chat_turn_profile("send"), context=view_context(), only_capabilities={"memory.view_image"})
        assert len(executed.executed) == 1
        assert any(item.error == "one_image_view_per_turn" for item in executed.results)
        denied = await execute_postprocessed_actions(processed, profile=initiative_turn_profile(toy_enabled=False), context=view_context(), only_capabilities={"memory.view_image"})
        assert not denied.executed
    asyncio.run(scenario())


@pytest.mark.parametrize("reason", ["disabled", "no_vision", "source_deleted", "file_changed", "parent_deleted"])
def test_view_refuses_invalid_source_or_disabled_feature_without_model_calls(viewable, monkeypatch, reason):
    from app.image_memory import view

    async def unexpected(*args, **kwargs):
        pytest.fail("失效或关闭时不应重看")
        yield ""
    monkeypatch.setattr(view, "stream_ai", unexpected)

    async def scenario():
        request = await view.execute_view_image(view_intent(), view_context())
        if reason == "disabled":
            config.SETTINGS["image_memory_enabled"] = False
        elif reason == "no_vision":
            config.SETTINGS["user_models"]["core-image"]["image_input"] = False
        elif reason == "file_changed":
            (repo.UPLOADS_DIR / "photo.png").write_bytes(b"changed")
        else:
            async with database.get_db() as db:
                await db.execute("DELETE FROM messages WHERE id=?", ("photo" if reason == "source_deleted" else "parent",))
                await db.commit()
        result = await view.followup(view_context(), request)
        assert result["status"] in {"disabled", "source_missing", "parent_missing"}
    asyncio.run(scenario())


def test_gemini_rejection_does_not_trigger_second_vision_call(viewable, monkeypatch):
    from app.image_memory import view
    monkeypatch.setitem(config.SETTINGS["endpoints"][0], "type", "gemini")
    calls = []

    def handle(request):
        calls.append(1)
        event = {"candidates": [{"finishReason": "PROHIBITED_CONTENT", "content": {"parts": []}}]}
        return httpx.Response(200, text="data: " + json.dumps(event) + "\n\n")
    transport(monkeypatch, handle)

    async def scenario():
        request = await view.execute_view_image(view_intent(), view_context())
        result = await view.followup(view_context(), request)
        assert result["status"] == "failed"
    asyncio.run(scenario())
    assert calls == [1]


def test_image_descriptions_are_not_removed_by_visible_text_or_other_attachments(viewable):
    async def scenario():
        plan = await hybrid.hybrid_recall("红色杯子", full_corpus=True, visible_message_ids=["photo"])
        assert any(item["source_type"] == "image" for item in plan["selected"])
    asyncio.run(scenario())
    first = {"id": "image1", "source_type": "image", "source_message_ids": ["photo"], "attachment_url": "/uploads/a.png", "score": 0.9, "content": "相似画面"}
    second = {**first, "id": "image2", "attachment_url": "/uploads/b.png"}
    original = {"id": "text", "source_type": "chunk", "source_message_ids": ["photo"], "score": 0.9, "content": "相似画面"}
    assert len(hybrid._dedupe([first, second, original])) == 3


@pytest.mark.parametrize("encoder", [ai_providers.build_multimodal_messages, ai_providers.build_gemini_contents])
def test_required_original_cannot_silently_disappear_or_change(photo, encoder):
    attachment = {"url": "/uploads/photo.png", "expected_sha256": repo.file_hash("/uploads/photo.png")}
    (repo.UPLOADS_DIR / "photo.png").write_bytes(b"changed")
    with pytest.raises(ValueError, match="图片来源"):
        encoder([{"role": "user", "content": "重看", "attachments": [attachment]}])
    attachment["url"] = "/uploads/missing.png"
    with pytest.raises(FileNotFoundError):
        encoder([{"role": "user", "content": "重看", "attachments": [attachment]}])


def test_status_routes_and_explicit_backfill_do_not_scan_or_repeat_history(viewable, monkeypatch):
    requested = []
    monkeypatch.setattr(image_routes, "schedule_message", lambda message_id: requested.append(message_id))

    async def scenario():
        result = await image_routes.backfill(image_routes.BackfillRequest(message_ids=["photo", "photo", "missing"]))
        assert result == {"scheduled_message_ids": ["photo"]}
        assert requested == ["photo"]
        listed = await image_routes.list_observations(limit=20)
        assert listed["items"][0]["status"] == "ready"
        assert "embedding" not in listed["items"][0] and "api_key" not in str(listed)
        config.SETTINGS["image_memory_enabled"] = False
        from fastapi import HTTPException
        with pytest.raises(HTTPException) as exc:
            await image_routes.backfill(image_routes.BackfillRequest(message_ids=["photo"]))
        assert exc.value.status_code == 409
    asyncio.run(scenario())


def test_restart_converts_interrupted_work_to_manual_retry_state(photo, monkeypatch):
    async def describe(*args, **kwargs):
        return DESCRIPTION
    monkeypatch.setattr(service, "call_slot_chat", describe)
    asyncio.run(service.process_image("photo", "/uploads/photo.png"))
    with sqlite3.connect(photo) as db:
        db.execute("UPDATE image_observations SET status='running'")
    asyncio.run(database.init_db())
    assert row(photo)["status"] == "deferred"
    assert row(photo)["error_type"] == "process_interrupted"
    assert row(photo)["attempts"] == 1


@pytest.mark.parametrize("source", ["send", "regenerate"])
def test_chat_advertises_exactly_the_enabled_image_capability(viewable, monkeypatch, source):
    from app.chat import prompt_builder as builder
    from app.chat.models import MsgCreate
    from app.modes import mode_service
    async def no_context(_conv_id):
        return {}
    monkeypatch.setattr(builder, "_self_wake_prompt_context", no_context)
    # 完整普通聊天构建器还有其他设备状态；沿用实际入口，清掉无关异步设备上下文。
    async def build():
        capabilities = mode_service.capabilities_for_mode("normal")
        if source == "send":
            return await builder.build_send_ability_block(body=MsgCreate(content="查看旧图"), conv_id="conv", model_key="core-image", user_name="小栀", capabilities=capabilities)
        return await builder.build_regenerate_ability_block(
            conv_id="conv", model_key="core-image", user_name="小栀", capabilities=capabilities,
            ai_dom_mode=False, safeword="", dom_history="", cnc_enabled=False, cnc_weakness="",
            resist_hits=0, short_streak=0, reply_delay_ms=0, compliance_streak=0, session_elapsed=0,
            scene_name="", scene_elapsed=0, since_last_punish=None, ratchet_valley=0, debt=0,
            stubborn_streak=0, whisper_mode=False,
        )
    block = asyncio.run(build())
    assert "memory.view_image" in block.advertised_tools
    assert "[VIEW_IMAGE:" in block
    config.SETTINGS["image_memory_enabled"] = False
    block = asyncio.run(build())
    assert "memory.view_image" not in block.advertised_tools and "[VIEW_IMAGE:" not in block


def test_cancelled_summary_closes_and_waits_for_explicit_retry(photo, monkeypatch):
    async def scenario():
        started = asyncio.Event()
        async def describe(*args, **kwargs):
            started.set()
            await asyncio.Future()
        monkeypatch.setattr(service, "call_slot_chat", describe)
        task = asyncio.create_task(service.process_image("photo", "/uploads/photo.png"))
        await asyncio.wait_for(started.wait(), timeout=2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    asyncio.run(scenario())
    assert row(photo)["status"] == "deferred" and row(photo)["error_type"] == "cancelled"


@pytest.mark.parametrize("switch", ["feature", "slot"])
def test_disabling_image_memory_cancels_embedding_retries_and_queued_jobs(photo, monkeypatch, switch):
    from app.background_tasks import begin_task_lifecycle

    async def describe(*args, **kwargs):
        return DESCRIPTION

    monkeypatch.setattr(service, "call_slot_chat", describe)
    # 绕过夹具的成功向量替身，实际走兼容接口编码与内部重试循环。
    monkeypatch.setattr(embedding, "get_embeddings_batch", embedding.get_document_embeddings_batch)
    monkeypatch.setattr(embedding, "get_key", lambda _name: "test-only")
    monkeypatch.setitem(config.SETTINGS, "memory_embedding", {"max_retries": 2, "request_interval_sec": 0})
    monkeypatch.setattr(settings_routes, "save_settings", lambda _settings: None)
    with sqlite3.connect(photo) as db:
        db.execute("INSERT INTO messages (id,conv_id,role,content,attachments,created_at) VALUES ('queued','conv','user','排队图片',?,2)", (json.dumps(["/uploads/photo.png"]),))
    requests = []

    async def scenario():
        begin_task_lifecycle()
        retry_waiting = asyncio.Event()
        queued_waiting = asyncio.Event()
        process_image = service.process_image

        async def observe_queue(message_id, url):
            if message_id == "queued":
                queued_waiting.set()
            return await process_image(message_id, url)

        def handle(request):
            requests.append(str(request.url))
            return httpx.Response(503, json={"error": "temporary"})

        def backoff(*args):
            retry_waiting.set()
            return 30.0

        transport(monkeypatch, handle)
        monkeypatch.setattr(embedding, "_backoff_seconds", backoff)
        monkeypatch.setattr(service, "process_image", observe_queue)
        first = service.schedule_message("photo")
        await asyncio.wait_for(retry_waiting.wait(), timeout=2)
        queued = service.schedule_message("queued")
        await asyncio.wait_for(queued_waiting.wait(), timeout=2)
        assert row(photo)["description"]
        if switch == "feature":
            await settings_routes.update_settings(settings_routes.SettingsUpdate(image_memory_enabled=False))
        else:
            await settings_routes.put_slot(settings_routes.SlotUpdate(
                name="vision_summary", endpoint="vision", model="glm-4.6v-flash", extras={"enabled": False},
            ))
        await asyncio.wait_for(asyncio.gather(first, queued, return_exceptions=True), timeout=2)
        assert first.cancelled() and queued.cancelled()

    asyncio.run(scenario())
    assert len(requests) == 1
    assert row(photo)["status"] == "deferred" and row(photo)["description"]
    assert row(photo)["embedding"] is None
    with sqlite3.connect(photo) as db:
        assert db.execute("SELECT COUNT(*) FROM image_observations WHERE message_id='queued'").fetchone()[0] == 0


@pytest.mark.parametrize("operation", ["delete_message", "delete_conversation", "retire"])
def test_summary_retry_rechecks_source_and_observation_status(photo, monkeypatch, operation):
    from app.chat import crud_routes
    original_sleep = asyncio.sleep
    calls = []
    monkeypatch.setattr(service, "RATE_LIMIT_DELAY", 123.456)

    async def noop(*args, **kwargs):
        return None

    monkeypatch.setattr(crud_routes, "export_conversation", noop)
    monkeypatch.setattr(crud_routes.manager, "broadcast", noop)
    import routes.files
    monkeypatch.setattr(routes.files, "delete_exported_file", lambda _id: None)

    async def scenario():
        waiting, resume = asyncio.Event(), asyncio.Event()

        async def sleep(delay):
            if delay == service.RATE_LIMIT_DELAY:
                waiting.set()
                await resume.wait()
            else:
                await original_sleep(delay)

        def handle(request):
            calls.append(str(request.url))
            return httpx.Response(429, json={"error": "rate_limited"})

        monkeypatch.setattr(service.asyncio, "sleep", sleep)
        transport(monkeypatch, handle)
        task = asyncio.create_task(service.process_image("photo", "/uploads/photo.png"))
        try:
            await asyncio.wait_for(waiting.wait(), timeout=2)
            if operation == "delete_message":
                assert (await crud_routes.delete_message("photo"))["ok"]
            elif operation == "delete_conversation":
                assert (await crud_routes.delete_conversation("conv"))["ok"]
            else:
                async with database.get_db() as db:
                    await db.execute("UPDATE image_observations SET status='retired'")
                    await db.commit()
        finally:
            resume.set()
        assert await asyncio.wait_for(task, timeout=2) == "retired"

    asyncio.run(scenario())
    assert len(calls) == 1
    assert row(photo)["status"] == "retired" and row(photo)["attempts"] == 1


@pytest.mark.parametrize("followup_outcome", ["succeeded", "failed", "invalid_output"])
def test_actual_chat_stream_schedules_one_image_followup_on_same_turn(viewable, monkeypatch, followup_outcome):
    from app.chat import streaming
    from app.image_memory import view
    from app.background_tasks import _BACKGROUND_TASKS, begin_task_lifecycle
    calls = []

    async def noop(*args, **kwargs):
        return None
    async def first_reply(*args, **kwargs):
        yield "我再看看。[VIEW_IMAGE:photo|/uploads/photo.png][VIEW_IMAGE:photo|/uploads/photo.png]"
    async def second_reply(history, model, **kwargs):
        calls.append((history, model))
        assert len([part for message in ai_providers.build_multimodal_messages(history)
                    if isinstance(message["content"], list) for part in message["content"] if part.get("type") == "image_url"]) == 1
        if followup_outcome == "failed":
            raise RuntimeError("合成重看失败")
        yield "看清了，是红色杯子。[REMEMBER:不应执行]" if followup_outcome == "succeeded" else "[REMEMBER:不应执行]"
    monkeypatch.setattr(streaming, "stream_ai", first_reply)
    monkeypatch.setattr(view, "stream_ai", second_reply)
    monkeypatch.setattr(streaming, "export_conversation", noop)
    monkeypatch.setattr(streaming, "_schedule_chunk_index_update", lambda *args, **kwargs: {})
    monkeypatch.setattr(streaming, "_schedule_working_model_pipeline_after_commit", lambda **kwargs: None)
    monkeypatch.setattr(streaming, "_maybe_auto_digest", noop)

    async def scenario():
        begin_task_lifecycle()
        finished = asyncio.Event()
        followup = view.followup

        async def complete_followup(*args, **kwargs):
            try:
                return await followup(*args, **kwargs)
            finally:
                finished.set()

        async def delay_main_completion(payload):
            if payload.get("type") == "debug":
                await asyncio.wait_for(finished.wait(), timeout=3)

        monkeypatch.setattr(view, "followup", complete_followup)
        monkeypatch.setattr(streaming.manager, "broadcast", delay_main_completion)
        history = [{"role": "user", "content": "旧图里的杯子呢？"}]
        response = await streaming.stream_chat_response(
            conv_id="conv", model_key="core-image", history=history, temperature=None,
            prompt_meta={"prompt_source": "send", "recall_keywords": "", "recall_query": "", "recall_topic": "",
                         "is_search_needed": False, "recalled_memories": [], "debug_top6": [],
                         "prompt_messages": history, "prompt_count": 1, "advertised_tools": ["memory.view_image"]},
        )
        events = [json.loads(raw[6:].strip()) async for raw in response.body_iterator]
        pending = [task for task in _BACKGROUND_TASKS if not task.done() and task.get_loop() is asyncio.get_running_loop()]
        if pending:
            await asyncio.wait_for(asyncio.gather(*pending), timeout=3)
        return events[0]["turn_id"]

    turn_id = asyncio.run(scenario())
    assert len(calls) == 1 and calls[0][1] == "core-image"
    with sqlite3.connect(viewable) as db:
        answers = db.execute("SELECT content FROM messages WHERE id LIKE '%_image_view'").fetchall()
        assert answers == ([("看清了，是红色杯子。",)] if followup_outcome == "succeeded" else [])
        turns = db.execute("SELECT DISTINCT turn_id FROM tool_invocation_events WHERE stage='model_request'").fetchall()
        assert turns == [(turn_id,)]
        assert db.execute("SELECT COUNT(*) FROM memory_items").fetchone()[0] == 0
        final = db.execute("SELECT prompt_source,turn_outcome,metadata_json FROM tool_invocation_events WHERE stage='turn'").fetchall()
        assert len(final) == 1 and final[0][:2] == ("send", "succeeded")
        assert json.loads(final[0][2])["diagnostics"]["timings"]["total_ms"] is not None
        phases = db.execute("SELECT invocation_id,outcome,metadata_json FROM tool_invocation_events WHERE stage='diagnostic' AND source='image_view_followup'").fetchall()
        assert len(phases) == 1 and phases[0][0] and phases[0][1] == followup_outcome
        assert json.loads(phases[0][2])["phase"] == "image_view_followup"
