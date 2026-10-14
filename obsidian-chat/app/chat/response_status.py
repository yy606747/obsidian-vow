"""Foreground reply completion and the single empty-reply retry decision."""

from contextvars import ContextVar

from .error_text import looks_like_model_error_text


# The foreground chat owns its final error UI while it may still recover.
defer_empty_notice: ContextVar[bool] = ContextVar("defer_empty_notice", default=False)


def is_filtered(usage: dict) -> bool:
    return str(usage.get("finish_reason") or "").lower() == "content_filter"


def has_delivery_candidate(reply) -> bool:
    # These outlets can legitimately deliver without ordinary chat text.
    return bool(
        reply.content.strip()
        or reply.music_cards
        or getattr(getattr(reply, "vow", None), "found", False)
        or any(intent.tool_name in {
            "music.search", "sticker.send", "memory.view_image", "mobile.look_at_me",
        } for intent in reply.tool_intents)
    )


def retry_empty_reply(raw: str, reply, usage: dict) -> bool:
    # Native providers may already have used their one retry.
    if len(usage.get("provider_calls") or []) > 1:
        return False
    if is_filtered(usage):
        return not reply.content.strip() or looks_like_model_error_text(raw)
    if looks_like_model_error_text(raw) and raw.strip():
        return (usage.get("provider_last") or {}).get("error_type") == "empty_response" or "empty_response" in raw
    return not has_delivery_candidate(reply)


def merge_retry_usage(usage: dict, attempts: list[dict]) -> None:
    """Keep final-attempt state, while counting both requests' reported usage."""
    if len(attempts) < 2:
        return
    for key in ("prompt_tokens", "completion_tokens", "total_tokens", "billable_output_tokens",
                "cache_read_tokens", "cache_write_tokens", "reasoning_tokens"):
        values = [attempt.get(key) for attempt in attempts]
        if all(isinstance(value, (int, float)) for value in values):
            usage[key] = sum(values)
        else:
            usage.pop(key, None)
    for key in ("cache_metrics_reported", "reasoning_metrics_reported"):
        usage[key] = all(attempt.get(key) is True for attempt in attempts)
    if usage["cache_metrics_reported"]:
        usage["cache_hit"] = any(attempt.get("cache_hit") for attempt in attempts)
    else:
        usage.pop("cache_hit", None)
    usage["provider_calls"] = [call for attempt in attempts for call in attempt.get("provider_calls", [])]
    usage["auto_retry_count"] = len(attempts) - 1
