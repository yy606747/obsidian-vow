"""
Offline replay for Memory V2 prompt block readiness.

Batch 2.6d uses a copied SQLite database and historical user messages to check
what V2 would put into the chat prompt. It does not call any model and does not
send chat requests.
"""

from __future__ import annotations

import argparse
import asyncio
from collections import Counter
from datetime import datetime
import json
from pathlib import Path
from statistics import mean

import database

from app.memory_v2.migrations import migrate_legacy_memories, migration_status
from app.memory_v2.prompt_block import (
    DEFAULT_MAX_BLOCK_CHARS,
    DEFAULT_MAX_ITEM_CHARS,
    DEFAULT_MAX_ITEMS,
    REALTIME_GUARDED_NAMESPACES,
)
from app.memory_v2.recall_config import (
    normalize_recall_config,
    prompt_injection_decision,
    recall_runtime,
)
from app.memory_v2.service import MemoryService
from app.memory_v2.taxonomy import (
    PROTECTED_CONTENT_HINTS,
    TAXONOMY_SOURCE,
    classify_query,
    contains_any,
)

from .replay_eval import fetch_user_messages
from .v2_recall import V2RecallPlanner
from .migration_compare import configure_db_path, file_sha256, temporary_db


DEFAULT_REPLAY_MODES = ("debug", "canary", "full")


def _preview(text: str, length: int = 90) -> str:
    normalized = " ".join((text or "").split())
    if len(normalized) <= length:
        return normalized
    return normalized[: length - 1] + "..."


def _percentile(values: list[int | float], ratio: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, round((len(ordered) - 1) * ratio)))
    return round(float(ordered[index]), 2)


def _query_mode(classification: dict) -> str:
    return "intimate" if classification.get("query_type") == "intimate" else "normal"


def _query_namespace(classification: dict) -> str | None:
    query_type = classification.get("query_type") or "normal"
    return query_type if query_type != "normal" else None


def _content_lookup(plan: dict) -> dict[str, str]:
    out = {}
    for item in (plan.get("selected") or []) + (plan.get("debug_top") or []):
        if item.get("id") and item.get("content"):
            out[item["id"]] = item["content"]
        if item.get("legacy_memory_id") and item.get("content"):
            out[item["legacy_memory_id"]] = item["content"]
    return out


def _block_has_protected_leak(record: dict) -> bool:
    if record.get("query_type") == "intimate":
        return False
    lookup = record.get("_content_lookup") or {}
    for item in record.get("block_items") or []:
        if item.get("namespace") == "intimate":
            return True
        content = lookup.get(item.get("id")) or lookup.get(item.get("legacy_memory_id")) or item.get("preview") or ""
        if contains_any(content, PROTECTED_CONTENT_HINTS):
            return True
    return False


def _block_has_disallowed_namespace(record: dict) -> bool:
    allowed = set(record.get("allowed_namespaces") or [])
    if not allowed:
        return False
    return any((item.get("namespace") or "normal") not in allowed for item in record.get("block_items") or [])


def _realtime_guard_missing(record: dict) -> bool:
    namespaces = {item.get("namespace") for item in record.get("block_items") or []}
    if not (namespaces & REALTIME_GUARDED_NAMESPACES):
        return False
    content = record.get("block_content") or ""
    return "必须以当前工具或服务返回为准" not in content


def _record_has_low_score_item(record: dict, min_score: float) -> bool:
    for item in record.get("block_items") or []:
        try:
            if float(item.get("score") or 0) < min_score:
                return True
        except (TypeError, ValueError):
            return True
    return False


