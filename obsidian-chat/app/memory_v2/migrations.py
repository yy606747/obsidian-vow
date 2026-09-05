"""
Memory V2 migration helpers.

Batch 2.1 提供 dry-run 和显式 apply。默认不会修改旧 memories 表。
"""

from __future__ import annotations

import argparse
import asyncio
from collections import Counter
import json
import re
import time

from database import init_db

from .taxonomy import (
    EMOTIONAL_HINTS,
    INTERACTION_HINTS,
    NAMESPACE_PRIORITY,
    build_haystack,
    contains_any,
    detect_emotion,
    has_open_loop_signal,
    namespace_hits,
)
from .v2_repository import MemoryRepository


def normalize_keywords(value) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    text = str(value).strip()
    if not text:
        return []
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        parsed = None
    if isinstance(parsed, list):
        return [str(item).strip() for item in parsed if str(item).strip()]
    if isinstance(parsed, str):
        text = parsed
    return [part.strip() for part in re.split(r"[,，、\s]+", text) if part.strip()]


def _haystack(content: str, keywords: list[str]) -> str:
    return build_haystack(content, keywords)


def _namespace_hits(content: str, keywords: list[str]) -> dict[str, list[str]]:
    return namespace_hits(content, keywords)


def infer_kind(legacy_type: str | None, unresolved: int | None,
               content: str = "", keywords: list[str] | None = None) -> str:
    keywords = keywords or []
    haystack = _haystack(content, keywords)
    if unresolved:
        return "open_loop"
    if has_open_loop_signal(content, keywords):
        if (legacy_type or "").strip().lower() == "ai_note":
            return "open_loop"
    legacy_type = (legacy_type or "").strip().lower()
    if legacy_type == "ai_note":
        if contains_any(haystack, EMOTIONAL_HINTS):
            return "emotional"
        if contains_any(haystack, INTERACTION_HINTS):
            return "interaction"
        return "semantic"
    mapping = {
        "digest": "episode",
        "event": "episode",
        "fact": "semantic",
        "semantic": "semantic",
        "preference": "interaction",
        "interaction": "interaction",
        "emotional": "emotional",
    }
    if contains_any(haystack, EMOTIONAL_HINTS):
        return "emotional"
    return mapping.get(legacy_type, "episode")


def infer_namespace(content: str, keywords: list[str]) -> str:
    hits = _namespace_hits(content, keywords)
    for namespace in NAMESPACE_PRIORITY:
        if namespace in hits:
            return namespace
    return "normal"


def infer_secondary_namespaces(content: str, keywords: list[str], primary: str) -> list[str]:
    hits = _namespace_hits(content, keywords)
    return [
        namespace
        for namespace in NAMESPACE_PRIORITY
        if namespace != primary and namespace in hits
    ]


def legacy_memory_to_item(row: dict) -> dict:
    keywords = normalize_keywords(row.get("keywords"))
    content = row.get("content") or ""
    namespace = infer_namespace(content, keywords)
    secondary_namespaces = infer_secondary_namespaces(content, keywords, namespace)
    kind = infer_kind(row.get("type"), row.get("unresolved"), content, keywords)
    created_at = float(row.get("created_at") or time.time())
    importance = row.get("importance")
    try:
        importance = float(importance if importance is not None else 0.5)
    except (TypeError, ValueError):
        importance = 0.5
    legacy_id = row["id"]
    metadata = {
        "migrated_from": "memories",
        "legacy_type": row.get("type") or "",
        "legacy_unresolved": bool(row.get("unresolved")),
        "secondary_namespaces": secondary_namespaces,
    }
    return {
        "id": f"memv2_{legacy_id}",
        "legacy_memory_id": legacy_id,
        "origin_type": "ai_note" if str(row.get("type") or "").strip().lower() == "ai_note" else "legacy",
        "kind": kind,
        "namespace": namespace,
        "content": content,
        "emotion": detect_emotion(content, keywords),
        "importance": importance,
        "confidence": 0.7,
        "status": "active",
        "visibility": "prompt",
        "embedding": row.get("embedding"),
        "keywords_json": json.dumps(keywords, ensure_ascii=False),
        "source_conv": row.get("source_conv"),
        "source_start_ts": row.get("source_start_ts"),
        "source_end_ts": row.get("source_end_ts"),
        "created_at": created_at,
        "updated_at": created_at,
        "metadata_json": json.dumps(metadata, ensure_ascii=False),
    }


