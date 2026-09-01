"""
Historical replay evaluation for Memory V2 recall.

Batch 2.3b-offline runs past user messages through both a legacy keyword
baseline and the V2 recall planner. It is read-only and does not affect chat.
"""

from __future__ import annotations

import argparse
import asyncio
from collections import Counter
from datetime import datetime
import json
from pathlib import Path

import aiosqlite

from database import get_db, init_db

from app.memory_v2.migrations import migrate_legacy_memories, migration_status
from app.memory_v2.taxonomy import PROTECTED_CONTENT_HINTS, classify_query, contains_any

from .compare_recall import legacy_keyword_baseline
from .v2_recall import V2RecallPlanner


def _preview(text: str, length: int = 72) -> str:
    return " ".join((text or "").split())[:length]


def _contains_any(text: str, hints: tuple[str, ...]) -> bool:
    return contains_any(text, hints)


def classify_message(content: str) -> dict:
    return classify_query(content)


async def fetch_user_messages(limit: int) -> list[dict]:
    async with get_db() as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT id, conv_id, content, created_at FROM messages "
            "WHERE role='user' AND TRIM(content) != '' "
            "ORDER BY created_at DESC LIMIT ?",
            (int(limit),),
        )
        rows = [dict(row) for row in await cur.fetchall()]
    rows.reverse()
    return rows


async def fetch_v2_by_legacy_id(legacy_ids: set[str]) -> dict[str, dict]:
    if not legacy_ids:
        return {}
    placeholders = ",".join("?" for _ in legacy_ids)
    async with get_db() as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            f"SELECT legacy_memory_id, kind, namespace, metadata_json FROM memory_items "
            f"WHERE legacy_memory_id IN ({placeholders})",
            list(legacy_ids),
        )
        rows = [dict(row) for row in await cur.fetchall()]
    return {row["legacy_memory_id"]: row for row in rows if row.get("legacy_memory_id")}


def result_namespace(result: dict, lookup: dict[str, dict], *, v2: bool) -> str:
    if v2:
        return result.get("namespace") or "unknown"
    mapped = lookup.get(result.get("id"))
    return mapped.get("namespace") if mapped else "unknown"


def result_kind(result: dict, lookup: dict[str, dict], *, v2: bool) -> str:
    if v2:
        return result.get("kind") or "unknown"
    mapped = lookup.get(result.get("id"))
    return mapped.get("kind") if mapped else result.get("type") or "unknown"


def has_protected_leak(results: list[dict], lookup: dict[str, dict], *, v2: bool, query_type: str) -> bool:
    if query_type == "intimate":
        return False
    for result in results:
        namespace = result_namespace(result, lookup, v2=v2)
        if namespace == "intimate":
            return True
        if _contains_any(result.get("content") or "", PROTECTED_CONTENT_HINTS):
            return True
    return False


def has_open_loop(results: list[dict], lookup: dict[str, dict], *, v2: bool, top_n: int = 3) -> bool:
    for result in results[:top_n]:
        if result_kind(result, lookup, v2=v2) == "open_loop":
            return True
    return False


def namespace_match(query_type: str, namespace: str) -> bool:
    if query_type == "normal":
        return namespace in ("normal", "work", "schedule")
    if query_type == "schedule":
        return namespace in ("schedule", "work", "normal")
    if query_type == "work":
        return namespace in ("work", "schedule", "normal")
    return namespace == query_type


def _coverage(results: list[dict]) -> bool:
    return bool(results)


def _top_id(result: dict | None, *, v2: bool) -> str | None:
    if not result:
        return None
    return result.get("legacy_memory_id") if v2 else result.get("id")


def summarize_side(records: list[dict], side: str) -> dict:
    total = len(records)
    selected_key = f"{side}_selected"
    leak_key = f"{side}_protected_leak"
    open_loop_key = f"{side}_open_loop_hit"
    top_match_key = f"{side}_top1_namespace_match"
    top_id_key = f"{side}_top1_id"

    need_records = [r for r in records if r["needs_memory"]]
    no_need_records = [r for r in records if not r["needs_memory"]]
    protected_records = [r for r in records if r["query_type"] != "intimate"]
    open_loop_records = [r for r in records if r["is_open_loop_query"]]
    selected_records = [r for r in records if r[selected_key]]
    top_ids = [r[top_id_key] for r in records if r.get(top_id_key)]
    top_counts = Counter(top_ids)
    return {
        "coverage_all": round(sum(1 for r in records if r[selected_key]) / total, 4) if total else 0.0,
        "coverage_needs_memory": round(sum(1 for r in need_records if r[selected_key]) / len(need_records), 4) if need_records else 0.0,
        "abstention_no_memory": round(sum(1 for r in no_need_records if not r[selected_key]) / len(no_need_records), 4) if no_need_records else 0.0,
        "protected_scope_count": len(protected_records),
        "protected_leak_query_rate": round(sum(1 for r in protected_records if r[leak_key]) / len(protected_records), 4) if protected_records else 0.0,
        "open_loop_hit_rate": round(sum(1 for r in open_loop_records if r[open_loop_key]) / len(open_loop_records), 4) if open_loop_records else 0.0,
        "top1_namespace_match_rate": round(sum(1 for r in selected_records if r[top_match_key]) / len(selected_records), 4) if selected_records else 0.0,
        "unique_top1": len(top_counts),
        "top1_repeat_max": max(top_counts.values()) if top_counts else 0,
        "top1_repeat_samples": top_counts.most_common(5),
    }


