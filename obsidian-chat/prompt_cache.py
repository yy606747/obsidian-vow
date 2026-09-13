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
USAGE_META_KEYS = (
    "prompt_tokens", "completion_tokens", "total_tokens", "cache_read_tokens",
    "cache_write_tokens", "cache_metrics_reported", "cache_hit",
    "reasoning_tokens", "reasoning_metrics_reported", "billable_output_tokens",
)


def reset_usage_meta(meta: MutableMapping[str, Any] | None) -> MutableMapping[str, Any]:
    """每次实际请求独立计数；重试不继承上次用量，未传统计字典也照常记账。"""
    if meta is None:
        meta = {}
    for key in (*USAGE_META_KEYS, "raw", "finish_reason", "cache_adapter",
                "cache_boundary_found", "cacheable_prefix_chars"):
        meta.pop(key, None)
    return meta


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
        thoughts = usage.get("thoughtsTokenCount", usage.get("thoughts_token_count"))
        output = usage.get("candidatesTokenCount", usage.get("candidates_token_count"))
        reasoning = _non_negative_int(thoughts) if thoughts is not None else None
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
            "cache_metrics_reported": any(usage.get(name) is not None for name in cache_fields),
            "reasoning_tokens": reasoning,
            "reasoning_metrics_reported": reasoning is not None,
            # 保留 completion_tokens 的原有含义；新增计费输出，避免漏算思考。
            "billable_output_tokens": (
                _non_negative_int(output) + reasoning
                if output is not None and reasoning is not None else None
            ),
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
            "reasoning_tokens": None,
            "reasoning_metrics_reported": False,
            "billable_output_tokens": (
                completion_tokens if usage.get("output_tokens") is not None else None
            ),
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
    output_details = usage.get("completion_tokens_details") or usage.get("output_tokens_details")
    output_details = output_details if isinstance(output_details, Mapping) else {}
    thoughts = output_details.get("reasoning_tokens")
    reasoning = _non_negative_int(thoughts) if thoughts is not None else None
    cache_read = details.get("cached_tokens", usage.get("prompt_cache_hit_tokens"))
    return {
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "total_tokens": _non_negative_int(
            usage.get("total_tokens", prompt_tokens + completion_tokens)
        ),
        "cache_read_tokens": _non_negative_int(cache_read),
        "cache_write_tokens": _non_negative_int(
            details.get("cache_write_tokens", details.get("cache_creation_tokens"))
        ),
        "cache_metrics_reported": (
            any(details.get(name) is not None for name in cache_fields)
            or usage.get("prompt_cache_hit_tokens") is not None
        ),
        "reasoning_tokens": reasoning,
        "reasoning_metrics_reported": reasoning is not None,
        # 兼容协议的 completion_tokens 已含思考，不能再加 reasoning_tokens。
        "billable_output_tokens": (
            completion_tokens
            if usage.get("completion_tokens", usage.get("output_tokens")) is not None
            else None
        ),
    }


def update_usage_meta(
    meta: MutableMapping[str, Any] | None,
    raw: Mapping[str, Any] | None,
    *,
    provider_type: str,
) -> dict:
    """Update the debug/stream metadata and return the normalized values."""

    # 流式元数据是累计值，但不同帧可能只带部分字段；合并字段，不累加令牌数。
    merged = dict((meta or {}).get("raw") or {})
    for key, value in (raw or {}).items():
        if value is None:
            continue
        if isinstance(value, Mapping) and isinstance(merged.get(key), Mapping):
            merged[key] = {**merged[key], **value}
        else:
            merged[key] = value
    normalized = normalize_usage(merged, provider_type=provider_type)
    if meta is not None:
        meta.update(normalized)
        meta["cache_hit"] = normalized["cache_read_tokens"] > 0
        meta["raw"] = merged
    return normalized


def provider_event_usage_meta(meta: Mapping[str, Any] | None) -> dict:
    """Extract only safe scalar counters for persistent provider diagnostics."""

    if not isinstance(meta, Mapping):
        return {}
    keys = (
        *USAGE_META_KEYS,
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