def summarize_records(records: list[dict], *, max_block_chars: int, min_score: float) -> dict:
    total = len(records)
    needs_memory = [r for r in records if r.get("needs_memory")]
    no_memory = [r for r in records if not r.get("needs_memory")]
    enabled = [r for r in records if r.get("block_enabled")]
    injected = [r for r in records if r.get("decision_inject") and r.get("block_enabled")]
    preview = [r for r in records if not r.get("decision_inject") and r.get("block_enabled")]
    chars = [int(r.get("block_chars") or 0) for r in enabled]
    item_counts = [int(r.get("block_item_count") or 0) for r in enabled]
    skipped = Counter(r.get("block_skipped_reason") or "enabled" for r in records if not r.get("block_enabled"))
    by_query = Counter(r.get("query_type") or "normal" for r in records)
    by_namespace = Counter(
        item.get("namespace") or "normal"
        for r in records
        for item in (r.get("block_items") or [])
    )
    usage_status = Counter((r.get("usage") or {}).get("status") or "not_simulated" for r in records)
    usage_types = Counter((r.get("usage") or {}).get("usage_type") or "-" for r in records if r.get("usage"))
    usage_rows = sum(int((r.get("usage") or {}).get("count") or 0) for r in records)
    touched_rows = sum(
        int((r.get("usage") or {}).get("count") or 0)
        for r in records
        if (r.get("usage") or {}).get("touch_last_used")
    )
    protected_leaks = [r for r in records if _block_has_protected_leak(r)]
    disallowed = [r for r in records if _block_has_disallowed_namespace(r)]
    missing_guard = [r for r in records if _realtime_guard_missing(r)]
    low_score = [r for r in records if _record_has_low_score_item(r, min_score)]
    over_budget = [r for r in records if int(r.get("block_chars") or 0) > max_block_chars]
    truncated = [r for r in records if "prompt_block_truncated" in (r.get("block_warnings") or [])]

    return {
        "total_messages": total,
        "needs_memory_count": len(needs_memory),
        "query_type_counts": dict(by_query),
        "block_enabled_count": len(enabled),
        "block_enabled_rate": round(len(enabled) / total, 4) if total else 0.0,
        "block_enabled_needs_memory_rate": round(len(enabled) / len(needs_memory), 4) if needs_memory else 0.0,
        "block_abstain_no_memory_rate": round(
            sum(1 for r in no_memory if not r.get("block_enabled")) / len(no_memory),
            4,
        ) if no_memory else 0.0,
        "decision_injected_count": len(injected),
        "decision_preview_count": len(preview),
        "usage_rows": usage_rows,
        "usage_touched_last_used_rows": touched_rows,
        "usage_status_counts": dict(usage_status),
        "usage_type_counts": dict(usage_types),
        "skipped_reasons": dict(skipped),
        "block_item_namespace_counts": dict(by_namespace),
        "block_chars": {
            "avg": round(mean(chars), 2) if chars else 0.0,
            "p95": _percentile(chars, 0.95),
            "max": max(chars) if chars else 0,
            "budget": max_block_chars,
            "over_budget_count": len(over_budget),
            "truncated_count": len(truncated),
        },
        "block_item_count": {
            "avg": round(mean(item_counts), 2) if item_counts else 0.0,
            "p95": _percentile(item_counts, 0.95),
            "max": max(item_counts) if item_counts else 0,
        },
        "checks": {
            "no_protected_leak": len(protected_leaks) == 0,
            "no_disallowed_namespace": len(disallowed) == 0,
            "realtime_guard_present": len(missing_guard) == 0,
            "no_low_score_prompt_items": len(low_score) == 0,
            "within_char_budget": len(over_budget) == 0,
        },
        "check_counts": {
            "protected_leak_count": len(protected_leaks),
            "disallowed_namespace_count": len(disallowed),
            "realtime_guard_missing_count": len(missing_guard),
            "low_score_item_count": len(low_score),
            "over_budget_count": len(over_budget),
        },
        "samples": {
            "protected_leaks": [_sample_record(r) for r in protected_leaks[:8]],
            "disallowed_namespace": [_sample_record(r) for r in disallowed[:8]],
            "realtime_guard_missing": [_sample_record(r) for r in missing_guard[:8]],
            "enabled_blocks": [_sample_record(r) for r in enabled[:8]],
        },
    }


def _sample_record(record: dict) -> dict:
    return {
        "message_id": record.get("message_id"),
        "query_type": record.get("query_type"),
        "needs_memory": record.get("needs_memory"),
        "preview": record.get("preview"),
        "allowed_namespaces": record.get("allowed_namespaces"),
        "block_chars": record.get("block_chars"),
        "block_item_count": record.get("block_item_count"),
        "decision": record.get("decision_reason"),
        "items": [
            {
                "id": item.get("legacy_memory_id") or item.get("id"),
                "namespace": item.get("namespace"),
                "kind": item.get("kind"),
                "score": item.get("score"),
                "preview": _preview(item.get("preview") or "", 70),
            }
            for item in (record.get("block_items") or [])[:3]
        ],
    }


