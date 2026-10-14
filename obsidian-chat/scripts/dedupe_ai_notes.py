#!/usr/bin/env python3
"""Plan, archive, or restore redundant AI notes without deleting their text."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
from pathlib import Path
import sys
import time
import uuid

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.memory_v2 import ai_note_dedup
from database import get_db


async def plan_cleanup(*, db_factory=get_db, judge=None) -> dict:
    async with db_factory() as db:
        rows = await ai_note_dedup.fetch_notes(db)
    semaphore = asyncio.Semaphore(3)

    async def compare(index, row):
        async with semaphore:
            duplicate = await ai_note_dedup.find_duplicate(
                row["content"], row.get("embedding"), row["created_at"],
                rows=rows[:index], judge=judge,
            )
        if not duplicate:
            return None
        return {"id": row["id"], "duplicate_of": duplicate["id"],
                "reason": duplicate["dedup_reason"]}

    matches = await asyncio.gather(*(compare(i, row) for i, row in enumerate(rows)))
    # Every edge points to an older note. Resolve chains to a retained root.
    roots = {}
    pairs = []
    for match in matches:
        if match:
            target = match["duplicate_of"]
            roots[match["id"]] = roots.get(target, target)
            pairs.append({**match, "duplicate_of": roots[match["id"]]})
    return {
        "run_id": "ai_note_dedup_" + uuid.uuid4().hex[:12],
        "created_at": time.time(), "checked": len(rows), "pairs": pairs,
        "notes": {row["id"]: {key: value for key, value in row.items() if key != "embedding"} for row in rows},
    }


async def apply_plan(plan: dict, *, db_factory=get_db) -> dict:
    archived, skipped = [], []
    async with ai_note_dedup.write_lock():
        async with db_factory() as db:
            await db.execute("BEGIN IMMEDIATE")
            try:
                for pair in plan["pairs"]:
                    source = await ai_note_dedup.fetch_notes(db, item_id=pair["id"])
                    target = await ai_note_dedup.fetch_notes(db, item_id=pair["duplicate_of"])
                    if (not source or not target or source[0]["content"] != plan["notes"][pair["id"]]["content"]
                            or target[0]["content"] != plan["notes"][pair["duplicate_of"]]["content"]):
                        skipped.append(pair["id"])
                        continue
                    await ai_note_dedup.ensure_shadow_in_tx(db, source[0])
                    await ai_note_dedup.ensure_shadow_in_tx(db, target[0])
                    metadata = json.loads(source[0]["metadata_json"] or "{}")
                    metadata["ai_note_dedup_archive"] = {**pair, "run_id": plan["run_id"]}
                    await db.execute(
                        "UPDATE memory_items SET status='archived',visibility='hidden',metadata_json=? WHERE id=?",
                        (json.dumps(metadata, ensure_ascii=False), pair["id"]),
                    )
                    archived.append(pair["id"])
                await db.commit()
            except BaseException:
                await db.rollback()
                raise
    if archived:
        from app.memory_v2.hybrid_recall import invalidate_full_corpus_cache
        invalidate_full_corpus_cache(notes=True)
    return {"archived": archived, "skipped": skipped}


async def restore_plan(plan: dict, *, db_factory=get_db) -> dict:
    restored = []
    async with db_factory() as db:
        await db.execute("BEGIN IMMEDIATE")
        try:
            for pair in plan["pairs"]:
                cur = await db.execute("SELECT metadata_json FROM memory_items WHERE id=? AND status='archived'", (pair["id"],))
                row = await cur.fetchone()
                if not row:
                    continue
                metadata = json.loads(row[0] or "{}")
                if metadata.get("ai_note_dedup_archive", {}).get("run_id") != plan["run_id"]:
                    continue
                metadata.pop("ai_note_dedup_archive", None)
                await db.execute(
                    "UPDATE memory_items SET status='active',visibility='prompt',metadata_json=? WHERE id=?",
                    (json.dumps(metadata, ensure_ascii=False), pair["id"]),
                )
                restored.append(pair["id"])
            await db.commit()
        except BaseException:
            await db.rollback()
            raise
    if restored:
        from app.memory_v2.hybrid_recall import invalidate_full_corpus_cache
        invalidate_full_corpus_cache(notes=True)
    return {"restored": restored}


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("plan", "apply", "restore"))
    parser.add_argument("path", type=Path, help="Private JSON plan; retained for undo")
    args = parser.parse_args()
    if args.action == "plan":
        plan = await plan_cleanup()
        args.path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(args.path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as stream:
            json.dump(plan, stream, ensure_ascii=False, indent=2)
        print(json.dumps({"checked": plan["checked"], "duplicates": len(plan["pairs"]), "plan": str(args.path)}))
    else:
        plan = json.loads(args.path.read_text())
        operation = apply_plan if args.action == "apply" else restore_plan
        print(json.dumps(await operation(plan)))


if __name__ == "__main__":
    asyncio.run(main())
