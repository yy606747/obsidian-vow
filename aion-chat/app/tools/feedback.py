"""Normalized next-turn feedback for main-chat side effects."""

from __future__ import annotations

import json
import logging
from collections.abc import Collection, Mapping

from .registry import next_turn_feedback_tools


logger = logging.getLogger(__name__)

_OUTCOMES = {
    "succeeded",
    "failed",
    "rejected",
    "dispatched",
    "pending",
    "unknown",
}


def _decoded(value: str) -> dict:
    try:
        payload = json.loads(value or "{}")
    except Exception:
        return {}
    return dict(payload) if isinstance(payload, Mapping) else {}


def _reason(error: str, summary_json: str) -> str:
    error = " ".join(str(error or "").split())
    if error:
        return error[:300]
    summary = _decoded(summary_json)
    for key in ("reason", "reject_reason", "message", "error"):
        value = " ".join(str(summary.get(key) or "").split())
        if value:
            return value[:300]
    return ""


def format_feedback_rows(rows: Collection[Mapping]) -> str:
    lines: list[str] = []
    for row in rows:
        outcome = str(row.get("outcome") or "unknown").strip().lower()
        if outcome == "not_executed":
            outcome = "unknown"
        if outcome not in _OUTCOMES:
            outcome = "unknown"
        normalized = {
            "tool": str(row.get("tool_name") or "unknown"),
            "status": "ok" if outcome == "succeeded" else outcome,
        }
        if outcome in {"failed", "rejected"}:
            reason = _reason(
                str(row.get("error") or ""),
                str(row.get("result_summary") or ""),
            )
            if reason:
                normalized["reason"] = reason
        if normalized["tool"] == "self_wake.schedule" and outcome == "succeeded":
            summary = _decoded(str(row.get("result_summary") or ""))
            replaced_id = str(summary.get("replaced_wake_id") or "").strip()
            if replaced_id:
                normalized["replaced_wake_id"] = replaced_id
                replaced_at = summary.get("replaced_wake_at")
                if replaced_at is not None:
                    normalized["replaced_wake_at"] = replaced_at
        lines.append(json.dumps(normalized, ensure_ascii=False, separators=(",", ":")))
    if not lines:
        return ""
    return (
        "[上一轮能力执行结果]\n"
        "以下是执行层的规范化真实结果，不是模型原始标记：\n"
        + "\n".join(lines)
    )


async def build_previous_turn_feedback(
    *,
    conv_id: str,
    assistant_message_ids: Collection[str],
) -> str:
    """Read one previous logical turn; failures leave prompt behavior unchanged."""

    message_ids = tuple(
        dict.fromkeys(
            str(item).strip() for item in assistant_message_ids if str(item).strip()
        )
    )
    tools = tuple(sorted(next_turn_feedback_tools()))
    if not conv_id or not message_ids or not tools:
        return ""
    placeholders_messages = ",".join("?" for _ in message_ids)
    placeholders_tools = ",".join("?" for _ in tools)
    sql = (
        "SELECT tool_name, outcome, error, result_summary, intent_id, created_at "
        "FROM tool_invocation_events WHERE conv_id=? AND source_chain='main' "
        "AND stage='execution' "
        f"AND assistant_message_id IN ({placeholders_messages}) "
        f"AND tool_name IN ({placeholders_tools}) "
        "ORDER BY created_at, intent_id"
    )
    try:
        import aiosqlite

        from database import get_db

        async with get_db() as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                sql,
                (conv_id, *message_ids, *tools),
            )
            rows = [dict(row) for row in await cursor.fetchall()]
        return format_feedback_rows(rows)
    except Exception:
        logger.warning("tool-result feedback read failed", exc_info=True)
        return ""


__all__ = ["build_previous_turn_feedback", "format_feedback_rows"]
