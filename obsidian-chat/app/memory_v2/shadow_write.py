"""
Memory V2 shadow-write helpers.

Batch 2.2 keeps legacy `memories` as the source of truth. V2 writes are best-effort
mirrors used for later recall comparison and debugging.
"""

from __future__ import annotations

import json
import time
import traceback

from .migrations import legacy_memory_to_item
from .v2_repository import MemoryRepository


def _load_json_object(value: str | None) -> dict:
    if not value:
        return {}
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _with_shadow_metadata(item: dict, *,
                          write_path: str,
                          source: str,
                          extra_metadata: dict | None = None) -> dict:
    item = dict(item)
    metadata = _load_json_object(item.get("metadata_json"))
    metadata.update({
        "shadow_write": True,
        "write_path": write_path,
        "source": source,
        "shadow_written_at": time.time(),
    })
    if extra_metadata:
        metadata.update(extra_metadata)
    item["metadata_json"] = json.dumps(metadata, ensure_ascii=False)
    if source == "remember_cmd" or str(metadata.get("legacy_type") or "") == "ai_note":
        item["origin_type"] = "ai_note"
    elif source == "digest.multi_note":
        item["origin_type"] = "auto_digest"
    return item


async def shadow_legacy_memory(legacy_memory_id: str, *,
                               write_path: str,
                               source: str,
                               extra_metadata: dict | None = None,
                               strict: bool = False) -> dict:
    repo = MemoryRepository()
    try:
        legacy = await repo.get_legacy_memory(legacy_memory_id)
        if not legacy:
            return {
                "ok": False,
                "inserted": False,
                "legacy_memory_id": legacy_memory_id,
                "message": "legacy memory not found",
            }

        item = _with_shadow_metadata(
            legacy_memory_to_item(legacy),
            write_path=write_path,
            source=source,
            extra_metadata=extra_metadata,
        )
        inserted = await repo.insert_memory_item(item, ignore_existing=True)
        linked = False
        if inserted:
            linked = await repo.insert_memory_link(
                item["id"],
                legacy_memory_id,
                "legacy_memory",
                "shadowed_from",
            )
        return {
            "ok": True,
            "inserted": inserted,
            "linked": linked,
            "legacy_memory_id": legacy_memory_id,
            "memory_item_id": item["id"],
            "kind": item["kind"],
            "namespace": item["namespace"],
        }
    except Exception as exc:
        if strict:
            raise
        print(
            "[MemoryV2Shadow] shadow write failed "
            f"legacy={legacy_memory_id} path={write_path}: {exc}\n{traceback.format_exc()}"
        )
        return {
            "ok": False,
            "inserted": False,
            "legacy_memory_id": legacy_memory_id,
            "message": exc.__class__.__name__,
        }


async def shadow_new_legacy_memories(before_ids: set[str], *,
                                     write_path: str,
                                     source: str,
                                     extra_metadata: dict | None = None) -> dict:
    repo = MemoryRepository()
    try:
        rows = await repo.fetch_legacy_memories()
        new_rows = [row for row in rows if row["id"] not in before_ids]
        results = []
        for row in new_rows:
            results.append(await shadow_legacy_memory(
                row["id"],
                write_path=write_path,
                source=source,
                extra_metadata=extra_metadata,
            ))
        return {
            "ok": all(result.get("ok") for result in results),
            "selected": len(new_rows),
            "inserted": sum(1 for result in results if result.get("inserted")),
            "results": results,
        }
    except Exception as exc:
        print(
            "[MemoryV2Shadow] batch shadow write failed "
            f"path={write_path}: {exc}\n{traceback.format_exc()}"
        )
        return {
            "ok": False,
            "selected": 0,
            "inserted": 0,
            "message": exc.__class__.__name__,
            "results": [],
        }
