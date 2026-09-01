import asyncio
from contextlib import asynccontextmanager
from datetime import datetime
from zoneinfo import ZoneInfo

import aiosqlite
import pytest

from app.self_wake import repository as repository_module
from app.self_wake.repository import (
    MAX_WAKE_CALLS_PER_DAY,
    SelfWakeRepository,
    SelfWakeRepositoryError,
)
from app.self_wake.schema import init_self_wake_tables
from app.self_wake.time_policy import (
    SelfWakeTimeError,
    is_quiet_at,
    parse_wake_at,
)


def _run(awaitable):
    return asyncio.run(awaitable)


def _repo(tmp_path, monkeypatch, *, conversations=("conv",)):
    path = tmp_path / "self-wake.db"

    @asynccontextmanager
    async def get_db():
        async with aiosqlite.connect(path, timeout=5.0) as db:
            yield db

    async def initialize():
        async with get_db() as db:
            await db.execute("PRAGMA journal_mode=WAL")
            await db.execute("CREATE TABLE conversations(id TEXT PRIMARY KEY)")
            for conv_id in conversations:
                await db.execute(
                    "INSERT INTO conversations(id) VALUES (?)",
                    (conv_id,),
                )
            await init_self_wake_tables(db)
            await db.commit()

    _run(initialize())
    monkeypatch.setattr(repository_module, "get_db", get_db)
    return SelfWakeRepository(), get_db


async def _schedule(
    repo,
    *,
    now,
    wake_at=None,
    conv_id="conv",
    intent="稍后回来",
    capabilities=("memory.remember",),
    source_turn_id="turn",
):
    return await repo.schedule_or_replace(
        wake_at=now + 60 if wake_at is None else wake_at,
        intent=intent,
        requested_capabilities=capabilities,
        origin="relationship",
        origin_ref=conv_id,
        source="chat",
        conv_id=conv_id,
        source_turn_id=source_turn_id,
        owner_timezone="America/Los_Angeles",
        now=now,
    )


def test_atomic_replace_returns_summary_and_canonical_capabilities(tmp_path, monkeypatch):
    repo, get_db = _repo(tmp_path, monkeypatch)

    async def run():
        first = await _schedule(
            repo,
            now=1_800_000_000,
            intent="第一件事",
            capabilities=("memory.remember", "heart.whisper", "memory.remember"),
            source_turn_id="turn-1",
        )
        second = await _schedule(
            repo,
            now=1_800_000_001,
            wake_at=1_800_000_200,
            intent="第二件事",
            source_turn_id="turn-2",
        )
        assert first["replaced"] is None
        assert first["wake"]["requested_capabilities"] == [
            "heart.whisper",
            "memory.remember",
        ]
        assert second["replaced"] == {
            "id": first["wake"]["id"],
            "wake_at": first["wake"]["wake_at"],
            "intent": "第一件事",
        }
        async with get_db() as db:
            cursor = await db.execute(
                "SELECT state, close_reason, COUNT(*) FROM self_wakes "
                "GROUP BY state, close_reason ORDER BY state"
            )
            rows = await cursor.fetchall()
        assert rows == [("invalidated", "replaced", 1), ("pending", None, 1)]

    _run(run())


def test_concurrent_schedules_leave_exactly_one_pending(tmp_path, monkeypatch):
    repo, get_db = _repo(tmp_path, monkeypatch)

    async def run():
        now = 1_800_000_000
        results = await asyncio.gather(
            _schedule(repo, now=now, intent="A", source_turn_id="turn-a"),
            _schedule(repo, now=now, intent="B", source_turn_id="turn-b"),
        )
        assert len(results) == 2
        async with get_db() as db:
            cursor = await db.execute(
                "SELECT state, COUNT(*) FROM self_wakes GROUP BY state"
            )
            states = dict(await cursor.fetchall())
        assert states == {"invalidated": 1, "pending": 1}

    _run(run())


def test_horizon_and_missing_origin_are_repository_guards(tmp_path, monkeypatch):
    repo, _get_db = _repo(tmp_path, monkeypatch)

    async def run():
        now = 1_800_000_000
        with pytest.raises(SelfWakeTimeError, match="wake_horizon_exceeded"):
            await _schedule(
                repo,
                now=now,
                wake_at=now + 30 * 24 * 60 * 60 + 1,
            )
        with pytest.raises(SelfWakeTimeError, match="wake_at_not_future"):
            await _schedule(repo, now=now, wake_at=now)
        with pytest.raises(SelfWakeRepositoryError, match="origin_not_found"):
            await _schedule(repo, now=now, conv_id="missing")

    _run(run())


