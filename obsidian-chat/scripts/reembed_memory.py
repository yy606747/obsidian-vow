#!/usr/bin/env python3
"""Stage and atomically apply a full memory embedding model migration.

The expensive provider phase never writes the business database.  It stores
vectors in a resumable sidecar SQLite database keyed by table/id/content hash.
Only a complete, hash-matched stage can be applied, and apply is one SQLite
transaction.  Run the chat service stopped for ``apply``.
"""

from __future__ import annotations

import argparse
import asyncio
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sqlite3
import sys
import time
from typing import Iterable


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.memory_v2 import embedding
from config import DB_PATH


STAGE_SCHEMA_VERSION = "memory-reembed-stage.v1"
DEFAULT_MAX_ACTIVE_CHUNKS = 5_000


@dataclass(frozen=True)
class TargetSpec:
    table: str
    where: str


TARGETS = (
    TargetSpec(
        table="memory_chunks",
        where="TRIM(content) != '' AND status IN ('active','cold')",
    ),
    TargetSpec(
        table="memory_items",
        where="TRIM(content) != '' AND status='active' AND visibility='prompt'",
    ),
    TargetSpec(
        table="memories",
        where="TRIM(content) != ''",
    ),
)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _content_hash(content: str) -> str:
    return _sha256_bytes(str(content).encode("utf-8", errors="surrogatepass"))


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _json_dump(path: Path, payload: dict) -> None:
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _business_connection(*, read_only: bool) -> sqlite3.Connection:
    path = Path(DB_PATH).resolve()
    if read_only:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=30)
        conn.execute("PRAGMA query_only=ON")
    else:
        conn = sqlite3.connect(path, timeout=60)
    conn.row_factory = sqlite3.Row
    return conn


def _target_rows(conn: sqlite3.Connection, spec: TargetSpec) -> list[dict]:
    rows = conn.execute(
        f"SELECT id, content FROM {spec.table} WHERE {spec.where} ORDER BY id ASC"
    ).fetchall()
    return [{"id": str(row["id"]), "content": str(row["content"])} for row in rows]


def _target_inventory(conn: sqlite3.Connection) -> dict:
    tables: dict[str, dict] = {}
    for spec in TARGETS:
        row = conn.execute(
            f"SELECT COUNT(*) AS n, COALESCE(SUM(LENGTH(content)),0) AS chars "
            f"FROM {spec.table} WHERE {spec.where}"
        ).fetchone()
        tables[spec.table] = {"rows": int(row["n"]), "chars": int(row["chars"])}
    totals = {
        "rows": sum(item["rows"] for item in tables.values()),
        "chars": sum(item["chars"] for item in tables.values()),
    }
    return {"tables": tables, "totals": totals}


def _config_from_args(args) -> dict:
    overrides = {
        "provider": args.provider,
        "model": args.model,
        "dimensions": args.dimensions,
        "batch_size": args.batch_size,
        "request_interval_sec": args.request_interval_sec,
        "max_retries": args.max_retries,
        "initial_backoff_sec": args.initial_backoff_sec,
        "max_backoff_sec": args.max_backoff_sec,
        "jitter_ratio": args.jitter_ratio,
        "timeout_sec": args.timeout_sec,
        "profile": "retrieval-v1",
    }
    return embedding.load_embedding_config(overrides)


def _public_config(config: dict) -> dict:
    public = {
        key: config[key]
        for key in (
            "provider",
            "model",
            "dimensions",
            "batch_size",
            "request_interval_sec",
            "timeout_sec",
            "max_retries",
            "initial_backoff_sec",
            "max_backoff_sec",
            "jitter_ratio",
            "profile",
            "signature",
        )
    }
    public["proxy_enabled"] = bool(config.get("proxy_url"))
    return public


