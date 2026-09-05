"""
Memory diagnostics helpers.

Batch 2.3f keeps recall tracing read-only: it explains V2 planner decisions
without writing memory_usage or changing prompt injection.
"""

from __future__ import annotations

import hashlib
import json
import time


def empty_trace() -> dict:
    return {
        "planner": "legacy",
        "steps": [],
        "notes": "Batch 2.0 uses legacy memory.py behavior behind MemoryService.",
    }


def _preview(text: str, length: int = 120) -> str:
    normalized = " ".join((text or "").split())
    if len(normalized) <= length:
        return normalized
    return f"{normalized[:length - 1]}..."


def _trace_id(query: str) -> str:
    seed = f"{time.time_ns()}:{query}".encode("utf-8", errors="ignore")
    return f"recall_{hashlib.sha1(seed).hexdigest()[:12]}"


def infer_abstain_reason(plan_result: dict) -> str | None:
    turn_plan = plan_result.get("turn_plan") or {}
    selected = plan_result.get("selected") or []
    candidate_count = int(plan_result.get("candidate_count") or 0)
    if selected:
        return None
    if not turn_plan.get("needs_memory"):
        return "no_memory_signal"
    if candidate_count == 0:
        return "no_candidates"
    return "no_positive_score"


def summarize_recall_item(item: dict, *, preview_length: int = 120) -> dict:
    """Return a UI-safe scored-memory summary with short content preview only."""
    return {
        "id": item.get("id"),
        "legacy_memory_id": item.get("legacy_memory_id"),
        "namespace": item.get("namespace"),
        "secondary_namespaces": item.get("secondary_namespaces") or [],
        "kind": item.get("kind"),
        "score": item.get("score"),
        "relevance": item.get("relevance"),
        "semantic_similarity": item.get("semantic_similarity"),
        "keyword_relevance": item.get("keyword_relevance"),
        "emotion": item.get("emotion") or "",
        "emotion_resonance": item.get("emotion_resonance"),
        "importance": item.get("importance"),
        "confidence": item.get("confidence"),
        "reason": item.get("reason") or "",
        "preview": _preview(item.get("content") or "", preview_length),
        "created_at": item.get("created_at"),
    }


def build_recall_trace(
    plan_result: dict,
    *,
    classification: dict | None = None,
    taxonomy_source: str = "",
    max_debug_items: int = 12,
) -> dict:
    turn_plan = plan_result.get("turn_plan") or {}
    selected = plan_result.get("selected") or []
    debug_top = plan_result.get("debug_top") or []
    abstain_reason = plan_result.get("abstain_reason")
    if abstain_reason is None:
        abstain_reason = infer_abstain_reason(plan_result)

    candidate_count = int(plan_result.get("candidate_count") or 0)
    selected_count = len(selected)
    status = "selected" if selected_count else "abstained"
    classification = classification or {}
    trace = {
        "planner": "memory_v2",
        "schema_version": "2.3f",
        "trace_id": _trace_id(plan_result.get("query") or ""),
        "taxonomy_source": taxonomy_source,
        "query": plan_result.get("query") or "",
        "query_preview": _preview(plan_result.get("query") or "", 160),
        "keywords": plan_result.get("keywords") or [],
        "classification": {
            "query_type": classification.get("query_type") or "normal",
            "needs_memory": bool(classification.get("needs_memory", turn_plan.get("needs_memory"))),
            "is_open_loop_query": bool(classification.get("is_open_loop_query", False)),
            "namespace_hits": classification.get("namespace_hits") or {},
            "keywords": classification.get("keywords") or [],
        },
        "turn_plan": {
            "mode": turn_plan.get("mode") or "normal",
            "namespace": turn_plan.get("namespace") or "normal",
            "detected_namespaces": turn_plan.get("detected_namespaces") or [],
            "preferred_kinds": turn_plan.get("preferred_kinds") or [],
            "needs_memory": bool(turn_plan.get("needs_memory")),
            "emotion": turn_plan.get("emotion") or "",
            "terms": turn_plan.get("terms") or [],
        },
        "allowed_namespaces": plan_result.get("allowed_namespaces") or [],
        "candidate_count": candidate_count,
        "selected_count": selected_count,
        "semantic_query": bool(plan_result.get("semantic_query")),
        "abstain_reason": abstain_reason,
        "selected": [summarize_recall_item(item) for item in selected],
        "debug_top": [
            summarize_recall_item(item, preview_length=96)
            for item in debug_top[:max_debug_items]
        ],
        "steps": [],
    }
    trace["steps"] = [
        {
            "name": "classify_query",
            "status": "done",
            "data": trace["classification"],
        },
        {
            "name": "turn_plan",
            "status": "done",
            "data": trace["turn_plan"],
        },
        {
            "name": "namespace_gate",
            "status": "done",
            "data": {"allowed_namespaces": trace["allowed_namespaces"]},
        },
        {
            "name": "candidate_fetch",
            "status": "skipped" if not trace["turn_plan"]["needs_memory"] else "done",
            "data": {"candidate_count": candidate_count},
        },
        {
            "name": "score_and_rank",
            "status": "skipped" if candidate_count == 0 else "done",
            "data": {"debug_top_count": len(trace["debug_top"])},
        },
        {
            "name": "select",
            "status": status,
            "data": {
                "selected_count": selected_count,
                "abstain_reason": abstain_reason,
            },
        },
    ]
    return trace