def test_claim_commits_consumed_and_fourth_attempt_expires_with_feedback(tmp_path, monkeypatch):
    repo, get_db = _repo(tmp_path, monkeypatch)

    async def run():
        base = datetime(2027, 1, 8, 10, tzinfo=ZoneInfo("America/Los_Angeles")).timestamp()
        claimed_ids = []
        for index in range(MAX_WAKE_CALLS_PER_DAY):
            scheduled = await _schedule(
                repo,
                now=base + index * 10,
                wake_at=base + index * 10 + 1,
                source_turn_id=f"turn-{index}",
            )
            rows = await repo.claim_due_batch(
                now=base + index * 10 + 2,
                timezone_name="America/Los_Angeles",
            )
            assert [row["id"] for row in rows] == [scheduled["wake"]["id"]]
            claimed_ids.extend(row["id"] for row in rows)
            # This separate connection proves the consumed transition was
            # committed before a runner could call a provider.
            async with get_db() as db:
                cursor = await db.execute(
                    "SELECT state, consumed_at FROM self_wakes WHERE id=?",
                    (scheduled["wake"]["id"],),
                )
                state, consumed_at = await cursor.fetchone()
            assert state == "consumed"
            assert consumed_at is not None

        fourth = await _schedule(
            repo,
            now=base + 40,
            wake_at=base + 41,
            source_turn_id="turn-four",
        )
        assert await repo.claim_due_batch(
            now=base + 42,
            timezone_name="America/Los_Angeles",
        ) == []
        row = await repo.get(fourth["wake"]["id"])
        assert row["state"] == "expired"
        assert row["close_reason"] == "daily_quota_exhausted"
        status = await repo.load_prompt_status(
            origin="relationship",
            origin_ref="conv",
            now=base + 43,
            timezone_name="America/Los_Angeles",
        )
        assert status["quota"]["used"] == 3
        assert status["quota"]["remaining"] == 0
        assert status["recent_nonexecution"]["outcome"] == "daily_quota_exhausted"
        assert len(claimed_ids) == 3

    _run(run())


def test_quota_resets_on_owner_natural_day(tmp_path, monkeypatch):
    repo, _get_db = _repo(tmp_path, monkeypatch)

    async def run():
        timezone = ZoneInfo("America/Los_Angeles")
        day_one = datetime(2027, 2, 2, 22, 0, tzinfo=timezone).timestamp()
        for index in range(3):
            now = day_one + index * 10
            await _schedule(repo, now=now, wake_at=now + 1, source_turn_id=f"d1-{index}")
            assert len(await repo.claim_due_batch(
                now=now + 2,
                timezone_name="America/Los_Angeles",
            )) == 1
        day_two = datetime(2027, 2, 3, 0, 1, tzinfo=timezone).timestamp()
        scheduled = await _schedule(
            repo,
            now=day_two,
            wake_at=day_two + 1,
            source_turn_id="d2",
        )
        claimed = await repo.claim_due_batch(
            now=day_two + 2,
            timezone_name="America/Los_Angeles",
        )
        assert [row["id"] for row in claimed] == [scheduled["wake"]["id"]]

    _run(run())


def test_two_hour_lateness_expires_without_claim(tmp_path, monkeypatch):
    repo, _get_db = _repo(tmp_path, monkeypatch)

    async def run():
        now = 1_800_000_000
        scheduled = await _schedule(repo, now=now, wake_at=now + 10)
        assert await repo.claim_due_batch(
            now=now + 10 + 2 * 60 * 60,
            timezone_name="America/Los_Angeles",
        ) == []
        row = await repo.get(scheduled["wake"]["id"])
        assert row["state"] == "expired"
        assert row["close_reason"] == "expired"
        status = await repo.load_prompt_status(
            origin="relationship",
            origin_ref="conv",
            now=now + 10 + 2 * 60 * 60 + 1,
            timezone_name="America/Los_Angeles",
        )
        assert status["recent_nonexecution"]["outcome"] == "expired"

    _run(run())