async def evaluate(limit: int = 200, top_k: int = 5, apply_migration: bool = False) -> dict:
    await init_db()
    migration = None
    if apply_migration:
        migration = await migrate_legacy_memories(apply=True)
    status = await migration_status()
    messages = await fetch_user_messages(limit)
    planner = V2RecallPlanner()
    records = []

    for message in messages:
        content = message["content"]
        classification = classify_message(content)
        keywords = classification["keywords"]
        legacy = await legacy_keyword_baseline(content, keywords, top_k=top_k)
        v2_plan = await planner.plan(
            content,
            keywords,
            mode="intimate" if classification["query_type"] == "intimate" else "normal",
            namespace=classification["query_type"] if classification["query_type"] != "normal" else None,
            top_k=top_k,
        )
        v2 = v2_plan["selected"]
        legacy_lookup = await fetch_v2_by_legacy_id({item["id"] for item in legacy})

        legacy_top_ns = result_namespace(legacy[0], legacy_lookup, v2=False) if legacy else ""
        v2_top_ns = result_namespace(v2[0], {}, v2=True) if v2 else ""
        record = {
            "message_id": message["id"],
            "created_at": message["created_at"],
            "query": content,
            "preview": _preview(content),
            "query_type": classification["query_type"],
            "keywords": keywords,
            "needs_memory": classification["needs_memory"],
            "is_open_loop_query": classification["is_open_loop_query"],
            "legacy_selected": len(legacy),
            "v2_selected": len(v2),
            "legacy_top1_id": _top_id(legacy[0] if legacy else None, v2=False),
            "v2_top1_id": _top_id(v2[0] if v2 else None, v2=True),
            "legacy_top1_namespace": legacy_top_ns,
            "v2_top1_namespace": v2_top_ns,
            "legacy_top1_namespace_match": namespace_match(classification["query_type"], legacy_top_ns) if legacy_top_ns else False,
            "v2_top1_namespace_match": namespace_match(classification["query_type"], v2_top_ns) if v2_top_ns else False,
            "legacy_protected_leak": has_protected_leak(legacy, legacy_lookup, v2=False, query_type=classification["query_type"]),
            "v2_protected_leak": has_protected_leak(v2, {}, v2=True, query_type=classification["query_type"]),
            "legacy_open_loop_hit": has_open_loop(legacy, legacy_lookup, v2=False),
            "v2_open_loop_hit": has_open_loop(v2, {}, v2=True),
            "v2_candidate_count": v2_plan["candidate_count"],
            "legacy_top": [
                {
                    "id": item["id"],
                    "namespace": result_namespace(item, legacy_lookup, v2=False),
                    "kind": result_kind(item, legacy_lookup, v2=False),
                    "score": item["score"],
                    "preview": _preview(item["content"], 60),
                }
                for item in legacy[:3]
            ],
            "v2_top": [
                {
                    "id": item.get("legacy_memory_id") or item["id"],
                    "namespace": item["namespace"],
                    "kind": item["kind"],
                    "score": item["score"],
                    "reason": item["reason"],
                    "preview": _preview(item["content"], 60),
                }
                for item in v2[:3]
            ],
        }
        records.append(record)

    query_type_counts = Counter(record["query_type"] for record in records)
    metrics = {
        "total_messages": len(records),
        "needs_memory_count": sum(1 for record in records if record["needs_memory"]),
        "query_type_counts": dict(query_type_counts),
        "legacy": summarize_side(records, "legacy"),
        "v2": summarize_side(records, "v2"),
    }
    metrics["delta"] = {
        "protected_leak_query_rate": round(metrics["v2"]["protected_leak_query_rate"] - metrics["legacy"]["protected_leak_query_rate"], 4),
        "abstention_no_memory": round(metrics["v2"]["abstention_no_memory"] - metrics["legacy"]["abstention_no_memory"], 4),
        "top1_namespace_match_rate": round(metrics["v2"]["top1_namespace_match_rate"] - metrics["legacy"]["top1_namespace_match_rate"], 4),
        "open_loop_hit_rate": round(metrics["v2"]["open_loop_hit_rate"] - metrics["legacy"]["open_loop_hit_rate"], 4),
        "coverage_needs_memory": round(metrics["v2"]["coverage_needs_memory"] - metrics["legacy"]["coverage_needs_memory"], 4),
    }

    leak_examples = [
        record for record in records
        if record["legacy_protected_leak"] or record["v2_protected_leak"]
    ][:12]
    mismatch_examples = [
        record for record in records
        if record["legacy_top1_namespace_match"] != record["v2_top1_namespace_match"]
    ][:12]
    return {
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "migration": migration,
        "status": status,
        "metrics": metrics,
        "leak_examples": leak_examples,
        "mismatch_examples": mismatch_examples,
        "records": records,
    }