def _ensure_stage_schema(conn: sqlite3.Connection, *, config: dict) -> None:
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=FULL")
    conn.execute(
        "CREATE TABLE IF NOT EXISTS stage_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)"
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS staged_vectors (
            owner_table TEXT NOT NULL,
            owner_id TEXT NOT NULL,
            content_sha256 TEXT NOT NULL,
            embedding BLOB NOT NULL,
            dimensions INTEGER NOT NULL,
            signature TEXT NOT NULL,
            prompt_tokens INTEGER NOT NULL DEFAULT 0,
            created_at REAL NOT NULL,
            PRIMARY KEY (owner_table, owner_id)
        )
        """
    )
    expected = {
        "schema_version": STAGE_SCHEMA_VERSION,
        "signature": str(config["signature"]),
        "dimensions": str(config["dimensions"]),
    }
    existing = {
        str(row[0]): str(row[1])
        for row in conn.execute("SELECT key, value FROM stage_meta").fetchall()
    }
    for key, value in expected.items():
        if key in existing and existing[key] != value:
            raise RuntimeError(
                f"stage metadata mismatch for {key}: {existing[key]!r} != {value!r}"
            )
        conn.execute(
            "INSERT INTO stage_meta(key,value) VALUES(?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )
    conn.commit()


def _stage_count(conn: sqlite3.Connection, signature: str) -> int:
    row = conn.execute(
        "SELECT COUNT(*) FROM staged_vectors WHERE signature=?", (signature,)
    ).fetchone()
    return int(row[0] or 0)


def _iter_batches(values: list[dict], size: int) -> Iterable[list[dict]]:
    for start in range(0, len(values), size):
        yield values[start: start + size]


def _plan(args) -> dict:
    config = _config_from_args(args)
    with _business_connection(read_only=True) as conn:
        inventory = _target_inventory(conn)
    # Chinese text is commonly near one token per character.  Use a deliberately
    # wider range for a budget guard; provider usage metadata remains canonical.
    estimated_low_tokens = int(inventory["totals"]["chars"] * 0.70)
    estimated_high_tokens = int(inventory["totals"]["chars"] * 1.35)
    price = float(args.usd_per_million_tokens)
    return {
        "ok": True,
        "command": "plan",
        "business_db": str(Path(DB_PATH).resolve()),
        "config": _public_config(config),
        "inventory": inventory,
        "estimated_input_tokens": {
            "low": estimated_low_tokens,
            "high": estimated_high_tokens,
        },
        "estimated_usd": {
            "low": round(estimated_low_tokens / 1_000_000 * price, 6),
            "high": round(estimated_high_tokens / 1_000_000 * price, 6),
            "unit_usd_per_million_tokens": price,
        },
        "generated_at": _utc_now(),
    }


async def _stage(args) -> dict:
    config = _config_from_args(args)
    run_dir = Path(args.run_dir).resolve()
    run_dir.mkdir(parents=True, exist_ok=True)
    stage_path = run_dir / "vectors.private.sqlite3"

    with _business_connection(read_only=True) as business:
        inventory = _target_inventory(business)
        chunk_count = inventory["tables"]["memory_chunks"]["rows"]
        if chunk_count > int(args.max_active_chunks):
            raise RuntimeError(
                f"active chunk count {chunk_count} exceeds safety ceiling "
                f"{args.max_active_chunks}; run full reconciliation first"
            )
        target_rows = [
            {"table": spec.table, **row}
            for spec in TARGETS
            for row in _target_rows(business, spec)
        ]

    stage = sqlite3.connect(stage_path, timeout=60)
    stage.row_factory = sqlite3.Row
    try:
        _ensure_stage_schema(stage, config=config)
        target_keys = {(row["table"], row["id"]) for row in target_rows}
        staged_rows = stage.execute(
            "SELECT owner_table, owner_id, content_sha256 FROM staged_vectors "
            "WHERE signature=? AND dimensions=?",
            (config["signature"], config["dimensions"]),
        ).fetchall()
        stale_keys = [
            (str(row["owner_table"]), str(row["owner_id"]))
            for row in staged_rows
            if (str(row["owner_table"]), str(row["owner_id"])) not in target_keys
        ]
        if stale_keys:
            stage.executemany(
                "DELETE FROM staged_vectors WHERE owner_table=? AND owner_id=? AND signature=?",
                [(*key, config["signature"]) for key in stale_keys],
            )
            stage.commit()
        existing = {
            (str(row["owner_table"]), str(row["owner_id"])): str(row["content_sha256"])
            for row in staged_rows
            if (str(row["owner_table"]), str(row["owner_id"])) in target_keys
        }
        pending = [
            row for row in target_rows
            if existing.get((row["table"], row["id"])) != _content_hash(row["content"])
        ]
        stats = {
            "target_rows": len(target_rows),
            "already_staged": len(target_rows) - len(pending),
            "pending_at_start": len(pending),
            "pruned_stale": len(stale_keys),
            "staged_this_run": 0,
            "failed": 0,
            "provider_requests": 0,
            "provider_retries": 0,
            "rate_limited": 0,
            "prompt_tokens": 0,
        }
        started = time.perf_counter()
        batches = list(_iter_batches(pending, int(config["batch_size"])))
        for index, batch in enumerate(batches, 1):
            result = await embedding.embed_texts_detailed(
                [row["content"] for row in batch],
                purpose="document",
                config=config,
            )
            stats["provider_requests"] += result.requests
            stats["provider_retries"] += result.retries
            stats["rate_limited"] += result.rate_limited
            stats["prompt_tokens"] += result.prompt_tokens
            now = time.time()
            for row, vector in zip(batch, result.vectors):
                if not vector or len(vector) != int(config["dimensions"]):
                    stats["failed"] += 1
                    continue
                stage.execute(
                    "INSERT INTO staged_vectors "
                    "(owner_table,owner_id,content_sha256,embedding,dimensions,signature,prompt_tokens,created_at) "
                    "VALUES(?,?,?,?,?,?,?,?) "
                    "ON CONFLICT(owner_table,owner_id) DO UPDATE SET "
                    "content_sha256=excluded.content_sha256, embedding=excluded.embedding, "
                    "dimensions=excluded.dimensions, signature=excluded.signature, "
                    "prompt_tokens=excluded.prompt_tokens, created_at=excluded.created_at",
                    (
                        row["table"],
                        row["id"],
                        _content_hash(row["content"]),
                        embedding.pack_embedding(vector),
                        config["dimensions"],
                        config["signature"],
                        0,
                        now,
                    ),
                )
                stats["staged_this_run"] += 1
            stage.commit()
            print(
                f"[stage {index}/{len(batches)}] "
                f"stored={stats['staged_this_run']} failed={stats['failed']} "
                f"retries={stats['provider_retries']} 429={stats['rate_limited']}",
                flush=True,
            )

        stats["stage_rows_after"] = _stage_count(stage, str(config["signature"]))
        stats["elapsed_seconds"] = round(time.perf_counter() - started, 3)
        complete = stats["stage_rows_after"] == len(target_rows) and stats["failed"] == 0
        stage.execute(
            "INSERT INTO stage_meta(key,value) VALUES('complete',?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            ("true" if complete else "false",),
        )
        stage.execute(
            "INSERT INTO stage_meta(key,value) VALUES('completed_at',?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (_utc_now() if complete else "",),
        )
        stage.commit()
    finally:
        stage.close()

    report = {
        "ok": complete,
        "command": "stage",
        "config": _public_config(config),
        "inventory": inventory,
        "stats": stats,
        "stage_db": str(stage_path),
        "stage_db_sha256": _file_sha256(stage_path),
        "generated_at": _utc_now(),
    }
    _json_dump(run_dir / "stage_report.safe.json", report)
    return report


def _stage_rows(stage: sqlite3.Connection, *, signature: str) -> list[sqlite3.Row]:
    return stage.execute(
        "SELECT owner_table, owner_id, content_sha256, embedding, dimensions, signature "
        "FROM staged_vectors WHERE signature=? ORDER BY owner_table, owner_id",
        (signature,),
    ).fetchall()


def _apply(args) -> dict:
    if not args.yes:
        raise RuntimeError("apply requires --yes")
    config = _config_from_args(args)
    run_dir = Path(args.run_dir).resolve()
    stage_path = run_dir / "vectors.private.sqlite3"
    if not stage_path.exists():
        raise RuntimeError(f"missing stage database: {stage_path}")

    stage = sqlite3.connect(f"file:{stage_path}?mode=ro", uri=True, timeout=30)
    stage.row_factory = sqlite3.Row
    try:
        meta = {
            str(row[0]): str(row[1])
            for row in stage.execute("SELECT key,value FROM stage_meta").fetchall()
        }
        if meta.get("complete") != "true":
            raise RuntimeError("stage is not marked complete")
        if meta.get("signature") != str(config["signature"]):
            raise RuntimeError("stage signature does not match requested target")
        staged = _stage_rows(stage, signature=str(config["signature"]))
    finally:
        stage.close()

    business = _business_connection(read_only=False)
    started = time.perf_counter()
    updated: dict[str, int] = {}
    try:
        business.execute("PRAGMA foreign_keys=ON")
        business.execute("BEGIN IMMEDIATE")
        inventory = _target_inventory(business)
        current = {
            (spec.table, row["id"]): _content_hash(row["content"])
            for spec in TARGETS
            for row in _target_rows(business, spec)
        }
        staged_keys = {(str(row["owner_table"]), str(row["owner_id"])) for row in staged}
        if set(current) != staged_keys:
            missing = sorted(set(current) - staged_keys)[:5]
            extra = sorted(staged_keys - set(current))[:5]
            raise RuntimeError(
                f"stage target set drifted: missing={missing!r} extra={extra!r}"
            )
        mismatched = [
            (str(row["owner_table"]), str(row["owner_id"]))
            for row in staged
            if current[(str(row["owner_table"]), str(row["owner_id"]))]
            != str(row["content_sha256"])
        ]
        if mismatched:
            raise RuntimeError(f"content drift detected for {len(mismatched)} rows")
        for spec in TARGETS:
            table_rows = [row for row in staged if str(row["owner_table"]) == spec.table]
            business.executemany(
                f"UPDATE {spec.table} SET embedding=? WHERE id=?",
                [(row["embedding"], str(row["owner_id"])) for row in table_rows],
            )
            updated[spec.table] = len(table_rows)
        expected_bytes = int(config["dimensions"]) * 4
        invalid = {}
        for spec in TARGETS:
            row = business.execute(
                f"SELECT COUNT(*) FROM {spec.table} WHERE {spec.where} "
                "AND (embedding IS NULL OR LENGTH(embedding) != ?)",
                (expected_bytes,),
            ).fetchone()
            invalid[spec.table] = int(row[0] or 0)
        if any(invalid.values()):
            raise RuntimeError(f"post-update dimension validation failed: {invalid}")
        business.commit()
    except BaseException:
        business.rollback()
        raise
    finally:
        business.close()

    report = {
        "ok": True,
        "command": "apply",
        "config": _public_config(config),
        "updated": updated,
        "inventory": inventory,
        "elapsed_seconds": round(time.perf_counter() - started, 3),
        "applied_at": _utc_now(),
    }
    _json_dump(run_dir / "apply_report.safe.json", report)
    return report


def _verify(args) -> dict:
    config = _config_from_args(args)
    expected_bytes = int(config["dimensions"]) * 4
    with _business_connection(read_only=True) as conn:
        inventory = _target_inventory(conn)
        tables = {}
        for spec in TARGETS:
            row = conn.execute(
                f"SELECT COUNT(*) AS n, "
                "SUM(embedding IS NULL) AS missing, "
                "SUM(embedding IS NOT NULL AND LENGTH(embedding) != ?) AS wrong_dims "
                f"FROM {spec.table} WHERE {spec.where}",
                (expected_bytes,),
            ).fetchone()
            tables[spec.table] = {
                "rows": int(row["n"] or 0),
                "missing": int(row["missing"] or 0),
                "wrong_dimensions": int(row["wrong_dims"] or 0),
            }
        quick_check = str(conn.execute("PRAGMA quick_check").fetchone()[0])
    ok = quick_check == "ok" and all(
        item["missing"] == 0 and item["wrong_dimensions"] == 0
        for item in tables.values()
    )
    return {
        "ok": ok,
        "command": "verify",
        "config": _public_config(config),
        "inventory": inventory,
        "tables": tables,
        "quick_check": quick_check,
        "verified_at": _utc_now(),
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("plan", "stage", "apply", "verify"))
    parser.add_argument("--run-dir", default="", help="private resumable stage directory")
    parser.add_argument("--provider", default="gemini")
    parser.add_argument("--model", default="gemini-embedding-2")
    parser.add_argument("--dimensions", type=int, default=768)
    parser.add_argument("--batch-size", type=int, default=32)
    parser.add_argument("--request-interval-sec", type=float, default=1.0)
    parser.add_argument("--timeout-sec", type=float, default=90.0)
    parser.add_argument("--max-retries", type=int, default=8)
    parser.add_argument("--initial-backoff-sec", type=float, default=2.0)
    parser.add_argument("--max-backoff-sec", type=float, default=90.0)
    parser.add_argument("--jitter-ratio", type=float, default=0.25)
    parser.add_argument("--max-active-chunks", type=int, default=DEFAULT_MAX_ACTIVE_CHUNKS)
    parser.add_argument("--usd-per-million-tokens", type=float, default=0.20)
    parser.add_argument("--yes", action="store_true", help="confirm atomic business DB update")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.command in {"stage", "apply"} and not str(args.run_dir).strip():
        raise SystemExit("--run-dir is required for stage/apply")
    if args.command == "plan":
        result = _plan(args)
    elif args.command == "stage":
        result = asyncio.run(_stage(args))
    elif args.command == "apply":
        result = _apply(args)
    else:
        result = _verify(args)
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
