from prompt_cache import normalize_usage, provider_event_usage_meta, update_usage_meta
import provider_status


def test_normalize_openai_cache_usage_keeps_explicit_zero_distinct_from_missing():
    reported = normalize_usage({
        "prompt_tokens": 2048,
        "completion_tokens": 20,
        "total_tokens": 2068,
        "prompt_tokens_details": {"cached_tokens": 0, "cache_write_tokens": 1024},
    }, provider_type="openai")
    missing = normalize_usage({"prompt_tokens": 2048}, provider_type="openai")

    assert reported == {
        "prompt_tokens": 2048,
        "completion_tokens": 20,
        "total_tokens": 2068,
        "cache_read_tokens": 0,
        "cache_write_tokens": 1024,
        "cache_metrics_reported": True,
    }
    assert missing["cache_metrics_reported"] is False


def test_normalize_gemini_cached_content_tokens():
    usage = normalize_usage({
        "promptTokenCount": 5000,
        "candidatesTokenCount": 40,
        "totalTokenCount": 5040,
        "cachedContentTokenCount": 4096,
    }, provider_type="vertex")

    assert usage["cache_read_tokens"] == 4096
    assert usage["cache_write_tokens"] == 0
    assert usage["cache_metrics_reported"] is True


def test_normalize_native_anthropic_cache_usage():
    usage = normalize_usage({
        "input_tokens": 120,
        "output_tokens": 10,
        "cache_read_input_tokens": 4096,
        "cache_creation_input_tokens": 0,
    }, provider_type="anthropic")

    assert usage["prompt_tokens"] == 120
    assert usage["cache_read_tokens"] == 4096
    assert usage["cache_metrics_reported"] is True


def test_update_usage_meta_keeps_raw_and_persistent_view_is_scalar_only():
    raw = {
        "prompt_tokens": 2048,
        "prompt_tokens_details": {"cached_tokens": 1024},
    }
    meta = {}

    update_usage_meta(meta, raw, provider_type="openai")
    event_meta = provider_event_usage_meta(meta)

    assert meta["raw"] == raw
    assert meta["cache_hit"] is True
    assert event_meta["cache_read_tokens"] == 1024
    assert "raw" not in event_meta


def test_provider_summary_aggregates_only_calls_with_reported_cache_metrics(monkeypatch):
    events = [
        {
            "ts": 1,
            "endpoint_id": "openrouter",
            "endpoint_name": "OpenRouter",
            "provider_type": "openai",
            "model": "openai/gpt-5.6-sol",
            "ok": True,
            "meta": {},
        },
        {
            "ts": 2,
            "endpoint_id": "openrouter",
            "endpoint_name": "OpenRouter",
            "provider_type": "openai",
            "model": "openai/gpt-5.6-sol",
            "ok": True,
            "meta": {
                "prompt_tokens": 2000,
                "cache_read_tokens": 0,
                "cache_write_tokens": 1024,
                "cache_metrics_reported": True,
                "cache_hit": False,
            },
        },
        {
            "ts": 3,
            "endpoint_id": "openrouter",
            "endpoint_name": "OpenRouter",
            "provider_type": "openai",
            "model": "openai/gpt-5.6-sol",
            "ok": True,
            "meta": {
                "prompt_tokens": 3000,
                "cache_read_tokens": 2048,
                "cache_write_tokens": 0,
                "cache_metrics_reported": True,
                "cache_hit": True,
            },
        },
    ]
    monkeypatch.setattr(
        provider_status,
        "recent_provider_events",
        lambda _limit: list(reversed(events)),
    )

    summary = provider_status.summarize_provider_events()[0]

    assert summary["cache_metric_calls"] == 2
    assert summary["cache_hit_calls"] == 1
    assert summary["cache_hit_rate"] == 0.5
    assert summary["cache_token_rate"] == 2048 / 5000
    assert summary["cache_write_tokens"] == 1024
