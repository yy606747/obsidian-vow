"""Owner-triggered retention cleanup for tool_invocation_events."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
import sys
import time


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from config import DB_PATH, load_ai_behavior


DEFAULT_RETENTION_DAYS = 90


def configured_retention_days() -> int:
    try:
        value = load_ai_behavior().get(
            "tool_ledger_retention_days",
            DEFAULT_RETENTION_DAYS,
        )
        return max(1, int(value))
    except Exception:
        return DEFAULT_RETENTION_DAYS


def cleanup_tool_invocation_events(
    db_path: Path,
    *,
    retention_days: int,
    now: float | None = None,
    dry_run: bool = False,
) -> dict:
    path = Path(db_path)
    if not path.is_file():
        raise FileNotFoundError(f"database does not exist: {path}")

    days = max(1, int(retention_days))
    cutoff = (time.time() if now is None else float(now)) - days * 86400
    connection = sqlite3.connect(path)
    try:
        table = connection.execute(
            "SELECT 1 FROM sqlite_master "
            "WHERE type='table' AND name='tool_invocation_events'"
        ).fetchone()
        if table is None:
            raise RuntimeError("tool_invocation_events table is not initialized")
        selected = int(connection.execute(
            "SELECT COUNT(*) FROM tool_invocation_events WHERE created_at < ?",
            (cutoff,),
        ).fetchone()[0])
        deleted = 0
        if not dry_run and selected:
            with connection:
                cursor = connection.execute(
                    "DELETE FROM tool_invocation_events WHERE created_at < ?",
                    (cutoff,),
                )
            deleted = max(0, int(cursor.rowcount))
        return {
            "db": str(path.resolve()),
            "retention_days": days,
            "cutoff": cutoff,
            "cutoff_iso": datetime.fromtimestamp(
                cutoff,
                tz=timezone.utc,
            ).isoformat(),
            "matched": selected,
            "deleted": deleted,
            "dry_run": bool(dry_run),
        }
    finally:
        connection.close()


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default=str(DB_PATH))
    parser.add_argument(
        "--days",
        type=int,
        help="retention window; defaults to tool_ledger_retention_days",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="count expired rows without deleting them",
    )
    return parser.parse_args(argv)


def main() -> None:
    args = parse_args()
    days = configured_retention_days() if args.days is None else max(1, args.days)
    result = cleanup_tool_invocation_events(
        Path(args.db),
        retention_days=days,
        dry_run=args.dry_run,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