def test_prompt_status_shows_provider_failure_and_ignores_success(tmp_path, monkeypatch):
    repo, _get_db = _repo(tmp_path, monkeypatch, conversations=("conv", "other"))

    async def run():
        now = 1_800_000_000
        failed = await _schedule(
            repo,
            now=now,
            wake_at=now + 1,
            source_turn_id="failed",
        )
        await repo.claim_due_batch(now=now + 2, timezone_name="UTC")
        await repo.finish_trigger(
            failed["wake"]["id"],
            outcome="provider_failed",
            error="provider down",
            now=now + 3,
        )
        failed_status = await repo.load_prompt_status(
            origin="relationship",
            origin_ref="conv",
            now=now + 4,
            timezone_name="UTC",
        )
        assert failed_status["recent_nonexecution"]["outcome"] == "provider_failed"

        succeeded = await _schedule(
            repo,
            now=now + 10,
            wake_at=now + 11,
            conv_id="other",
            source_turn_id="succeeded",
        )
        await repo.claim_due_batch(now=now + 12, timezone_name="UTC")
        await repo.finish_trigger(
            succeeded["wake"]["id"],
            outcome="succeeded",
            now=now + 13,
        )
        success_status = await repo.load_prompt_status(
            origin="relationship",
            origin_ref="other",
            now=now + 14,
            timezone_name="UTC",
        )
        assert success_status["recent_nonexecution"] is None

    _run(run())


def test_quiet_hours_cross_midnight_and_dst_inputs():
    timezone = "America/Los_Angeles"
    quiet = {
        "quiet_hours_enabled": True,
        "quiet_hours_start": "22:00",
        "quiet_hours_end": "08:00",
    }
    late = datetime(2026, 8, 20, 23, 0, tzinfo=ZoneInfo(timezone)).timestamp()
    noon = datetime(2026, 8, 20, 12, 0, tzinfo=ZoneInfo(timezone)).timestamp()
    assert is_quiet_at(late, timezone_name=timezone, config=quiet)
    assert not is_quiet_at(noon, timezone_name=timezone, config=quiet)

    spring_now = datetime(2026, 2, 20, tzinfo=ZoneInfo(timezone)).timestamp()
    with pytest.raises(SelfWakeTimeError, match="nonexistent_local_time"):
        parse_wake_at("2026-03-08 02:30", now=spring_now, timezone_name=timezone)
    fall_now = datetime(2026, 10, 20, tzinfo=ZoneInfo(timezone)).timestamp()
    with pytest.raises(SelfWakeTimeError, match="ambiguous_local_time_requires_offset"):
        parse_wake_at("2026-11-01 01:30", now=fall_now, timezone_name=timezone)
    first_fold = parse_wake_at(
        "2026-11-01T01:30:00-07:00",
        now=fall_now,
        timezone_name=timezone,
    )
    assert datetime.fromtimestamp(first_fold, ZoneInfo(timezone)).fold == 0


def test_parse_wake_at_accepts_naive_iso_t_minutes_and_seconds():
    timezone = "America/Los_Angeles"
    now = datetime(2026, 8, 22, 8, 0, tzinfo=ZoneInfo(timezone)).timestamp()
    expected = datetime(2026, 8, 23, 15, 0, tzinfo=ZoneInfo(timezone)).timestamp()

    assert parse_wake_at(
        "2026-08-23T15:00",
        now=now,
        timezone_name=timezone,
    ) == expected
    assert parse_wake_at(
        "2026-08-23T15:00:00",
        now=now,
        timezone_name=timezone,
    ) == expected
    assert parse_wake_at(
        "2026-08-23 15:00",
        now=now,
        timezone_name=timezone,
    ) == expected

    with pytest.raises(SelfWakeTimeError, match="nonexistent_local_time"):
        parse_wake_at(
            "2026-03-08T02:30",
            now=datetime(2026, 2, 20, tzinfo=ZoneInfo(timezone)).timestamp(),
            timezone_name=timezone,
        )
    with pytest.raises(
        SelfWakeTimeError,
        match="ambiguous_local_time_requires_offset",
    ):
        parse_wake_at(
            "2026-11-01T01:30:00",
            now=datetime(2026, 10, 20, tzinfo=ZoneInfo(timezone)).timestamp(),
            timezone_name=timezone,
        )
