"""Read-only Working Model V2 audit export.

The command opens SQLite with ``mode=ro`` plus ``PRAGMA query_only=ON`` and
never calls a provider.  It writes one machine-readable JSON report and one
SSH-friendly text summary to an explicitly supplied output directory.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import difflib
import hashlib
import json
import math
from pathlib import Path
import sqlite3
import statistics
import sys
from typing import Any, Iterable


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

REPOSITORY_ROOT = ROOT.parent.resolve()
# CP4 writes private audit material here and the path is explicitly ignored by
# Git.  Other in-repository destinations require an opt-in so a typo such as
# ``--output-dir docs/...`` cannot stage real chat/model text for commit.
ALLOWED_IN_REPOSITORY_OUTPUT_ROOTS = (
    (ROOT / "data" / "working_model_v2_cp4").resolve(),
)

from app.working_model.runtime import (  # noqa: E402
    WORKING_MODEL_DIFF_ALGORITHM,
    working_model_diff_ratio,
)
from config import DB_PATH  # noqa: E402


AUDIT_SCHEMA_VERSION = "working_model_v2_audit.v1"
FLAGGED_COLUMN_SEMANTICS = "migration_or_missing_provenance_only"
DESIRE_ROOT_SENTINELS = frozenset({"root", "system_root"})
REQUIRED_TABLES = (
    "working_model_versions",
    "working_model_requests",
    "desire_versions",
    "messages",
)
OPTIONAL_TABLES = ("reflection_log",)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _read_only_connection(path: Path) -> sqlite3.Connection:
    resolved = path.resolve()
    if not resolved.is_file():
        raise FileNotFoundError(f"database does not exist: {resolved}")
    connection = sqlite3.connect(
        f"file:{resolved}?mode=ro",
        uri=True,
        timeout=30,
    )
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only=ON")
    return connection


def _fetch_all(connection: sqlite3.Connection, table: str) -> list[dict[str, Any]]:
    rows = connection.execute(
        f"SELECT rowid AS _rowid, * FROM {table} ORDER BY rowid"
    ).fetchall()
    return [dict(row) for row in rows]


def _chunks(values: list[str], size: int = 500) -> Iterable[list[str]]:
    for start in range(0, len(values), size):
        yield values[start:start + size]


def _fetch_messages(
    connection: sqlite3.Connection,
    message_ids: set[str],
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    ordered_ids = sorted(value for value in message_ids if value)
    for batch in _chunks(ordered_ids):
        placeholders = ",".join("?" for _ in batch)
        result = connection.execute(
            "SELECT rowid AS _rowid, id, conv_id, role, content, created_at "
            f"FROM messages WHERE id IN ({placeholders}) ORDER BY rowid",
            batch,
        ).fetchall()
        rows.extend(dict(row) for row in result)
    return rows


def _load_snapshot(path: Path) -> dict[str, Any]:
    with _read_only_connection(path) as connection:
        tables = {
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        missing = sorted(set(REQUIRED_TABLES) - tables)
        if missing:
            raise RuntimeError("missing Working Model V2 audit tables: " + ", ".join(missing))

        versions = _fetch_all(connection, "working_model_versions")
        requests = _fetch_all(connection, "working_model_requests")
        desires = _fetch_all(connection, "desire_versions")
        reflections = (
            _fetch_all(connection, "reflection_log")
            if "reflection_log" in tables
            else []
        )
        message_ids = {
            str(row.get(column) or "")
            for row in requests
            for column in ("origin_user_message_id", "origin_assistant_message_id")
        }
        messages = _fetch_messages(connection, message_ids)
        counted_tables = [
            *REQUIRED_TABLES,
            *(table for table in OPTIONAL_TABLES if table in tables),
        ]
        table_counts = {
            table: int(
                connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            )
            for table in counted_tables
        }
    return {
        "working_model_versions": versions,
        "working_model_requests": requests,
        "desire_versions": desires,
        "reflection_log": reflections,
        "messages": messages,
        "table_counts": table_counts,
    }


def _snapshot_sha256(snapshot: dict[str, Any]) -> str:
    payload = {
        key: snapshot[key]
        for key in (
            "working_model_versions",
            "working_model_requests",
            "desire_versions",
            "reflection_log",
            "messages",
            "table_counts",
        )
    }
    canonical = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _row_sort_key(row: dict[str, Any]) -> tuple[float, int, str]:
    return (
        float(row.get("created_at") or 0.0),
        int(row.get("_rowid") or 0),
        str(row.get("id") or ""),
    )


def _order_chain(
    rows: list[dict[str, Any]],
    *,
    previous_field: str = "previous_version_id",
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    by_id = {str(row["id"]): row for row in rows}
    children: dict[str, list[dict[str, Any]]] = {}
    roots: list[dict[str, Any]] = []
    orphans: list[str] = []
    for row in rows:
        previous = row.get(previous_field)
        if previous is None:
            roots.append(row)
            continue
        previous_id = str(previous)
        children.setdefault(previous_id, []).append(row)
        if previous_id not in by_id:
            orphans.append(str(row["id"]))
    for values in children.values():
        values.sort(key=_row_sort_key)
    roots.sort(key=_row_sort_key)

    ordered: list[dict[str, Any]] = []
    seen: set[str] = set()
    cycles: list[str] = []

    def walk(start: dict[str, Any]) -> None:
        current = start
        while current is not None:
            current_id = str(current["id"])
            if current_id in seen:
                cycles.append(current_id)
                return
            seen.add(current_id)
            ordered.append(current)
            next_rows = children.get(current_id) or []
            current = next_rows[0] if next_rows else None

    for root in roots:
        if str(root["id"]) not in seen:
            walk(root)
    for row in sorted(rows, key=_row_sort_key):
        if str(row["id"]) not in seen:
            walk(row)

    forks = {
        previous_id: [str(row["id"]) for row in values]
        for previous_id, values in children.items()
        if len(values) > 1
    }
    diagnostics = {
        "valid_single_chain": (
            len(roots) == 1
            and not forks
            and not orphans
            and not cycles
            and len(ordered) == len(rows)
        ),
        "root_ids": [str(row["id"]) for row in roots],
        "forks": forks,
        "orphan_ids": sorted(orphans),
        "cycle_ids": sorted(set(cycles)),
    }
    return ordered, diagnostics


def _working_model_root_kind(row: dict[str, Any]) -> str | None:
    if row.get("previous_version_id") is not None:
        return None
    if (
        row.get("origin_request_id") is None
        and (
            int(row.get("flagged") or 0) == 1
            or str(row.get("writer_model") or "") == "unknown"
        )
    ):
        return "migration_root"
    return "working_model_root"


def _desire_root_kind(row: dict[str, Any]) -> str | None:
    if row.get("previous_version_id") is not None:
        return None
    if str(row.get("origin_request_id") or "") in DESIRE_ROOT_SENTINELS:
        return "desire_root"
    return "desire_root_unrecognized"


def _public_working_model_version(
    row: dict[str, Any],
    *,
    index: int,
) -> dict[str, Any]:
    flagged = int(row.get("flagged") or 0)
    return {
        "index": index,
        "rowid": int(row.get("_rowid") or 0),
        "id": str(row.get("id") or ""),
        "previous_version_id": row.get("previous_version_id"),
        "content": str(row.get("content") or ""),
        "content_chars": len(str(row.get("content") or "")),
        "created_at": float(row.get("created_at") or 0.0),
        "origin_conv_id": row.get("origin_conv_id"),
        "origin_message_id": row.get("origin_message_id"),
        "origin_request_id": row.get("origin_request_id"),
        "reason": str(row.get("reason") or ""),
        "writer_model": row.get("writer_model"),
        "prompt_version": row.get("prompt_version"),
        "diff_ratio": row.get("diff_ratio"),
        "flagged": flagged,
        "flag_semantics": (
            FLAGGED_COLUMN_SEMANTICS if flagged else "none"
        ),
        "root_kind": _working_model_root_kind(row),
    }


def _public_desire_version(
    row: dict[str, Any],
    *,
    index: int,
) -> dict[str, Any]:
    return {
        "index": index,
        "rowid": int(row.get("_rowid") or 0),
        "id": str(row.get("id") or ""),
        "previous_version_id": row.get("previous_version_id"),
        "content": str(row.get("content") or ""),
        "content_chars": len(str(row.get("content") or "")),
        "change_note": str(row.get("change_note") or ""),
        "origin_request_id": str(row.get("origin_request_id") or ""),
        "working_model_id": str(row.get("working_model_id") or ""),
        "writer_model": row.get("writer_model"),
        "prompt_version": row.get("prompt_version"),
        "created_at": float(row.get("created_at") or 0.0),
        "root_kind": _desire_root_kind(row),
    }


def _unified_diff(
    before: str,
    after: str,
    *,
    before_label: str,
    after_label: str,
) -> str:
    lines = difflib.unified_diff(
        before.splitlines(),
        after.splitlines(),
        fromfile=before_label,
        tofile=after_label,
        lineterm="",
    )
    return "\n".join(lines)


def _diff_entry(
    before: dict[str, Any],
    after: dict[str, Any],
) -> dict[str, Any]:
    computed = working_model_diff_ratio(before["content"], after["content"])
    stored = after.get("diff_ratio")
    return {
        "before_version_id": before["id"],
        "after_version_id": after["id"],
        "stored_diff_ratio": stored,
        "computed_diff_ratio": computed,
        "stored_matches_computed": (
            None if stored is None else abs(float(stored) - computed) <= 0.000001
        ),
        "diff": _unified_diff(
            before["content"],
            after["content"],
            before_label=f"wm:{before['id']}",
            after_label=f"wm:{after['id']}",
        ),
    }


def build_anchor_diffs(
    versions: list[dict[str, Any]],
    distances: tuple[int, ...] = (5, 20),
) -> dict[str, dict[str, Any]]:
    anchors: dict[str, dict[str, Any]] = {}
    if not versions:
        return {
            str(distance): {
                "distance": distance,
                "status": "N/A",
                "reason": "working_model_chain_empty",
            }
            for distance in distances
        }
    current_index = len(versions) - 1
    current = versions[current_index]
    for distance in distances:
        base_index = current_index - distance
        if base_index < 0:
            anchors[str(distance)] = {
                "distance": distance,
                "status": "N/A",
                "reason": f"only_{current_index}_predecessor_versions_available",
                "current_version_id": current["id"],
            }
            continue
        base = versions[base_index]
        anchors[str(distance)] = {
            "distance": distance,
            "status": "available",
            "base_version_id": base["id"],
            "current_version_id": current["id"],
            "computed_diff_ratio": working_model_diff_ratio(
                base["content"],
                current["content"],
            ),
            "diff": _unified_diff(
                base["content"],
                current["content"],
                before_label=f"wm:{base['id']}",
                after_label=f"wm:{current['id']}",
            ),
        }
    return anchors


def _diff_ratio_summary(versions: list[dict[str, Any]]) -> dict[str, Any]:
    non_root = [row for row in versions if row.get("previous_version_id") is not None]
    values = sorted(
        float(row["diff_ratio"])
        for row in non_root
        if row.get("diff_ratio") is not None
    )
    buckets = {
        "[0.00,0.10)": 0,
        "[0.10,0.25)": 0,
        "[0.25,0.50)": 0,
        "[0.50,0.75)": 0,
        "[0.75,1.00]": 0,
    }
    for value in values:
        if value < 0.10:
            buckets["[0.00,0.10)"] += 1
        elif value < 0.25:
            buckets["[0.10,0.25)"] += 1
        elif value < 0.50:
            buckets["[0.25,0.50)"] += 1
        elif value < 0.75:
            buckets["[0.50,0.75)"] += 1
        else:
            buckets["[0.75,1.00]"] += 1
    if not values:
        distribution = {
            "minimum": None,
            "median": None,
            "mean": None,
            "p95_nearest_rank": None,
            "maximum": None,
        }
    else:
        p95_index = max(0, math.ceil(len(values) * 0.95) - 1)
        distribution = {
            "minimum": round(values[0], 6),
            "median": round(float(statistics.median(values)), 6),
            "mean": round(float(statistics.fmean(values)), 6),
            "p95_nearest_rank": round(values[p95_index], 6),
            "maximum": round(values[-1], 6),
        }
    return {
        "algorithm_version": WORKING_MODEL_DIFF_ALGORITHM,
        "flagged_column_semantics": FLAGGED_COLUMN_SEMANTICS,
        "non_root_version_count": len(non_root),
        "observed_value_count": len(values),
        "missing_value_count": len(non_root) - len(values),
        "raw_values": values,
        "distribution": distribution,
        "buckets": buckets,
        "automatic_threshold": None,
        "automatic_flagging": False,
    }


def _message_view(
    messages: dict[str, dict[str, Any]],
    message_id: Any,
) -> dict[str, Any]:
    value = str(message_id or "")
    if not value:
        return {"id": None, "status": "N/A", "reason": "id_not_recorded"}
    row = messages.get(value)
    if row is None:
        return {"id": value, "status": "not_found"}
    return {
        "id": value,
        "status": "found",
        "conv_id": row.get("conv_id"),
        "role": row.get("role"),
        "content": str(row.get("content") or ""),
        "created_at": float(row.get("created_at") or 0.0),
    }


def _outcome_description(request: dict[str, Any]) -> str:
    status = str(request.get("status") or "")
    route = str(request.get("route") or "")
    disposition = str(request.get("disposition") or "")
    if status == "processing":
        return "in_progress_or_interrupted"
    if status == "applied":
        return "working_model_version_applied"
    if status == "writer_noop":
        return "writer_semantic_noop"
    if status == "failed":
        return "technical_failure"
    if route == "reject":
        return "gate_rejected"
    if route == "memory" or disposition == "memory":
        return "routed_to_ai_note_memory"
    return "unknown"


def _request_views(
    requests: list[dict[str, Any]],
    *,
    working_versions: list[dict[str, Any]],
    desire_versions: list[dict[str, Any]],
    messages: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    wm_by_request = {
        str(row.get("origin_request_id")): row
        for row in working_versions
        if row.get("origin_request_id")
    }
    wm_by_id = {str(row["id"]): row for row in working_versions}
    desire_by_request = {
        str(row.get("origin_request_id")): row
        for row in desire_versions
        if str(row.get("origin_request_id") or "") not in DESIRE_ROOT_SENTINELS
    }
    desire_by_id = {str(row["id"]): row for row in desire_versions}
    message_by_id = {str(row["id"]): row for row in messages}

    result: list[dict[str, Any]] = []
    for request in sorted(requests, key=_row_sort_key):
        request_id = str(request.get("id") or "")
        new_wm = wm_by_request.get(request_id)
        old_wm = (
            wm_by_id.get(str(new_wm.get("previous_version_id")))
            if new_wm is not None
            else None
        )
        if new_wm is not None and old_wm is not None:
            wm_change = {
                "status": "changed",
                "before": old_wm,
                "after": new_wm,
                **_diff_entry(old_wm, new_wm),
            }
        else:
            request_status = str(request.get("status") or "")
            wm_change = {
                "status": {
                    "writer_noop": "writer_noop_no_version",
                    "failed": "technical_failure_no_version",
                    "processing": "in_progress_or_interrupted_no_version",
                    "applied": "applied_but_version_missing",
                }.get(request_status, "routed_without_working_model_version"),
                "before": None,
                "after": None,
                "diff": None,
            }

        new_desire = desire_by_request.get(request_id)
        old_desire = (
            desire_by_id.get(str(new_desire.get("previous_version_id")))
            if new_desire is not None
            else None
        )
        if new_desire is not None and old_desire is not None:
            desire_change = {
                "status": "changed",
                "before": old_desire,
                "after": new_desire,
                "change_note": new_desire.get("change_note"),
                "diff": _unified_diff(
                    old_desire["content"],
                    new_desire["content"],
                    before_label=f"desire:{old_desire['id']}",
                    after_label=f"desire:{new_desire['id']}",
                ),
            }
        elif new_wm is not None:
            effective = None
            for candidate in desire_versions:
                if float(candidate.get("created_at") or 0.0) <= float(
                    new_wm.get("created_at") or 0.0
                ):
                    effective = candidate
            desire_change = {
                "status": "unchanged_no_version",
                "before": effective,
                "after": effective,
                "change_note": request.get("writer_change_note"),
                "diff": "",
            }
        else:
            desire_change = {
                "status": "not_applicable",
                "before": None,
                "after": None,
                "change_note": request.get("writer_change_note"),
                "diff": None,
            }

        result.append({
            "rowid": int(request.get("_rowid") or 0),
            "id": request_id,
            "conv_id": request.get("conv_id"),
            "statement": str(request.get("statement") or ""),
            "source": str(request.get("source") or ""),
            "status": str(request.get("status") or ""),
            "route": request.get("route"),
            "gate_reason": request.get("gate_reason"),
            "gate_model": request.get("gate_model"),
            "gate_prompt_version": request.get("gate_prompt_version"),
            "disposition": request.get("disposition"),
            "writer_model": request.get("writer_model"),
            "writer_prompt_version": request.get("writer_prompt_version"),
            "writer_change_note": request.get("writer_change_note"),
            "resulting_memory_id": request.get("resulting_memory_id"),
            "failure_code": request.get("failure_code"),
            "parse_error_code": request.get("parse_error_code"),
            "created_at": float(request.get("created_at") or 0.0),
            "updated_at": float(request.get("updated_at") or 0.0),
            "outcome_description": _outcome_description(request),
            "origin_user_message": _message_view(
                message_by_id,
                request.get("origin_user_message_id"),
            ),
            "origin_assistant_message": _message_view(
                message_by_id,
                request.get("origin_assistant_message_id"),
            ),
            "working_model_change": wm_change,
            "desire_change": desire_change,
        })
    return result


def _decorate_model_transitions(versions: list[dict[str, Any]]) -> None:
    for index, version in enumerate(versions):
        previous = versions[index - 1] if index > 0 else None
        comparable_previous = (
            previous if previous is not None and previous.get("root_kind") is None else None
        )
        previous_model = (
            comparable_previous.get("writer_model")
            if comparable_previous is not None
            else None
        )
        version["previous_writer_model"] = previous_model
        version["writer_model_changed"] = bool(
            previous_model
            and version.get("writer_model")
            and previous_model != version.get("writer_model")
        )


def _request_failure_summary(requests: list[dict[str, Any]]) -> dict[str, Any]:
    parse_counts: dict[tuple[str, str], int] = {}
    failure_counts: dict[str, int] = {}
    for request in requests:
        failure = str(request.get("failure_code") or "").strip()
        if not failure:
            continue
        failure_counts[failure] = failure_counts.get(failure, 0) + 1
        if failure == "parse_failed":
            model = str(request.get("writer_model") or "(unknown)").strip()
            code = str(request.get("parse_error_code") or "(legacy_unrecorded)").strip()
            key = (model, code)
            parse_counts[key] = parse_counts.get(key, 0) + 1
    return {
        "by_failure_code": dict(sorted(failure_counts.items())),
        "parse_failures_by_writer_and_code": [
            {"writer_model": model, "parse_error_code": code, "count": count}
            for (model, code), count in sorted(parse_counts.items())
        ],
    }


def _attach_paired_desire_changes(
    versions: list[dict[str, Any]],
    request_views: list[dict[str, Any]],
) -> None:
    by_request = {str(row["id"]): row for row in request_views}
    for version in versions:
        request_id = str(version.get("origin_request_id") or "")
        request = by_request.get(request_id)
        version["paired_desire_change"] = (
            request.get("desire_change") if request is not None else None
        )


def _reflection_views(
    rows: list[dict[str, Any]],
    *,
    request_views: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Join reflection -> request -> WM/desire outcomes for one audit read."""

    request_by_id = {str(row.get("id") or ""): row for row in request_views}
    result: list[dict[str, Any]] = []
    for row in sorted(rows, key=_row_sort_key):
        request_id = str(row.get("resulting_request_id") or "")
        raw_items = row.get("retrieved_items_json")
        try:
            items = json.loads(str(raw_items or "[]"))
        except json.JSONDecodeError:
            items = {"parse_error": True, "raw": str(raw_items or "")}
        request = request_by_id.get(request_id)
        result.append(
            {
                "id": str(row.get("id") or ""),
                "created_at": float(row.get("created_at") or 0.0),
                "target_conv_id": str(row.get("target_conv_id") or ""),
                "clue": str(row.get("clue") or ""),
                "working_model_id": str(row.get("working_model_id") or ""),
                "inverse_query": str(row.get("inverse_query") or ""),
                "query_model": row.get("query_model"),
                "query_prompt_version": row.get("query_prompt_version"),
                "retrieved_items": items,
                "verdict": row.get("verdict"),
                "reason": row.get("reason"),
                "proposed_statement": row.get("proposed_statement"),
                "outcome": row.get("outcome"),
                "reflection_model": row.get("reflection_model"),
                "reflection_prompt_version": row.get("reflection_prompt_version"),
                "resulting_request_id": row.get("resulting_request_id"),
                "request": request,
                "working_model_change": (
                    request.get("working_model_change") if request else None
                ),
                "desire_change": request.get("desire_change") if request else None,
            }
        )
    return result


