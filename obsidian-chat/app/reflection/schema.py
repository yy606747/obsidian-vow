"""Durable audit storage for the reflection chain."""

from __future__ import annotations


async def init_reflection_tables(db) -> None:
    await db.execute(
        """
        CREATE TABLE IF NOT EXISTS reflection_log (
            id                        TEXT PRIMARY KEY,
            created_at                REAL NOT NULL,
            target_conv_id            TEXT NOT NULL,
            clue                      TEXT NOT NULL,
            working_model_id          TEXT NOT NULL,
            inverse_query             TEXT,
            query_model               TEXT,
            query_prompt_version      TEXT,
            retrieved_items_json      TEXT,
            verdict                   TEXT CHECK (
                verdict IS NULL OR verdict IN ('holds','unclear','conflicts')
            ),
            reason                    TEXT,
            proposed_statement        TEXT,
            outcome                   TEXT CHECK (
                outcome IS NULL OR outcome IN (
                    'ok','no_evidence','query_failed','retrieval_failed',
                    'reflection_provider_failed','reflection_parse_failed',
                    'handoff_failed'
                )
            ),
            reflection_model          TEXT,
            reflection_prompt_version TEXT,
            resulting_request_id      TEXT
        )
        """
    )
    await db.execute(
        "CREATE INDEX IF NOT EXISTS idx_reflection_log_created "
        "ON reflection_log(created_at DESC)"
    )
    await db.execute(
        "CREATE INDEX IF NOT EXISTS idx_reflection_log_conv_created "
        "ON reflection_log(target_conv_id, created_at DESC)"
    )
    await db.execute(
        "CREATE INDEX IF NOT EXISTS idx_reflection_log_request "
        "ON reflection_log(resulting_request_id)"
    )


__all__ = ["init_reflection_tables"]
