import json
import sqlite3
import time

from scripts.tool_invocation_ledger_report import (
    _load_recent_tool_executions,
    _load_round_turns,
    _load_rows,
    parse_args,
    summarize,
)


def _database(path):
    connection = sqlite3.connect(path)
    connection.execute(
        """
        CREATE TABLE tool_invocation_events (
            turn_id TEXT,
            stage TEXT,
            turn_outcome TEXT,
            advertised_tools_json TEXT,
            created_at REAL,
            prompt_source TEXT,
            mode TEXT,
            history_trace_version INTEGER,
            metadata_json TEXT,
            tool_name TEXT,
            outcome TEXT,
            source_chain TEXT,
            error TEXT
        )
        """
    )
    now = time.time()
    rows = [
        (
            "night-draw", "turn", "succeeded", '["desktop.presence.draw"]',
            now - 100, "opportunity", "normal", 0,
            json.dumps({"round_kind": "night", "round_branch": "draw"}),
            None, "not_executed", "night", "",
        ),
        (
            "summon-none", "turn", "succeeded", '["desktop.presence.show"]',
            now - 90, "opportunity", "normal", 0,
            json.dumps({"round_kind": "summon", "round_branch": "none"}),
            None, "not_executed", "summon", "",
        ),
        (
            "night-provider", "turn", "provider_failed", "[]",
            now - 95, "opportunity", "normal", 0,
            json.dumps({"round_kind": "night", "round_branch": "provider_failed"}),
            None, "not_executed", "night", "",
        ),
        (
            "old", "turn", "succeeded", '["desktop.presence.draw"]',
            now - 3 * 86400, "opportunity", "normal", 0,
            json.dumps({"round_kind": "night", "round_branch": "invalid"}),
            None, "not_executed", "night", "",
        ),
        (
            "night-draw", "parsed", None, "[]", now - 101,
            "", "normal", 0, "{}", "desktop.presence.draw",
            "not_executed", "night", "",
        ),
    ]
    for index in range(12):
        rows.append((
            "night-draw", "execution", None, "[]", now - 110 + index,
            "", "normal", 0, "{}", "desktop.presence.draw",
            "failed" if index == 11 else "succeeded",
            "night", f"raw-provider-error-{index}" if index == 11 else "",
        ))
    connection.executemany(
        "INSERT INTO tool_invocation_events VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        rows,
    )
    connection.commit()
    connection.close()
    return now


def test_since_days_window_tool_filter_recent_errors_and_round_branches(tmp_path):
    path = tmp_path / "ledger.db"
    now = _database(path)
    since_at = now - 86400

    turns, events = _load_rows(path, limit_turns=1, since_at=since_at)
    round_turns = _load_round_turns(path, since_at=since_at, turns=turns)
    recent = _load_recent_tool_executions(
        path,
        tool_name="desktop.presence.draw",
        since_at=since_at,
        turns=turns,
    )
    report = summarize(
        turns,
        events,
        tool_filter="desktop.presence.draw",
        round_turns=round_turns,
        recent_tool_executions=recent,
    )

    assert {row["turn_id"] for row in turns} == {"night-draw", "summon-none"}
    assert list(report["tools"]) == ["desktop.presence.draw"]
    assert report["tools"]["desktop.presence.draw"]["executions"] == 12
    assert len(report["recent_executions"]) == 10
    assert report["recent_executions"][0] == {
        "created_at": now - 99,
        "source_chain": "night",
        "outcome": "failed",
        "error": "raw-provider-error-11",
    }
    assert report["autonomous_rounds"]["night"] == {
        "rounds": 2,
        "branches": {
            "draw": {"count": 1, "percent": 50.0},
            "provider_failed": {"count": 1, "percent": 50.0},
        },
    }
    assert report["autonomous_rounds"]["summon"]["branches"]["none"]["count"] == 1
    assert "invalid" not in report["autonomous_rounds"]["night"]["branches"]


def test_limit_window_and_cli_arguments_remain_bounded(tmp_path):
    path = tmp_path / "ledger.db"
    _database(path)

    turns, _events = _load_rows(path, limit_turns=1)
    assert len(turns) == 1
    assert parse_args(["--since-days", "2.5", "--tool", " desktop.presence.show "]).since_days == 2.5
    assert parse_args(["--since-days", "2.5", "--tool", " desktop.presence.show "]).tool == "desktop.presence.show"
    assert parse_args(["--limit-turns", "999999"]).limit_turns == 5000
