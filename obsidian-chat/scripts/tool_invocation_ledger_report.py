"""Read-only snapshot of the last successful tool-eligible model turns."""

from __future__ import annotations

import argparse
from collections import Counter, defaultdict
import json
from pathlib import Path
import sqlite3
import time
from typing import Any


DEFAULT_DB_PATH = Path(__file__).resolve().parents[1] / "data" / "chat.db"


def _rate(numerator: int, denominator: int) -> float | None:
    if denominator <= 0:
        return None
    return round(100.0 * numerator / denominator, 2)


def _load_rows(
    db_path: Path,
    *,
    limit_turns: int,
    since_at: float | None = None,
) -> tuple[list[dict], list[dict]]:
    uri = f"file:{db_path.resolve()}?mode=ro"
    connection = sqlite3.connect(uri, uri=True)
    connection.row_factory = sqlite3.Row
    try:
        table = connection.execute(
            "SELECT 1 FROM sqlite_master "
            "WHERE type='table' AND name='tool_invocation_events'"
        ).fetchone()
        if table is None:
            raise RuntimeError("tool_invocation_events table is not initialized")
        turn_sql = (
            "SELECT * FROM tool_invocation_events "
            "WHERE stage='turn' AND turn_outcome='succeeded' "
            "AND advertised_tools_json <> '[]' "
        )
        turn_params: list[Any] = []
        if since_at is not None:
            turn_sql += "AND created_at>=? ORDER BY created_at DESC"
            turn_params.append(float(since_at))
        else:
            turn_sql += "ORDER BY created_at DESC LIMIT ?"
            turn_params.append(int(limit_turns))
        turns = [
            dict(row)
            for row in connection.execute(turn_sql, turn_params).fetchall()
        ]
        if not turns:
            return [], []
        turn_ids = [str(row["turn_id"]) for row in turns]
        placeholders = ",".join("?" for _ in turn_ids)
        events = [
            dict(row)
            for row in connection.execute(
                "SELECT * FROM tool_invocation_events "
                f"WHERE stage <> 'turn' AND turn_id IN ({placeholders}) "
                "ORDER BY created_at",
                turn_ids,
            ).fetchall()
        ]
        return turns, events
    finally:
        connection.close()


def _load_round_turns(
    db_path: Path,
    *,
    since_at: float | None,
    turns: list[dict],
) -> list[dict]:
    uri = f"file:{db_path.resolve()}?mode=ro"
    connection = sqlite3.connect(uri, uri=True)
    connection.row_factory = sqlite3.Row
    try:
        sql = "SELECT * FROM tool_invocation_events WHERE stage='turn'"
        params: list[Any] = []
        if since_at is not None:
            sql += " AND created_at>=?"
            params.append(float(since_at))
        elif turns:
            timestamps = [float(row["created_at"]) for row in turns]
            sql += " AND created_at>=? AND created_at<=?"
            params.extend((min(timestamps), max(timestamps)))
        else:
            return []
        sql += " ORDER BY created_at DESC"
        return [dict(row) for row in connection.execute(sql, params).fetchall()]
    finally:
        connection.close()


def _load_recent_tool_executions(
    db_path: Path,
    *,
    tool_name: str,
    since_at: float | None,
    turns: list[dict],
) -> list[dict[str, Any]]:
    uri = f"file:{db_path.resolve()}?mode=ro"
    connection = sqlite3.connect(uri, uri=True)
    connection.row_factory = sqlite3.Row
    try:
        sql = (
            "SELECT created_at, source_chain, outcome, error "
            "FROM tool_invocation_events "
            "WHERE stage='execution' AND tool_name=?"
        )
        params: list[Any] = [tool_name]
        if since_at is not None:
            sql += " AND created_at>=?"
            params.append(float(since_at))
        elif turns:
            turn_ids = [str(row.get("turn_id") or "") for row in turns]
            placeholders = ",".join("?" for _item in turn_ids)
            sql += f" AND turn_id IN ({placeholders})"
            params.extend(turn_ids)
        else:
            return []
        sql += " ORDER BY created_at DESC LIMIT 10"
        return [dict(row) for row in connection.execute(sql, params).fetchall()]
    finally:
        connection.close()


