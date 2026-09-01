"""Provider-neutral prompt-cache telemetry helpers.

This module deliberately does not change provider requests.  It only normalizes
the different usage payloads returned by OpenAI-compatible, Anthropic and
Gemini endpoints so the pre-change baseline can be measured reliably.
"""

from __future__ import annotations

from typing import Any, Mapping, MutableMapping, Sequence
from urllib.parse import urlparse


CACHE_BOUNDARY_KEY = "_prompt_cache_boundary"
CACHE_SESSION_KEY = "_prompt_cache_session"


def _non_negative_int(value: Any) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


def normalize_usage(raw: Mapping[str, Any] | None, *, provider_type: str) -> dict:
    """Return one cache-usage shape without guessing omitted provider fields.

    ``cache_metrics_reported`` distinguishes an explicit zero from a provider
    response that omitted cache details altogether.  The latter must not be
    counted as a miss when calculating a hit rate.
    """

    usage = dict(raw or {})
    provider = str(provider_type or "").lower()

    if provider in {"gemini", "vertex"} or "promptTokenCount" in usage:
        cache_fields = ("cachedContentTokenCount", "cached_content_token_count")
        cache_read = next(
            (_non_negative_int(usage[name]) for name in cache_fields if name in usage),
            0,
        )
        return {
            "prompt_tokens": _non_negative_int(
                usage.get("promptTokenCount", usage.get("prompt_token_count"))
            ),
            "completion_tokens": _non_negative_int(
                usage.get("candidatesTokenCount", usage.get("candidates_token_count"))
            ),
            "total_tokens": _non_negative_int(
                usage.get("totalTokenCount", usage.get("total_token_count"))
            ),
            "cache_read_tokens": cache_read,
            "cache_write_tokens": 0,
            "cache_metrics_reported": any(name in usage for name in cache_fields),
        }

    # Native Anthropic Messages usage.  OpenRouter's Chat Completions endpoint
    # normally translates this to prompt_tokens_details, but accepting both
    # shapes keeps telemetry correct for future direct endpoints.
    anthropic_fields = (
        "cache_read_input_tokens",
        "cache_creation_input_tokens",
        "cache_write_input_tokens",
    )
    if provider == "anthropic" or any(name in usage for name in anthropic_fields):
        cache_write = _non_negative_int(
            usage.get(
                "cache_creation_input_tokens",
                usage.get("cache_write_input_tokens"),
            )
        )
        prompt_tokens = _non_negative_int(usage.get("input_tokens"))
        completion_tokens = _non_negative_int(usage.get("output_tokens"))
        return {
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": _non_negative_int(
                usage.get("total_tokens", prompt_tokens + completion_tokens)
            ),
            "cache_read_tokens": _non_negative_int(
                usage.get("cache_read_input_tokens")
            ),
            "cache_write_tokens": cache_write,
            "cache_metrics_reported": any(name in usage for name in anthropic_fields),
        }

    details = usage.get("prompt_tokens_details") or usage.get("input_tokens_details")
    details = details if isinstance(details, Mapping) else {}
    cache_fields = (
        "cached_tokens",
        "cache_write_tokens",
        "cache_creation_tokens",
    )
    prompt_tokens = _non_negative_int(
        usage.get("prompt_tokens", usage.get("input_tokens"))
    )
    completion_tokens = _non_negative_int(
        usage.get("completion_tokens", usage.get("output_tokens"))
    )
    return {
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "total_tokens": _non_negative_int(
            usage.get("total_tokens", prompt_tokens + completion_tokens)
        ),
        "cache_read_tokens": _non_negative_int(details.get("cached_tokens")),
        "cache_write_tokens": _non_negative_int(
            details.get("cache_write_tokens", details.get("cache_creation_tokens"))
        ),
        "cache_metrics_reported": any(name in details for name in cache_fields),
    }


def update_usage_meta(
    meta: MutableMapping[str, Any] | None,
    raw: Mapping[str, Any] | None,
    *,
    provider_type: str,
) -> dict:
    """Update the debug/stream metadata and return the normalized values."""

    normalized = normalize_usage(raw, provider_type=provider_type)
    if meta is not None:
        meta.update(normalized)
        meta["cache_hit"] = normalized["cache_read_tokens"] > 0
        meta["raw"] = dict(raw or {})
    return normalized


