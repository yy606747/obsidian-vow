import asyncio
import uuid
from contextlib import asynccontextmanager

import aiosqlite

from app.presence.db import init_presence_tables
from app.presence.summon import (
    SUMMON_EVENT_TTL_SEC,
    SummonCoordinator,
    SummonEventRepository,
)


def _repository(tmp_path, *, now):
    db_path = tmp_path / "summon.db"

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
    return SummonEventRepository(get_db_factory=get_db, now=lambda: now), get_db


def test_summon_uuid_insert_is_atomic_and_idempotent(tmp_path):
    now = 1_800_000_000.0
    repository, get_db = _repository(tmp_path, now=now)
    summon_id = str(uuid.uuid4())

    async def scenario():
        results = await asyncio.gather(*(
            repository.insert(
                summon_id=summon_id,
                conv_id="conv",
                device_id="pc",
            )
            for _ in range(8)
        ))
        assert sum(item["inserted"] for item in results) == 1
        assert {item["event"]["status"] for item in results} == {"processing"}
        async with get_db() as db:
            cursor = await db.execute(
                "SELECT COUNT(*) FROM summon_events WHERE summon_id=?",
                (summon_id,),
            )
            assert int((await cursor.fetchone())[0]) == 1

    asyncio.run(scenario())


def test_recent_summon_facts_live_24_hours_and_hide_technical_state(tmp_path):
    now = 1_800_000_000.0
    repository, _get_db = _repository(tmp_path, now=now)
    expired_id = str(uuid.uuid4())
    boundary_id = str(uuid.uuid4())
    recent_id = str(uuid.uuid4())

    async def scenario():
        await repository.insert(
            summon_id=expired_id,
            conv_id="conv",
            occurred_at=now - SUMMON_EVENT_TTL_SEC - 1,
        )
        await repository.insert(
            summon_id=boundary_id,
            conv_id="conv",
            occurred_at=now - SUMMON_EVENT_TTL_SEC,
        )
        await repository.insert(
            summon_id=recent_id,
            conv_id="conv",
            occurred_at=now - 60,
        )
        await repository.mark_status(
            recent_id,
            status="failed",
            failure_reason="provider detail that Core must not see",
        )
        facts = await repository.recent_facts(conv_id="conv", now=now)
        assert [row["summon_id"] for row in facts] == [boundary_id, recent_id]
        assert set(facts[0]) == {"summon_id", "conv_id", "occurred_at"}
        assert await repository.get(expired_id) is None
        excluded = await repository.recent_facts(
            conv_id="conv",
            now=now,
            exclude_summon_id=recent_id,
        )
        assert [row["summon_id"] for row in excluded] == [boundary_id]

    asyncio.run(scenario())


def test_restart_marks_processing_failed_without_deleting_fact(tmp_path):
    now = 1_800_000_000.0
    repository, _get_db = _repository(tmp_path, now=now)
    summon_id = str(uuid.uuid4())

    async def scenario():
        await repository.insert(summon_id=summon_id, conv_id="conv")
        assert await repository.fail_processing_after_restart(now=now + 1) == 1
        row = await repository.get(summon_id)
        assert row["status"] == "failed"
        assert row["failure_reason"] == "server_restart"
        assert [item["summon_id"] for item in await repository.recent_facts(
            conv_id="conv",
            now=now + 1,
        )] == [summon_id]

    asyncio.run(scenario())


def test_coordinator_coalesces_an_overlapping_click_without_losing_its_fact(
    tmp_path,
):
    now = 1_800_000_000.0
    repository, _get_db = _repository(tmp_path, now=now)
    primary_id = str(uuid.uuid4())
    overlap_id = str(uuid.uuid4())
    started = asyncio.Event()
    release = asyncio.Event()
    calls = []

    async def ready(**_kwargs):
        return {"ready": True, "reason": ""}

    async def run(**kwargs):
        calls.append(kwargs)
        started.set()
        await release.wait()
        return {"round_branch": "none"}

    coordinator = SummonCoordinator(
        repository=repository,
        readiness=ready,
        runner=run,
        now=lambda: now,
    )

    async def scenario():
        for summon_id in (primary_id, overlap_id):
            await repository.insert(summon_id=summon_id, conv_id="conv")
        primary = asyncio.create_task(coordinator.process(
            summon_id=primary_id,
            target={"conv_id": "conv", "model_key": "core", "last_user_ts": 1},
        ))
        await started.wait()
        overlap = await coordinator.process(
            summon_id=overlap_id,
            target={"conv_id": "conv", "model_key": "core", "last_user_ts": 1},
        )
        assert overlap == {
            "status": "coalesced",
            "summon_id": overlap_id,
            "coalesced_into": primary_id,
        }
        release.set()
        assert (await primary)["status"] == "processed"
        assert len(calls) == 1
        assert calls[0]["target"]["exclude_summon_id"] == primary_id
        row = await repository.get(overlap_id)
        assert row["status"] == "coalesced"
        assert row["coalesced_into"] == primary_id
        assert {item["summon_id"] for item in await repository.recent_facts(
            conv_id="conv",
        )} == {primary_id, overlap_id}

    asyncio.run(scenario())


def test_coordinator_gates_readiness_and_stabilizes_task_failure(tmp_path):
    now = 1_800_000_000.0
    repository, _get_db = _repository(tmp_path, now=now)
    gated_id = str(uuid.uuid4())
    failed_id = str(uuid.uuid4())

    async def gated(**_kwargs):
        return {"ready": False, "reason": "presence_agent_offline"}

    async def should_not_run(**_kwargs):
        raise AssertionError("gated summon must not run Core")

    async def ready(**_kwargs):
        return {"ready": True, "reason": ""}

    async def fail(**_kwargs):
        raise RuntimeError("provider detail")

    async def scenario():
        await repository.insert(summon_id=gated_id, conv_id="conv")
        gated_result = await SummonCoordinator(
            repository=repository,
            readiness=gated,
            runner=should_not_run,
            now=lambda: now,
        ).process(
            summon_id=gated_id,
            target={"conv_id": "conv", "model_key": "core", "last_user_ts": 1},
        )
        assert gated_result["status"] == "gated"
        assert (await repository.get(gated_id))["failure_reason"] == "presence_agent_offline"

        await repository.insert(summon_id=failed_id, conv_id="conv")
        failed_result = await SummonCoordinator(
            repository=repository,
            readiness=ready,
            runner=fail,
            now=lambda: now,
        ).process(
            summon_id=failed_id,
            target={"conv_id": "conv", "model_key": "core", "last_user_ts": 1},
        )
        assert failed_result["status"] == "failed"
        assert (await repository.get(failed_id))["failure_reason"] == "summon_failed:RuntimeError"

    asyncio.run(scenario())
