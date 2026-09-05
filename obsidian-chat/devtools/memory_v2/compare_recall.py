"""
Offline recall comparison for Memory V2.

This is a Batch 2.3a diagnostic tool. It does not call external embedding APIs,
does not affect chat, and writes only the optional markdown report.
"""

from __future__ import annotations

import argparse
import asyncio
from datetime import datetime
import json
from pathlib import Path
import re

import aiosqlite

from database import get_db, init_db

from app.memory_v2.migrations import (
    migrate_legacy_memories,
    migration_status,
    normalize_keywords,
)

from .v2_recall import V2RecallPlanner


DEFAULT_QUERIES = [
    {
        "title": "项目记忆重构",
        "query": "我之前为什么想重构记忆库？",
        "keywords": ["记忆库", "重构", "项目"],
        "mode": "normal",
    },
    {
        "title": "上课和作业",
        "query": "提醒我最近上课和作业相关的事",
        "keywords": ["上课", "作业", "提醒"],
        "mode": "normal",
    },
    {
        "title": "智能戒指设备",
        "query": "我说过戒指设备想怎么用吗？",
        "keywords": ["戒指", "设备", "震动"],
        "mode": "normal",
    },
    {
        "title": "身体和饮食",
        "query": "我最近身体和饮食有什么需要注意的吗？",
        "keywords": ["身体", "饮食", "健康"],
        "mode": "normal",
    },
    {
        "title": "互动偏好",
        "query": "我不喜欢 AI 哪些回复方式？",
        "keywords": ["回复", "不喜欢", "AI"],
        "mode": "normal",
    },
    {
        "title": "特殊模式边界",
        "query": "特殊模式里之前说过哪些边界？",
        "keywords": ["特殊模式", "边界", "安全词"],
        "mode": "intimate",
        "namespace": "intimate",
    },
]


def _norm(text: str) -> str:
    return (text or "").lower()


def _terms(query: str, keywords: list[str]) -> list[str]:
    out = []
    for kw in keywords:
        kw = str(kw).strip().lower()
        if kw and kw not in out:
            out.append(kw)
    for token in re.findall(r"[a-zA-Z0-9_+#.-]{2,}", _norm(query)):
        if token not in out:
            out.append(token)
    if keywords:
        return out[:24]
    for token in re.findall(r"[\u4e00-\u9fff]{2,}", query):
        if token not in out:
            out.append(token)
    return out[:24]


def _preview(text: str, length: int = 80) -> str:
    return " ".join((text or "").split())[:length]


def _legacy_score(row: dict, terms: list[str]) -> dict:
    content = row.get("content") or ""
    content_lower = _norm(content)
    keywords = normalize_keywords(row.get("keywords"))
    keywords_lower = [kw.lower() for kw in keywords]
    hits = []
    for term in terms:
        term_lower = term.lower()
        if any(term_lower in kw or kw in term_lower for kw in keywords_lower if kw):
            hits.append(term)
            continue
        if term_lower in content_lower:
            hits.append(term)
    relevance = len(set(hits)) / max(len(terms), 1)
    importance = float(row.get("importance") or 0.5)
    unresolved_bonus = 0.08 if row.get("unresolved") else 0.0
    score = relevance * 0.75 + importance * 0.17 + unresolved_bonus
    return {
        "id": row["id"],
        "content": content,
        "type": row.get("type"),
        "score": round(score, 4),
        "importance": round(importance, 2),
        "unresolved": bool(row.get("unresolved")),
        "reason": "keyword:" + ",".join(hits[:5]) if hits else "importance_baseline",
    }


async def legacy_keyword_baseline(query: str, keywords: list[str], top_k: int = 6) -> list[dict]:
    terms = _terms(query, keywords)
    async with get_db() as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT id, content, type, created_at, keywords, importance, unresolved "
            "FROM memories ORDER BY created_at DESC"
        )
        rows = [dict(row) for row in await cur.fetchall()]
    scored = [_legacy_score(row, terms) for row in rows]
    scored.sort(key=lambda item: item["score"], reverse=True)
    return [item for item in scored if item["score"] > 0][:top_k]


