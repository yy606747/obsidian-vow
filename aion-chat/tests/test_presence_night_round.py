import asyncio
from contextlib import asynccontextmanager
from datetime import datetime
from zoneinfo import ZoneInfo

import aiosqlite
import pytest

from app.presence import night_round as night_round_module
from app.presence.db import init_presence_tables
from app.presence.night_round import (
    NightRoundRepository,
    NightRoundScheduler,
    resolve_night_window,
)


TIMEZONE = "America/Los_Angeles"


def test_scheduler_loop_logs_tick_failure_and_keeps_its_cancel_boundary(
    monkeypatch,
    caplog,
):
    scheduler = NightRoundScheduler()

    async def fail_tick():
        raise RuntimeError("tick exploded")

    async def cancel_sleep(_interval):
        raise asyncio.CancelledError

    monkeypatch.setattr(scheduler, "run_once", fail_tick)
    monkeypatch.setattr(night_round_module.asyncio, "sleep", cancel_sleep)

    with caplog.at_level("ERROR", logger="app.presence.night_round"):
        with pytest.raises(asyncio.CancelledError):
            asyncio.run(scheduler.run_loop(poll_interval_sec=1))

    assert "Night round scheduler tick failed" in caplog.text
    assert "tick exploded" in caplog.text


def _timestamp(year, month, day, hour, minute=0):
    return datetime(
        year,
        month,
        day,
        hour,
        minute,
        tzinfo=ZoneInfo(TIMEZONE),
    ).timestamp()


def _repository(tmp_path, *, now):
    db_path = tmp_path / "night.db"

    @asynccontextmanager
    async def get_db():
        async with aiosqlite.connect(db_path, timeout=5.0) as db:
            yield db

    async def initialize():
        async with get_db() as db:
            await db.execute("PRAGMA journal_mode=WAL")
            await init_presence_tables(db)
            await db.commit()

    asyncio.run(initialize())
    return NightRoundRepository(get_db_factory=get_db, now=lambda: now)


def test_night_window_supports_normal_and_cross_midnight_ranges():
    normal = resolve_night_window(
        _timestamp(2027, 1, 9, 2, 30),
        timezone_name=TIMEZONE,
        start="02:00",
        end="05:00",
    )
    assert normal is not None
    assert normal.night_key == "2027-01-09"
    assert resolve_night_window(
        _timestamp(2027, 1, 9, 5, 0),
        timezone_name=TIMEZONE,
        start="02:00",
        end="05:00",
    ) is None

    before_midnight = resolve_night_window(
        _timestamp(2027, 1, 9, 23, 30),
        timezone_name=TIMEZONE,
        start="23:00",
        end="03:00",
    )
    after_midnight = resolve_night_window(
        _timestamp(2027, 1, 10, 1, 0),
        timezone_name=TIMEZONE,
        start="23:00",
        end="03:00",
    )
    assert before_midnight is not None
    assert after_midnight is not None
    assert before_midnight.night_key == after_midnight.night_key == "2027-01-09"


def test_concurrent_night_claim_allows_exactly_one_caller(tmp_path):
    now = _timestamp(2027, 1, 9, 2, 30)
    repository = _repository(tmp_path, now=now)

    async def scenario():
        claims = await asyncio.gather(*(
            repository.claim_current(
                timezone_name=TIMEZONE,
                start="02:00",
                end="05:00",
            )
            for _ in range(8)
        ))
        claimed = [row for row in claims if row is not None]
        assert len(claimed) == 1
        assert claimed[0]["night_key"] == "2027-01-09"
        assert claimed[0]["status"] == "running"

    asyncio.run(scenario())


def test_restart_marks_running_failed_and_same_night_never_reclaims(tmp_path):
    now = _timestamp(2027, 1, 9, 2, 30)
    repository = _repository(tmp_path, now=now)

    async def scenario():
        claimed = await repository.claim_current(
            timezone_name=TIMEZONE,
            start="02:00",
            end="05:00",
        )
        assert claimed is not None
        assert await repository.fail_running_after_restart(now=now + 1) == 1
        row = await repository.get("2027-01-09")
        assert row["status"] == "failed"
        assert row["error"] == "server_restart"
        assert await repository.claim_current(
            timezone_name=TIMEZONE,
            start="02:00",
            end="05:00",
            now=now + 60,
        ) is None

    asyncio.run(scenario())


def test_scheduler_polling_and_restart_never_call_core_twice(tmp_path):
    now = _timestamp(2027, 1, 9, 2, 30)
    repository = _repository(tmp_path, now=now)
    calls = []

    async def target():
        return {"conv_id": "conv", "model_key": "core", "last_user_ts": 1.0}

    async def turn_runner(**kwargs):
        calls.append(kwargs)
        return {
            "status": "none_explicit",
            "round_kind": "night",
            "round_branch": "none",
        }

    async def synced_sprite_state():
        return {"has_non_seed": True, "has_synced_non_seed": True}

    config = {
        "night_round_enabled": True,
        "night_round_start": "02:00",
        "night_round_end": "05:00",
        "opportunity_intervals_min": [1],
    }
    scheduler = NightRoundScheduler(
        repository=repository,
        now=lambda: now,
        config_loader=lambda: config,
        timezone_resolver=lambda: TIMEZONE,
        target_resolver=target,
        turn_runner=turn_runner,
        sprite_state_resolver=synced_sprite_state,
    )

    async def scenario():
        first = await scheduler.run_once()
        second = await scheduler.run_once()
        restarted = NightRoundScheduler(
            repository=repository,
            now=lambda: now + 60,
            config_loader=lambda: config,
            timezone_resolver=lambda: TIMEZONE,
            target_resolver=target,
            turn_runner=turn_runner,
            sprite_state_resolver=synced_sprite_state,
        )
        third = await restarted.run_once()
        assert first["round_branch"] == "none"
        assert second["reason"] == third["reason"] == "already_claimed"
        assert len(calls) == 1
        assert calls[0]["kind"] == "night"
        row = await repository.get("2027-01-09")
        assert row["status"] == "completed"
        assert row["branch"] == "none"

    asyncio.run(scenario())


def test_scheduler_waits_for_first_formal_sprite_sync_before_claim(tmp_path):
    now = _timestamp(2027, 1, 9, 2, 30)
    repository = _repository(tmp_path, now=now)
    state = {"has_non_seed": True, "has_synced_non_seed": False}
    calls = []

    async def sprite_state():
        return dict(state)

    async def target():
        calls.append("target")
        return {"conv_id": "conv", "model_key": "core", "last_user_ts": 1.0}

    async def turn_runner(**_kwargs):
        calls.append("core")
        return {
            "status": "none_explicit",
            "round_kind": "night",
            "round_branch": "none",
        }

    scheduler = NightRoundScheduler(
        repository=repository,
        now=lambda: now,
        config_loader=lambda: {
            "night_round_enabled": True,
            "night_round_start": "02:00",
            "night_round_end": "05:00",
        },
        timezone_resolver=lambda: TIMEZONE,
        target_resolver=target,
        turn_runner=turn_runner,
        sprite_state_resolver=sprite_state,
    )

    async def scenario():
        pending = await scheduler.run_once()
        assert pending == {
            "status": "gated",
            "reason": "presence_sprite_pending_sync",
        }
        assert calls == []
        assert await repository.get("2027-01-09") is None

        state["has_synced_non_seed"] = True
        result = await scheduler.run_once()
        assert result["round_branch"] == "none"
        assert calls == ["target", "core"]
        assert (await repository.get("2027-01-09"))["status"] == "completed"

    asyncio.run(scenario())
