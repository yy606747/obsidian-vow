#!/usr/bin/env python3
"""Operate the bounded natural-shadow arm for Working Model V2 CP4.

This script never calls a model. Before activation it installs a temporary
SQLite trigger which rejects the sixth request and every request at or after
the preregistered 24-hour deadline. The monitor closes the product write flag
when either stop condition is reached; the trigger remains the fail-safe if
the monitor is late or dies.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import signal
import sqlite3
import subprocess
import sys
import time
from typing import Any


CHAT_ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = CHAT_ROOT.parent
if str(CHAT_ROOT) not in sys.path:
    sys.path.insert(0, str(CHAT_ROOT))

from config import AI_BEHAVIOR_PATH, DB_PATH  # noqa: E402


DEFAULT_PREREGISTRATION = (
    REPO_ROOT
    / "docs/planning/checkpoints/working_model_v2/artifacts/CP4_NATURAL_PREREGISTRATION.json"
)
TRIGGER_NAME = "wm_cp4_natural_request_cap"
STATE_SCHEMA_VERSION = "working_model_v2_cp4_natural_state.v1"
PREREGISTRATION_SCHEMA_VERSION = "working_model_v2_cp4_natural_preregistration.v1"
NATURAL_MAX_REQUESTS = 5
NATURAL_DURATION_SECONDS = 24 * 60 * 60
REQUIRED_TABLES = {
    "working_model_versions",
    "working_model_requests",
    "desire_versions",
    "messages",
}


class CP4ContractError(RuntimeError):
    pass


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _now_epoch() -> float:
    return datetime.now(timezone.utc).timestamp()


def _parse_utc(value: object) -> float:
    text = str(value or "").strip()
    if not text:
        raise CP4ContractError("activation UTC timestamp is missing")
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise CP4ContractError("activation UTC timestamp is invalid") from exc
    if parsed.tzinfo is None:
        raise CP4ContractError("activation UTC timestamp must include a timezone")
    return parsed.timestamp()


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _sha256_file(path: Path) -> str:
    return _sha256_bytes(path.read_bytes())


def _load_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise CP4ContractError(f"cannot read JSON object: {path}") from exc
    if not isinstance(value, dict):
        raise CP4ContractError(f"expected JSON object: {path}")
    return value


def _atomic_write_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def _resolve_repo_path(value: object, *, field: str) -> Path:
    text = str(value or "").strip()
    relative = Path(text)
    if not text or relative.is_absolute() or ".." in relative.parts:
        raise CP4ContractError(f"{field} must be a repository-relative path")
    resolved = (REPO_ROOT / relative).resolve()
    if REPO_ROOT != resolved and REPO_ROOT not in resolved.parents:
        raise CP4ContractError(f"{field} escapes repository root")
    return resolved


def _resolve_private_path(value: object, *, field: str) -> Path:
    path = _resolve_repo_path(value, field=field)
    private_root = (CHAT_ROOT / "data/working_model_v2_cp4").resolve()
    if private_root != path and private_root not in path.parents:
        raise CP4ContractError(f"{field} must stay under {private_root}")
    return path


def _git_head() -> str:
    result = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=REPO_ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def _open_db(path: Path, *, read_only: bool) -> sqlite3.Connection:
    resolved = path.resolve()
    if not resolved.is_file():
        raise CP4ContractError(f"database does not exist: {resolved}")
    if read_only:
        connection = sqlite3.connect(f"file:{resolved}?mode=ro", uri=True, timeout=30)
        connection.execute("PRAGMA query_only=ON")
    else:
        connection = sqlite3.connect(resolved, timeout=30)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA busy_timeout=30000")
    return connection


def _assert_required_tables(connection: sqlite3.Connection) -> None:
    present = {
        str(row[0])
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    }
    missing = sorted(REQUIRED_TABLES - present)
    if missing:
        raise CP4ContractError(
            "database is not migrated for CP4: missing " + ", ".join(missing)
        )


def _high_water(connection: sqlite3.Connection) -> int:
    row = connection.execute(
        "SELECT COALESCE(MAX(rowid), 0) FROM working_model_requests"
    ).fetchone()
    return int(row[0] or 0)


def _request_count_after(connection: sqlite3.Connection, high_water: int) -> int:
    row = connection.execute(
        "SELECT COUNT(*) FROM working_model_requests WHERE rowid > ?",
        (high_water,),
    ).fetchone()
    return int(row[0] or 0)


def _trigger_sql(*, high_water: int, maximum: int, deadline_epoch: int) -> str:
    if high_water < 0 or maximum <= 0 or deadline_epoch <= 0:
        raise CP4ContractError("invalid trigger high-water, maximum or deadline")
    return f"""
        CREATE TRIGGER {TRIGGER_NAME}
        BEFORE INSERT ON working_model_requests
        WHEN CAST(strftime('%s', 'now') AS INTEGER) >= {deadline_epoch}
             OR (
                 SELECT COUNT(*) FROM working_model_requests
                 WHERE rowid > {high_water}
             ) >= {maximum}
        BEGIN
            SELECT RAISE(ABORT, 'wm_cp4_natural_stop_reached');
        END
    """


def _install_trigger(
    connection: sqlite3.Connection,
    *,
    high_water: int,
    maximum: int,
    deadline_epoch: int,
) -> str:
    existing = connection.execute(
        "SELECT sql FROM sqlite_master WHERE type='trigger' AND name=?",
        (TRIGGER_NAME,),
    ).fetchone()
    if existing is not None:
        raise CP4ContractError(f"trigger already exists: {TRIGGER_NAME}")
    sql = _trigger_sql(
        high_water=high_water,
        maximum=maximum,
        deadline_epoch=deadline_epoch,
    )
    connection.execute("BEGIN IMMEDIATE")
    try:
        connection.execute(sql)
        connection.commit()
    except BaseException:
        connection.rollback()
        raise
    return _sha256_bytes(" ".join(sql.split()).encode("utf-8"))


def _drop_trigger(connection: sqlite3.Connection) -> bool:
    exists = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='trigger' AND name=?",
        (TRIGGER_NAME,),
    ).fetchone()
    if exists is None:
        return False
    connection.execute("BEGIN IMMEDIATE")
    try:
        connection.execute(f"DROP TRIGGER {TRIGGER_NAME}")
        connection.commit()
    except BaseException:
        connection.rollback()
        raise
    return True


def assert_natural_trigger_absent(db_path: Path) -> dict[str, Any]:
    """Fail closed before CP5 if the temporary natural-run cap still exists."""

    with _open_db(db_path, read_only=True) as connection:
        row = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='trigger' AND name=?",
            (TRIGGER_NAME,),
        ).fetchone()
    if row is not None:
        raise CP4ContractError(
            f"temporary CP4 trigger still exists and would block CP5: {TRIGGER_NAME}"
        )
    return {"ok": True, "trigger_absent": True, "trigger_name": TRIGGER_NAME}


def _set_write_flag(path: Path, *, enabled: bool) -> dict[str, Any]:
    current = _load_json(path)
    current["working_model_v2_write_enabled"] = bool(enabled)
    if bool(current.get("working_model_v2_injection_enabled", False)):
        raise CP4ContractError("CP4 refuses to mutate flags while V2 injection is enabled")
    _atomic_write_json(path, current)
    return current


def _safe_config_view(path: Path, value: dict[str, Any]) -> dict[str, Any]:
    return {
        "path": str(path.resolve()),
        "file_sha256": _sha256_file(path),
        "working_model_v2_write_enabled": bool(
            value.get("working_model_v2_write_enabled", False)
        ),
        "working_model_v2_injection_enabled": bool(
            value.get("working_model_v2_injection_enabled", False)
        ),
        "opportunity_enabled": bool(value.get("opportunity_enabled", False)),
    }


def _validate_preregistration(
    path: Path,
    *,
    require_authorization: bool,
    require_activation_seal: bool,
) -> dict[str, Any]:
    prereg = _load_json(path)
    if prereg.get("schema_version") != PREREGISTRATION_SCHEMA_VERSION:
        raise CP4ContractError("unknown CP4-natural preregistration schema")
    if str(prereg.get("status") or "").startswith("superseded"):
        raise CP4ContractError("CP4-natural preregistration is superseded")
    if prereg.get("product_code_commit") != _git_head():
        raise CP4ContractError("git HEAD differs from the frozen product code commit")

    natural = prereg.get("natural_shadow") or {}
    if natural.get("max_qualified_requests") != NATURAL_MAX_REQUESTS:
        raise CP4ContractError("natural shadow cap must remain exactly five")
    if natural.get("duration_seconds") != NATURAL_DURATION_SECONDS:
        raise CP4ContractError("natural shadow duration must remain exactly 24 hours")
    if natural.get("count_failures_reject_memory_noop") is not True:
        raise CP4ContractError("all qualified request outcomes must count toward five")
    if natural.get("replace_or_replenish") is not False:
        raise CP4ContractError("natural failures must not be replaced")
    if natural.get("trigger_name") != TRIGGER_NAME:
        raise CP4ContractError("preregistered trigger name differs from runtime")
    budget = prereg.get("provider_call_budget") or {}
    hard = budget.get("absolute_upper_bound") or {}
    if hard.get("gate") != 5 or hard.get("writer") != 20 or hard.get("total") != 25:
        raise CP4ContractError("CP4-natural provider hard bound must remain 5/20/25")

    implementation_files = prereg.get("implementation_files") or []
    if not implementation_files:
        raise CP4ContractError("implementation file hashes are missing")

    for item in implementation_files:
        implementation_path = _resolve_repo_path(
            item.get("path"),
            field="implementation_files.path",
        )
        if _sha256_file(implementation_path) != item.get("sha256"):
            raise CP4ContractError(
                f"implementation hash changed: {item.get('path')}"
            )

    if require_authorization:
        authorization = prereg.get("authorization") or {}
        if authorization.get("status") != "approved":
            raise CP4ContractError("CP4 paid run has no explicit user authorization")
        if not str(authorization.get("verbatim_user_text") or "").strip():
            raise CP4ContractError("CP4 authorization text is empty")
        if not str(authorization.get("approved_at") or "").strip():
            raise CP4ContractError("CP4 authorization timestamp is empty")
        _parse_utc(authorization.get("approved_at"))
        pricing = prereg.get("pricing_snapshot") or {}
        if pricing.get("status") != "verified_primary_sources":
            raise CP4ContractError("provider pricing snapshot is not verified")
        _parse_utc(pricing.get("captured_at"))

    activation = prereg.get("activation") or {}
    if require_activation_seal:
        if not activation.get("sealed"):
            raise CP4ContractError("activation high-water mark has not been sealed")
        if not isinstance(activation.get("high_water_rowid"), int):
            raise CP4ContractError("sealed high-water rowid is missing")
        start_epoch = _parse_utc(activation.get("utc_start"))
        deadline_epoch = _parse_utc(activation.get("utc_deadline"))
        if int(deadline_epoch - start_epoch) != NATURAL_DURATION_SECONDS:
            raise CP4ContractError("sealed natural deadline is not exactly 24 hours")
        if not str(activation.get("ai_behavior_before_sha256") or "").strip():
            raise CP4ContractError("sealed pre-activation config hash is missing")

    _resolve_private_path(prereg.get("private_run_dir"), field="private_run_dir")
    return prereg


def preflight(
    preregistration_path: Path,
    *,
    db_path: Path,
    behavior_path: Path,
) -> dict[str, Any]:
    prereg = _validate_preregistration(
        preregistration_path,
        require_authorization=False,
        require_activation_seal=False,
    )
    database = {
        "path": str(db_path.resolve()),
        "exists": db_path.is_file(),
        "migrated": False,
        "high_water_rowid": None,
        "request_count": None,
    }
    if db_path.is_file():
        try:
            with _open_db(db_path, read_only=True) as connection:
                _assert_required_tables(connection)
                database.update({
                    "migrated": True,
                    "high_water_rowid": _high_water(connection),
                    "request_count": int(
                        connection.execute(
                            "SELECT COUNT(*) FROM working_model_requests"
                        ).fetchone()[0]
                    ),
                })
        except CP4ContractError as exc:
            database["migration_error"] = str(exc)
    behavior = _load_json(behavior_path)
    return {
        "ok": True,
        "mode": "read_only_preflight",
        "run_id": prereg.get("run_id"),
        "authorization_status": (prereg.get("authorization") or {}).get("status"),
        "natural_cap": prereg["natural_shadow"]["max_qualified_requests"],
        "duration_seconds": prereg["natural_shadow"]["duration_seconds"],
        "activation_sealed": bool((prereg.get("activation") or {}).get("sealed")),
        "database": database,
        "ai_behavior": _safe_config_view(behavior_path, behavior),
        "provider_calls_made": 0,
    }


def activate(
    preregistration_path: Path,
    *,
    db_path: Path,
    behavior_path: Path,
) -> dict[str, Any]:
    prereg = _validate_preregistration(
        preregistration_path,
        require_authorization=True,
        require_activation_seal=True,
    )
    activation = prereg["activation"]
    natural = prereg["natural_shadow"]
    high_water = int(activation["high_water_rowid"])
    maximum = int(natural["max_qualified_requests"])
    deadline_epoch = int(_parse_utc(activation["utc_deadline"]))
    if _now_epoch() >= deadline_epoch:
        raise CP4ContractError("natural activation deadline has already passed")
    private_dir = _resolve_private_path(
        prereg["private_run_dir"],
        field="private_run_dir",
    )
    private_dir.mkdir(parents=True, exist_ok=True)
    state_path = private_dir / "natural_state.json"
    if state_path.exists():
        raise CP4ContractError(
            "natural state already exists; inspect or resume it instead of re-activating"
        )

    behavior_before = _load_json(behavior_path)
    safe_before = _safe_config_view(behavior_path, behavior_before)
    if safe_before["file_sha256"] != activation["ai_behavior_before_sha256"]:
        raise CP4ContractError("AI behavior changed after the activation seal")
    if safe_before["working_model_v2_write_enabled"]:
        raise CP4ContractError("write flag must be off before CP4 activation")
    if safe_before["working_model_v2_injection_enabled"]:
        raise CP4ContractError("injection flag must remain off throughout CP4")

    with _open_db(db_path, read_only=False) as connection:
        _assert_required_tables(connection)
        actual_high_water = _high_water(connection)
        if actual_high_water != high_water:
            raise CP4ContractError(
                f"database high-water changed after seal: {high_water} -> {actual_high_water}"
            )
        trigger_sha256 = _install_trigger(
            connection,
            high_water=high_water,
            maximum=maximum,
            deadline_epoch=deadline_epoch,
        )

    # Persist the armed state before opening the paid write path. If the
    # process dies after the flag flip, the monitor still has enough state to
    # recover and close the run; if it dies before the flip, no call can start.
    state = {
        "schema_version": STATE_SCHEMA_VERSION,
        "run_id": prereg["run_id"],
        "preregistration_sha256": _sha256_file(preregistration_path),
        "status": "armed",
        "armed_at": _now_iso(),
        "activated_at": None,
        "stopped_at": None,
        "stop_reason": None,
        "high_water_rowid": high_water,
        "max_qualified_requests": maximum,
        "utc_deadline": activation["utc_deadline"],
        "deadline_epoch": deadline_epoch,
        "qualified_request_count": 0,
        "trigger": {
            "name": TRIGGER_NAME,
            "normalized_sql_sha256": trigger_sha256,
            "installed": True,
        },
        "ai_behavior_before": safe_before,
        "ai_behavior_after": None,
        "requests": [],
        "provider_events": [],
    }
    _atomic_write_json(state_path, state)

    try:
        behavior_after = _set_write_flag(behavior_path, enabled=True)
    except BaseException:
        with _open_db(db_path, read_only=False) as connection:
            _drop_trigger(connection)
        state["status"] = "activation_failed_before_open"
        state["trigger"]["installed"] = False
        state["activation_failed_at"] = _now_iso()
        _atomic_write_json(state_path, state)
        raise

    state["status"] = "active"
    state["activated_at"] = _now_iso()
    state["ai_behavior_after"] = _safe_config_view(behavior_path, behavior_after)
    _atomic_write_json(state_path, state)
    return state


def _provider_events_since(path: Path, since_epoch: float) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    events: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if float(event.get("ts") or 0.0) < since_epoch:
            continue
        if event.get("scope") not in {"working_model:gate", "working_model:writer"}:
            continue
        events.append({
            "ts": event.get("ts"),
            "request_id": event.get("request_id"),
            "scope": event.get("scope"),
            "model": event.get("model"),
            "endpoint_id": event.get("endpoint_id"),
            "ok": bool(event.get("ok")),
            "error_type": event.get("error_type"),
            "elapsed_ms": int(event.get("elapsed_ms") or 0),
            "http_status": event.get("http_status"),
            "meta": event.get("meta") or {},
        })
    return events


def _request_views(
    connection: sqlite3.Connection,
    *,
    high_water: int,
) -> list[dict[str, Any]]:
    rows = connection.execute(
        """
        SELECT rowid, id, origin_user_message_id, origin_assistant_message_id,
               statement, source, route, gate_model, gate_prompt_version,
               disposition, writer_model, writer_prompt_version,
               resulting_memory_id, status, failure_code, created_at, updated_at
        FROM working_model_requests
        WHERE rowid > ?
        ORDER BY rowid
        """,
        (high_water,),
    ).fetchall()
    return [{
        "rowid": int(row["rowid"]),
        "id": row["id"],
        "origin_user_message_id": row["origin_user_message_id"],
        "origin_assistant_message_id": row["origin_assistant_message_id"],
        "statement_sha256": _sha256_bytes(
            str(row["statement"] or "").encode("utf-8")
        ),
        "source_sha256": _sha256_bytes(str(row["source"] or "").encode("utf-8")),
        "route": row["route"],
        "gate_model": row["gate_model"],
        "gate_prompt_version": row["gate_prompt_version"],
        "disposition": row["disposition"],
        "writer_model": row["writer_model"],
        "writer_prompt_version": row["writer_prompt_version"],
        "resulting_memory_id": row["resulting_memory_id"],
        "status": row["status"],
        "failure_code": row["failure_code"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    } for row in rows]


def monitor_once(
    preregistration_path: Path,
    *,
    db_path: Path,
    behavior_path: Path,
    provider_events_path: Path,
    manual_stop: bool = False,
) -> dict[str, Any]:
    prereg = _validate_preregistration(
        preregistration_path,
        require_authorization=True,
        require_activation_seal=True,
    )
    private_dir = _resolve_private_path(
        prereg["private_run_dir"],
        field="private_run_dir",
    )
    state_path = private_dir / "natural_state.json"
    state = _load_json(state_path)
    if state.get("run_id") != prereg.get("run_id"):
        raise CP4ContractError("natural state belongs to another run")
    if state.get("preregistration_sha256") != _sha256_file(preregistration_path):
        raise CP4ContractError("preregistration changed after activation")

    behavior = _load_json(behavior_path)
    if state.get("status") == "armed" and bool(
        behavior.get("working_model_v2_write_enabled", False)
    ):
        state["status"] = "active"
        state["activation_recovered_at"] = _now_iso()
        state["ai_behavior_after"] = _safe_config_view(behavior_path, behavior)

    high_water = int(state["high_water_rowid"])
    maximum = int(state["max_qualified_requests"])
    with _open_db(db_path, read_only=True) as connection:
        _assert_required_tables(connection)
        requests = _request_views(connection, high_water=high_water)
    count = len(requests)
    state["qualified_request_count"] = count
    state["requests"] = requests
    state["provider_events"] = _provider_events_since(
        provider_events_path,
        _parse_utc(prereg["activation"]["utc_start"]),
    )
    state["last_checked_at"] = _now_iso()

    stop_reason = None
    if manual_stop:
        stop_reason = "manual_stop"
    elif count >= maximum:
        stop_reason = "natural_request_cap_reached"
    elif _now_epoch() >= float(state["deadline_epoch"]):
        stop_reason = "natural_24h_deadline_reached"
    if stop_reason and state.get("status") == "active":
        behavior_after = _set_write_flag(behavior_path, enabled=False)
        state["status"] = "stopped"
        state["stopped_at"] = _now_iso()
        state["stop_reason"] = stop_reason
        state["ai_behavior_stopped"] = _safe_config_view(
            behavior_path,
            behavior_after,
        )
    _atomic_write_json(state_path, state)
    return state


def monitor_loop(
    preregistration_path: Path,
    *,
    db_path: Path,
    behavior_path: Path,
    provider_events_path: Path,
    poll_interval_sec: float,
) -> dict[str, Any]:
    requested_stop = False

    def stop_handler(_signum, _frame) -> None:
        nonlocal requested_stop
        requested_stop = True

    previous_sigint = signal.signal(signal.SIGINT, stop_handler)
    previous_sigterm = signal.signal(signal.SIGTERM, stop_handler)
    try:
        while True:
            state = monitor_once(
                preregistration_path,
                db_path=db_path,
                behavior_path=behavior_path,
                provider_events_path=provider_events_path,
                manual_stop=requested_stop,
            )
            if state.get("status") != "active":
                return state
            time.sleep(max(0.25, min(float(poll_interval_sec), 10.0)))
    finally:
        signal.signal(signal.SIGINT, previous_sigint)
        signal.signal(signal.SIGTERM, previous_sigterm)


def cleanup_trigger(
    preregistration_path: Path,
    *,
    db_path: Path,
    behavior_path: Path,
) -> dict[str, Any]:
    prereg = _validate_preregistration(
        preregistration_path,
        require_authorization=True,
        require_activation_seal=True,
    )
    behavior = _load_json(behavior_path)
    if bool(behavior.get("working_model_v2_write_enabled", False)):
        raise CP4ContractError("refusing to remove cap trigger while write flag is on")
    with _open_db(db_path, read_only=False) as connection:
        removed = _drop_trigger(connection)
    private_dir = _resolve_private_path(
        prereg["private_run_dir"],
        field="private_run_dir",
    )
    state_path = private_dir / "natural_state.json"
    state = _load_json(state_path)
    state["trigger"]["installed"] = False
    state["trigger"]["removed_at"] = _now_iso()
    _atomic_write_json(state_path, state)
    return {"ok": True, "trigger_removed": removed, "state": state}


def _print(value: dict[str, Any]) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--preregistration",
        type=Path,
        default=DEFAULT_PREREGISTRATION,
    )
    parser.add_argument("--db", type=Path, default=DB_PATH)
    parser.add_argument("--ai-behavior", type=Path, default=AI_BEHAVIOR_PATH)
    parser.add_argument(
        "--provider-events",
        type=Path,
        default=CHAT_ROOT / "data/provider_events.jsonl",
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--activate", action="store_true")
    mode.add_argument("--monitor", action="store_true")
    mode.add_argument("--monitor-once", action="store_true")
    mode.add_argument("--stop", action="store_true")
    mode.add_argument("--cleanup-trigger", action="store_true")
    mode.add_argument("--assert-trigger-absent", action="store_true")
    parser.add_argument("--poll-interval-sec", type=float, default=1.0)
    args = parser.parse_args(argv)

    preregistration_path = args.preregistration.resolve()
    if args.activate:
        result = activate(
            preregistration_path,
            db_path=args.db,
            behavior_path=args.ai_behavior,
        )
    elif args.monitor:
        result = monitor_loop(
            preregistration_path,
            db_path=args.db,
            behavior_path=args.ai_behavior,
            provider_events_path=args.provider_events,
            poll_interval_sec=args.poll_interval_sec,
        )
    elif args.monitor_once or args.stop:
        result = monitor_once(
            preregistration_path,
            db_path=args.db,
            behavior_path=args.ai_behavior,
            provider_events_path=args.provider_events,
            manual_stop=args.stop,
        )
    elif args.cleanup_trigger:
        result = cleanup_trigger(
            preregistration_path,
            db_path=args.db,
            behavior_path=args.ai_behavior,
        )
    elif args.assert_trigger_absent:
        result = assert_natural_trigger_absent(args.db)
    else:
        result = preflight(
            preregistration_path,
            db_path=args.db,
            behavior_path=args.ai_behavior,
        )
    _print(result)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except CP4ContractError as exc:
        print(f"CP4 shadow contract error: {exc}", file=sys.stderr)
        raise SystemExit(2)