def _format_rows(rows: list[dict], *, v2: bool) -> str:
    if not rows:
        return "_无结果_"
    lines = ["| rank | score | id | kind/type | namespace | reason | preview |",
             "| ---: | ---: | --- | --- | --- | --- | --- |"]
    for idx, row in enumerate(rows, 1):
        kind = row.get("kind") or row.get("type") or ""
        namespace = row.get("namespace") if v2 else ""
        reason = row.get("reason") or ""
        preview = _preview(row.get("content") or "")
        lines.append(
            f"| {idx} | {row.get('score')} | `{row.get('legacy_memory_id') or row.get('id')}` "
            f"| {kind} | {namespace or '-'} | {reason} | {preview} |"
        )
    return "\n".join(lines)


async def build_report(*, apply_migration: bool = False, top_k: int = 6) -> str:
    await init_db()
    migration = None
    if apply_migration:
        migration = await migrate_legacy_memories(apply=True)
    status = await migration_status()

    planner = V2RecallPlanner()
    sections = [
        "# Memory V2 Recall Planner 离线对比",
        "",
        f"生成时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        "",
        "说明：",
        "",
        "- 这是 Batch 2.3a 的只读对比报告。",
        "- 旧侧是离线关键词基线，不调用 embedding API。",
        "- V2 侧使用 `memory_items` 的 kind/namespace/keywords/metadata 打分。",
        "- 本报告不接入聊天，不改变 prompt。",
        "",
        "## 数据状态",
        "",
        "```text",
        f"legacy_total: {status.get('legacy_total')}",
        f"migrated_legacy_items: {status.get('migrated_legacy_items')}",
        "```",
        "",
    ]
    if migration:
        sections += [
            "## 迁移状态",
            "",
            "```text",
            f"legacy_total: {migration.get('legacy_total')}",
            f"planned: {migration.get('planned')}",
            f"inserted: {migration.get('inserted')}",
            f"already_migrated: {migration.get('already_migrated')}",
            "```",
            "",
        ]

    for spec in DEFAULT_QUERIES:
        legacy = await legacy_keyword_baseline(spec["query"], spec["keywords"], top_k=top_k)
        v2_plan = await planner.plan(
            spec["query"],
            spec["keywords"],
            mode=spec.get("mode", "normal"),
            namespace=spec.get("namespace"),
            top_k=top_k,
        )
        sections += [
            f"## {spec['title']}",
            "",
            f"Query：`{spec['query']}`",
            "",
            f"Keywords：`{', '.join(spec['keywords'])}`",
            "",
            "V2 plan：",
            "",
            "```json",
            json.dumps({
                "mode": v2_plan["turn_plan"]["mode"],
                "namespace": v2_plan["turn_plan"]["namespace"],
                "detected_namespaces": v2_plan["turn_plan"]["detected_namespaces"],
                "preferred_kinds": v2_plan["turn_plan"]["preferred_kinds"],
                "allowed_namespaces": v2_plan["allowed_namespaces"],
                "candidate_count": v2_plan["candidate_count"],
            }, ensure_ascii=False, indent=2),
            "```",
            "",
            "### Legacy Keyword Baseline",
            "",
            _format_rows(legacy, v2=False),
            "",
            "### V2 Recall Planner",
            "",
            _format_rows(v2_plan["selected"], v2=True),
            "",
        ]
    return "\n".join(sections)


async def _main():
    parser = argparse.ArgumentParser(description="Compare legacy keyword recall with Memory V2 planner")
    parser.add_argument("--apply-migration", action="store_true", help="apply legacy memories into V2 tables before comparing")
    parser.add_argument("--output", default="", help="markdown output path")
    parser.add_argument("--top-k", type=int, default=6)
    args = parser.parse_args()

    report = await build_report(apply_migration=args.apply_migration, top_k=args.top_k)
    if args.output:
        path = Path(args.output)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(report, encoding="utf-8")
        print(str(path))
    else:
        print(report)


if __name__ == "__main__":
    asyncio.run(_main())
