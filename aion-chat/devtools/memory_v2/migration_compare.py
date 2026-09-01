"""
Batch 2.4 migration and recall comparison runner.

This tool ties together the existing legacy migration, V2 recall planner, and
trace output into one repeatable offline check. It can run against the current
SQLite DB, or copy a source DB into /tmp first so real server data is never
modified in-place.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
from collections import Counter
from contextlib import contextmanager
from datetime import datetime
import json
from pathlib import Path
import shutil
import tempfile
from typing import Iterator

import aiosqlite

import config
import database
from app.memory_v2.diagnostics import summarize_recall_item
from app.memory_v2.migrations import migrate_legacy_memories, migration_status
from app.memory_v2.taxonomy import TAXONOMY_SOURCE, classify_query
from app.memory_v2.v2_repository import MemoryRepository

from .compare_recall import DEFAULT_QUERIES, legacy_keyword_baseline
from .v2_recall import V2RecallPlanner


B24_EXTRA_QUERIES = [
    {
        "title": "位置误报边界",
        "query": "我在宿舍和教室边缘时定位老误报，之前打算怎么改？",
        "keywords": ["宿舍", "教室", "定位", "误报"],
        "mode": "normal",
        "namespace": "location",
    },
    {
        "title": "普通闲聊不硬塞",
        "query": "我先坐一会儿，等下再说。",
        "keywords": [],
        "mode": "normal",
    },
]


DEFAULT_B24_QUERIES = DEFAULT_QUERIES + B24_EXTRA_QUERIES


@contextmanager
def temporary_db(source_db: str | Path | None, *, keep_temp: bool = False) -> Iterator[dict]:
    if not source_db:
        yield {
            "db_path": config.DB_PATH,
            "source_db": None,
            "copied_to_temp": False,
            "temp_dir": None,
        }
        return

    source = Path(source_db).expanduser().resolve()
    if not source.exists():
        raise FileNotFoundError(f"source db does not exist: {source}")
    temp_dir = Path(tempfile.mkdtemp(prefix="obsidianvow-b24-"))
    db_path = temp_dir / "chat.db"
    shutil.copy2(source, db_path)
    try:
        yield {
            "db_path": db_path,
            "source_db": source,
            "copied_to_temp": True,
            "temp_dir": temp_dir,
        }
    finally:
        if keep_temp:
            print(f"kept temp db dir: {temp_dir}")
        else:
            shutil.rmtree(temp_dir, ignore_errors=True)


def configure_db_path(db_path: str | Path) -> None:
    path = Path(db_path)
    config.DB_PATH = path
    database.DB_PATH = path


def file_sha256(path: str | Path | None) -> str | None:
    if not path:
        return None
    hasher = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            hasher.update(chunk)
    return hasher.hexdigest()


async def count_migration_links() -> int:
    async with database.get_db() as db:
        cur = await db.execute(
            "SELECT COUNT(*) FROM memory_links "
            "WHERE target_type='legacy_memory' AND relation='migrated_from'"
        )
        row = await cur.fetchone()
    return int(row[0] or 0)


async def grouped_counts(table: str, columns: list[str], where: str = "") -> list[dict]:
    column_sql = ", ".join(columns)
    where_sql = f"WHERE {where}" if where else ""
    async with database.get_db() as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            f"SELECT {column_sql}, COUNT(*) AS count FROM {table} "
            f"{where_sql} GROUP BY {column_sql} ORDER BY count DESC, {column_sql} ASC"
        )
        rows = await cur.fetchall()
    return [dict(row) for row in rows]


async def collect_migration_integrity() -> dict:
    repo = MemoryRepository()
    legacy_ids = await repo.fetch_legacy_memory_ids()
    migrated_ids = await repo.fetch_migrated_legacy_ids()
    missing = sorted(legacy_ids - migrated_ids)
    extra = sorted(migrated_ids - legacy_ids)
    link_count = await count_migration_links()
    return {
        "legacy_total": len(legacy_ids),
        "migrated_legacy_items": len(migrated_ids),
        "coverage": round(len(migrated_ids) / len(legacy_ids), 4) if legacy_ids else 1.0,
        "missing_count": len(missing),
        "extra_count": len(extra),
        "missing_samples": missing[:10],
        "extra_samples": extra[:10],
        "migration_link_count": link_count,
        "by_kind": await grouped_counts(
            "memory_items",
            ["kind"],
            "legacy_memory_id IS NOT NULL",
        ),
        "by_namespace": await grouped_counts(
            "memory_items",
            ["namespace"],
            "legacy_memory_id IS NOT NULL",
        ),
        "by_namespace_kind": await grouped_counts(
            "memory_items",
            ["namespace", "kind"],
            "legacy_memory_id IS NOT NULL",
        ),
    }


def item_id_for_overlap(item: dict, *, v2: bool) -> str | None:
    if v2:
        return item.get("legacy_memory_id") or item.get("id")
    return item.get("id")


def short_items(rows: list[dict], *, v2: bool, limit: int = 5) -> list[dict]:
    if v2:
        return [summarize_recall_item(row, preview_length=100) for row in rows[:limit]]
    out = []
    for row in rows[:limit]:
        out.append({
            "id": row.get("id"),
            "kind": row.get("type"),
            "score": row.get("score"),
            "importance": row.get("importance"),
            "unresolved": row.get("unresolved"),
            "reason": row.get("reason") or "",
            "preview": " ".join((row.get("content") or "").split())[:100],
        })
    return out


async def compare_query(spec: dict, *, top_k: int, include_trace: bool) -> dict:
    keywords = [str(item) for item in spec.get("keywords") or []]
    query = spec["query"]
    legacy = await legacy_keyword_baseline(query, keywords, top_k=top_k)
    plan = await V2RecallPlanner().plan(
        query,
        keywords,
        mode=spec.get("mode", "normal"),
        namespace=spec.get("namespace") or None,
        top_k=top_k,
        include_trace=include_trace,
    )
    selected = plan.get("selected") or []
    legacy_ids = [item_id_for_overlap(item, v2=False) for item in legacy]
    v2_ids = [item_id_for_overlap(item, v2=True) for item in selected]
    overlap = sorted({item for item in legacy_ids if item} & {item for item in v2_ids if item})
    classification = classify_query(query)
    return {
        "title": spec.get("title") or query[:24],
        "query": query,
        "keywords": keywords,
        "classification": {
            "query_type": classification.get("query_type"),
            "needs_memory": classification.get("needs_memory"),
            "is_open_loop_query": classification.get("is_open_loop_query"),
        },
        "legacy_selected_count": len(legacy),
        "v2_selected_count": len(selected),
        "v2_candidate_count": plan.get("candidate_count") or 0,
        "overlap_count": len(overlap),
        "overlap_legacy_ids": overlap,
        "v2_abstain_reason": plan.get("abstain_reason"),
        "turn_plan": plan.get("turn_plan") or {},
        "allowed_namespaces": plan.get("allowed_namespaces") or [],
        "legacy_top": short_items(legacy, v2=False),
        "v2_top": short_items(selected, v2=True),
        "trace": plan.get("trace") if include_trace else None,
    }


def summarize_query_comparisons(comparisons: list[dict]) -> dict:
    total = len(comparisons)
    v2_abstained = sum(1 for item in comparisons if not item["v2_selected_count"])
    overlap_any = sum(1 for item in comparisons if item["overlap_count"] > 0)
    query_types = Counter(
        (item.get("classification") or {}).get("query_type") or "normal"
        for item in comparisons
    )
    return {
        "total_queries": total,
        "v2_abstained_queries": v2_abstained,
        "queries_with_overlap": overlap_any,
        "query_type_counts": dict(query_types),
    }


async def run_batch24(
    *,
    source_db: str | None,
    top_k: int,
    include_trace: bool,
    keep_temp: bool,
) -> dict:
    with temporary_db(source_db, keep_temp=keep_temp) as db_info:
        source_hash_before = file_sha256(db_info["source_db"])
        configure_db_path(db_info["db_path"])
        await database.init_db()
        before_status = await migration_status()
        dry_run = await migrate_legacy_memories(apply=False)
        migration = await migrate_legacy_memories(apply=True)
        after_status = await migration_status()
        integrity = await collect_migration_integrity()
        comparisons = [
            await compare_query(spec, top_k=top_k, include_trace=include_trace)
            for spec in DEFAULT_B24_QUERIES
        ]
        source_hash_after = file_sha256(db_info["source_db"])
        result = {
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
            "top_k": top_k,
            "before_status": before_status,
            "dry_run": dry_run,
            "migration": migration,
            "after_status": after_status,
            "integrity": integrity,
            "comparison_summary": summarize_query_comparisons(comparisons),
            "comparisons": comparisons,
            "checks": {
                "migration_complete": integrity["missing_count"] == 0
                and integrity["extra_count"] == 0
                and integrity["legacy_total"] == integrity["migrated_legacy_items"],
                "source_db_not_modified": bool(db_info["copied_to_temp"]),
                "source_db_hash_unchanged": source_hash_before == source_hash_after,
                "has_trace_samples": include_trace and any(item.get("trace") for item in comparisons),
            },
        }
        return result


def md_table(rows: list[dict], columns: list[tuple[str, str]]) -> str:
    lines = [
        "| " + " | ".join(label for label, _ in columns) + " |",
        "| " + " | ".join("---" for _ in columns) + " |",
    ]
    for row in rows:
        values = []
        for _, key in columns:
            value = row.get(key, "")
            if isinstance(value, (list, dict)):
                value = json.dumps(value, ensure_ascii=False)
            values.append(str(value).replace("|", "\\|").replace("\n", " "))
        lines.append("| " + " | ".join(values) + " |")
    return "\n".join(lines)


def format_item_list(items: list[dict], *, v2: bool) -> str:
    if not items:
        return "_无结果_"
    if v2:
        columns = [
            ("rank", "rank"),
            ("id", "legacy_memory_id"),
            ("namespace", "namespace"),
            ("kind", "kind"),
            ("score", "score"),
            ("reason", "reason"),
            ("preview", "preview"),
        ]
    else:
        columns = [
            ("rank", "rank"),
            ("id", "id"),
            ("type", "kind"),
            ("score", "score"),
            ("reason", "reason"),
            ("preview", "preview"),
        ]
    ranked = []
    for index, item in enumerate(items, 1):
        row = dict(item)
        row["rank"] = index
        ranked.append(row)
    return md_table(ranked, columns)


def build_markdown(result: dict) -> str:
    integrity = result["integrity"]
    summary = result["comparison_summary"]
    sections = [
        "# Memory V2 Batch 2.4 Migration Comparison",
        "",
        f"生成时间：{result['generated_at']}",
        "",
        "## 执行环境",
        "",
        "```text",
        f"source_db: {result['db'].get('source_db') or '-'}",
        f"working_db: {result['db'].get('path')}",
        f"copied_to_temp: {result['db'].get('copied_to_temp')}",
        f"kept_temp: {result['db'].get('kept_temp')}",
        f"source_sha256_before: {result['db'].get('source_sha256_before') or '-'}",
        f"source_sha256_after: {result['db'].get('source_sha256_after') or '-'}",
        f"source_hash_unchanged: {result['checks'].get('source_db_hash_unchanged')}",
        f"taxonomy_source: {result.get('taxonomy_source') or '-'}",
        "```",
        "",
        "## 迁移完整性",
        "",
        "```text",
        f"legacy_total: {integrity['legacy_total']}",
        f"migrated_legacy_items: {integrity['migrated_legacy_items']}",
        f"coverage: {integrity['coverage']}",
        f"missing_count: {integrity['missing_count']}",
        f"extra_count: {integrity['extra_count']}",
        f"migration_link_count: {integrity['migration_link_count']}",
        f"migration_complete: {result['checks']['migration_complete']}",
        "```",
        "",
        "### Kind 分布",
        "",
        md_table(integrity["by_kind"], [("kind", "kind"), ("count", "count")]),
        "",
        "### Namespace 分布",
        "",
        md_table(integrity["by_namespace"], [("namespace", "namespace"), ("count", "count")]),
        "",
        "## 新旧召回对比总览",
        "",
        "```text",
        f"total_queries: {summary['total_queries']}",
        f"v2_abstained_queries: {summary['v2_abstained_queries']}",
        f"queries_with_overlap: {summary['queries_with_overlap']}",
        "```",
        "",
    ]
    overview_rows = []
    for item in result["comparisons"]:
        v2_top = item["v2_top"][0] if item["v2_top"] else {}
        legacy_top = item["legacy_top"][0] if item["legacy_top"] else {}
        overview_rows.append({
            "title": item["title"],
            "query_type": item["classification"].get("query_type"),
            "legacy_top": f"{legacy_top.get('kind', '-')}/{legacy_top.get('id', '-')}",
            "v2_top": f"{v2_top.get('namespace', '-')}/{v2_top.get('kind', '-')}/{v2_top.get('legacy_memory_id') or v2_top.get('id', '-')}",
            "legacy_count": item["legacy_selected_count"],
            "v2_count": item["v2_selected_count"],
            "overlap": item["overlap_count"],
            "abstain": item.get("v2_abstain_reason") or "-",
        })
    sections += [
        md_table(
            overview_rows,
            [
                ("query", "title"),
                ("type", "query_type"),
                ("legacy_top", "legacy_top"),
                ("v2_top", "v2_top"),
                ("legacy_count", "legacy_count"),
                ("v2_count", "v2_count"),
                ("overlap", "overlap"),
                ("abstain", "abstain"),
            ],
        ),
        "",
        "## 详细样本",
        "",
    ]
    for item in result["comparisons"]:
        sections += [
            f"### {item['title']}",
            "",
            f"Query：`{item['query']}`",
            "",
            "V2 plan：",
            "",
            "```json",
            json.dumps({
                "classification": item["classification"],
                "turn_plan": item["turn_plan"],
                "allowed_namespaces": item["allowed_namespaces"],
                "candidate_count": item["v2_candidate_count"],
                "selected_count": item["v2_selected_count"],
                "abstain_reason": item["v2_abstain_reason"],
            }, ensure_ascii=False, indent=2),
            "```",
            "",
            "Legacy top：",
            "",
            format_item_list(item["legacy_top"], v2=False),
            "",
            "V2 top：",
            "",
            format_item_list(item["v2_top"], v2=True),
            "",
        ]
    sections += [
        "## 结论",
        "",
        "- 本报告只做离线迁移和召回对比，不接入聊天 prompt。",
        "- 使用 `--source-db` 时会先复制到 `/tmp`，原服务器数据库副本不被写入。",
        "- `migration_complete=true` 只代表旧 `memories` 到 V2 `memory_items` 的来源覆盖完整，不代表召回质量已适合全量接入。",
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
    parser = argparse.ArgumentParser(description="Run Memory V2 Batch 2.4 migration comparison")
    parser.add_argument("--source-db", default="", help="copy this SQLite DB to /tmp before running")
    parser.add_argument("--top-k", type=int, default=5)
    parser.add_argument("--no-trace", action="store_true", help="skip V2 trace payloads in JSON")
    parser.add_argument("--keep-temp", action="store_true", help="keep copied temp DB for manual inspection")
    parser.add_argument("--output-md", default="")
    parser.add_argument("--output-json", default="")
    args = parser.parse_args()

    result = await run_batch24(
        source_db=args.source_db or None,
        top_k=args.top_k,
        include_trace=not args.no_trace,
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
