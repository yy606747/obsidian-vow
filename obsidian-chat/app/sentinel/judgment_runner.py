"""Dry-run runner for Layer 2 Sentinel Judgment provider calls."""

from __future__ import annotations

import inspect
from collections.abc import Awaitable, Callable, Mapping
from typing import Any

from .eval import RUNTIME_MODE_DRY_RUN
from .judgment import build_sentinel_judgment_messages, parse_sentinel_judgment


SENTINEL_JUDGMENT_RUN_SCHEMA_VERSION = "sentinel_judgment_run.v1"
SentinelJudgmentProvider = Callable[[list[dict[str, str]]], Awaitable[str] | str]


async def run_sentinel_judgment_dry_run(
    handoff: Mapping[str, Any],
    *,
    provider: SentinelJudgmentProvider,
    context: Mapping[str, Any] | None = None,
    request_id: str = "",
    tone_hint_max_chars: int | None = None,
) -> dict[str, Any]:
    """Call an injected Sentinel provider and parse its judgment without side effects."""
    if not callable(provider):
        raise ValueError("sentinel judgment provider must be callable")

    messages = build_sentinel_judgment_messages(handoff, context=context)
    raw_result = provider(messages)
    raw_text = await raw_result if inspect.isawaitable(raw_result) else raw_result
    judgment = parse_sentinel_judgment(
        raw_text,
        tone_hint_max_chars=tone_hint_max_chars,
    )
    return {
        "schema_version": SENTINEL_JUDGMENT_RUN_SCHEMA_VERSION,
        "runtime_mode": RUNTIME_MODE_DRY_RUN,
        "request_id": request_id,
        "side_effects": [],
        "fallback_used": False,
        "fallback_reason": "",
        "messages": messages,
        "raw_output": raw_text,
        "judgment": judgment,
    }


__all__ = [
    "SENTINEL_JUDGMENT_RUN_SCHEMA_VERSION",
    "SentinelJudgmentProvider",
    "run_sentinel_judgment_dry_run",
]
