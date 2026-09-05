from ai_providers import _normalize_for_claude_like, build_multimodal_messages
from prompt_cache import (
    CACHE_BOUNDARY_KEY,
    CACHE_SESSION_KEY,
    cache_request_policy,
)


def _marked_messages():
    return [
        {
            "role": "user",
            "content": "stable prefix",
            CACHE_BOUNDARY_KEY: True,
            CACHE_SESSION_KEY: "chat:conv-1",
        },
        {"role": "assistant", "content": "ack"},
        {"role": "user", "content": "runtime\n\nquestion"},
    ]


def test_openrouter_gpt56_uses_explicit_breakpoint_and_sticky_session():
    messages = _marked_messages()
    policy = cache_request_policy(
        base_url="https://openrouter.ai/api/v1",
        model="openai/gpt-5.6-sol",
        messages=messages,
    )
    converted = build_multimodal_messages(
        messages,
        marker_style=policy["marker_style"],
    )

    assert policy["cache_adapter"] == "openai_explicit"
    assert policy["request_fields"] == {
        "session_id": "chat:conv-1",
        "prompt_cache_key": "chat:conv-1",
        "prompt_cache_options": {"mode": "explicit"},
    }
    assert converted[0]["content"][0]["prompt_cache_breakpoint"] == {
        "mode": "explicit"
    }
    assert CACHE_BOUNDARY_KEY not in converted[0]
    assert CACHE_SESSION_KEY not in converted[0]
    assert isinstance(converted[2]["content"], str)


def test_openrouter_anthropic_uses_explicit_cache_control():
    policy = cache_request_policy(
        base_url="https://openrouter.ai/api/v1",
        model="anthropic/claude-opus-5",
        messages=_marked_messages(),
    )
    converted = build_multimodal_messages(
        _marked_messages(),
        marker_style=policy["marker_style"],
    )

    assert policy["cache_adapter"] == "anthropic_explicit"
    assert policy["request_fields"] == {"session_id": "chat:conv-1"}
    assert converted[0]["content"][0]["cache_control"] == {"type": "ephemeral"}


def test_openrouter_gemini_uses_explicit_cache_control():
    policy = cache_request_policy(
        base_url="https://openrouter.ai/api/v1",
        model="google/gemini-3.1-pro-preview",
        messages=_marked_messages(),
    )
    converted = build_multimodal_messages(
        _marked_messages(),
        marker_style=policy["marker_style"],
    )

    assert policy["cache_adapter"] == "gemini_openrouter_explicit"
    assert policy["request_fields"] == {"session_id": "chat:conv-1"}
    assert converted[0]["content"][0]["cache_control"] == {"type": "ephemeral"}


def test_native_vertex_keeps_implicit_caching_and_reports_boundary_size():
    policy = cache_request_policy(
        base_url="https://aiplatform.googleapis.com/v1/projects/p/locations/l/publishers/google",
        model="gemini-3.1-pro-preview",
        endpoint_type="vertex",
        messages=_marked_messages(),
    )

    assert policy["cache_adapter"] == "gemini_implicit"
    assert policy["marker_style"] == ""
    assert policy["request_fields"] == {}
    assert policy["cacheable_prefix_chars"] == len("stable prefix")


def test_normalization_preserves_cache_marker_when_same_roles_merge():
    messages = [
        {"role": "user", "content": "one"},
        {
            "role": "user",
            "content": "two",
            CACHE_BOUNDARY_KEY: True,
            CACHE_SESSION_KEY: "chat:merge",
        },
        {"role": "assistant", "content": "ack"},
        {"role": "user", "content": "question"},
    ]

    normalized = _normalize_for_claude_like(messages)

    assert normalized[0]["content"] == "one\n\ntwo"
    assert normalized[0][CACHE_BOUNDARY_KEY] is True
    assert normalized[0][CACHE_SESSION_KEY] == "chat:merge"


def test_unknown_openai_compatible_gateway_does_not_receive_vendor_fields():
    policy = cache_request_policy(
        base_url="https://example-gateway.invalid/v1",
        model="vendor/model",
        messages=_marked_messages(),
    )
    converted = build_multimodal_messages(
        _marked_messages(),
        marker_style=policy["marker_style"],
    )

    assert policy["cache_adapter"] == "implicit"
    assert policy["request_fields"] == {}
    assert isinstance(converted[0]["content"], str)
    assert CACHE_BOUNDARY_KEY not in converted[0]
    assert CACHE_SESSION_KEY not in converted[0]
