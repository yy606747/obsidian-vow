"""Inspect, enable, run or undo memory compaction without deleting originals."""
from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.memory_v2 import compaction
from app.memory_v2.compaction_config import load_config, save_config


async def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["status", "enable", "disable", "run", "rollback"])
    parser.add_argument("--batches", type=int, default=1)
    parser.add_argument("--batch-id")
    args = parser.parse_args()
    if args.action == "enable":
        result = save_config({"enabled": True})
    elif args.action == "disable":
        result = save_config({"enabled": False})
    elif args.action == "rollback":
        # Full rollback also disables the worker/resident policy. A targeted
        # rollback leaves the feature running and reverses just one batch.
        if not args.batch_id:
            save_config({"enabled": False})
        result = await compaction.rollback(batch_id=args.batch_id)
    elif args.action == "run":
        result = []
        for _ in range(max(1, min(args.batches, load_config()["daily_batches"]))):
            row = await compaction.compact_once()
            result.append(row)
            if row["status"] != "applied":
                break
    else:
        result = await compaction.status()
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    asyncio.run(main())
