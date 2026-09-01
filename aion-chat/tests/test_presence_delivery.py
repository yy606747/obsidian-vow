import asyncio
import io
import time
from contextlib import asynccontextmanager

import aiosqlite
import pytest
from PIL import Image

from app.presence.db import init_presence_tables
from app.presence.service import (
    PresenceDeliveryError,
    PresenceDeliveryService,
    PresenceNotFound,
)
from app.presence.sprites import SpriteLibrary


class MutableClock:
    def __init__(self, value=1_000.0):
        self.value = float(value)

    def __call__(self):
        return self.value

    def advance(self, seconds):
        self.value += float(seconds)


class RecordingLedger:
    def __init__(self):
        self.calls = []

    async def record_terminal_outcome(self, **kwargs):
        self.calls.append(kwargs)
        return 1


def _transparent_png() -> bytes:
    image = Image.new("RGBA", (32, 32), (0, 0, 0, 0))
    for x in range(7, 25):
        for y in range(5, 27):
            image.putpixel((x, y), (110, 65, 215, 255))
    output = io.BytesIO()
    image.save(output, format="PNG")
    return output.getvalue()


def _trajectory(sprite_id="fog_seed", duration_ms=1_000):
    return {
        "sprite_id": sprite_id,
        "target_screen": "active",
        "anchor": "bottom_right",
        "transform_origin": "center",
        "duration_ms": duration_ms,
        "tracks": [
            {"prop": "x", "keys": [[0, 80], [duration_ms, 0]], "ease": "out_cubic"},
            {"prop": "opacity", "keys": [[0, 0], [duration_ms, 1]]},
        ],
    }


async def _stack(tmp_path, clock, *, ledger=None, grace=30.0):
    db_path = tmp_path / "presence-delivery.db"

    @asynccontextmanager
    async def db_factory():
        async with aiosqlite.connect(db_path, timeout=2.0) as db:
            yield db

    async with db_factory() as db:
        await init_presence_tables(db)
        await db.commit()
    sprites = SpriteLibrary(
        get_db_factory=db_factory,
        storage_dir=tmp_path / "sprites",
        now=clock,
        timezone_name="UTC",
    )
    if await sprites.get_sprite("fog_seed") is None:
        added = await sprites.add_sprite(
            sprite_id="fog_seed",
            png=_transparent_png(),
            description="a small violet fog",
            base_height_dip=240,
        )
        await sprites.mark_synced(added["sprite_hash"])
    service = PresenceDeliveryService(
        get_db_factory=db_factory,
        sprites=sprites,
        now=clock,
        monotonic=clock,
        terminal_recorder=ledger or RecordingLedger(),
        accepted_grace_sec=grace,
    )
    return service, sprites, db_factory


def test_queue_overwrite_duplicate_delivery_and_idempotent_terminal_ack(tmp_path):
    async def scenario():
        clock = MutableClock()
        ledger = RecordingLedger()
        service, _sprites, _db = await _stack(tmp_path, clock, ledger=ledger)

        first = await service.enqueue_trajectory(
            conv_id="conv-1", intent_text="first", trajectory=_trajectory()
        )
        clock.advance(0.1)
        second = await service.enqueue_trajectory(
            conv_id="conv-1", intent_text="second", trajectory=_trajectory()
        )
        assert (await service.get_event(first["event_id"]))["status"] == "superseded"
        assert (await service.get_event(second["event_id"]))["status"] == "queued"

        delivered = await service.poll_pending(timeout=0)
        duplicate = await service.poll_pending(timeout=0)
        assert delivered["event_id"] == duplicate["event_id"] == second["event_id"]
        assert delivered["remaining_ttl_ms"] > 0

        accepted = await service.ack(second["event_id"], status="accepted")
        assert accepted["status"] == "accepted"
        assert (await service.ack(second["event_id"], status="accepted"))["status"] == "accepted"
        played = await service.ack(
            second["event_id"], status="played", actual_playback_ms=990
        )
        assert played["status"] == "played"
        # A duplicate/stale ACK returns the saved terminal state, never downgrades it.
        assert (await service.ack(second["event_id"], status="accepted"))["status"] == "played"

        mapped = {(call["event_type"], call["outcome"]) for call in ledger.calls}
        assert ("presence.superseded", "rejected") in mapped
        assert ("presence.played", "succeeded") in mapped
        outcome_rows = await service._outcome_recorder.rows_for_event(
            second["event_id"]
        )
        assert len(outcome_rows) == 1
        assert outcome_rows[0]["presence_status"] == "played"

    asyncio.run(scenario())