async def replay_mode(
    *,
    mode: str,
    limit: int,
    top_k: int,
    candidate_limit: int,
    canary_ratio: float,
    prompt_min_score: float,
    max_items: int,
    max_item_chars: int,
    max_block_chars: int,
    simulate_usage: bool,
) -> dict:
    config = normalize_recall_config({
        "mode": mode,
        "top_k": top_k,
        "candidate_limit": candidate_limit,
        "canary_ratio": canary_ratio,
        "prompt_min_score": prompt_min_score,
        "include_trace": mode in {"debug", "canary", "full"},
    })
    runtime = recall_runtime(config)
    planner = V2RecallPlanner()
    service = MemoryService()
    messages = await fetch_user_messages(limit)
    records = []
    for message in messages:
        content = message.get("content") or ""
        classification = classify_query(content)
        plan = await planner.plan(
            content,
            classification.get("keywords") or [],
            mode=_query_mode(classification),
            namespace=_query_namespace(classification),
            top_k=runtime["top_k"],
            candidate_limit=runtime["candidate_limit"],
            include_trace=False,
        )
        block = service.build_v2_prompt_block(
            plan,
            min_score=runtime["prompt_min_score"],
            max_items=max_items,
            max_item_chars=max_item_chars,
            max_block_chars=max_block_chars,
        ) if runtime["prompt_block_enabled"] else {
            "enabled": False,
            "content": "",
            "item_count": 0,
            "items": [],
            "skipped_reason": "prompt_block_disabled",
            "warnings": [],
        }
        decision = prompt_injection_decision(config, seed=f"{message.get('conv_id')}:{content}")
        if not block.get("enabled"):
            decision = {
                **decision,
                "inject": False,
                "reason": block.get("skipped_reason") or "prompt_block_empty",
            }
        usage = None
        if simulate_usage:
            usage = await service.record_v2_prompt_usage({
                "runtime": runtime,
                "prompt_block": block,
                "prompt_decision": decision,
            }, conv_id=message.get("conv_id"), request_id=message.get("id"))
        records.append({
            "message_id": message.get("id"),
            "conv_id": message.get("conv_id"),
            "created_at": message.get("created_at"),
            "preview": _preview(content),
            "query_type": classification.get("query_type") or "normal",
            "needs_memory": bool(classification.get("needs_memory")),
            "keywords": classification.get("keywords") or [],
            "allowed_namespaces": plan.get("allowed_namespaces") or [],
            "candidate_count": int(plan.get("candidate_count") or 0),
            "selected_count": len(plan.get("selected") or []),
            "abstain_reason": plan.get("abstain_reason"),
            "block_enabled": bool(block.get("enabled")),
            "block_chars": len(block.get("content") or ""),
            "block_item_count": int(block.get("item_count") or 0),
            "block_skipped_reason": block.get("skipped_reason"),
            "block_warnings": block.get("warnings") or [],
            "block_items": block.get("items") or [],
            "block_content": block.get("content") or "",
            "decision_inject": bool(decision.get("inject")),
            "decision_reason": decision.get("reason") or "",
            "decision_bucket": decision.get("bucket"),
            "usage": usage,
            "_content_lookup": _content_lookup(plan),
        })

    summary = summarize_records(
        records,
        max_block_chars=max_block_chars,
        min_score=runtime["prompt_min_score"],
    )
    return {
        "mode": mode,
        "config": config,
        "runtime": runtime,
        "simulate_usage": simulate_usage,
        "summary": summary,
        "records": records,
    }


