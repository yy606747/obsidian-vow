"""Prompt layout helpers for provider prefix caching.

Private keys added here are consumed by ``ai_providers`` and never sent as
message fields.  Keeping the marker on the message lets normalization retain
the exact semantic boundary without coupling chat orchestration to a provider.
"""

from __future__ import annotations

from typing import Any

from prompt_cache import CACHE_BOUNDARY_KEY, CACHE_SESSION_KEY


def latest_user_index(history: list[dict]) -> int:
    """Return the current/trigger user message that runtime blocks precede."""

    for index in range(len(history) - 1, -1, -1):
        if history[index].get("role") == "user":
            return index
    raise ValueError("provider prompt has no user message")


def _content_chars(content: Any) -> int:
    if isinstance(content, str):
        return len(content)
    if isinstance(content, list):
        return sum(
            len(str(part.get("text") or ""))
            for part in content
            if isinstance(part, dict)
        )
    return len(str(content or ""))


def mark_cache_boundary(
    history: list[dict],
    *,
    before_index: int,
    session_id: str,
) -> dict:
    """Mark the last reusable text block in the truly stable prefix.

    A user block is preferred because every Chat Completions-compatible
    provider accepts text content parts on user messages.  Falling back to an
    assistant block keeps prompts cacheable even if a custom caller omitted
    the standard stable ability pair.  Callers must not include rolling chat
    history in ``before_index``: GPT-5.6 exact-breakpoint caching would turn
    every conversation turn into a different cache entry.
    """

    for message in history:
        message.pop(CACHE_BOUNDARY_KEY, None)
        message.pop(CACHE_SESSION_KEY, None)

    candidates = range(min(before_index, len(history)) - 1, -1, -1)
    boundary_index = next(
        (
            index
            for index in candidates
            if history[index].get("role") == "user"
            and _content_chars(history[index].get("content")) > 0
        ),
        None,
    )
    if boundary_index is None:
        boundary_index = next(
            (
                index
                for index in range(min(before_index, len(history)) - 1, -1, -1)
                if _content_chars(history[index].get("content")) > 0
            ),
            None,
        )
    if boundary_index is None:
        return {
            "boundary_message_index": None,
            "cacheable_prefix_chars": 0,
            "session_id": str(session_id or ""),
        }

    boundary = history[boundary_index]
    boundary[CACHE_BOUNDARY_KEY] = True
    boundary[CACHE_SESSION_KEY] = str(session_id or "")[:256]
    return {
        "boundary_message_index": boundary_index,
        "cacheable_prefix_chars": sum(
            _content_chars(message.get("content"))
            for message in history[: boundary_index + 1]
        ),
        "session_id": str(session_id or "")[:256],
    }


def cache_session_id(messages: list[dict]) -> str:
    for message in messages:
        value = str(message.get(CACHE_SESSION_KEY) or "").strip()
        if value:
            return value[:256]
    return ""
