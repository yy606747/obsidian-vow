#!/usr/bin/env python3
"""用合成记录测补充检索；临时数据库、禁止真实网络，不调用模型。"""

from __future__ import annotations

import argparse
import asyncio
import importlib
import json
import os
from pathlib import Path
import resource
import sqlite3
import statistics
import subprocess
import sys
import tempfile
import time
import tracemalloc

from check_backend import isolated_environment


ROOT = Path(__file__).resolve().parents[1]


async def measure(count: int, dimensions: int) -> dict:
    import database
    import numpy as np
    from app.memory_v2.embedding import pack_embedding

    hybrid = importlib.import_module("app.memory_v2.hybrid_recall")
    await database.init_db()
    rng = np.random.default_rng(904)
    vectors = rng.normal(size=(count, dimensions)).astype(np.float32)
    query = vectors[0].tolist()
    with sqlite3.connect(database.DB_PATH) as db:
        db.executemany(
            "INSERT INTO memory_chunks (id,conv_id,content,created_at,updated_at,embedding,metadata_json) "
            "VALUES (?,'synthetic',?,?,?,?,?)",
            ((str(index), f"合成记录 {index}", index, index, pack_embedding(vector.tolist()),
              json.dumps({"source_start_ts": index, "source_end_ts": index})) for index, vector in enumerate(vectors)),
        )
    del vectors

    async def synthetic_embedding(_text):
        return query

    hybrid.embedding.get_embedding = synthetic_embedding
    hybrid.clear_full_corpus_cache()

    async def once():
        started = time.perf_counter()
        result = await hybrid.wide_chunk_recall(
            "合成检索", top_k=20, candidate_limit=1000, as_of_ts=count + 1,
        )
        assert result[0]["candidate_id"] == "0"
        return (time.perf_counter() - started) * 1000

    # 不在计时段启用跟踪器，避免把逐行分配追踪的开销算作检索耗时。
    first_ms = await once()
    hit_ms = statistics.median([await once() for _ in range(3)])
    hybrid.clear_full_corpus_cache()
    tracemalloc.start()
    await once()
    allocated_peak = tracemalloc.get_traced_memory()[1]
    tracemalloc.stop()
    rss_peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * 1024
    hybrid.clear_full_corpus_cache()
    return {
        "rows": count, "dimensions": dimensions,
        "first_ms": round(first_ms, 2), "cache_hit_median_ms": round(hit_ms, 2),
        "recall_allocated_peak_mib": round(allocated_peak / 1024 ** 2, 2),
        "process_peak_rss_mib": round(rss_peak / 1024 ** 2, 2),
        "external_model_calls": 0,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--rows", type=int, choices=(1000, 10000))
    parser.add_argument("--dimensions", type=int, default=1024)
    parser.add_argument("--worker", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()
    if not 2 <= args.dimensions <= 4096:
        parser.error("向量维数需在 2～4096 之间")
    if args.worker:
        if os.environ.get("OBSIDIAN_TEST_MODE") != "1" or not os.environ.get("OBSIDIAN_DATA_DIR"):
            parser.error("子进程必须由隔离入口启动")
        sys.path.insert(0, str(ROOT / "obsidian-chat"))
        import runtime_safety
        runtime_safety.install_test_network_guard()
        print(json.dumps(asyncio.run(measure(args.rows, args.dimensions)), ensure_ascii=False))
        return 0
    for count in ([args.rows] if args.rows else [1000, 10000]):
        with tempfile.TemporaryDirectory(prefix="obsidianvow-recall-benchmark-") as directory:
            result = subprocess.run(
                [sys.executable, str(Path(__file__).resolve()), "--worker", "--rows", str(count),
                 "--dimensions", str(args.dimensions)],
                env=isolated_environment(Path(directory)), check=False, timeout=120,
            )
            if result.returncode:
                return result.returncode
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
