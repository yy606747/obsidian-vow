"""单模型思考设置与计费用量回归；仅使用内存中的模拟接口。"""
import asyncio
import json

import httpx
import pytest
from pydantic import ValidationError

import ai_providers
import config
from app.turn_diagnostics import TurnDiagnostics
from routes import settings as settings_routes
from fastapi import HTTPException
from model_thinking import thinking_capabilities


@pytest.mark.parametrize("endpoint_type", ["vertex", "gemini", "openai"])
def test_custom_model_setting_reaches_transport_and_persists_usage(monkeypatch, endpoint_type):
    endpoint = {
        "id": "test", "type": endpoint_type, "name": "合成端点",
        "base_url": "https://provider.invalid/v1", "api_key": "synthetic",
    }
    monkeypatch.setattr(config, "SETTINGS", {
        "endpoints": [endpoint], "user_models": {
            "flash-high": {"endpoint": "test", "model": "gemini-3.8-flash", "thinking_level": "HIGH"},
        },
    })
    requests, events = [], []

    def handler(request):
        payload = json.loads(request.content)
        requests.append(payload)
        if endpoint_type == "openai":
            assert payload["reasoning_effort"] == "high"
            frames = [
                {"choices": [{"delta": {"content": "合成回复"}}]},
                {"choices": [], "usage": {"prompt_tokens": 10000, "completion_tokens": 1700,
                    "completion_tokens_details": {"reasoning_tokens": 1500},
                    "prompt_tokens_details": {"cached_tokens": 7000}, "total_tokens": 11700}},
            ]
        else:
            assert payload["generationConfig"]["thinkingConfig"] == {"thinkingLevel": "HIGH"}
            assert "thinkingBudget" not in payload["generationConfig"]["thinkingConfig"]
            frames = [
                {"usageMetadata": {"promptTokenCount": 10000, "cachedContentTokenCount": 7000},
                 "candidates": [{"content": {"parts": [
                     {"text": "不应成为正文的思考摘要", "thought": True},
                     {"text": "合成"}, {"text": "回复"},
                 ]}}]},
                {"candidates": [], "usageMetadata": {
                    "candidatesTokenCount": 200, "thoughtsTokenCount": 1500, "totalTokenCount": 11700}},
            ]
        body = "".join("data: " + json.dumps(frame) + "\n\n" for frame in frames)
        if endpoint_type == "openai":
            body += "data: [DONE]\n\n"
        return httpx.Response(200, content=body, headers={"content-type": "text/event-stream"})

    real_client = httpx.AsyncClient
    monkeypatch.setattr(ai_providers.httpx, "AsyncClient", lambda **kwargs: real_client(transport=httpx.MockTransport(handler)))
    monkeypatch.setattr(ai_providers, "record_provider_event", lambda event: events.append(event) or event)

    async def collect():
        # 没有传 meta 的调用也必须持久记录用量。
        return "".join([chunk async for chunk in ai_providers.stream_ai(
            [{"role": "user", "content": "合成测试"}], "flash-high",
        )])

    assert asyncio.run(collect()) == "合成回复"
    assert len(requests) == len(events) == 1
    usage = events[0]["meta"]
    assert usage["prompt_tokens"] == 10000
    assert usage["cache_read_tokens"] == 7000
    assert usage["reasoning_tokens"] == 1500
    assert usage["billable_output_tokens"] == 1700
    assert "raw" not in usage


@pytest.mark.parametrize("endpoint_type", ["vertex", "openai"])
def test_background_call_without_meta_still_records_usage(monkeypatch, endpoint_type):
    events = []

    def handler(request):
        if endpoint_type == "vertex":
            body = {"candidates": [{"content": {"parts": [{"text": "摘要"}]}}], "usageMetadata": {
                "promptTokenCount": 100, "candidatesTokenCount": 20, "thoughtsTokenCount": 30, "totalTokenCount": 150,
            }}
        else:
            body = {"choices": [{"message": {"content": "摘要"}}], "usage": {
                "prompt_tokens": 100, "completion_tokens": 50, "completion_tokens_details": {"reasoning_tokens": 30},
            }}
        return httpx.Response(200, json=body)

    real_client = httpx.AsyncClient
    monkeypatch.setattr(ai_providers.httpx, "AsyncClient", lambda **kwargs: real_client(transport=httpx.MockTransport(handler)))
    monkeypatch.setattr(ai_providers, "record_provider_event", lambda event: events.append(event) or event)
    result = asyncio.run(ai_providers._call_endpoint_chat_once(
        endpoint={"type": endpoint_type, "base_url": "https://provider.invalid/v1"},
        model="gemini-3.8-flash", messages=[{"role": "user", "content": "合成输入"}],
        error_channel="test", scope="test", thinking_level="HIGH",
    ))
    assert result == "摘要"
    assert events[0]["meta"]["reasoning_tokens"] == 30
    assert events[0]["meta"]["billable_output_tokens"] == 50