def _build_report_from_snapshot(
    snapshot: dict[str, Any],
    *,
    selected_request_id: str | None,
) -> dict[str, Any]:
    wm_rows, wm_diagnostics = _order_chain(snapshot["working_model_versions"])
    desire_rows, desire_diagnostics = _order_chain(snapshot["desire_versions"])
    working_versions = [
        _public_working_model_version(row, index=index)
        for index, row in enumerate(wm_rows)
    ]
    desire_versions = [
        _public_desire_version(row, index=index)
        for index, row in enumerate(desire_rows)
    ]
    _decorate_model_transitions(working_versions)

    adjacent_diffs = [
        _diff_entry(working_versions[index - 1], working_versions[index])
        for index in range(1, len(working_versions))
    ]
    requests = _request_views(
        snapshot["working_model_requests"],
        working_versions=working_versions,
        desire_versions=desire_versions,
        messages=snapshot["messages"],
    )
    _attach_paired_desire_changes(working_versions, requests)
    reflections = _reflection_views(
        snapshot.get("reflection_log") or [],
        request_views=requests,
    )
    request_by_id = {str(row["id"]): row for row in requests}
    selected = None
    if selected_request_id:
        selected = request_by_id.get(selected_request_id)
        if selected is None:
            raise KeyError(f"request not found: {selected_request_id}")

    return {
        "schema_version": AUDIT_SCHEMA_VERSION,
        "generated_at": _utc_now(),
        "read_only": True,
        "diff_algorithm_version": WORKING_MODEL_DIFF_ALGORITHM,
        "flagged_column_semantics": FLAGGED_COLUMN_SEMANTICS,
        "working_model": {
            "diagnostics": wm_diagnostics,
            "versions": working_versions,
            "adjacent_diffs": adjacent_diffs,
            "current_anchor_diffs": build_anchor_diffs(working_versions),
            "diff_ratio_summary": _diff_ratio_summary(working_versions),
        },
        "desire": {
            "diagnostics": desire_diagnostics,
            "versions": desire_versions,
        },
        "requests": requests,
        "request_failure_summary": _request_failure_summary(requests),
        "reflections": reflections,
        "selected_request": selected,
    }