def provider_event_usage_meta(meta: Mapping[str, Any] | None) -> dict:
    """Extract only safe scalar counters for persistent provider diagnostics."""

    if not isinstance(meta, Mapping):
        return {}
    keys = (
        "prompt_tokens",
        "completion_tokens",
        "total_tokens",
        "cache_read_tokens",
        "cache_write_tokens",
        "cache_metrics_reported",
        "cache_hit",
        "cache_adapter",
        "cache_boundary_found",
        "cacheable_prefix_chars",
        "finish_reason",
    )
    return {key: meta[key] for key in keys if key in meta}


def cache_boundary_info(messages: Sequence[Mapping[str, Any]]) -> dict:
    """Describe the internal boundary without exposing prompt text."""

    prefix_chars = 0
    for index, message in enumerate(messages):
        content = message.get("content")
        if isinstance(content, str):
            prefix_chars += len(content)
        elif isinstance(content, list):
            prefix_chars += sum(
                len(str(part.get("text") or ""))
                for part in content
                if isinstance(part, Mapping)
            )
        if message.get(CACHE_BOUNDARY_KEY):
            return {
                "cache_boundary_found": True,
                "cache_boundary_index": index,
                "cacheable_prefix_chars": prefix_chars,
                "session_id": str(message.get(CACHE_SESSION_KEY) or "")[:256],
            }
    return {
        "cache_boundary_found": False,
        "cache_boundary_index": None,
        "cacheable_prefix_chars": 0,
        "session_id": "",
    }


def _endpoint_host(base_url: str) -> str:
    try:
        return (urlparse(str(base_url or "")).hostname or "").lower()
    except Exception:
        return ""


def _is_gpt_56_or_newer(model: str) -> bool:
    # Explicit caching is currently an OpenAI GPT-5.6+ feature.  Keep this
    # intentionally narrow so older OpenAI-compatible gateways never receive
    # request fields they may reject.
    normalized = str(model or "").lower()
    return "gpt-5.6" in normalized


def cache_request_policy(
    *,
    base_url: str,
    model: str,
    endpoint_type: str = "openai",
    messages: Sequence[Mapping[str, Any]] = (),
) -> dict:
    """Choose provider-specific request fields and content-block marker style."""

    boundary = cache_boundary_info(messages)
    host = _endpoint_host(base_url)
    normalized_model = str(model or "").lower()
    is_openrouter = host == "openrouter.ai" or host.endswith(".openrouter.ai")
    is_openai = host == "api.openai.com" or host.endswith(".openai.com")
    session_id = boundary["session_id"]
    request_fields: dict[str, Any] = {}
    marker_style = ""
    adapter = "implicit"

    if is_openrouter and session_id:
        request_fields["session_id"] = session_id

    if endpoint_type in {"gemini", "vertex"}:
        adapter = "gemini_implicit"
    elif (is_openrouter or is_openai) and _is_gpt_56_or_newer(normalized_model):
        adapter = "openai_explicit"
        marker_style = "prompt_cache_breakpoint"
        if session_id:
            request_fields["prompt_cache_key"] = session_id
        request_fields["prompt_cache_options"] = {"mode": "explicit"}
    elif is_openrouter and (
        normalized_model.startswith("anthropic/") or "claude" in normalized_model
    ):
        adapter = "anthropic_explicit"
        marker_style = "cache_control"
    elif is_openrouter and (
        normalized_model.startswith("google/") or "gemini" in normalized_model
    ):
        adapter = "gemini_openrouter_explicit"
        marker_style = "cache_control"
    elif is_openrouter:
        adapter = "openrouter_implicit"
    elif is_openai:
        adapter = "openai_implicit"

    if not boundary["cache_boundary_found"]:
        marker_style = ""
        # Explicit mode without a breakpoint would suppress OpenAI's automatic
        # breakpoint selection, so fall back safely for malformed callers.
        request_fields.pop("prompt_cache_options", None)

    return {
        **boundary,
        "cache_adapter": adapter,
        "marker_style": marker_style,
        "request_fields": request_fields,
    }


def update_cache_policy_meta(
    meta: MutableMapping[str, Any] | None,
    policy: Mapping[str, Any],
) -> None:
    if meta is None:
        return
    for key in (
        "cache_adapter",
        "cache_boundary_found",
        "cacheable_prefix_chars",
    ):
        if key in policy:
            meta[key] = policy[key]
