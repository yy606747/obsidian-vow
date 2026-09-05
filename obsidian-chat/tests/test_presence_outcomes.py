import asyncio
from contextlib import asynccontextmanager

import aiosqlite

from app.presence.db import init_presence_tables
from app.presence.outcomes import OUTCOME_TTL_SEC, PresenceOutcomeInbox


class Clock:
    def __init__(self, value=10_000.0):
        self.value = float(value)

    def __call__(self):
        return self.value


async def _inbox(tmp_path, clock):
    db_path = tmp_path / "outcomes.db"

    @asynccontextmanager
    async def db_factory():
        async with aiosqlite.connect(db_path, timeout=2) as db:
            yield db

    async with db_factory() as db:
        await init_presence_tables(db)
        await db.commit()
    return PresenceOutcomeInbox(get_db_factory=db_factory, now=clock), db_factory


def test_outcome_is_claimed_and_consumed_exactly_once(tmp_path):
    async def scenario():
        clock = Clock()
        inbox, db_factory = await _inbox(tmp_path, clock)
        assert await inbox.record_terminal(
            event_id="event-played",
            conv_id="conv-1",
            status="played",
            actual_playback_ms=1_900,
        )
        # Duplicate terminal ACK cannot create a second inbox item.
        assert not await inbox.record_terminal(
            event_id="event-played",
            conv_id="conv-1",
            status="played",
            actual_playback_ms=1_900,
        )
        claimed = await inbox.claim_for_turn(
            conv_id="conv-1", bound_turn_id="send:user-1"
        )
        assert claimed["status"] == "claimed"
        assert "确实在 PC 桌面播放完成" in claimed["block"]
        assert "1900ms" in claimed["block"]
        assert (await inbox.claim_for_turn(
            conv_id="conv-1", bound_turn_id="send:user-other"
        ))["status"] == "empty"

        async with db_factory() as db:
            await db.execute("BEGIN IMMEDIATE")
            consumed = await inbox.consume_claimed_in_tx(
                db,
                bound_turn_id="send:user-1",
                assistant_message_id="assistant-1",
            )
            await db.commit()
        assert consumed == 1
        assert (await inbox.claim_for_turn(
            conv_id="conv-1", bound_turn_id="send:user-2"
        ))["status"] == "empty"
        rows = await inbox.rows_for_event("event-played")
        assert rows[0]["state"] == "consumed"
        assert rows[0]["consumed_by_message_id"] == "assistant-1"

    asyncio.run(scenario())


def test_failed_turn_release_makes_outcome_available_to_next_natural_turn(tmp_path):
    async def scenario():
        clock = Clock()
        inbox, _db = await _inbox(tmp_path, clock)
        await inbox.record_terminal(
            event_id="event-rejected",
            conv_id="conv-2",
            status="rejected",
            reason="geometry_invalid",
        )
        first = await inbox.claim_for_turn(
            conv_id="conv-2", bound_turn_id="send:failed-user"
        )
        assert first["status"] == "claimed"
        assert await inbox.release_claim(bound_turn_id="send:failed-user") == 1
        second = await inbox.claim_for_turn(
            conv_id="conv-2", bound_turn_id="send:next-user"
        )
        assert second["outcome_ids"] == first["outcome_ids"]

    asyncio.run(scenario())


def test_outcome_expires_at_24_hours_and_late_played_is_a_correction(tmp_path):
    async def scenario():
        clock = Clock()
        inbox, _db = await _inbox(tmp_path, clock)
        await inbox.record_terminal(
            event_id="event-old", conv_id="conv-3", status="expired"
        )
        clock.value += OUTCOME_TTL_SEC
        assert (await inbox.claim_for_turn(
            conv_id="conv-3", bound_turn_id="send:after-day"
        ))["status"] == "empty"
        assert (await inbox.rows_for_event("event-old"))[0]["state"] == "expired"

        await inbox.record_terminal(
            event_id="event-corrected", conv_id="conv-3", status="expired"
        )
        clock.value += 1
        await inbox.record_terminal(
            event_id="event-corrected",
            conv_id="conv-3",
            status="played",
            reason="late_played_ack",
            actual_playback_ms=900,
        )
        correction = await inbox.claim_for_turn(
            conv_id="conv-3", bound_turn_id="send:correction"
        )
        assert correction["outcome_ids"] == ["event-corrected:played"]
        assert "已过期" not in correction["block"]
        assert "播放完成" in correction["block"]
        rows = await inbox.rows_for_event("event-corrected")
        assert [(row["presence_status"], row["state"]) for row in rows] == [
            ("expired", "expired"),
            ("played", "claimed"),
        ]

    asyncio.run(scenario())


def test_stale_terminal_cannot_replace_authoritative_played_outcome(tmp_path):
    async def scenario():
        clock = Clock()
        inbox, _db = await _inbox(tmp_path, clock)
        assert await inbox.record_terminal(
            event_id="event-played-first",
            conv_id="conv-4",
            status="played",
            actual_playback_ms=750,
        )
        # A cleanup task may still hold an older expired snapshot while the
        # played ACK is being recorded.  Its late write must be ignored.
        assert not await inbox.record_terminal(
            event_id="event-played-first",
            conv_id="conv-4",
            status="expired",
            reason="stale_cleanup_snapshot",
        )
        claimed = await inbox.claim_for_turn(
            conv_id="conv-4", bound_turn_id="send:played-first"
        )
        assert claimed["outcome_ids"] == ["event-played-first:played"]
        assert "已过期" not in claimed["block"]

    asyncio.run(scenario())


def test_consumed_failure_history_is_kept_and_played_is_delivered_as_correction(
    tmp_path,
):
    async def scenario():
        clock = Clock()
        inbox, db_factory = await _inbox(tmp_path, clock)
        await inbox.record_terminal(
            event_id="event-consumed-correction",
            conv_id="conv-5",
            status="expired",
        )
        await inbox.claim_for_turn(
            conv_id="conv-5", bound_turn_id="send:old-result"
        )
        async with db_factory() as db:
            await db.execute("BEGIN IMMEDIATE")
            await inbox.consume_claimed_in_tx(
                db,
                bound_turn_id="send:old-result",
                assistant_message_id="assistant-old-result",
            )
            await db.commit()

        clock.value += 1
        assert await inbox.record_terminal(
            event_id="event-consumed-correction",
            conv_id="conv-5",
            status="played",
            actual_playback_ms=600,
        )
        correction = await inbox.claim_for_turn(
            conv_id="conv-5", bound_turn_id="send:new-result"
        )
        assert correction["outcome_ids"] == [
            "event-consumed-correction:played"
        ]
        rows = await inbox.rows_for_event("event-consumed-correction")
        assert [(row["presence_status"], row["state"]) for row in rows] == [
            ("expired", "consumed"),
            ("played", "claimed"),
        ]

    asyncio.run(scenario())