def generate_audit_report(
    db_path: str | Path,
    *,
    selected_request_id: str | None = None,
) -> dict[str, Any]:
    path = Path(db_path).resolve()
    file_hash_before = _file_sha256(path)
    before = _load_snapshot(path)
    snapshot_hash_before = _snapshot_sha256(before)
    report = _build_report_from_snapshot(
        before,
        selected_request_id=selected_request_id,
    )
    after = _load_snapshot(path)
    file_hash_after = _file_sha256(path)
    snapshot_hash_after = _snapshot_sha256(after)
    report["database"] = {
        "path": str(path),
        "open_mode": "sqlite_uri_mode_ro_query_only",
        "file_sha256_before": file_hash_before,
        "file_sha256_after": file_hash_after,
        "audited_snapshot_sha256_before": snapshot_hash_before,
        "audited_snapshot_sha256_after": snapshot_hash_after,
        "row_counts_before": before["table_counts"],
        "row_counts_after": after["table_counts"],
        "unchanged": (
            file_hash_before == file_hash_after
            and snapshot_hash_before == snapshot_hash_after
            and before["table_counts"] == after["table_counts"]
        ),
    }
    return report


def _indent_block(text: Any, prefix: str = "    ") -> list[str]:
    value = str(text if text is not None else "")
    if not value:
        return [prefix + "(empty)"]
    return [prefix + line for line in value.splitlines()]