def _format_metric_table(metrics: dict) -> str:
    rows = [
        ("coverage_all", "覆盖率（全部消息）"),
        ("coverage_needs_memory", "覆盖率（需记忆消息）"),
        ("abstention_no_memory", "非记忆消息 abstain 率"),
        ("protected_scope_count", "保护评测样本数"),
        ("protected_leak_query_rate", "保护内容泄漏率"),
        ("open_loop_hit_rate", "open_loop 命中率"),
        ("top1_namespace_match_rate", "Top1 namespace 合理率（有召回）"),
        ("unique_top1", "Top1 去重数"),
        ("top1_repeat_max", "Top1 最大重复次数"),
    ]
    lines = ["| 指标 | Legacy | V2 |", "| --- | ---: | ---: |"]
    for key, label in rows:
        lines.append(f"| {label} | {metrics['legacy'].get(key)} | {metrics['v2'].get(key)} |")
    return "\n".join(lines)


def _format_examples(records: list[dict], title: str) -> str:
    lines = [f"## {title}", ""]
    if not records:
        lines.append("_无样本_")
        return "\n".join(lines)
    lines += [
        "| type | query | legacy top | v2 top | legacy leak | v2 leak |",
        "| --- | --- | --- | --- | ---: | ---: |",
    ]
    for record in records:
        legacy_top = record["legacy_top"][0] if record["legacy_top"] else {}
        v2_top = record["v2_top"][0] if record["v2_top"] else {}
        lines.append(
            f"| {record['query_type']} | {record['preview']} "
            f"| {legacy_top.get('namespace','-')}/{legacy_top.get('kind','-')}: {legacy_top.get('preview','')} "
            f"| {v2_top.get('namespace','-')}/{v2_top.get('kind','-')}: {v2_top.get('preview','')} "
            f"| {record['legacy_protected_leak']} | {record['v2_protected_leak']} |"
        )
    return "\n".join(lines)


def build_markdown(result: dict) -> str:
    metrics = result["metrics"]
    sections = [
        "# Memory V2 历史消息回放评测",
        "",
        f"生成时间：{result['generated_at']}",
        "",
        "说明：",
        "",
        "- 使用服务器数据库副本中的历史 `messages` 回放。",
        "- Legacy 侧是离线关键词基线，不调用 embedding API。",
        "- V2 侧是只读 `V2RecallPlanner`，不写 `memory_usage`，不接聊天。",
        "- `daddy` 这类称呼词不单独触发 intimate；只有明确特殊内容词或模式态才进入 intimate。",
        "",
        "## 数据状态",
        "",
        "```text",
        f"legacy_total: {result['status'].get('legacy_total')}",
        f"migrated_legacy_items: {result['status'].get('migrated_legacy_items')}",
        f"total_messages_eval: {metrics['total_messages']}",
        f"needs_memory_count: {metrics['needs_memory_count']}",
        "```",
        "",
        "## Query 类型分布",
        "",
        "```json",
        json.dumps(metrics["query_type_counts"], ensure_ascii=False, indent=2),
        "```",
        "",
        "## 指标对比",
        "",
        _format_metric_table(metrics),
        "",
        "Delta（V2 - Legacy）：",
        "",
        "```json",
        json.dumps(metrics["delta"], ensure_ascii=False, indent=2),
        "```",
        "",
        _format_examples(result["leak_examples"], "保护内容泄漏样本"),
        "",
        _format_examples(result["mismatch_examples"], "Top1 namespace 差异样本"),
        "",
        "## 解读",
        "",
        "- `保护内容泄漏率` 越低越好，分母只包含非 intimate 查询，尤其是 normal/work/schedule 不应召回 intimate 内容。",
        "- `非记忆消息 abstain 率` 越高越好，用来观察普通闲聊是否会被硬塞记忆。",
        "- `open_loop 命中率` 只统计带提醒/计划/上课/作业等意图的消息，越高越好。",
        "- `Top1 namespace 合理率` 只统计有召回的消息；这是规则指标，不等同于人工相关性，但可用于发现串场。",
        "- `Top1 最大重复次数` 越低越好，用来观察某条记忆是否过度霸榜。",
        "",
    ]
    return "\n".join(sections)


async def _main():
    parser = argparse.ArgumentParser(description="Replay historical user messages through legacy and V2 recall")
    parser.add_argument("--apply-migration", action="store_true")
    parser.add_argument("--limit", type=int, default=200)
    parser.add_argument("--top-k", type=int, default=5)
    parser.add_argument("--output-md", default="")
    parser.add_argument("--output-json", default="")
    args = parser.parse_args()

    result = await evaluate(limit=args.limit, top_k=args.top_k, apply_migration=args.apply_migration)
    if args.output_json:
        path = Path(args.output_json)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        print(str(path))
    if args.output_md:
        path = Path(args.output_md)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(build_markdown(result), encoding="utf-8")
        print(str(path))
    if not args.output_json and not args.output_md:
        print(build_markdown(result))


if __name__ == "__main__":
    asyncio.run(_main())