def test_poll_does_not_lose_enqueue_wakeup_between_query_and_wait():
    class RaceService(PresenceDeliveryService):
        def __init__(self):
            self._wake = asyncio.Event()
            self._monotonic = time.monotonic
            self.calls = 0

        async def _poll_once(self, _device_id):
            self.calls += 1
            if self.calls == 1:
                # Reproduce enqueue committing immediately after the empty
                # database read but before poll_pending starts waiting.
                self._wake.set()
                return None, []
            return {"event_id": "race-delivery"}, []

        async def _notify_terminals(self, _rows):
            return None

    async def scenario():
        service = RaceService()
        result = await service.poll_pending(timeout=0.1)
        assert result == {"event_id": "race-delivery"}
        assert service.calls == 2

    asyncio.run(scenario())


def test_accepted_crash_lease_frees_slot_after_restart(tmp_path):
    async def scenario():
        clock = MutableClock()
        ledger = RecordingLedger()
        service, sprites, db_factory = await _stack(
            tmp_path, clock, ledger=ledger, grace=3.0
        )
        old = await service.enqueue_trajectory(
            conv_id="conv-lease", intent_text="old", trajectory=_trajectory(duration_ms=1_000)
        )
        assert (await service.poll_pending(timeout=0))["event_id"] == old["event_id"]
        await service.ack(old["event_id"], status="accepted")

        clock.advance(1.0)
        new = await service.enqueue_trajectory(
            conv_id="conv-lease", intent_text="new", trajectory=_trajectory(duration_ms=1_000)
        )
        assert await service.poll_pending(timeout=0) is None

        # Simulate a process restart: no in-memory queue state survives.
        restarted = PresenceDeliveryService(
            get_db_factory=db_factory,
            sprites=sprites,
            now=clock,
            monotonic=clock,
            terminal_recorder=ledger,
            accepted_grace_sec=3.0,
        )
        clock.advance(3.1)
        delivered = await restarted.poll_pending(timeout=0)
        assert delivered["event_id"] == new["event_id"]
        assert (await restarted.get_event(old["event_id"]))["status"] == "expired"
        assert any(
            call["event_type"] == "presence.expired"
            and call["outcome"] == "failed"
            and call["result"]["reason"] == "delivery_lease_expired"
            for call in ledger.calls
        )

    asyncio.run(scenario())


def test_start_deadline_expiry_and_late_played_correction(tmp_path):
    async def scenario():
        clock = MutableClock()
        ledger = RecordingLedger()
        service, _sprites, _db = await _stack(tmp_path, clock, ledger=ledger)
        queued = await service.enqueue_trajectory(
            conv_id="conv-expiry",
            intent_text="too late",
            trajectory=_trajectory(),
            start_ttl_sec=1,
        )
        clock.advance(1.1)
        assert await service.poll_pending(timeout=0) is None
        assert (await service.get_event(queued["event_id"]))["status"] == "expired"

        corrected = await service.ack(
            queued["event_id"], status="played", actual_playback_ms=1_010
        )
        assert corrected["status"] == "played"
        outcome = await service._outcome_recorder.claim_for_turn(
            conv_id="conv-expiry", bound_turn_id="send:after-correction"
        )
        assert outcome["outcome_ids"] == [f"{queued['event_id']}:played"]
        assert "已过期" not in outcome["block"]
        assert [call["event_type"] for call in ledger.calls][-2:] == [
            "presence.expired",
            "presence.played",
        ]

    asyncio.run(scenario())


def test_start_deadline_is_anchored_to_intent_creation_not_render_completion(tmp_path):
    async def scenario():
        clock = MutableClock()
        service, _sprites, _db = await _stack(tmp_path, clock)
        reserved = await service.reserve_intent(
            conv_id="conv-slow-render",
            intent_text="render slowly",
        )

        clock.advance(20)
        queued = await service.enqueue_trajectory(
            intent_id=reserved["intent_id"],
            intent_version=reserved["intent_version"],
            trajectory=_trajectory(),
            start_ttl_sec=30,
        )
        assert queued["start_before"] == 1_030.0

        delivery = await service.poll_pending(timeout=0)
        assert delivery["remaining_ttl_ms"] == 10_000
        clock.advance(10.1)
        assert await service.poll_pending(timeout=0) is None
        assert (await service.get_event(queued["event_id"]))["status"] == "expired"

    asyncio.run(scenario())


