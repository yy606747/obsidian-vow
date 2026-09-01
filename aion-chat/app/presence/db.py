"""SQLite schema for Desktop Presence V1."""

from __future__ import annotations


async def init_presence_tables(db) -> None:
    await db.execute(
        """
        CREATE TABLE IF NOT EXISTS presence_sprites (
            sprite_id TEXT PRIMARY KEY,
            sprite_hash TEXT NOT NULL UNIQUE,
            file_name TEXT NOT NULL UNIQUE,
            base_height_dip REAL NOT NULL,
            description TEXT NOT NULL,
            prompt TEXT NOT NULL DEFAULT '',
            form TEXT NOT NULL DEFAULT 'unknown'
                CHECK(form IN ('unknown','human','nonhuman')),
            provider TEXT NOT NULL DEFAULT '',
            width_px INTEGER NOT NULL,
            height_px INTEGER NOT NULL,
            active INTEGER NOT NULL DEFAULT 1,
            archived INTEGER NOT NULL DEFAULT 0,
            created_at REAL NOT NULL
        )
        """
    )
    cursor = await db.execute("PRAGMA table_info(presence_sprites)")
    sprite_columns = {str(row[1]) for row in await cursor.fetchall()}
    if "form" not in sprite_columns:
        await db.execute(
            "ALTER TABLE presence_sprites ADD COLUMN form TEXT NOT NULL "
            "DEFAULT 'unknown' CHECK(form IN ('unknown','human','nonhuman'))"
        )
    await db.execute(
        """
        CREATE TABLE IF NOT EXISTS presence_sprite_sync (
            sprite_hash TEXT NOT NULL,
            device_id TEXT NOT NULL DEFAULT 'pc',
            status TEXT NOT NULL CHECK(status IN ('pending','synced')),
            synced_at REAL,
            updated_at REAL NOT NULL,
            PRIMARY KEY(sprite_hash, device_id),
            FOREIGN KEY(sprite_hash) REFERENCES presence_sprites(sprite_hash)
                ON DELETE CASCADE
        )
        """
    )
    await db.execute(
        """
        CREATE TABLE IF NOT EXISTS presence_draw_quota (
            local_date TEXT PRIMARY KEY,
            used_count INTEGER NOT NULL CHECK(used_count >= 0),
            updated_at REAL NOT NULL
        )
        """
    )
    await db.execute(
        "CREATE INDEX IF NOT EXISTS idx_presence_sprites_active_created "
        "ON presence_sprites(active, archived, created_at DESC)"
    )
    await db.execute(
        "CREATE INDEX IF NOT EXISTS idx_presence_sprite_sync_device_status "
        "ON presence_sprite_sync(device_id, status, updated_at DESC)"
    )
    await db.execute(
        """
        CREATE TABLE IF NOT EXISTS presence_intent_state (
            device_id TEXT PRIMARY KEY,
            latest_version INTEGER NOT NULL CHECK(latest_version >= 0),
            updated_at REAL NOT NULL
        )
        """
    )
    await db.execute(
        """
        CREATE TABLE IF NOT EXISTS presence_intents (
            intent_id TEXT PRIMARY KEY,
            conv_id TEXT NOT NULL,
            device_id TEXT NOT NULL DEFAULT 'pc',
            intent_version INTEGER NOT NULL,
            intent_text TEXT NOT NULL,
            status TEXT NOT NULL CHECK(status IN (
                'rendering','rendered','rejected','superseded'
            )),
            event_id TEXT,
            failure_reason TEXT NOT NULL DEFAULT '',
            created_at REAL NOT NULL,
            updated_at REAL NOT NULL,
            UNIQUE(device_id, intent_version)
        )
        """
    )
    await db.execute(
        """
        CREATE TABLE IF NOT EXISTS presence_events (
            event_id TEXT PRIMARY KEY,
            correlation_id TEXT NOT NULL UNIQUE,
            conv_id TEXT NOT NULL,
            device_id TEXT NOT NULL DEFAULT 'pc',
            intent_id TEXT NOT NULL,
            intent_version INTEGER NOT NULL,
            sprite_id TEXT NOT NULL,
            sprite_hash TEXT NOT NULL,
            trajectory_json TEXT NOT NULL,
            duration_ms INTEGER NOT NULL,
            status TEXT NOT NULL CHECK(status IN (
                'queued','dispatched','accepted','played',
                'rejected','expired','superseded'
            )),
            created_at REAL NOT NULL,
            start_before REAL NOT NULL,
            dispatched_at REAL,
            accepted_at REAL,
            terminal_at REAL,
            reason TEXT NOT NULL DEFAULT '',
            actual_playback_ms INTEGER,
            terminal_notified_at REAL,
            updated_at REAL NOT NULL,
            FOREIGN KEY(intent_id) REFERENCES presence_intents(intent_id),
            FOREIGN KEY(sprite_hash) REFERENCES presence_sprites(sprite_hash)
        )
        """
    )
    await db.execute(
        """
        CREATE TABLE IF NOT EXISTS presence_agent_state (
            device_id TEXT PRIMARY KEY,
            last_seen_at REAL NOT NULL,
            updated_at REAL NOT NULL
        )
        """
    )
    await db.execute(
        """
        CREATE TABLE IF NOT EXISTS presence_outcomes (
            outcome_id TEXT PRIMARY KEY,
            event_id TEXT NOT NULL,
            conv_id TEXT NOT NULL,
            presence_status TEXT NOT NULL CHECK(presence_status IN (
                'played','rejected','expired','superseded'
            )),
            reason TEXT NOT NULL DEFAULT '',
            actual_playback_ms INTEGER,
            payload_json TEXT NOT NULL DEFAULT '{}',
            state TEXT NOT NULL CHECK(state IN (
                'ready','claimed','consumed','expired'
            )),
            created_at REAL NOT NULL,
            expires_at REAL NOT NULL,
            claimed_turn_id TEXT,
            claimed_at REAL,
            consumed_by_message_id TEXT,
            consumed_at REAL,
            UNIQUE(event_id, presence_status)
        )
        """
    )
    await db.execute(
        "CREATE INDEX IF NOT EXISTS idx_presence_intents_device_version "
        "ON presence_intents(device_id, intent_version DESC)"
    )
    await db.execute(
        "CREATE INDEX IF NOT EXISTS idx_presence_events_device_status_time "
        "ON presence_events(device_id, status, created_at DESC)"
    )
    await db.execute(
        "CREATE INDEX IF NOT EXISTS idx_presence_events_unnotified "
        "ON presence_events(status, terminal_notified_at, terminal_at)"
    )
    await db.execute(
        "CREATE INDEX IF NOT EXISTS idx_presence_outcomes_conv_state_time "
        "ON presence_outcomes(conv_id, state, created_at)"
    )
    await db.execute(
        """
        CREATE TABLE IF NOT EXISTS summon_events (
            summon_id TEXT PRIMARY KEY,
            conv_id TEXT NOT NULL,
            device_id TEXT NOT NULL DEFAULT 'pc',
            occurred_at REAL NOT NULL,
            received_at REAL NOT NULL,
            status TEXT NOT NULL CHECK(status IN (
                'processing','processed','coalesced','gated','failed'
            )),
            coalesced_into TEXT,
            failure_reason TEXT NOT NULL DEFAULT '',
            updated_at REAL NOT NULL
        )
        """
    )
    await db.execute(
        "CREATE INDEX IF NOT EXISTS idx_summon_events_conv_occurred "
        "ON summon_events(conv_id, occurred_at DESC)"
    )
    await db.execute(
        "CREATE INDEX IF NOT EXISTS idx_summon_events_status_updated "
        "ON summon_events(status, updated_at)"
    )
    await db.execute(
        """
        CREATE TABLE IF NOT EXISTS presence_night_rounds (
            night_key TEXT PRIMARY KEY,
            timezone TEXT NOT NULL,
            status TEXT NOT NULL CHECK(status IN ('running','completed','failed')),
            branch TEXT CHECK(branch IS NULL OR branch IN (
                'draw','reflect','none','invalid','provider_failed'
            )),
            started_at REAL NOT NULL,
            finished_at REAL,
            error TEXT NOT NULL DEFAULT '',
            updated_at REAL NOT NULL
        )
        """
    )
    await db.execute(
        "CREATE INDEX IF NOT EXISTS idx_presence_night_rounds_started "
        "ON presence_night_rounds(started_at DESC)"
    )


__all__ = ["init_presence_tables"]
