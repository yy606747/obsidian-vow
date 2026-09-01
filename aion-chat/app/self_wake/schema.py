"""SQLite schema for Self-Wake V1."""

from __future__ import annotations


async def init_self_wake_tables(db) -> None:
    await db.execute(
        """
        CREATE TABLE IF NOT EXISTS self_wakes (
            id TEXT PRIMARY KEY,
            wake_at REAL NOT NULL,
            intent TEXT NOT NULL,
            requested_capabilities_json TEXT NOT NULL,
            origin TEXT NOT NULL,
            origin_ref TEXT NOT NULL,
            source TEXT NOT NULL CHECK(source IN ('chat', 'opportunity')),
            conv_id TEXT NOT NULL,
            source_turn_id TEXT NOT NULL,
            owner_timezone TEXT NOT NULL,
            state TEXT NOT NULL CHECK(
                state IN ('pending', 'consumed', 'invalidated', 'expired')
            ),
            created_at REAL NOT NULL,
            expires_at REAL NOT NULL,
            consumed_at REAL,
            closed_at REAL,
            close_reason TEXT,
            trigger_outcome TEXT,
            trigger_error TEXT,
            finished_at REAL
        )
        """
    )
    await db.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_self_wakes_one_pending_origin "
        "ON self_wakes(origin, origin_ref) WHERE state='pending'"
    )
    await db.execute(
        "CREATE INDEX IF NOT EXISTS idx_self_wakes_due "
        "ON self_wakes(state, wake_at)"
    )
    await db.execute(
        "CREATE INDEX IF NOT EXISTS idx_self_wakes_quota "
        "ON self_wakes(state, consumed_at)"
    )
    await db.execute(
        "CREATE INDEX IF NOT EXISTS idx_self_wakes_audit "
        "ON self_wakes(origin, origin_ref, created_at DESC)"
    )


__all__ = ["init_self_wake_tables"]