def test_played_ack_survives_lost_accepted_transport_response(tmp_path):
    async def scenario():
        clock = MutableClock()
        service, _sprites, _db = await _stack(tmp_path, clock)
        event = await service.enqueue_trajectory(
            conv_id="conv-lost-ack", intent_text="play", trajectory=_trajectory()
        )
        await service.poll_pending(timeout=0)
        # The PC persisted/sent accepted but its request never reached this
        # server. Its later durable played result must still win.
        result = await service.ack(
            event["event_id"], status="played", actual_playback_ms=1_000
        )
        assert result["status"] == "played"
        stored = await service.get_event(event["event_id"])
        assert stored["accepted_at"] == clock.value

    asyncio.run(scenario())


def test_playback_ack_uses_event_duration_and_not_found_precedes_payload_validation(
    tmp_path,
):
    async def scenario():
        clock = MutableClock()
        service, _sprites, _db = await _stack(tmp_path, clock)

        with pytest.raises(PresenceNotFound, match="presence_event_not_found"):
            await service.ack(
                "missing-event",
                status="played",
                actual_playback_ms=-1,
            )

        event = await service.enqueue_trajectory(
            conv_id="conv-long-ack",
            intent_text="stay",
            trajectory=_trajectory(duration_ms=600_000),
        )
        await service.poll_pending(timeout=0)
        with pytest.raises(
            PresenceDeliveryError,
            match="presence_actual_playback_invalid",
        ):
            await service.ack(
                event["event_id"],
                status="played",
                actual_playback_ms=602_001,
            )

        played = await service.ack(
            event["event_id"],
            status="played",
            actual_playback_ms=602_000,
        )
        assert played["status"] == "played"
        assert played["actual_playback_ms"] == 602_000

    asyncio.run(scenario())


def test_sprite_sync_is_required_and_missing_ack_revokes_it(tmp_path):
    async def scenario():
        clock = MutableClock()
        service, sprites, _db = await _stack(tmp_path, clock)
        sprite = await sprites.get_sprite("fog_seed")
        await sprites.mark_missing(sprite["sprite_hash"])
        with pytest.raises(PresenceDeliveryError, match="sprite_not_synced"):
            await service.enqueue_trajectory(
                conv_id="conv-sync", intent_text="show", trajectory=_trajectory()
            )
        synced = await sprites.mark_synced(sprite["sprite_hash"])
        assert synced["available_on_device"] is True

        event = await service.enqueue_trajectory(
            conv_id="conv-sync", intent_text="show", trajectory=_trajectory()
        )
        await service.poll_pending(timeout=0)
        rejected = await service.ack(
            event["event_id"], status="rejected", reason="sprite_missing"
        )
        assert rejected["status"] == "rejected"
        manifest = await sprites.manifest()
        fog = next(item for item in manifest if item["sprite_id"] == "fog_seed")
        assert fog["sync_status"] == "pending"
        assert fog["available_on_device"] is False

    asyncio.run(scenario())


def test_all_terminal_statuses_use_frozen_ledger_mapping(tmp_path):
    async def scenario():
        clock = MutableClock()
        ledger = RecordingLedger()
        service, _sprites, _db = await _stack(tmp_path, clock, ledger=ledger)

        replaced = await service.enqueue_trajectory(
            conv_id="conv-map", intent_text="replace me", trajectory=_trajectory()
        )
        rejected = await service.enqueue_trajectory(
            conv_id="conv-map", intent_text="reject me", trajectory=_trajectory()
        )
        await service.poll_pending(timeout=0)
        await service.ack(rejected["event_id"], status="rejected", reason="geometry_invalid")

        expired = await service.enqueue_trajectory(
            conv_id="conv-map",
            intent_text="expire me",
            trajectory=_trajectory(),
            start_ttl_sec=1,
        )
        clock.advance(2)
        await service.expire_leases()

        played = await service.enqueue_trajectory(
            conv_id="conv-map", intent_text="play me", trajectory=_trajectory()
        )
        await service.poll_pending(timeout=0)
        await service.ack(played["event_id"], status="accepted")
        await service.ack(played["event_id"], status="played")

        assert (await service.get_event(replaced["event_id"]))["status"] == "superseded"
        assert (await service.get_event(expired["event_id"]))["status"] == "expired"
        mapped = {
            call["event_type"]: call["outcome"] for call in ledger.calls
        }
        expected = {
            "presence.played": "succeeded",
            "presence.expired": "failed",
            "presence.rejected": "rejected",
            "presence.superseded": "rejected",
        }
        assert all(mapped.get(key) == value for key, value in expected.items())

    asyncio.run(scenario())
