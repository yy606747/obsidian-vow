#!/usr/bin/env python3
"""Backfill Memory V2 notes from historical chat messages."""

from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import aiosqlite

from config import load_digest_anchor, load_worldbook, save_digest_anchor
from database import get_db, init_db
from memory import _call_flash_lite
from app.memory_v2.digest import _digest_prompt, _insert_note, _split_into_groups


async def _call_digest_with_retries(prompt: str, *, retries: int, retry_sleep: float) -> dict | None:
    attempts = max(0, int(retries)) + 1
    for attempt in range(1, attempts + 1):
        try:
            result = await _call_flash_lite(prompt, scope="memory:backfill_notes")
        except Exception as exc:
            result = None
            print(f"[backfill_notes] model exception attempt={attempt}/{attempts}: {exc}")
        if isinstance(result, dict):
            return result
        if attempt < attempts:
            print(f"[backfill_notes] model returned invalid result; retry {attempt}/{attempts - 1}")
            await asyncio.sleep(float(retry_sleep))
    return None


async def _fetch_messages(anchor_ts: float, *, limit: int | None = None) -> list[dict]:
    sql = (
        "SELECT id, conv_id, role, content, created_at FROM messages "
        "WHERE role IN ('user','assistant') AND created_at > ? "
        "ORDER BY created_at ASC"
    )
    params: list = [anchor_ts]
    if limit is not None:
        sql += " LIMIT ?"
        params.append(int(limit))
    async with get_db() as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(sql, params)
        rows = await cur.fetchall()
    return [dict(row) for row in rows]


async def run(args) -> dict:
    await init_db()
    started = time.perf_counter()
    anchor_ts = 0.0 if args.from_start else load_digest_anchor()
    messages = await _fetch_messages(anchor_ts, limit=args.limit)
    groups = _split_into_groups(messages, args.group_size)
    stats = {
        "ok": True,
        "dry_run": bool(args.dry_run),
        "from_start": bool(args.from_start),
        "resume_anchor_ts": anchor_ts,
        "limit": args.limit,
        "group_size": args.group_size,
        "sleep": args.sleep,
        "retries": args.retries,
        "processed_messages": len(messages),
        "groups": len(groups),
        "notes_inserted": 0,
        "model_failures": 0,
        "insert_failures": 0,
        "stopped_at_group": None,
        "last_success_anchor_ts": anchor_ts,
        "elapsed_seconds": 0.0,
    }
    if args.dry_run or not groups:
        stats["elapsed_seconds"] = round(time.perf_counter() - started, 3)
        return stats

    wb = load_worldbook()
    user_name = wb.get("user_name", "用户")
    ai_name = wb.get("ai_name", "AI")

    for index, group in enumerate(groups, 1):
        prompt = _digest_prompt(group, user_name=user_name, ai_name=ai_name)
        result = await _call_digest_with_retries(
            prompt,
            retries=args.retries,
            retry_sleep=args.retry_sleep,
        )
        if not isinstance(result, dict):
            stats["model_failures"] += 1
            stats["stopped_at_group"] = index
            print(
                f"[{index}/{len(groups)}] messages={len(group)} "
                f"model_failed=1 stopping=true"
            )
            break
        notes = result.get("notes") or []
        if not isinstance(notes, list):
            notes = []
        group_by_id = {row["id"]: row for row in group}
        group_insert_failures = 0
        for raw_note in notes:
            if not isinstance(raw_note, dict):
                continue
            try:
                inserted = await _insert_note(raw_note, group, group_by_id=group_by_id, broadcast=False)
            except Exception as exc:
                print(f"[backfill_notes] insert failed group={index}: {exc}")
                stats["insert_failures"] += 1
                group_insert_failures += 1
                continue
            if inserted:
                stats["notes_inserted"] += 1
        if group_insert_failures:
            stats["stopped_at_group"] = index
            print(
                f"[{index}/{len(groups)}] messages={len(group)} "
                f"insert_failures={group_insert_failures} stopping=true"
            )
            break
        if args.resume:
            anchor = float(group[-1]["created_at"])
            save_digest_anchor(anchor)
            stats["last_success_anchor_ts"] = anchor
        print(
            f"[{index}/{len(groups)}] messages={len(group)} "
            f"notes_inserted={stats['notes_inserted']} "
            f"model_failures={stats['model_failures']}"
        )
        if args.sleep > 0 and index < len(groups):
            await asyncio.sleep(float(args.sleep))

    stats["ok"] = (
        stats["model_failures"] == 0
        and stats["insert_failures"] == 0
        and stats["stopped_at_group"] is None
    )
    stats["elapsed_seconds"] = round(time.perf_counter() - started, 3)
    return stats


def parse_args(argv: list[str] | None = None):
    parser = argparse.ArgumentParser(description="Backfill Memory V2 short notes")
    parser.add_argument("--dry-run", action="store_true", help="count messages/groups without model calls or writes")
    parser.add_argument("--limit", type=int, default=None, help="max source messages to process")
    parser.add_argument("--group-size", type=int, default=20, help="messages per digest group")
    parser.add_argument("--sleep", type=float, default=0.0, help="seconds to sleep between model calls")
    parser.add_argument("--retries", type=int, default=2, help="model retries per group before stopping")
    parser.add_argument("--retry-sleep", type=float, default=2.0, help="seconds to sleep between retries")
    parser.add_argument("--resume", action="store_true", help="advance digest_anchor after each successful group")
    parser.add_argument("--from-start", action="store_true", help="ignore digest_anchor and start from timestamp 0")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    stats = asyncio.run(run(parse_args(argv)))
    print(json.dumps(stats, ensure_ascii=False, indent=2))
    return 0 if stats.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