async def run_prompt_block_replay(
    *,
    source_db: str | None,
    modes: list[str],
    limit: int,
    top_k: int,
    candidate_limit: int,
    canary_ratio: float,
    prompt_min_score: float,
    max_items: int,
    max_item_chars: int,
    max_block_chars: int,
    simulate_usage: bool,
    keep_temp: bool = False,
) -> dict:
    with temporary_db(source_db, keep_temp=keep_temp) as db_info:
        source_hash_before = file_sha256(db_info["source_db"])
        configure_db_path(db_info["db_path"])
        await database.init_db()
        migration = await migrate_legacy_memories(apply=True)
        status = await migration_status()
        mode_results = []
        for mode in modes:
            mode_results.append(await replay_mode(
                mode=mode,
                limit=limit,
                top_k=top_k,
                candidate_limit=candidate_limit,
                canary_ratio=canary_ratio,
                prompt_min_score=prompt_min_score,
                max_items=max_items,
                max_item_chars=max_item_chars,
                max_block_chars=max_block_chars,
                simulate_usage=simulate_usage,
            ))
        source_hash_after = file_sha256(db_info["source_db"])
        return {
            "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "db": {
                "path": str(db_info["db_path"]),
                "source_db": str(db_info["source_db"]) if db_info["source_db"] else None,
                "copied_to_temp": bool(db_info["copied_to_temp"]),
                "temp_dir": str(db_info["temp_dir"]) if db_info["temp_dir"] else None,
                "kept_temp": keep_temp,
                "source_sha256_before": source_hash_before,
                "source_sha256_after": source_hash_after,
            },
            "taxonomy_source": TAXONOMY_SOURCE,
            "migration": migration,
            "status": status,
            "parameters": {
                "modes": modes,
                "limit": limit,
                "top_k": top_k,
                "candidate_limit": candidate_limit,
                "canary_ratio": canary_ratio,
                "prompt_min_score": prompt_min_score,
                "max_items": max_items,
                "max_item_chars": max_item_chars,
                "max_block_chars": max_block_chars,
                "simulate_usage": simulate_usage,
            },
            "modes": mode_results,
            "checks": {
                "source_db_hash_unchanged": source_hash_before == source_hash_after,
                "migration_complete": status.get("legacy_total") == status.get("migrated_legacy_items"),
                "all_modes_passed": all(
                    all(mode_result["summary"]["checks"].values())
                    for mode_result in mode_results
                ),
            },
        }


def _md_table(rows: list[dict], columns: list[tuple[str, str]]) -> str:
    lines = [
        "| " + " | ".join(label for label, _ in columns) + " |",
        "| " + " | ".join("---" for _ in columns) + " |",
    ]
    for row in rows:
        values = []
        for _, key in columns:
            value = row.get(key, "")
            if isinstance(value, (dict, list)):
                value = json.dumps(value, ensure_ascii=False)
            values.append(str(value).replace("|", "\\|").replace("\n", " "))
        lines.append("| " + " | ".join(values) + " |")
    return "\n".join(lines)


def build_markdown(result: dict) -> str:
    params = result["parameters"]
    sections = [
        "# Memory V2 Batch 2.6d Prompt Block Replay",
        "",
        f"生成时间：{result['generated_at']}",
        "",
        "说明：",
        "",
        "- 本报告只做离线 prompt block 回放，不调用模型，不发送聊天请求。",
        "- 使用 `--source-db` 时会先复制 SQLite 到 `/tmp`，原数据库副本不被写入。",
        "- usage 写入只发生在临时库，用于验证 debug/canary/full 的写入策略。",
        "",
        "## 执行环境",
        "",
        "```text",
        f"source_db: {result['db'].get('source_db') or '-'}",
        f"working_db: {result['db'].get('path')}",
        f"copied_to_temp: {result['db'].get('copied_to_temp')}",
        f"source_hash_unchanged: {result['checks'].get('source_db_hash_unchanged')}",
        f"taxonomy_source: {result.get('taxonomy_source') or '-'}",
        "```",
        "",
        "## 参数",
        "",
        "```json",
        json.dumps(params, ensure_ascii=False, indent=2),
        "```",
        "",
        "## 数据状态",
        "",
        "```text",
        f"legacy_total: {result['status'].get('legacy_total')}",
        f"migrated_legacy_items: {result['status'].get('migrated_legacy_items')}",
        f"migration_complete: {result['checks'].get('migration_complete')}",
        f"all_modes_passed: {result['checks'].get('all_modes_passed')}",
        "```",
        "",
        "## 模式总览",
        "",
    ]
    overview_rows = []
    for mode_result in result["modes"]:
        summary = mode_result["summary"]
        overview_rows.append({
            "mode": mode_result["mode"],
            "messages": summary["total_messages"],
            "enabled": summary["block_enabled_count"],
            "enabled_rate": summary["block_enabled_rate"],
            "abstain_no_memory": summary["block_abstain_no_memory_rate"],
            "injected": summary["decision_injected_count"],
            "usage_rows": summary["usage_rows"],
            "touched": summary["usage_touched_last_used_rows"],
            "max_chars": summary["block_chars"]["max"],
            "p95_chars": summary["block_chars"]["p95"],
            "checks": summary["checks"],
        })
    sections += [
        _md_table(
            overview_rows,
            [
                ("mode", "mode"),
                ("messages", "messages"),
                ("enabled", "enabled"),
                ("enabled_rate", "enabled_rate"),
                ("abstain_no_memory", "abstain_no_memory"),
                ("injected", "injected"),
                ("usage_rows", "usage_rows"),
                ("touched", "touched"),
                ("max_chars", "max_chars"),
                ("p95_chars", "p95_chars"),
                ("checks", "checks"),
            ],
        ),
        "",
    ]
    for mode_result in result["modes"]:
        summary = mode_result["summary"]
        sections += [
            f"## Mode: {mode_result['mode']}",
            "",
            "### 指标",
            "",
            "```json",
            json.dumps({
                "query_type_counts": summary["query_type_counts"],
                "skipped_reasons": summary["skipped_reasons"],
                "block_item_namespace_counts": summary["block_item_namespace_counts"],
                "block_chars": summary["block_chars"],
                "block_item_count": summary["block_item_count"],
                "usage_status_counts": summary["usage_status_counts"],
                "usage_type_counts": summary["usage_type_counts"],
                "check_counts": summary["check_counts"],
            }, ensure_ascii=False, indent=2),
            "```",
            "",
            "### Enabled Block 样本",
            "",
        ]
        samples = summary["samples"]["enabled_blocks"]
        if samples:
            sections.append(_md_table(
                samples,
                [
                    ("type", "query_type"),
                    ("needs", "needs_memory"),
                    ("preview", "preview"),
                    ("allowed", "allowed_namespaces"),
                    ("items", "items"),
                ],
            ))
        else:
            sections.append("_无样本_")
        sections.append("")
    sections += [
        "## 结论判定",
        "",
        "- `no_protected_leak=true`：非 intimate 查询没有生成 intimate 或受保护内容 prompt item。",
        "- `no_disallowed_namespace=true`：prompt item 的 namespace 都在本轮 allowed_namespaces 内。",
        "- `realtime_guard_present=true`：设备/定位/日程/健康类记忆块带实时事实 guard。",
        "- `within_char_budget=true`：生成的 prompt block 没有超过字符预算。",
        "",
    ]
    return "\n".join(sections)


