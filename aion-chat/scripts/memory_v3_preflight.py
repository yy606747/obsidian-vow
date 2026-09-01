"""Read-only Memory V3 deployment/schema preflight.

This command never calls a provider and never mutates the database or settings.
It intentionally reports static worker evidence separately from live runtime
facts so a local checkout is not mistaken for the deployed host.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
from pathlib import Path
import sqlite3

import aiosqlite

from app.memory_v2.recall_config import normalize_recall_config
from app.memory_v3.config import normalize_memory_v3_config
from app.memory_v3.schema import inspect_memory_v3_schema
from config import DB_PATH, SETTINGS_PATH


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _load_settings(path: Path) -> dict:
    if not path.exists():
        return {}
    parsed = json.loads(path.read_text(encoding="utf-8"))
    return parsed if isinstance(parsed, dict) else {}


def _read_only_counts(path: Path) -> dict:
    uri = f"file:{path.resolve()}?mode=ro"
    connection = sqlite3.connect(uri, uri=True)
    try:
        tables = {
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        counts = {}
        for table in (
            "messages",
            "memory_chunks",
            "memory_items",
            "memory_usage",
            "memory_relational_cards",
            "memory_pending_recalls",
            "memory_timeline_versions",
            "memory_injection_events",
        ):
            counts[table] = (
                int(connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
                if table in tables
                else None
            )
        reason_counts = {}
        if "memory_usage" in tables:
            reason_counts = {
                str(reason): int(count)
                for reason, count in connection.execute(
                    "SELECT reason, COUNT(*) FROM memory_usage GROUP BY reason"
                ).fetchall()
            }
        return {"tables": sorted(tables), "counts": counts, "memory_usage_by_reason": reason_counts}
    finally:
        connection.close()


async def _schema_plan(path: Path) -> dict:
    uri = f"file:{path.resolve()}?mode=ro"
    async with aiosqlite.connect(uri, uri=True) as db:
        await db.execute("PRAGMA query_only=ON")
        return await inspect_memory_v3_schema(db)


def _static_worker_evidence(repo_root: Path) -> dict:
    main_path = repo_root / "aion-chat/main.py"
    dockerfile = repo_root / "aion-chat/Dockerfile"
    compose = repo_root / "deploy/docker-compose.prod.yml"
    main_text = main_path.read_text(encoding="utf-8") if main_path.exists() else ""
    docker_text = dockerfile.read_text(encoding="utf-8") if dockerfile.exists() else ""
    compose_text = compose.read_text(encoding="utf-8") if compose.exists() else ""
    evidence = {
        "main_uses_uvicorn_run": "uvicorn.run(" in main_text,
        "main_declares_workers": "workers=" in main_text,
        "docker_command": "python -u main.py" if '"python", "-u", "main.py"' in docker_text else "unknown",
        "compose_replicas_declared": "replicas:" in compose_text,
    }
    evidence["static_single_worker_consistent"] = bool(
        evidence["main_uses_uvicorn_run"]
        and not evidence["main_declares_workers"]
        and evidence["docker_command"] != "unknown"
        and not evidence["compose_replicas_declared"]
    )
    return evidence


async def run(args: argparse.Namespace) -> dict:
    db_path = Path(args.db).resolve()
    settings_path = Path(args.settings).resolve()
    repo_root = Path(args.repo_root).resolve()
    if not db_path.is_file():
        raise FileNotFoundError(db_path)
    settings = _load_settings(settings_path)
    return {
        "mode": "read_only",
        "provider_calls": 0,
        "business_writes": 0,
        "db": {
            "path": str(db_path),
            "sha256": _sha256(db_path),
            **_read_only_counts(db_path),
        },
        "schema_plan": await _schema_plan(db_path),
        "config_layers": {
            "code_default_memory_v2": normalize_recall_config({}),
            "workspace_memory_v2": normalize_recall_config(settings.get("memory_v2_recall")),
            "code_default_memory_v3": normalize_memory_v3_config({}),
            "workspace_memory_v3": normalize_memory_v3_config(settings.get("memory_v3")),
            "deployment_runtime": "not inspected by this local command",
        },
        "worker_model": {
            "scope": "static checkout evidence; not a live process inspection",
            **_static_worker_evidence(repo_root),
        },
    }


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    repo_root = Path(__file__).resolve().parents[2]
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default=str(DB_PATH))
    parser.add_argument("--settings", default=str(SETTINGS_PATH))
    parser.add_argument("--repo-root", default=str(repo_root))
    return parser.parse_args(argv)


def main() -> None:
    print(json.dumps(asyncio.run(run(parse_args())), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
