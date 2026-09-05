#!/usr/bin/env python3
"""Repair Working Model memory outcomes that failed before the defaults fix.

The repair is deliberately provider-free.  It reuses each durable request's
statement and provenance, stores an AI-authored note with a NULL embedding,
and changes only ``memory_write_failed`` terminal rows.  Re-running is
idempotent because repaired rows no longer match the candidate predicate and
their child ids are deterministic.
"""

from __future__ import annotations

import argparse
import asyncio
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sqlite3
import sys
import time
from typing import Any

import aiosqlite


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.memory_v2.repository import create_working_model_ai_note_in_tx  # noqa: E402
from app.working_model import repository as wm_repository  # noqa: E402
from app.working_model.runtime import (  # noqa: E402
    stable_working_model_memory_id,
    validate_terminal_request_outcome,
)
from config import DB_PATH  # noqa: E402


FAILURE_CODE = "memory_write_failed"


class WorkingModelMemoryRepairError(RuntimeError):
    """The stored failed request cannot be repaired without guessing."""


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def _read_candidates(db_path: Path) -> list[dict[str, Any]]:
    if not db_path.is_file():
        raise WorkingModelMemoryRepairError(f"database does not exist: {db_path}")
    uri = f"file:{db_path.resolve()}?mode=ro"
    with sqlite3.connect(uri, uri=True) as db:
        db.row_factory = sqlite3.Row
        columns = {
            str(row[1])
            for row in db.execute("PRAGMA table_info(working_model_requests)")
        }
        required = {
            "id",
            "conv_id",
            "statement",
            "route",
            "gate_reason",
            "gate_model",
            "gate_prompt_version",
            "writer_model",
            "writer_prompt_version",
            "writer_change_note",
            "status",
            "failure_code",
            "created_at",
        }
        missing = sorted(required - columns)
        if missing:
            raise WorkingModelMemoryRepairError(
                "working_model_requests is missing columns: " + ", ".join(missing)
            )
        rows = db.execute(
            "SELECT rowid AS _rowid, * FROM working_model_requests "
            "WHERE status='failed' AND failure_code=? ORDER BY rowid",
            (FAILURE_CODE,),
        ).fetchall()
    return [dict(row) for row in rows]


def _target_disposition(row: dict[str, Any]) -> str | None:
    route = str(row.get("route") or "")
    if route == "memory":
        return None
    if route == "working_model":
        if not str(row.get("writer_prompt_version") or "").strip():
            raise WorkingModelMemoryRepairError(
                f"working-model memory failure lacks writer provenance: {row.get('id')}"
            )
        return "memory"
    raise WorkingModelMemoryRepairError(
        f"unsupported memory failure route {route!r}: {row.get('id')}"
    )


def inspect_repair(*, db_path: Path) -> dict[str, Any]:
    rows = _read_candidates(db_path)
    candidates = []
    for row in rows:
        disposition = _target_disposition(row)
        request_id = str(row["id"])
        candidates.append({
            "rowid": int(row.get("_rowid") or 0),
            "request_id": request_id,
            "route": str(row.get("route") or ""),
            "target_disposition": disposition,
            "memory_id": stable_working_model_memory_id(request_id),
        })
    return {
        "ok": True,
        "mode": "dry_run",
        "provider_calls": 0,
        "database": str(db_path.resolve()),
        "candidate_count": len(candidates),
        "candidates": candidates,
    }


async def _repair_one(
    db_path: Path,
    *,
    row: dict[str, Any],
    repaired_at: float,
) -> dict[str, Any]:
    request_id = str(row["id"])
    route = str(row.get("route") or "")
    disposition = _target_disposition(row)
    memory_id = stable_working_model_memory_id(request_id)
    created_at = float(row.get("created_at") or repaired_at)

    async with aiosqlite.connect(db_path, timeout=30.0) as db:
        await db.execute("PRAGMA foreign_keys=ON")
        await db.execute("BEGIN IMMEDIATE")
        try:
            current = await wm_repository.get_request(db, request_id)
            if current is None:
                raise WorkingModelMemoryRepairError(
                    f"request disappeared during repair: {request_id}"
                )
            if not (
                current.get("status") == "failed"
                and current.get("failure_code") == FAILURE_CODE
            ):
                await db.rollback()
                return {
                    "request_id": request_id,
                    "status": "already_repaired_or_changed",
                    "memory_id": current.get("resulting_memory_id"),
                }

            memory = await create_working_model_ai_note_in_tx(
                db,
                memory_id=memory_id,
                content=str(current.get("statement") or ""),
                source_conv=current.get("conv_id"),
                origin_request_id=request_id,
                embedding_blob=None,
                created_at=created_at,
                importance=0.6,
            )
            validate_terminal_request_outcome(
                status="routed",
                route=route,
                disposition=disposition,
                resulting_memory_id=memory_id,
                failure_code=None,
                parse_error_code=None,
            )
            updated = await wm_repository.update_request(
                db,
                request_id=request_id,
                status="routed",
                updated_at=repaired_at,
                route=route,
                gate_reason=current.get("gate_reason"),
                gate_model=current.get("gate_model"),
                gate_prompt_version=current.get("gate_prompt_version"),
                disposition=disposition,
                writer_model=current.get("writer_model"),
                writer_prompt_version=current.get("writer_prompt_version"),
                writer_change_note=current.get("writer_change_note"),
                resulting_memory_id=memory_id,
                failure_code=None,
                parse_error_code=None,
            )
            if not updated:
                raise WorkingModelMemoryRepairError(
                    f"request update failed during repair: {request_id}"
                )
            await db.commit()
        except BaseException:
            await db.rollback()
            raise

    return {
        "request_id": request_id,
        "status": "repaired",
        "memory_id": memory_id,
        "memory_item_id": memory.get("memory_item_id"),
        "embedding": "NULL",
    }


async def apply_repair(*, db_path: Path) -> dict[str, Any]:
    rows = _read_candidates(db_path)
    repaired_at = time.time()
    results = []
    errors = []
    for row in rows:
        try:
            results.append(await _repair_one(
                db_path,
                row=row,
                repaired_at=repaired_at,
            ))
        except Exception as exc:
            errors.append({
                "request_id": str(row.get("id") or ""),
                "error": f"{exc.__class__.__name__}:{exc}",
            })

    repaired_count = sum(row.get("status") == "repaired" for row in results)
    if repaired_count:
        from app.memory_v2.hybrid_recall import invalidate_full_corpus_cache

        invalidate_full_corpus_cache(notes=True)
    remaining = len(_read_candidates(db_path))
    return {
        "ok": not errors and remaining == 0,
        "mode": "apply",
        "provider_calls": 0,
        "database": str(db_path.resolve()),
        "started_candidate_count": len(rows),
        "repaired_count": repaired_count,
        "remaining_candidate_count": remaining,
        "results": results,
        "errors": errors,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=DB_PATH)
    parser.add_argument(
        "--apply",
        action="store_true",
        help="write repaired AI notes and terminal outcomes; default is read-only",
    )
    parser.add_argument(
        "--report",
        type=Path,
        help="optional JSON report path (contains ids and counts, never message text)",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        result = (
            asyncio.run(apply_repair(db_path=args.db))
            if args.apply
            else inspect_repair(db_path=args.db)
        )
    except Exception as exc:
        result = {
            "ok": False,
            "mode": "apply" if args.apply else "dry_run",
            "provider_calls": 0,
            "error": f"{exc.__class__.__name__}:{exc}",
        }
    result["generated_at"] = _utc_now()
    if args.report:
        _atomic_write_json(args.report, result)
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