def write_outputs(result: dict, *, output_md: str, output_json: str) -> None:
    if output_json:
        path = Path(output_json)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        print(path)
    if output_md:
        path = Path(output_md)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(build_markdown(result), encoding="utf-8")
        print(path)


async def _main() -> int:
    parser = argparse.ArgumentParser(description="Run offline Memory V2 prompt block replay")
    parser.add_argument("--source-db", default="", help="copy this SQLite DB to /tmp before running")
    parser.add_argument("--mode", action="append", choices=list(DEFAULT_REPLAY_MODES), help="mode to replay; can be repeated")
    parser.add_argument("--limit", type=int, default=300)
    parser.add_argument("--top-k", type=int, default=5)
    parser.add_argument("--candidate-limit", type=int, default=500)
    parser.add_argument("--canary-ratio", type=float, default=0.1)
    parser.add_argument("--prompt-min-score", type=float, default=0.45)
    parser.add_argument("--max-items", type=int, default=DEFAULT_MAX_ITEMS)
    parser.add_argument("--max-item-chars", type=int, default=DEFAULT_MAX_ITEM_CHARS)
    parser.add_argument("--max-block-chars", type=int, default=DEFAULT_MAX_BLOCK_CHARS)
    parser.add_argument("--no-usage", action="store_true", help="do not write memory_usage rows in the temp DB")
    parser.add_argument("--keep-temp", action="store_true", help="keep copied temp DB for manual inspection")
    parser.add_argument("--output-md", default="")
    parser.add_argument("--output-json", default="")
    args = parser.parse_args()

    result = await run_prompt_block_replay(
        source_db=args.source_db or None,
        modes=args.mode or list(DEFAULT_REPLAY_MODES),
        limit=args.limit,
        top_k=args.top_k,
        candidate_limit=args.candidate_limit,
        canary_ratio=args.canary_ratio,
        prompt_min_score=args.prompt_min_score,
        max_items=args.max_items,
        max_item_chars=args.max_item_chars,
        max_block_chars=args.max_block_chars,
        simulate_usage=not args.no_usage,
        keep_temp=args.keep_temp,
    )
    if args.output_md or args.output_json:
        write_outputs(result, output_md=args.output_md, output_json=args.output_json)
    else:
        print(build_markdown(result))
    return 0


def main() -> int:
    return asyncio.run(_main())


if __name__ == "__main__":
    raise SystemExit(main())
