import pytest

from prompt_cache import normalize_usage, provider_event_usage_meta, reset_usage_meta, update_usage_meta
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
        "reasoning_tokens": None,
        "reasoning_metrics_reported": False,
        "billable_output_tokens": 20,
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


@pytest.mark.parametrize("provider", ["gemini", "vertex"])
def test_native_thinking_is_added_only_to_separate_billable_counter(provider):
    usage = normalize_usage({
        "promptTokenCount": 10000, "cachedContentTokenCount": 7000,
        "candidatesTokenCount": 200, "thoughtsTokenCount": 1500,
        "totalTokenCount": 11700,
    }, provider_type=provider)
    assert usage["completion_tokens"] == 200
    assert usage["reasoning_tokens"] == 1500
    assert usage["reasoning_metrics_reported"] is True
    assert usage["billable_output_tokens"] == 1700
    assert usage["total_tokens"] == 11700


@pytest.mark.parametrize("details_key,output_key", [
    ("completion_tokens_details", "completion_tokens"),
    ("output_tokens_details", "output_tokens"),
])
def test_compatible_thinking_is_already_in_output(details_key, output_key):
    usage = normalize_usage({
        "prompt_tokens": 10000, output_key: 1700,
        details_key: {"reasoning_tokens": 1500},
        "prompt_tokens_details": {"cached_tokens": 7000},
    }, provider_type="openai")
    assert usage["completion_tokens"] == 1700
    assert usage["reasoning_tokens"] == 1500
    assert usage["billable_output_tokens"] == 1700
    assert usage["cache_read_tokens"] == 7000


@pytest.mark.parametrize("thoughts,reported,billable", [(None, False, None), (0, True, 200)])
def test_unknown_native_thinking_does_not_become_zero(thoughts, reported, billable):
    usage = normalize_usage({
        "promptTokenCount": 10000, "candidatesTokenCount": 200,
        "thoughtsTokenCount": thoughts, "cachedContentTokenCount": None,
    }, provider_type="vertex")
    assert usage["reasoning_metrics_reported"] is reported
    assert usage["reasoning_tokens"] == thoughts
    assert usage["billable_output_tokens"] == billable
    assert usage["cache_metrics_reported"] is False


def test_stream_usage_merges_partial_frames_without_adding_cumulative_counts():
    meta = {}
    update_usage_meta(meta, {"promptTokenCount": 10000, "cachedContentTokenCount": 7000}, provider_type="vertex")
    last = {"candidatesTokenCount": 200, "thoughtsTokenCount": 1500, "totalTokenCount": 11700}
    update_usage_meta(meta, last, provider_type="vertex")
    update_usage_meta(meta, last, provider_type="vertex")
    assert meta["prompt_tokens"] == 10000
    assert meta["cache_read_tokens"] == 7000
    assert meta["billable_output_tokens"] == 1700
    assert meta["reasoning_tokens"] == 1500
    assert meta["total_tokens"] == 11700
    assert provider_event_usage_meta(meta)["reasoning_tokens"] == 1500


def test_reset_clears_retry_usage_but_keeps_previous_call_records():
    calls = [{"request_id": "previous"}]
    meta = {"provider_calls": calls, "finish_reason": "STOP"}
    update_usage_meta(meta, {
        "prompt_tokens": 10000, "completion_tokens": 1700,
        "completion_tokens_details": {"reasoning_tokens": 1500},
        "prompt_tokens_details": {"cached_tokens": 7000},
    }, provider_type="openai")
    assert reset_usage_meta(meta) is meta
    assert meta == {"provider_calls": calls}
    assert reset_usage_meta(None) == {}
    update_usage_meta(meta, {"prompt_tokens": 20, "completion_tokens": 5}, provider_type="openai")
    assert meta["cache_metrics_reported"] is False
    assert meta["reasoning_tokens"] is None
    assert meta["billable_output_tokens"] == 5


def test_compatible_flat_cache_counter_is_recognized():
    usage = normalize_usage({
        "prompt_tokens": 10000, "prompt_cache_hit_tokens": 7000,
        "prompt_cache_miss_tokens": 3000, "completion_tokens": 200,
    }, provider_type="openai")
    assert usage["cache_read_tokens"] == 7000
    assert usage["cache_metrics_reported"] is True


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
