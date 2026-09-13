"""单模型思考设置与计费用量回归；仅使用内存中的模拟接口。"""
import asyncio
import json
from pathlib import Path

import httpx
import pytest
from pydantic import ValidationError

import ai_providers
import config
from app.turn_diagnostics import TurnDiagnostics
from routes import settings as settings_routes


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


@pytest.mark.parametrize("value", ["MINIMAL", "ultra", 1000])
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


def test_settings_page_exposes_per_model_thinking_level():
    source = (Path(__file__).resolve().parents[1] / "static/settings.html").read_text()
    assert 'id="umThinkingLevel"' in source
    assert "thinking_level: thinkingLevel" in source