def _short(value: Any) -> str:
    text = str(value or "").strip()
    return text if text else "N/A"


def render_text_summary(report: dict[str, Any]) -> str:
    database = report["database"]
    working = report["working_model"]
    desire = report["desire"]
    lines = [
        "Working Model V2 Audit",
        f"schema: {report['schema_version']}",
        f"generated_at: {report['generated_at']}",
        f"database: {database['path']}",
        f"read_only_verified: {'YES' if database['unchanged'] else 'NO'}",
        f"diff_algorithm: {report['diff_algorithm_version']}",
        (
            "flagged semantics: migration/provenance only; "
            "NEVER interpreted as a diff anomaly"
        ),
        "",
        "Counts",
        f"  working_model_versions: {len(working['versions'])}",
        f"  desire_versions: {len(desire['versions'])}",
        f"  requests: {len(report['requests'])}",
        f"  reflections: {len(report.get('reflections') or [])}",
        "  failures: " + json.dumps(
            report["request_failure_summary"],
            ensure_ascii=False,
            sort_keys=True,
        ),
        "",
        "Chain diagnostics",
        f"  working_model valid_single_chain: {working['diagnostics']['valid_single_chain']}",
        f"  desire valid_single_chain: {desire['diagnostics']['valid_single_chain']}",
        "",
        "Diff ratio distribution (raw stored values)",
        json.dumps(working["diff_ratio_summary"], ensure_ascii=False, sort_keys=True),
        "",
        "Working model chain",
    ]

    diff_by_after = {
        row["after_version_id"]: row for row in working["adjacent_diffs"]
    }
    for version in working["versions"]:
        root = f" root={version['root_kind']}" if version.get("root_kind") else ""
        model_change = (
            f" MODEL_SWITCH:{version['previous_writer_model']}->{version['writer_model']}"
            if version.get("writer_model_changed")
            else ""
        )
        lines.append(
            f"[{version['index']:02d}] {version['id']} prev={_short(version['previous_version_id'])}"
            f" request={_short(version['origin_request_id'])}{root}{model_change}"
        )
        lines.append(
            f"  writer={_short(version['writer_model'])} "
            f"prompt={_short(version['prompt_version'])} "
            f"diff_ratio={version['diff_ratio'] if version['diff_ratio'] is not None else 'NULL'} "
            f"flagged={version['flagged']}({version['flag_semantics']})"
        )
        lines.append("  content:")
        lines.extend(_indent_block(version["content"], "    "))
        adjacent = diff_by_after.get(version["id"])
        if adjacent is not None:
            lines.append("  adjacent diff:")
            lines.extend(_indent_block(adjacent["diff"], "    "))
        paired = version.get("paired_desire_change")
        if paired is not None:
            lines.append(
                f"  paired desire: {paired['status']} "
                f"note={_short(paired.get('change_note'))}"
            )
            if paired.get("diff"):
                lines.append("  paired desire diff:")
                lines.extend(_indent_block(paired["diff"], "    "))
        lines.append("")

    lines.append("Current anchor diffs")
    for distance in (5, 20):
        anchor = working["current_anchor_diffs"][str(distance)]
        if anchor["status"] == "N/A":
            lines.append(f"  {distance} versions ago: N/A ({anchor['reason']})")
            continue
        lines.append(
            f"  {distance} versions ago: {anchor['base_version_id']} -> "
            f"{anchor['current_version_id']} ratio={anchor['computed_diff_ratio']}"
        )
        lines.extend(_indent_block(anchor["diff"], "    "))

    lines.extend(["", "Desire chain"])
    for version in desire["versions"]:
        root = f" root={version['root_kind']}" if version.get("root_kind") else ""
        lines.append(
            f"[{version['index']:02d}] {version['id']} prev={_short(version['previous_version_id'])}"
            f" request={_short(version['origin_request_id'])} wm={_short(version['working_model_id'])}{root}"
        )
        lines.append(
            f"  writer={_short(version['writer_model'])} "
            f"prompt={_short(version['prompt_version'])} "
            f"change_note={_short(version['change_note'])}"
        )
        lines.extend(_indent_block(version["content"], "    "))

    lines.extend(["", "Requests"])
    for request in report["requests"]:
        lines.append(
            f"- {request['id']} status={request['status']} route={_short(request['route'])} "
            f"disposition={_short(request['disposition'])} outcome={request['outcome_description']}"
        )
        lines.append(
            f"  gate={_short(request['gate_model'])}/{_short(request['gate_prompt_version'])} "
            f"writer={_short(request['writer_model'])}/{_short(request['writer_prompt_version'])}"
        )
        lines.append(f"  gate reason: {_short(request['gate_reason'])}")
        lines.append(
            f"  writer change note: {_short(request['writer_change_note'])}"
        )
        lines.append(f"  statement: {request['statement']}")
        lines.append(f"  source: {request['source']}")
        lines.append(
            f"  wm={request['working_model_change']['status']} "
            f"desire={request['desire_change']['status']} "
            f"memory={_short(request['resulting_memory_id'])} "
            f"failure={_short(request['failure_code'])} "
            f"parse_error={_short(request['parse_error_code'])}"
        )

    lines.extend(["", "Reflections"])
    for reflection in report.get("reflections") or []:
        request = reflection.get("request")
        lines.append(
            f"- {reflection['id']} outcome={_short(reflection['outcome'])} "
            f"verdict={_short(reflection['verdict'])} "
            f"request={_short(reflection['resulting_request_id'])}"
        )
        lines.append(
            f"  working_model_head={_short(reflection['working_model_id'])} "
            f"query={_short(reflection['query_model'])}/{_short(reflection['query_prompt_version'])} "
            f"reflection={_short(reflection['reflection_model'])}/"
            f"{_short(reflection['reflection_prompt_version'])}"
        )
        lines.append(f"  clue: {reflection['clue']}")
        lines.append(f"  inverse query: {_short(reflection['inverse_query'])}")
        lines.append(f"  reason: {_short(reflection['reason'])}")
        if reflection.get("proposed_statement"):
            lines.append(f"  proposed statement: {reflection['proposed_statement']}")
        if request is not None:
            lines.append(
                f"  downstream: status={request['status']} route={_short(request['route'])} "
                f"wm={request['working_model_change']['status']} "
                f"desire={request['desire_change']['status']}"
            )

    selected = report.get("selected_request")
    if selected is not None:
        lines.extend(["", f"Selected request: {selected['id']}"])
        lines.append(
            f"  status={selected['status']} route={_short(selected['route'])} "
            f"disposition={_short(selected['disposition'])}"
        )
        lines.append(f"  gate reason: {_short(selected['gate_reason'])}")
        lines.append(
            f"  writer change note: {_short(selected['writer_change_note'])}"
        )
        lines.append(f"  statement: {selected['statement']}")
        lines.append(f"  source: {selected['source']}")
        lines.append("  origin user message:")
        lines.extend(_indent_block(selected["origin_user_message"].get("content"), "    "))
        lines.append("  origin assistant message:")
        lines.extend(
            _indent_block(selected["origin_assistant_message"].get("content"), "    ")
        )
        lines.append("  working model before:")
        before = selected["working_model_change"].get("before")
        lines.extend(_indent_block(before.get("content") if before else "N/A", "    "))
        lines.append("  working model after:")
        after = selected["working_model_change"].get("after")
        lines.extend(_indent_block(after.get("content") if after else "N/A", "    "))
        lines.append("  desire change:")
        lines.extend(_indent_block(selected["desire_change"].get("diff"), "    "))

    return "\n".join(lines).rstrip() + "\n"


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(text, encoding="utf-8")
    temporary.replace(path)