async def backfill_memory_item_emotions(
    *,
    apply: bool = False,
    limit: int | None = None,
    repository: MemoryRepository | None = None,
) -> dict:
    repo = repository or MemoryRepository()
    rows = await repo.list_items(limit=limit or 100000)
    stats = {
        "ok": True,
        "apply": apply,
        "selected": len(rows),
        "already_tagged": 0,
        "planned": 0,
        "updated": 0,
        "by_emotion": {},
        "samples": [],
    }
    by_emotion = Counter()
    for row in rows:
        if str(row.get("emotion") or "").strip():
            stats["already_tagged"] += 1
            continue
        keywords = normalize_keywords(row.get("keywords_json"))
        emotion = detect_emotion(row.get("content") or "", keywords)
        if not emotion:
            continue
        stats["planned"] += 1
        by_emotion[emotion] += 1
        if len(stats["samples"]) < 5:
            stats["samples"].append({
                "id": row.get("id"),
                "legacy_memory_id": row.get("legacy_memory_id"),
                "emotion": emotion,
                "content_preview": (row.get("content") or "")[:80],
            })
        if apply and await repo.update_item_emotion(row["id"], emotion):
            stats["updated"] += 1
    stats["by_emotion"] = dict(by_emotion)
    return stats


def _public_sample(item: dict) -> dict:
    try:
        metadata = json.loads(item.get("metadata_json") or "{}")
    except json.JSONDecodeError:
        metadata = {}
    return {
        "id": item["id"],
        "legacy_memory_id": item["legacy_memory_id"],
        "kind": item["kind"],
        "namespace": item["namespace"],
        "secondary_namespaces": metadata.get("secondary_namespaces", []),
        "importance": item["importance"],
        "content_preview": item["content"][:80],
    }


async def migrate_legacy_memories(
    *,
    apply: bool = False,
    limit: int | None = None,
    repository: MemoryRepository | None = None,
) -> dict:
    repo = repository or MemoryRepository()
    legacy_total = await repo.count_legacy_memories()
    migrated_ids = await repo.fetch_migrated_legacy_ids()
    rows = await repo.fetch_legacy_memories(limit=limit)

    stats = {
        "ok": True,
        "apply": apply,
        "legacy_total": legacy_total,
        "selected": len(rows),
        "already_migrated": 0,
        "planned": 0,
        "inserted": 0,
        "linked": 0,
        "by_kind": {},
        "by_namespace": {},
        "by_secondary_namespace": {},
        "samples": [],
    }
    by_kind = Counter()
    by_namespace = Counter()
    by_secondary_namespace = Counter()

    for row in rows:
        if row["id"] in migrated_ids:
            stats["already_migrated"] += 1
            continue
        item = legacy_memory_to_item(row)
        stats["planned"] += 1
        by_kind[item["kind"]] += 1
        by_namespace[item["namespace"]] += 1
        try:
            metadata = json.loads(item.get("metadata_json") or "{}")
        except json.JSONDecodeError:
            metadata = {}
        by_secondary_namespace.update(metadata.get("secondary_namespaces") or [])
        if len(stats["samples"]) < 5:
            stats["samples"].append(_public_sample(item))
        if not apply:
            continue
        inserted = await repo.insert_memory_item(item, ignore_existing=True)
        if inserted:
            stats["inserted"] += 1
            linked = await repo.insert_memory_link(
                item["id"],
                row["id"],
                "legacy_memory",
                "migrated_from",
                created_at=time.time(),
            )
            if linked:
                stats["linked"] += 1

    stats["by_kind"] = dict(by_kind)
    stats["by_namespace"] = dict(by_namespace)
    stats["by_secondary_namespace"] = dict(by_secondary_namespace)
    return stats


async def migration_status(repository: MemoryRepository | None = None) -> dict:
    repo = repository or MemoryRepository()
    return {
        "legacy_total": await repo.count_legacy_memories(),
        "migrated_legacy_items": await repo.count_migrated_legacy_items(),
    }


async def _main():
    parser = argparse.ArgumentParser(description="Memory V2 legacy migration helper")
    parser.add_argument("--apply", action="store_true", help="write planned items into memory_items")
    parser.add_argument("--limit", type=int, default=None, help="limit legacy rows for smoke tests")
    parser.add_argument("--status", action="store_true", help="only print migration status")
    parser.add_argument(
        "--backfill-emotions",
        action="store_true",
        help="infer emotion labels for existing memory_items",
    )
    args = parser.parse_args()

    await init_db()
    if args.status:
        result = await migration_status()
    elif args.backfill_emotions:
        result = await backfill_memory_item_emotions(apply=args.apply, limit=args.limit)
    else:
        result = await migrate_legacy_memories(apply=args.apply, limit=args.limit)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    asyncio.run(_main())
