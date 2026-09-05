#!/usr/bin/env python3
"""Backfill memory_chunks from historical chat messages."""

from __future__ import annotations

import argparse
import asyncio
from datetime import datetime
import json
from pathlib import Path
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import aiosqlite

from config import DATA_DIR
from database import get_db, init_db
from app.memory_v2.chunks import build_chunks_from_messages, ensure_conversation_chunks


STATE_PATH = DATA_DIR / "memory_chunks_backfill_state.json"


def _load_state() -> dict:
    if not STATE_PATH.exists():
        return {"processed_conv_ids": []}
    try:
        data = json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {"processed_conv_ids": []}
    if not isinstance(data, dict):
        return {"processed_conv_ids": []}
    data.setdefault("processed_conv_ids", [])
    return data


def _save_state(state: dict) -> None:
    state["updated_at"] = time.time()
    STATE_PATH.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


async def _conversation_ids(*, conv_id: str | None, limit: int | None, resume: bool) -> list[str]:
    if conv_id:
        return [conv_id]
    if limit is not None:
        async with get_db() as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute(
                "SELECT conv_id FROM messages "
                "WHERE role IN ('user','assistant') "
                "ORDER BY created_at ASC LIMIT ?",
                (int(limit),),
            )
            rows = [dict(row) for row in await cur.fetchall()]
        ids = list(dict.fromkeys(row["conv_id"] for row in rows if row.get("conv_id")))
    else:
        async with get_db() as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute("SELECT id FROM conversations ORDER BY created_at ASC")
            rows = [dict(row) for row in await cur.fetchall()]
        ids = [row["id"] for row in rows if row.get("id")]
    if resume:
        processed = set(_load_state().get("processed_conv_ids") or [])
        ids = [item for item in ids if item not in processed]
    return ids


async def _fetch_messages(conv_id: str) -> list[dict]:
    async with get_db() as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT id, conv_id, role, content, created_at FROM messages "
            "WHERE conv_id=? AND role IN ('user','assistant') ORDER BY created_at ASC",
            (conv_id,),
        )
        rows = await cur.fetchall()
    return [dict(row) for row in rows]


async def _dry_run_conv(conv_id: str) -> dict:
    messages = await _fetch_messages(conv_id)
    chunks = build_chunks_from_messages(messages)
    return {
        "conv_id": conv_id,
        "scanned_messages": len(messages),
        "generated_chunks": len(chunks),
        "inserted_chunks": 0,
        "existing_chunks": 0,
        "embedding_selected": 0,
        "embedding_success": 0,
        "embedding_failed": 0,
    }


def _empty_stats(args) -> dict:
    return {
        "ok": True,
        "dry_run": bool(args.dry_run),
        "resume": bool(args.resume),
        "conv_id": args.conv_id,
        "limit": args.limit,
        "batch_size": args.batch_size,
        "sleep": args.sleep,
        "started_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "elapsed_seconds": 0.0,
        "conversations": 0,
        "scanned_messages": 0,
        "generated_chunks": 0,
        "inserted_chunks": 0,
        "existing_chunks": 0,
        "embedding_selected": 0,
        "embedding_success": 0,
        "embedding_failed": 0,
        "errors": 0,
    }


def _merge(stats: dict, result: dict) -> None:
    for key in (
        "scanned_messages",
        "generated_chunks",
        "inserted_chunks",
        "existing_chunks",
        "embedding_selected",
        "embedding_success",
        "embedding_failed",
    ):
        stats[key] += int(result.get(key) or 0)


async def run(args) -> dict:
    await init_db()
    start = time.perf_counter()
    stats = _empty_stats(args)
    conv_ids = await _conversation_ids(conv_id=args.conv_id, limit=args.limit, resume=args.resume)
    state = _load_state()
    processed = list(state.get("processed_conv_ids") or [])
    processed_set = set(processed)

    for index, conv_id in enumerate(conv_ids, 1):
        try:
            if args.dry_run:
                result = await _dry_run_conv(conv_id)
            else:
                result = await ensure_conversation_chunks(
                    conv_id,
                    embed=not args.no_embed,
                    batch_size=args.batch_size,
                    sleep_seconds=args.sleep,
                )
            stats["conversations"] += 1
            _merge(stats, result)
            print(
                f"[{index}/{len(conv_ids)}] conv={conv_id} "
                f"messages={result.get('scanned_messages', 0)} "
                f"chunks={result.get('generated_chunks', 0)} "
                f"inserted={result.get('inserted_chunks', 0)} "
                f"embedded={result.get('embedding_success', 0)} "
                f"failed={result.get('embedding_failed', 0)}"
            )
            if args.resume and not args.dry_run and conv_id not in processed_set:
                processed.append(conv_id)
                processed_set.add(conv_id)
                state["processed_conv_ids"] = processed
                _save_state(state)
        except Exception as exc:
            stats["errors"] += 1
            print(f"[error] conv={conv_id}: {exc}")
    stats["elapsed_seconds"] = round(time.perf_counter() - start, 3)
    return stats


def parse_args(argv: list[str] | None = None):
    parser = argparse.ArgumentParser(description="Backfill Memory V2 raw chunks")
    parser.add_argument("--dry-run", action="store_true", help="scan and build chunks without writing DB or embeddings")
    parser.add_argument("--limit", type=int, default=None, help="approximate max source messages to scan")
    parser.add_argument("--batch-size", type=int, default=8, help="embedding provider batch size")
    parser.add_argument("--sleep", type=float, default=0.0, help="seconds to sleep between embedding batches")
    parser.add_argument(
        "--no-embed",
        action="store_true",
        help="apply chunk reconciliation without generating missing embeddings",
    )
    parser.add_argument("--resume", action="store_true", help=f"skip conversations recorded in {STATE_PATH}")
    parser.add_argument("--conv-id", default=None, help="only backfill one conversation id")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    stats = asyncio.run(run(args))
    print(json.dumps(stats, ensure_ascii=False, indent=2))
    return 1 if stats.get("errors") else 0


if __name__ == "__main__":
    raise SystemExit(main())
