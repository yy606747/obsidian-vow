"""Shared final eligibility rules for ordinary Memory V2 prompt items."""

from __future__ import annotations

from typing import Any


def item_score(item: dict[str, Any]) -> float:
    try:
        return float(item.get("score") or 0.0)
    except (TypeError, ValueError):
        return 0.0


def semantic_query_available(plan_result: dict | None) -> bool:
    """Read the explicit semantic-query contract.

    Older/manual plans did not carry ``semantic_query``.  Treat a missing key
    as available for compatibility; the production hybrid planner always
    writes a real boolean, including ``False`` on embedding failure.
    """

    if not isinstance(plan_result, dict) or "semantic_query" not in plan_result:
        return True
    return bool(plan_result.get("semantic_query"))


def prompt_eligible(
    item: dict[str, Any],
    *,
    min_score: float,
    semantic_available: bool,
) -> bool:
    """Return whether an item may consume a final ordinary-RAG prompt slot.

    Selector-approved pending recall is a separate route and keeps its explicit
    prompt priority.  Ordinary chunks/notes and AI notes fail closed when the
    semantic query embedding is unavailable; keyword fallback candidates may
    remain visible to planner diagnostics but cannot leak into the prompt.
    """

    if int(item.get("prompt_priority") or 0) > 0:
        return True
    if not semantic_available:
        return False
    return item_score(item) >= float(min_score)


__all__ = ["item_score", "prompt_eligible", "semantic_query_available"]