def summarize_recall_plan(plan_result: dict | None, *, max_items: int = 5) -> dict | None:
    if not plan_result:
        return None
    turn_plan = plan_result.get("turn_plan") or {}
    return {
        "query_preview": _preview(plan_result.get("query") or "", 160),
        "keywords": plan_result.get("keywords") or [],
        "needs_memory": bool(turn_plan.get("needs_memory")),
        "namespace": turn_plan.get("namespace") or "normal",
        "detected_namespaces": turn_plan.get("detected_namespaces") or [],
        "preferred_kinds": turn_plan.get("preferred_kinds") or [],
        "emotion": turn_plan.get("emotion") or "",
        "allowed_namespaces": plan_result.get("allowed_namespaces") or [],
        "candidate_count": int(plan_result.get("candidate_count") or 0),
        "selected_count": len(plan_result.get("selected") or []),
        "semantic_query": bool(plan_result.get("semantic_query")),
        "abstain_reason": plan_result.get("abstain_reason"),
        "selected": [
            summarize_recall_item(item, preview_length=96)
            for item in (plan_result.get("selected") or [])[:max_items]
        ],
        "debug_top": [
            summarize_recall_item(item, preview_length=72)
            for item in (plan_result.get("debug_top") or [])[:max_items]
        ],
    }


def _cell(value) -> str:
    if isinstance(value, (list, dict)):
        value = json.dumps(value, ensure_ascii=False)
    if value is None:
        return "-"
    return str(value).replace("|", "\\|").replace("\n", " ")


def _md(value) -> str:
    return _cell(value)


def format_recall_trace_markdown(trace: dict) -> str:
    classification = trace.get("classification") or {}
    turn_plan = trace.get("turn_plan") or {}
    rows = [
        ("trace_id", trace.get("trace_id")),
        ("query_type", classification.get("query_type")),
        ("needs_memory", turn_plan.get("needs_memory")),
        ("is_open_loop_query", classification.get("is_open_loop_query")),
        ("namespace", turn_plan.get("namespace")),
        ("detected_namespaces", turn_plan.get("detected_namespaces")),
        ("preferred_kinds", turn_plan.get("preferred_kinds")),
        ("emotion", turn_plan.get("emotion")),
        ("allowed_namespaces", trace.get("allowed_namespaces")),
        ("candidate_count", trace.get("candidate_count")),
        ("selected_count", trace.get("selected_count")),
        ("semantic_query", trace.get("semantic_query")),
        ("abstain_reason", trace.get("abstain_reason")),
    ]
    lines = [
        "# Memory V2 Recall Trace",
        "",
        f"planner: `{trace.get('planner')}`",
        f"schema_version: `{trace.get('schema_version')}`",
        f"taxonomy_source: `{trace.get('taxonomy_source') or '-'}`",
        "",
        "## Query",
        "",
        "```text",
        trace.get("query_preview") or "",
        "```",
        "",
        "## Decision",
        "",
        "| field | value |",
        "| --- | --- |",
    ]
    for key, value in rows:
        lines.append(f"| {key} | `{_cell(value)}` |")

    lines += [
        "",
        "## Steps",
        "",
        "| step | status | data |",
        "| --- | --- | --- |",
    ]
    for step in trace.get("steps") or []:
        lines.append(
            f"| {step.get('name')} | {step.get('status')} | "
            f"`{_cell(step.get('data'))}` |"
        )

    lines += [
        "",
        "## Selected",
        "",
    ]
    if trace.get("selected"):
        lines += [
            "| rank | id | namespace | kind | score | reason | preview |",
            "| ---: | --- | --- | --- | ---: | --- | --- |",
        ]
        for index, item in enumerate(trace["selected"], 1):
            lines.append(
                f"| {index} | {item.get('legacy_memory_id') or item.get('id')} "
                f"| {item.get('namespace')} | {item.get('kind')} | {item.get('score')} "
                f"| {_md(item.get('reason') or '-')} | {_md(_preview(item.get('preview') or '', 80))} |"
            )
    else:
        lines.append("_No selected memories._")

    lines += [
        "",
        "## Debug Top",
        "",
    ]
    if trace.get("debug_top"):
        lines += [
            "| rank | id | namespace | kind | score | semantic | keyword | reason | preview |",
            "| ---: | --- | --- | --- | ---: | ---: | ---: | --- | --- |",
        ]
        for index, item in enumerate(trace["debug_top"], 1):
            lines.append(
                f"| {index} | {item.get('legacy_memory_id') or item.get('id')} "
                f"| {item.get('namespace')} | {item.get('kind')} | {item.get('score')} "
                f"| {item.get('semantic_similarity')} | {item.get('keyword_relevance')} "
                f"| {_md(item.get('reason') or '-')} "
                f"| {_md(_preview(item.get('preview') or '', 80))} |"
            )
    else:
        lines.append("_No ranked candidates._")
    lines.append("")
    return "\n".join(lines)