def summarize(
    turns: list[dict],
    events: list[dict],
    *,
    tool_filter: str | None = None,
    round_turns: list[dict] = (),
    recent_tool_executions: list[dict] = (),
) -> dict[str, Any]:
    advertised_turns: Counter[str] = Counter()
    cohorts: Counter[tuple[str, str, int]] = Counter()
    turn_cohorts: dict[str, tuple[str, str, int]] = {}
    for turn in turns:
        advertised = json.loads(turn.get("advertised_tools_json") or "[]")
        advertised_turns.update(str(item) for item in advertised)
        cohort = (
            str(turn.get("prompt_source") or "unknown"),
            str(turn.get("mode") or "unknown"),
            int(turn.get("history_trace_version") or 0),
        )
        cohorts[cohort] += 1
        turn_cohorts[str(turn.get("turn_id") or "")] = cohort

    stage_counts = Counter(str(event.get("stage") or "") for event in events)
    execution_outcomes = Counter(
        str(event.get("outcome") or "unknown")
        for event in events
        if event.get("stage") == "execution"
    )
    cohort_events: dict[tuple[str, str, int], Counter[str]] = defaultdict(Counter)
    per_tool: dict[str, Counter[str]] = defaultdict(Counter)
    for event in events:
        tool_name = str(event.get("tool_name") or "unknown")
        stage = str(event.get("stage") or "unknown")
        per_tool[tool_name][stage] += 1
        cohort = turn_cohorts.get(str(event.get("turn_id") or ""))
        if cohort is not None:
            cohort_events[cohort][stage] += 1
        if stage == "execution":
            per_tool[tool_name][f"outcome:{event.get('outcome') or 'unknown'}"] += 1

    parsed = stage_counts["parsed"]
    parse_failed = stage_counts["parse_failed"]
    report_tools = {}
    tool_names = sorted(set(advertised_turns) | set(per_tool))
    if tool_filter is not None:
        tool_names = [tool_filter]
    for tool_name in tool_names:
        counts = per_tool[tool_name]
        denominator = advertised_turns[tool_name]
        report_tools[tool_name] = {
            "advertised_turns": denominator,
            "parsed_calls": counts["parsed"],
            "calls_per_100_advertised_turns": _rate(counts["parsed"], denominator),
            "parse_failed": counts["parse_failed"],
            "not_enabled": counts["not_enabled"],
            "executions": counts["execution"],
            "execution_outcomes": {
                key[len("outcome:") :]: value
                for key, value in sorted(counts.items())
                if key.startswith("outcome:")
            },
        }

    timestamps = [float(turn["created_at"]) for turn in turns]
    report = {
        "mode": "read_only",
        "coverage": {
            "included": ["send", "regenerate", "initiative", "opportunity"],
            "excluded": ["vow", "tide_intent", "schedule_trigger", "sentinel"],
        },
        "eligible_turns": len(turns),
        "window_created_at": {
            "oldest": min(timestamps) if timestamps else None,
            "newest": max(timestamps) if timestamps else None,
        },
        "cohorts": {
            f"{source}:{mode}:history_trace_v{trace_version}": {
                "eligible_turns": count,
                "parsed_calls": cohort_events[(source, mode, trace_version)]["parsed"],
                "calls_per_100_eligible_turns": _rate(
                    cohort_events[(source, mode, trace_version)]["parsed"],
                    count,
                ),
                "parse_failed": cohort_events[(source, mode, trace_version)]["parse_failed"],
            }
            for (source, mode, trace_version), count in sorted(cohorts.items())
        },
        "parsed_calls": parsed,
        "calls_per_100_eligible_turns": _rate(parsed, len(turns)),
        "parse_failed": parse_failed,
        "parse_failure_percent_of_attempts": _rate(
            parse_failed,
            parsed + parse_failed,
        ),
        "not_enabled": stage_counts["not_enabled"],
        "executions": stage_counts["execution"],
        "execution_outcomes": dict(sorted(execution_outcomes.items())),
        "tools": report_tools,
        "autonomous_rounds": _summarize_autonomous_rounds(round_turns),
    }
    if tool_filter is not None:
        report["tool_filter"] = tool_filter
        report["recent_executions"] = [
            {
                "created_at": float(row["created_at"]),
                "source_chain": str(row.get("source_chain") or ""),
                "outcome": str(row.get("outcome") or "unknown"),
                "error": str(row.get("error") or ""),
            }
            for row in recent_tool_executions[:10]
        ]
    return report


def _summarize_autonomous_rounds(turns: list[dict]) -> dict[str, Any]:
    counts: dict[str, Counter[str]] = defaultdict(Counter)
    for turn in turns:
        try:
            metadata = json.loads(turn.get("metadata_json") or "{}")
        except (TypeError, json.JSONDecodeError):
            continue
        if not isinstance(metadata, dict):
            continue
        kind = str(metadata.get("round_kind") or "").strip().lower()
        branch = str(metadata.get("round_branch") or "").strip().lower()
        if kind not in {"idle", "summon", "night"} or not branch:
            continue
        counts[kind][branch] += 1
    result = {}
    for kind in ("idle", "summon", "night"):
        branches = counts[kind]
        total = sum(branches.values())
        result[kind] = {
            "rounds": total,
            "branches": {
                branch: {
                    "count": count,
                    "percent": _rate(count, total),
                }
                for branch, count in sorted(branches.items())
            },
        }
    return result


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default=str(DEFAULT_DB_PATH))
    parser.add_argument("--limit-turns", type=int, default=50)
    parser.add_argument("--since-days", type=float)
    parser.add_argument("--tool")
    args = parser.parse_args(argv)
    args.limit_turns = max(1, min(5000, int(args.limit_turns)))
    if args.since_days is not None and float(args.since_days) <= 0:
        parser.error("--since-days must be greater than zero")
    args.tool = str(args.tool or "").strip() or None
    return args


def main() -> None:
    args = parse_args()
    db_path = Path(args.db)
    since_at = (
        time.time() - float(args.since_days) * 24 * 60 * 60
        if args.since_days is not None
        else None
    )
    turns, events = _load_rows(
        db_path,
        limit_turns=args.limit_turns,
        since_at=since_at,
    )
    round_turns = _load_round_turns(
        db_path,
        since_at=since_at,
        turns=turns,
    )
    recent_executions = (
        _load_recent_tool_executions(
            db_path,
            tool_name=args.tool,
            since_at=since_at,
            turns=turns,
        )
        if args.tool is not None
        else []
    )
    print(json.dumps(summarize(
        turns,
        events,
        tool_filter=args.tool,
        round_turns=round_turns,
        recent_tool_executions=recent_executions,
    ), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
