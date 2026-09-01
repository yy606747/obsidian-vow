"""SQLite schema for the tool invocation ledger."""

from __future__ import annotations


TOOL_INVOCATION_TABLE = "tool_invocation_events"


async def init_tool_invocation_ledger_tables(db) -> None:
    """Create the append-only, fail-open tool invocation ledger."""

    await db.execute(
        f"""
        CREATE TABLE IF NOT EXISTS {TOOL_INVOCATION_TABLE} (
            id TEXT PRIMARY KEY,
            event_key TEXT NOT NULL UNIQUE,
            turn_id TEXT NOT NULL,
            invocation_id TEXT,
            source_chain TEXT NOT NULL DEFAULT '',
            correlation_id TEXT,
            conv_id TEXT NOT NULL,
            assistant_message_id TEXT,
            intent_id TEXT,
            tool_name TEXT,
            stage TEXT NOT NULL,
            status TEXT,
            outcome TEXT,
            turn_outcome TEXT,
            side_effect_level TEXT,
            source TEXT,
            arguments_json TEXT NOT NULL DEFAULT '{{}}',
            raw_text TEXT NOT NULL DEFAULT '',
            events_json TEXT NOT NULL DEFAULT '[]',
            error TEXT NOT NULL DEFAULT '',
            result_summary TEXT NOT NULL DEFAULT '',
            metadata_json TEXT NOT NULL DEFAULT '{{}}',
            model_key TEXT,
            prompt_source TEXT,
            mode TEXT,
            advertised_tools_json TEXT NOT NULL DEFAULT '[]',
            request_snapshot_json TEXT NOT NULL DEFAULT '',
            raw_output TEXT NOT NULL DEFAULT '',
            cleaned_content TEXT NOT NULL DEFAULT '',
            truncated INTEGER NOT NULL DEFAULT 0,
            snapshot_original_bytes INTEGER NOT NULL DEFAULT 0,
            history_trace_version INTEGER NOT NULL DEFAULT 0,
            created_at REAL NOT NULL,
            updated_at REAL
        )
        """
    )
    for column, definition in (
        ("invocation_id", "TEXT"),
        ("source_chain", "TEXT NOT NULL DEFAULT ''"),
        ("correlation_id", "TEXT"),
        ("request_snapshot_json", "TEXT NOT NULL DEFAULT ''"),
        ("raw_output", "TEXT NOT NULL DEFAULT ''"),
        ("cleaned_content", "TEXT NOT NULL DEFAULT ''"),
        ("truncated", "INTEGER NOT NULL DEFAULT 0"),
        ("snapshot_original_bytes", "INTEGER NOT NULL DEFAULT 0"),
        ("updated_at", "REAL"),
    ):
        try:
            await db.execute(
                f"ALTER TABLE {TOOL_INVOCATION_TABLE} ADD COLUMN {column} {definition}"
            )
        except Exception:
            pass
    await db.execute(
        f"CREATE INDEX IF NOT EXISTS idx_tool_invocation_turn "
        f"ON {TOOL_INVOCATION_TABLE}(turn_id, stage)"
    )
    await db.execute(
        f"CREATE INDEX IF NOT EXISTS idx_tool_invocation_tool_outcome "
        f"ON {TOOL_INVOCATION_TABLE}(tool_name, stage, outcome, created_at DESC)"
    )
    await db.execute(
        f"CREATE INDEX IF NOT EXISTS idx_tool_invocation_experiment "
        f"ON {TOOL_INVOCATION_TABLE}(prompt_source, mode, history_trace_version, turn_outcome, created_at DESC)"
    )
    await db.execute(
        f"CREATE INDEX IF NOT EXISTS idx_tool_invocation_invocation "
        f"ON {TOOL_INVOCATION_TABLE}(invocation_id, stage, created_at)"
    )
    await db.execute(
        f"CREATE INDEX IF NOT EXISTS idx_tool_invocation_correlation "
        f"ON {TOOL_INVOCATION_TABLE}(correlation_id, stage, created_at DESC)"
    )


__all__ = ["TOOL_INVOCATION_TABLE", "init_tool_invocation_ledger_tables"]