def test_model_settings_round_trip_preserves_omitted_level_and_allows_clear(monkeypatch):
    settings = {"user_models": {}}
    monkeypatch.setattr(settings_routes, "SETTINGS", settings)
    monkeypatch.setattr(settings_routes, "save_settings", lambda value: None)
    base = {"key": "flash", "endpoint": "vertex", "model": "gemini-3.8-flash"}
    asyncio.run(settings_routes.put_user_model(settings_routes.UserModelUpsert(**base, thinking_level="high")))
    assert settings["user_models"]["flash"]["thinking_level"] == "HIGH"
    asyncio.run(settings_routes.put_user_model(settings_routes.UserModelUpsert(**base)))
    assert settings["user_models"]["flash"]["thinking_level"] == "HIGH"
    asyncio.run(settings_routes.put_user_model(settings_routes.UserModelUpsert(**base, thinking_level=None)))
    assert "thinking_level" not in settings["user_models"]["flash"]


@pytest.mark.parametrize("value", ["NONE", "ultra", 1000])
def test_invalid_thinking_level_is_rejected(value):
    with pytest.raises(ValidationError):
        settings_routes.UserModelUpsert(key="flash", endpoint="vertex", model="gemini-3.8-flash", thinking_level=value)


def test_gemini_default_is_unchanged_and_budget_cannot_conflict():
    messages = [{"role": "user", "content": "合成输入"}]
    assert "generationConfig" not in ai_providers._gemini_stream_payload(messages)
    payload = ai_providers._gemini_stream_payload(messages, 0.2, thinking_level="HIGH")
    assert payload["generationConfig"] == {"temperature": 0.2, "thinkingConfig": {"thinkingLevel": "HIGH"}}
    with pytest.raises(ValueError, match="不能同时指定"):
        ai_providers._gemini_slot_payload(messages, thinking_level="HIGH", thinking_budget=1024)


def test_turn_diagnostics_keep_thinking_and_unknown_distinct():
    trace = TurnDiagnostics("synthetic", "send")
    missing = trace.snapshot()["usage"]
    assert missing["reasoning_tokens"] is None
    assert missing["billable_output_tokens"] is None
    present = trace.snapshot({"reasoning_metrics_reported": True, "reasoning_tokens": 1500,
                              "billable_output_tokens": 1700})["usage"]
    assert present["reasoning_tokens"] == 1500
    assert present["billable_output_tokens"] == 1700


@pytest.mark.parametrize("endpoint,model,controls,expected", [
    ({"type": "openai", "base_url": "https://api.deepseek.com/v1"}, "deepseek-v4-flash",
     {"enable_thinking": False, "thinking_level": "HIGH"}, {"thinking": {"type": "disabled"}}),
    ({"type": "openai", "base_url": "https://api.deepseek.com/v1"}, "deepseek-v4-flash",
     {"enable_thinking": True, "thinking_level": "MAX"}, {"thinking": {"type": "enabled"}, "reasoning_effort": "max"}),
    ({"type": "openai", "base_url": "https://api.siliconflow.cn/v1"}, "deepseek-ai/DeepSeek-V3.2",
     {"enable_thinking": False}, {"enable_thinking": False}),
    ({"type": "openai", "base_url": "https://api.siliconflow.cn/v1"}, "deepseek-ai/DeepSeek-V3.2",
     {"enable_thinking": True, "thinking_budget": 1024}, {"enable_thinking": True, "thinking_budget": 1024}),
    ({"type": "openai", "base_url": "https://api.siliconflow.cn/v1"}, "deepseek-ai/DeepSeek-V4-Flash",
     {"thinking_level": "MAX"}, {"reasoning_effort": "max"}),
    ({"type": "vertex", "base_url": "https://provider.invalid/v1"}, "gemini-2.5-flash",
     {"enable_thinking": False}, {"thinkingBudget": 0}),
    ({"type": "gemini", "base_url": "https://provider.invalid/v1"}, "gemini-2.5-flash",
     {"enable_thinking": True, "thinking_budget": 1024}, {"thinkingBudget": 1024}),
    ({"type": "vertex", "base_url": "https://provider.invalid/v1"}, "gemini-3.8-flash",
     {"thinking_level": "LOW"}, {"thinkingLevel": "LOW"}),
])
def test_settings_reach_slot_and_both_core_transports(monkeypatch, endpoint, model, controls, expected):
    endpoint = {**endpoint, "id": "fixture"}
    monkeypatch.setattr(config, "SETTINGS", {
        "endpoints": [endpoint],
        "slots": {"memory_digest": {"endpoint": "fixture", "model": model, **controls}},
        "user_models": {"fixture-core": {"endpoint": "fixture", "model": model, **controls}},
    })
    requests = []

    def handler(request):
        payload = json.loads(request.content)
        requests.append(payload)
        native = endpoint["type"] != "openai"
        actual = payload["generationConfig"]["thinkingConfig"] if native else {
            k: payload[k] for k in ("thinking", "enable_thinking", "reasoning_effort", "thinking_budget") if k in payload
        }
        assert actual == expected
        body = {"candidates": [{"content": {"parts": [{"text": "ok"}]}}]} if native else {
            "choices": [{"message": {"content": "ok"}, "delta": {"content": "ok"}}]}
        streaming = "streamGenerateContent" in str(request.url) or payload.get("stream")
        return httpx.Response(200, content="data: " + json.dumps(body) + "\n\n") if streaming else httpx.Response(200, json=body)

    real_client = httpx.AsyncClient
    monkeypatch.setattr(ai_providers.httpx, "AsyncClient", lambda **kwargs: real_client(transport=httpx.MockTransport(handler)))
    monkeypatch.setattr(ai_providers, "record_provider_event", lambda event: event)
    messages = [{"role": "user", "content": "合成测试"}]

    async def run():
        assert await ai_providers.call_slot_chat("memory_digest", messages, thinking_budget=0) == "ok"
        assert await ai_providers.call_core_chat_once("fixture-core", messages) == "ok"
        assert "".join([chunk async for chunk in ai_providers.stream_ai(messages, "fixture-core")]) == "ok"

    asyncio.run(run())
    assert len(requests) == 3


