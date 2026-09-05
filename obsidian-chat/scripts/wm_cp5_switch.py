#!/usr/bin/env python3
"""Safely enable, inspect, or roll back Working Model V2 steady state."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.desire.prompt import DESIRE_MAX_CHARS  # noqa: E402
from app.working_model.gate import WORKING_MODEL_GATE_SLOT  # noqa: E402
from app.working_model.prompt import WORKING_MODEL_ACTIVE_MAX_CHARS  # noqa: E402
from app.working_model.service import CP4_NATURAL_TRIGGER_NAME  # noqa: E402
from config import (  # noqa: E402
    AI_BEHAVIOR_PATH,
    DB_PATH,
    DEFAULT_AI_BEHAVIOR,
    DEFAULT_WORKING_MODEL_GATE_MODEL,
    get_slot,
)

WRITE_FLAG = "working_model_v2_write_enabled"
INJECTION_FLAG = "working_model_v2_injection_enabled"


class CP5SwitchError(RuntimeError):
    """The requested switch would violate the CP5 activation contract."""


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _table_exists(db: sqlite3.Connection, table: str) -> bool:
    row = db.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=? LIMIT 1",
        (table,),
    ).fetchone()
    return row is not None


def _head(db: sqlite3.Connection, table: str) -> dict[str, Any] | None:
    row = db.execute(
        f"SELECT current.id, current.content FROM {table} AS current "
        f"WHERE NOT EXISTS (SELECT 1 FROM {table} AS child "
        "WHERE child.previous_version_id=current.id) "
        "ORDER BY current.created_at DESC, current.rowid DESC LIMIT 1"
    ).fetchone()
    if row is None:
        return None
    content = str(row[1] or "")
    return {
        "id": str(row[0]),
        "content_chars": len(content),
        "content_sha256": _sha256(content),
    }


def inspect_runtime(*, db_path: Path) -> dict[str, Any]:
    if not db_path.exists():
        raise CP5SwitchError(f"database does not exist: {db_path}")
    uri = f"file:{db_path.resolve()}?mode=ro"
    with sqlite3.connect(uri, uri=True) as db:
        db.execute("PRAGMA query_only=ON")
        missing_tables = [
            table
            for table in ("working_model_versions", "desire_versions")
            if not _table_exists(db, table)
        ]
        if missing_tables:
            raise CP5SwitchError(
                "V2 tables are missing: " + ", ".join(missing_tables)
            )
        trigger_present = db.execute(
            "SELECT 1 FROM sqlite_master WHERE type='trigger' AND name=? LIMIT 1",
            (CP4_NATURAL_TRIGGER_NAME,),
        ).fetchone() is not None
        working_model = _head(db, "working_model_versions")
        desire = _head(db, "desire_versions")

    if working_model is None or desire is None:
        raise CP5SwitchError("both V2 version chains must have a durable head")
    if working_model["content_chars"] > WORKING_MODEL_ACTIVE_MAX_CHARS:
        raise CP5SwitchError("working-model head exceeds the 1200-character budget")
    if desire["content_chars"] > DESIRE_MAX_CHARS:
        raise CP5SwitchError("desire head exceeds the 200-character budget")
    return {
        "database": str(db_path.resolve()),
        "cp4_trigger_present": trigger_present,
        "working_model": working_model,
        "desire": desire,
    }


def _atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".cp5.tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    if path.exists():
        os.chmod(temporary, path.stat().st_mode & 0o777)
    temporary.replace(path)


def _load_behavior(path: Path) -> dict[str, Any]:
    if path.exists():
        try:
            loaded = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                behavior = loaded
            else:
                behavior = {}
        except (OSError, json.JSONDecodeError):
            behavior = {}
    else:
        behavior = {}
    for key, value in DEFAULT_AI_BEHAVIOR.items():
        behavior.setdefault(key, value)
    return behavior


def switch(*, enable: bool, db_path: Path, behavior_path: Path) -> dict[str, Any]:
    gate_slot = get_slot(WORKING_MODEL_GATE_SLOT)
    gate_model = str((gate_slot or {}).get("model") or "")

    if enable:
        runtime = inspect_runtime(db_path=db_path)
        if runtime["cp4_trigger_present"]:
            raise CP5SwitchError(
                f"remove trigger {CP4_NATURAL_TRIGGER_NAME!r} before CP5 activation"
            )
        if gate_slot is None:
            raise CP5SwitchError("working_model_gate slot is unavailable")
        if gate_model != DEFAULT_WORKING_MODEL_GATE_MODEL:
            raise CP5SwitchError(
                "gate model changed since CP1: "
                f"expected {DEFAULT_WORKING_MODEL_GATE_MODEL!r}, got {gate_model!r}; "
                "run a separately authorized frozen smoke before activation"
            )
    else:
        # Rollback must remain available even when the database itself is the
        # thing being diagnosed.
        runtime = {"database": str(db_path.resolve()), "checked": False}

    behavior = _load_behavior(behavior_path)
    behavior[WRITE_FLAG] = bool(enable)
    behavior[INJECTION_FLAG] = bool(enable)
    _atomic_write_json(behavior_path, behavior)
    return {
        "ok": True,
        "mode": "enabled" if enable else "disabled",
        "flags": {
            WRITE_FLAG: bool(enable),
            INJECTION_FLAG: bool(enable),
        },
        "gate_model": gate_model,
        "expected_gate_model": DEFAULT_WORKING_MODEL_GATE_MODEL,
        "runtime": runtime,
    }


def status(*, db_path: Path, behavior_path: Path) -> dict[str, Any]:
    runtime = inspect_runtime(db_path=db_path)
    behavior = _load_behavior(behavior_path)
    gate_slot = get_slot(WORKING_MODEL_GATE_SLOT) or {}
    return {
        "ok": True,
        "mode": "status",
        "behavior_path": str(behavior_path.resolve()),
        "flags": {
            WRITE_FLAG: bool(behavior.get(WRITE_FLAG, False)),
            INJECTION_FLAG: bool(behavior.get(INJECTION_FLAG, False)),
        },
        "gate_model": str(gate_slot.get("model") or ""),
        "expected_gate_model": DEFAULT_WORKING_MODEL_GATE_MODEL,
        "runtime": runtime,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--enable", action="store_true")
    mode.add_argument("--disable", action="store_true")
    mode.add_argument("--status", action="store_true")
    parser.add_argument("--db", type=Path, default=DB_PATH)
    parser.add_argument("--behavior", type=Path, default=AI_BEHAVIOR_PATH)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.enable:
            result = switch(
                enable=True,
                db_path=args.db,
                behavior_path=args.behavior,
            )
        elif args.disable:
            result = switch(
                enable=False,
                db_path=args.db,
                behavior_path=args.behavior,
            )
        else:
            result = status(db_path=args.db, behavior_path=args.behavior)
    except CP5SwitchError as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False))
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