def _is_within(path: Path, parent: Path) -> bool:
    return path == parent or parent in path.parents


def _validate_output_directory(
    directory: Path,
    *,
    allow_in_repo: bool,
) -> None:
    if allow_in_repo or not _is_within(directory, REPOSITORY_ROOT):
        return
    if any(
        _is_within(directory, allowed)
        for allowed in ALLOWED_IN_REPOSITORY_OUTPUT_ROOTS
    ):
        return
    raise RuntimeError(
        "refusing to write private audit output inside the Git repository; "
        "choose an external directory or pass --allow-in-repo explicitly"
    )


def write_audit_outputs(
    report: dict[str, Any],
    output_dir: str | Path,
    *,
    allow_in_repo: bool = False,
) -> dict[str, str]:
    directory = Path(output_dir).resolve()
    _validate_output_directory(directory, allow_in_repo=allow_in_repo)
    json_path = directory / "wm_audit.json"
    summary_path = directory / "wm_audit.txt"
    _atomic_write(
        json_path,
        json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
    )
    _atomic_write(summary_path, render_text_summary(report))
    return {"json": str(json_path), "summary": str(summary_path)}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default=str(DB_PATH), help="SQLite database path")
    parser.add_argument(
        "--output-dir",
        required=True,
        help="Explicit directory for wm_audit.json and wm_audit.txt",
    )
    parser.add_argument(
        "--request-id",
        default="",
        help="Also expand one exact request in the human summary",
    )
    parser.add_argument(
        "--allow-in-repo",
        action="store_true",
        help=(
            "Explicitly allow raw private audit output inside the Git repository "
            "(unsafe for production data)"
        ),
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    report = generate_audit_report(
        args.db,
        selected_request_id=str(args.request_id or "").strip() or None,
    )
    outputs = write_audit_outputs(
        report,
        args.output_dir,
        allow_in_repo=bool(args.allow_in_repo),
    )
    print(json.dumps({
        "ok": True,
        "read_only_verified": report["database"]["unchanged"],
        "diff_algorithm_version": report["diff_algorithm_version"],
        "outputs": outputs,
    }, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