def test_slot_save_preserves_extra_fields_and_can_clear_thinking(monkeypatch):
    settings = {"endpoints": [{"id": "ds", "type": "openai", "base_url": "https://api.deepseek.com/v1"}],
                "slots": {"sentinel": {"endpoint": "ds", "model": "deepseek-v4-flash", "max_tokens": 999, "enabled": True}}}
    monkeypatch.setattr(settings_routes, "SETTINGS", settings)
    monkeypatch.setattr(settings_routes, "save_settings", lambda value: None)
    base = dict(name="sentinel", endpoint="ds", model="deepseek-v4-flash")
    asyncio.run(settings_routes.put_slot(settings_routes.SlotUpdate(**base, extras={"enable_thinking": False})))
    asyncio.run(settings_routes.put_slot(settings_routes.SlotUpdate(**base)))
    assert settings["slots"]["sentinel"] == {"endpoint": "ds", "model": "deepseek-v4-flash",
                                           "max_tokens": 999, "enabled": True, "enable_thinking": False}
    asyncio.run(settings_routes.put_slot(settings_routes.SlotUpdate(**base, extras={"enable_thinking": None})))
    assert "enable_thinking" not in settings["slots"]["sentinel"]
    before = dict(settings["slots"]["sentinel"])
    with pytest.raises(HTTPException):
        asyncio.run(settings_routes.put_slot(settings_routes.SlotUpdate(**base, extras={"thinking_level": "MEDIUM"})))
    assert settings["slots"]["sentinel"] == before


def test_model_controls_roundtrip_and_change_clears_previous_controls(monkeypatch):
    settings = {"endpoints": [{"id": "sf", "type": "openai", "base_url": "https://api.siliconflow.cn/v1"}]}
    monkeypatch.setattr(settings_routes, "SETTINGS", settings)
    monkeypatch.setattr(settings_routes, "save_settings", lambda value: None)
    base = dict(key="custom", endpoint="sf", model="deepseek-ai/DeepSeek-V3.2")
    async def save(**updates):
        await settings_routes.put_user_model(settings_routes.UserModelUpsert(**{**base, **updates}))
    asyncio.run(save(enable_thinking=True, thinking_budget=1024))
    asyncio.run(save())
    assert settings["user_models"]["custom"]["thinking_budget"] == 1024
    asyncio.run(save(enable_thinking=False, thinking_budget=None))
    assert settings["user_models"]["custom"]["enable_thinking"] is False
    assert "thinking_budget" not in settings["user_models"]["custom"]
    asyncio.run(save(model="Qwen/Qwen2.5-7B-Instruct"))
    assert "enable_thinking" not in settings["user_models"]["custom"]


@pytest.mark.parametrize("model,controls", [
    ("gemini-3.8-flash", {"enable_thinking": False}),
    ("gemini-3.8-flash", {"thinking_level": "MINIMAL"}),
    ("gemini-2.5-pro", {"enable_thinking": False}),
    ("gemini-2.5-flash", {"thinking_budget": 24577}),
])
def test_unsupported_model_controls_rejected_without_saving(monkeypatch, model, controls):
    settings = {"endpoints": [{"id": "gemini", "type": "gemini"}]}
    monkeypatch.setattr(settings_routes, "SETTINGS", settings)
    monkeypatch.setattr(settings_routes, "save_settings", lambda value: pytest.fail("must not save"))
    with pytest.raises(HTTPException):
        asyncio.run(settings_routes.put_user_model(settings_routes.UserModelUpsert(
            key="fixture", endpoint="gemini", model=model, **controls)))
    assert not settings["user_models"]


def test_model_capabilities_and_timeout_defaults():
    sf = {"type": "openai", "base_url": "https://api.siliconflow.cn/v1"}
    assert thinking_capabilities(sf, "deepseek-ai/DeepSeek-V4-Flash")["levels"] == ["HIGH", "MAX"]
    assert thinking_capabilities(sf, "Qwen/Qwen2.5-7B-Instruct")["modes"] == ["default"]
    from app.memory_v3.config import normalize_memory_v3_config
    assert normalize_memory_v3_config({})["timeline_generation_timeout_sec"] == 60
    assert normalize_memory_v3_config({"timeline_generation_timeout_sec": 35})["timeline_generation_timeout_sec"] == 35
