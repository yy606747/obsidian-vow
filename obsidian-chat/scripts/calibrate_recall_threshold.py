#!/usr/bin/env python3
"""Calibrate recall score thresholds without emitting private message text."""

from __future__ import annotations

import argparse
import asyncio
from contextlib import contextmanager
from datetime import datetime, timezone
import importlib
import json
from pathlib import Path
import random
import sqlite3
import sys
from typing import Iterable


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.memory_v2 import embedding
from config import DB_PATH


recall = importlib.import_module("app.memory_v2.hybrid_recall")


def _percentile(values: list[float], ratio: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(float(value) for value in values)
    index = min(max(int(round((len(ordered) - 1) * ratio)), 0), len(ordered) - 1)
    return round(ordered[index], 4)


def _summary(values: Iterable[float]) -> dict:
    data = [float(value) for value in values]
    return {
        "min": round(min(data), 4) if data else 0.0,
        "p25": _percentile(data, 0.25),
        "p50": _percentile(data, 0.50),
        "p75": _percentile(data, 0.75),
        "p95": _percentile(data, 0.95),
        "max": round(max(data), 4) if data else 0.0,
        "mean": round(sum(data) / len(data), 4) if data else 0.0,
    }


def _rows(conn: sqlite3.Connection, sql: str, params: tuple) -> list[dict]:
    return [dict(row) for row in conn.execute(sql, params).fetchall()]


def _sample_queries(
    conn: sqlite3.Connection,
    *,
    recent_pool: int,
    sample_size: int,
    seed: int,
) -> list[dict]:
    pool = _rows(
        conn,
        "SELECT id, content, created_at FROM messages "
        "WHERE role='user' AND LENGTH(TRIM(content)) BETWEEN 8 AND 1200 "
        "ORDER BY created_at DESC LIMIT ?",
        (int(recent_pool),),
    )
    if len(pool) < sample_size:
        raise RuntimeError(f"only {len(pool)} eligible query messages for sample_size={sample_size}")
    selected = random.Random(seed).sample(pool, sample_size)
    return sorted(selected, key=lambda row: (float(row["created_at"]), str(row["id"])))


def _candidate_rows(
    conn: sqlite3.Connection,
    *,
    as_of_ts: float,
    candidate_limit: int,
) -> tuple[list[dict], list[dict]]:
    chunks = _rows(
        conn,
        "SELECT id, conv_id, message_ids_json, content, created_at, updated_at, "
        "source_hash, embedding, keywords_json, metadata_json "
        "FROM memory_chunks WHERE TRIM(content) != '' "
        "AND status IN ('active','cold') AND updated_at < ? "
        "ORDER BY updated_at DESC LIMIT ?",
        (float(as_of_ts), int(candidate_limit)),
    )
    notes = _rows(
        conn,
        "SELECT * FROM memory_items WHERE status='active' AND visibility='prompt' "
        "AND TRIM(content) != '' AND updated_at < ? "
        "ORDER BY updated_at DESC LIMIT ?",
        (float(as_of_ts), int(candidate_limit)),
    )
    return chunks, notes


@contextmanager
def _as_of_clock(as_of_ts: float):
    original = recall.time.time
    recall.time.time = lambda: float(as_of_ts)
    try:
        yield
    finally:
        recall.time.time = original


def _score_query(
    conn: sqlite3.Connection,
    *,
    query: dict,
    query_embedding: list[float],
    candidate_limit: int,
    chunk_top_k: int,
    note_top_k: int,
    top_k: int,
    thresholds: list[float],
) -> dict:
    as_of_ts = float(query["created_at"])
    chunks, notes = _candidate_rows(
        conn,
        as_of_ts=as_of_ts,
        candidate_limit=candidate_limit,
    )
    terms = recall._terms(str(query["content"]), None)
    chunk_sims = recall._batch_similarities(chunks, query_embedding)
    note_sims = recall._batch_similarities(notes, query_embedding)
    with _as_of_clock(as_of_ts):
        scored_chunks = [
            recall._score_chunk(row, terms, chunk_sims[index], None)
            for index, row in enumerate(chunks)
        ]
        scored_notes = [
            recall._score_note(row, terms, note_sims[index], None)
            for index, row in enumerate(notes)
        ]
    scored_chunks.sort(key=lambda item: item["score"], reverse=True)
    scored_notes.sort(key=lambda item: item["score"], reverse=True)
    wide = recall._dedupe(
        [*scored_chunks[: max(chunk_top_k, 0)], *scored_notes[: max(note_top_k, 0)]]
    )
    wide.sort(key=lambda item: item["score"], reverse=True)
    counts = {}
    for threshold in thresholds:
        selected = [
            item
            for item in wide
            if float(item.get("score") or 0) >= threshold
            or float(item.get("keyword_relevance") or 0) >= 0.35
        ][: max(top_k, 0)]
        counts[f"{threshold:.2f}"] = len(selected)
    return {
        "candidate_chunks": len(chunks),
        "candidate_notes": len(notes),
        "wide_count": len(wide),
        "top_score": float(wide[0]["score"]) if wide else 0.0,
        "top_semantic": float(wide[0]["semantic_similarity"]) if wide else 0.0,
        "counts": counts,
    }


async def _run(args) -> dict:
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
    config = embedding.load_embedding_config(overrides)
    thresholds = sorted({float(value) for value in args.thresholds.split(",")})
    conn = sqlite3.connect(f"file:{Path(DB_PATH).resolve()}?mode=ro", uri=True, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only=ON")
    try:
        queries = _sample_queries(
            conn,
            recent_pool=args.recent_pool,
            sample_size=args.sample_size,
            seed=args.seed,
        )
        batch = await embedding.embed_texts_detailed(
            [str(row["content"]) for row in queries],
            purpose="query",
            config=config,
        )
        if len(batch.vectors) != len(queries) or any(not vector for vector in batch.vectors):
            raise RuntimeError("query embedding batch incomplete")
        records = [
            _score_query(
                conn,
                query=query,
                query_embedding=batch.vectors[index],
                candidate_limit=args.candidate_limit,
                chunk_top_k=args.chunk_top_k,
                note_top_k=args.note_top_k,
                top_k=args.top_k,
                thresholds=thresholds,
            )
            for index, query in enumerate(queries)
        ]
    finally:
        conn.close()

    count_summary = {}
    for threshold in thresholds:
        key = f"{threshold:.2f}"
        values = [record["counts"][key] for record in records]
        count_summary[key] = {
            **_summary(values),
            "zero_turns": sum(1 for value in values if value == 0),
            "full_top_k_turns": sum(1 for value in values if value >= args.top_k),
        }
    return {
        "ok": True,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "privacy": "aggregate_only_no_message_text_or_ids",
        "config": {
            "provider": config["provider"],
            "model": config["model"],
            "dimensions": config["dimensions"],
            "signature": config["signature"],
            "sample_size": args.sample_size,
            "recent_pool": args.recent_pool,
            "seed": args.seed,
            "candidate_limit": args.candidate_limit,
            "chunk_top_k": args.chunk_top_k,
            "note_top_k": args.note_top_k,
            "top_k": args.top_k,
            "thresholds": thresholds,
        },
        "provider": {
            "requests": batch.requests,
            "retries": batch.retries,
            "rate_limited": batch.rate_limited,
            "prompt_tokens": batch.prompt_tokens,
        },
        "candidate_chunks": _summary(record["candidate_chunks"] for record in records),
        "candidate_notes": _summary(record["candidate_notes"] for record in records),
        "top_score": _summary(record["top_score"] for record in records),
        "top_semantic_similarity": _summary(record["top_semantic"] for record in records),
        "selected_count_by_threshold": count_summary,
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
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
    parser.add_argument("--sample-size", type=int, default=40)
    parser.add_argument("--recent-pool", type=int, default=400)
    parser.add_argument("--seed", type=int, default=20260803)
    parser.add_argument("--candidate-limit", type=int, default=1000)
    parser.add_argument("--chunk-top-k", type=int, default=8)
    parser.add_argument("--note-top-k", type=int, default=6)
    parser.add_argument("--top-k", type=int, default=8)
    parser.add_argument("--thresholds", default="0.18,0.25,0.30,0.35,0.40,0.45,0.50")
    parser.add_argument("--output", default="")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    result = asyncio.run(_run(args))
    payload = json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    if args.output:
        path = Path(args.output).resolve()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(payload, encoding="utf-8")
    print(payload, end="")
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
