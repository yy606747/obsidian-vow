import asyncio
from contextlib import asynccontextmanager
import json
import time

import aiosqlite

import app.memory_v3.pending_recall as pending_module
import app.memory_v3.repository as repositories
from app.memory_v3.pending_recall import PendingRecallService, validate_selector_result
from app.memory_v3.repository import PendingRecallRepository
from app.memory_v3.schema import init_memory_v3_tables


async def _create_db(path) -> None:
    async with aiosqlite.connect(path) as db:
        await db.execute(
            "CREATE TABLE messages ("
            "id TEXT PRIMARY KEY, conv_id TEXT NOT NULL, role TEXT NOT NULL, "
            "content TEXT NOT NULL, created_at REAL NOT NULL)"
        )
        await db.execute(
            "CREATE TABLE memory_chunks ("
            "id TEXT PRIMARY KEY, conv_id TEXT NOT NULL, "
            "message_ids_json TEXT NOT NULL DEFAULT '[]', content TEXT NOT NULL, "
            "created_at REAL NOT NULL, updated_at REAL NOT NULL, embedding BLOB, "
            "keywords_json TEXT NOT NULL DEFAULT '[]', metadata_json TEXT NOT NULL DEFAULT '{}')"
        )
        await db.execute(
            "CREATE TABLE memory_items ("
            "id TEXT PRIMARY KEY, legacy_memory_id TEXT, metadata_json TEXT NOT NULL DEFAULT '{}')"
        )
        await init_memory_v3_tables(db)
        await db.commit()


def _get_db_factory(path):
    @asynccontextmanager
    async def factory():
        async with aiosqlite.connect(path) as db:
            yield db

    return factory


def test_selector_contract_rejects_unknown_or_unselected_raw_detail():
    accepted = validate_selector_result(
        {
            "decision": "select",
            "selected_candidate_ids": ["chunk-1"],
            "needs_raw_detail_ids": ["chunk-1"],
            "reason_code": "matched",
        },
        candidate_ids={"chunk-1", "chunk-2"},
        select_max=2,
    )
    assert accepted["selected_candidate_ids"] == ["chunk-1"]

    for payload in (
        {
            "decision": "select",
            "selected_candidate_ids": ["outside"],
            "needs_raw_detail_ids": [],
            "reason_code": "matched",
        },
        {
            "decision": "select",
            "selected_candidate_ids": ["chunk-1"],
            "needs_raw_detail_ids": ["chunk-2"],
            "reason_code": "matched",
        },
    ):
        try:
            validate_selector_result(
                payload,
                candidate_ids={"chunk-1", "chunk-2"},
                select_max=2,
            )
        except ValueError:
            pass
        else:
            raise AssertionError("invalid selector output was accepted")


def test_pending_retrieval_selection_consumption_and_regenerate_replay(
    tmp_path, monkeypatch
):
    db_path = tmp_path / "pending.db"
    asyncio.run(_create_db(db_path))
    factory = _get_db_factory(db_path)
    monkeypatch.setattr(repositories, "get_db", factory)
    repository = PendingRecallRepository()
    candidate = {
        "candidate_id": "chunk-old",
        "source_chunk_id": "chunk-old",
        "source_message_ids": ["old-user"],
        "source_start_ts": 1.0,
        "source_end_ts": 2.0,
        "raw_content": "user: 那次在南京，她说其实很怕赶不上车。",
        "readout_text": "南京那次赶车让她很紧张。",
        "readout_type": "relational_card",
        "card_id": "card-1",
        "card_version": 1,
        "score": 0.71,
        "semantic_similarity": 0.8,
        "keyword_relevance": 0.1,
        "cooldown_penalty": 0.0,
        "rank": 1,
    }

    async def fake_wide(*_args, **_kwargs):
        return [candidate]

    selector_calls = 0

    async def fake_selector(*_args, **_kwargs):
        nonlocal selector_calls
        selector_calls += 1
        return {
            "decision": "select",
            "selected_candidate_ids": ["chunk-old"],
            "needs_raw_detail_ids": [],
            "reason_code": "matched",
        }

    monkeypatch.setattr(pending_module, "wide_chunk_recall", fake_wide)
    monkeypatch.setattr(pending_module, "_call_flash_lite", fake_selector)

    async def scenario():
        now = time.time()
        async with aiosqlite.connect(db_path) as db:
            await db.execute("BEGIN IMMEDIATE")
            pending_id = await repository.create_queued_in_tx(
                db,
                conv_id="conv",
                origin_assistant_message_id="assistant-origin",
                intent_text="找南京赶车那次",
                retrieval_deadline_at=now + 20,
                config={
                    "pending_recall_enabled": True,
                    "pending_join_max_wait_sec": 1,
                    "pending_selector_timeout_sec": 1,
                    "pending_selector_attempts": 1,
                    "pending_candidate_k": 20,
                    "pending_candidate_pool_limit": 1000,
                },
                created_at=now,
            )
            await db.commit()

        service = PendingRecallService(repository)
        selected = await service.prepare_for_user(
            conv_id="conv",
            user_message_id="user-target",
            current_user_message="你还记得南京那次吗",
            recent_messages=[],
            config_snapshot={
                "pending_recall_enabled": True,
                "pending_join_max_wait_sec": 1,
                "pending_selector_timeout_sec": 1,
                "pending_selector_attempts": 1,
                "pending_candidate_k": 20,
                "pending_candidate_pool_limit": 1000,
            },
        )
        async with aiosqlite.connect(db_path) as db:
            await db.execute("BEGIN IMMEDIATE")
            consumed = await repository.consume_selected_in_tx(
                db,
                pending_id=pending_id,
                target_user_message_id="user-target",
                assistant_message_id="assistant-answer",
                now=time.time(),
            )
            await db.commit()
        replay = await service.replay_for_assistant("assistant-answer")
        row = await repository.get(pending_id)
        return pending_id, selected, consumed, replay, row

    pending_id, selected, consumed, replay, row = asyncio.run(scenario())

    assert selected["status"] == "selected"
    assert selected["pending_id"] == pending_id
    assert selected["items"][0]["lane"] == "pending"
    assert selected["items"][0]["candidate_id"] == "chunk-old"
    assert selector_calls == 1
    assert consumed is True
    assert row["status"] == "consumed"
    assert json.loads(row["candidate_json"]) == []
    assert json.loads(row["selected_json"])["items"] == selected["items"]
    assert replay["status"] == "replay"
    assert replay["items"] == selected["items"]


def test_late_retrieval_cannot_revive_superseded_pending(tmp_path, monkeypatch):
    db_path = tmp_path / "late.db"
    asyncio.run(_create_db(db_path))
    factory = _get_db_factory(db_path)
    monkeypatch.setattr(repositories, "get_db", factory)
    repository = PendingRecallRepository()

    async def scenario():
        now = time.time()
        async with aiosqlite.connect(db_path) as db:
            await db.execute("BEGIN IMMEDIATE")
            old_id = await repository.create_queued_in_tx(
                db,
                conv_id="conv",
                origin_assistant_message_id="a1",
                intent_text="旧意图",
                retrieval_deadline_at=now + 20,
                config={},
                created_at=now,
            )
            await db.commit()
        await repository.mark_ready(
            old_id,
            candidates=[{"candidate_id": "old", "raw_content": "旧候选" * 1000}],
            now=now + 0.5,
        )
        async with aiosqlite.connect(db_path) as db:
            await db.execute("BEGIN IMMEDIATE")
            await repository.create_queued_in_tx(
                db,
                conv_id="conv",
                origin_assistant_message_id="a2",
                intent_text="新意图",
                retrieval_deadline_at=now + 21,
                config={},
                created_at=now + 1,
            )
            await db.commit()
        late_write = await repository.mark_ready(old_id, candidates=[], now=now + 2)
        old = await repository.get(old_id)
        active = await repository.active_for_conversation("conv")
        return late_write, old, active

    late_write, old, active = asyncio.run(scenario())

    assert late_write is False
    assert old["status"] == "superseded"
    assert json.loads(old["candidate_json"]) == []
    assert active["origin_assistant_message_id"] == "a2"


def test_terminal_pending_transitions_drop_bulk_snapshots(tmp_path, monkeypatch):
    db_path = tmp_path / "compact-terminal.db"
    asyncio.run(_create_db(db_path))
    factory = _get_db_factory(db_path)
    monkeypatch.setattr(repositories, "get_db", factory)
    repository = PendingRecallRepository()

    async def create(origin: str, created_at: float) -> str:
        async with aiosqlite.connect(db_path) as db:
            await db.execute("BEGIN IMMEDIATE")
            pending_id = await repository.create_queued_in_tx(
                db,
                conv_id="conv",
                origin_assistant_message_id=origin,
                intent_text="找旧事",
                retrieval_deadline_at=created_at + 20,
                config={},
                created_at=created_at,
            )
            await db.commit()
        return pending_id

    async def scenario():
        failed_id = await create("assistant-failed", 1.0)
        await repository.mark_ready(
            failed_id,
            candidates=[{"candidate_id": "chunk", "raw_content": "大候选" * 1000}],
            now=2.0,
        )
        await repository.mark_failed(failed_id, reason="selector_failed", now=3.0)
        failed = await repository.get(failed_id)

        cancelled_id = await create("assistant-cancelled", 4.0)
        await repository.mark_ready(
            cancelled_id,
            candidates=[{"candidate_id": "chunk", "raw_content": "大候选" * 1000}],
            now=5.0,
        )
        await repository.bind_target(
            cancelled_id,
            user_message_id="user-target",
            now=6.0,
        )
        await repository.save_selection(
            cancelled_id,
            user_message_id="user-target",
            selection={"decision": "none", "items": []},
            now=7.0,
        )
        async with aiosqlite.connect(db_path) as db:
            await db.execute("BEGIN IMMEDIATE")
            await repository.cancel_for_origin_in_tx(
                db,
                origin_assistant_message_id="assistant-cancelled",
                now=8.0,
            )
            await db.commit()
        cancelled = await repository.get(cancelled_id)
        return failed, cancelled

    failed, cancelled = asyncio.run(scenario())

    assert failed["status"] == "failed"
    assert json.loads(failed["candidate_json"]) == []
    assert json.loads(failed["selected_json"]) == []
    assert cancelled["status"] == "cancelled"
    assert json.loads(cancelled["candidate_json"]) == []
    assert json.loads(cancelled["selected_json"]) == []


def test_join_timeout_keeps_pending_and_next_turn_can_select(tmp_path, monkeypatch):
    db_path = tmp_path / "deferred.db"
    asyncio.run(_create_db(db_path))
    factory = _get_db_factory(db_path)
    monkeypatch.setattr(repositories, "get_db", factory)
    repository = PendingRecallRepository()
    release_retrieval = asyncio.Event()
    candidate = {
        "candidate_id": "chunk-deferred",
        "source_chunk_id": "chunk-deferred",
        "source_message_ids": ["old-user"],
        "raw_content": "user: 那次她在南京差点误车。",
        "readout_text": "南京那次差点误车。",
        "readout_type": "raw_excerpt",
        "score": 0.8,
        "rank": 1,
    }

    async def slow_wide(*_args, **_kwargs):
        await release_retrieval.wait()
        return [candidate]

    async def choose(*_args, **_kwargs):
        return {
            "decision": "select",
            "selected_candidate_ids": ["chunk-deferred"],
            "needs_raw_detail_ids": [],
            "reason_code": "matched",
        }

    monkeypatch.setattr(pending_module, "wide_chunk_recall", slow_wide)
    monkeypatch.setattr(pending_module, "_call_flash_lite", choose)

    async def scenario():
        now = time.time()
        config = {
            "pending_recall_enabled": True,
            "pending_join_max_wait_sec": 0,
            "pending_retrieval_timeout_sec": 20,
            "pending_selector_timeout_sec": 1,
            "pending_selector_attempts": 1,
            "pending_candidate_k": 20,
            "pending_candidate_pool_limit": 1000,
        }
        async with aiosqlite.connect(db_path) as db:
            await db.execute("BEGIN IMMEDIATE")
            pending_id = await repository.create_queued_in_tx(
                db,
                conv_id="conv",
                origin_assistant_message_id="assistant-origin",
                intent_text="找南京误车那次",
                retrieval_deadline_at=now + 20,
                config=config,
                created_at=now,
            )
            await db.commit()

        service = PendingRecallService(repository)
        first = await service.prepare_for_user(
            conv_id="conv",
            user_message_id="user-first",
            current_user_message="先说另一件事",
            recent_messages=[],
            config_snapshot=config,
        )
        queued = await repository.get(pending_id)
        release_retrieval.set()
        await (await service._task_for(pending_id))
        ready = await repository.get(pending_id)
        second = await service.prepare_for_user(
            conv_id="conv",
            user_message_id="user-second",
            current_user_message="你记得南京误车那次吗",
            recent_messages=[],
            config_snapshot=config,
        )
        return first, queued, ready, second

    first, queued, ready, second = asyncio.run(scenario())

    assert first["status"] == "deferred"
    assert queued["status"] == "queued"
    assert queued["deferred_count"] == 1
    assert queued["target_user_message_id"] is None
    assert ready["status"] == "ready"
    assert second["status"] == "selected"
    assert second["items"][0]["candidate_id"] == "chunk-deferred"


def test_selector_none_persists_empty_selection_without_injection(tmp_path, monkeypatch):
    db_path = tmp_path / "selector-none.db"
    asyncio.run(_create_db(db_path))
    factory = _get_db_factory(db_path)
    monkeypatch.setattr(repositories, "get_db", factory)
    repository = PendingRecallRepository()

    async def fake_wide(*_args, **_kwargs):
        return [{
            "candidate_id": "chunk-1",
            "source_chunk_id": "chunk-1",
            "raw_content": "旧事",
            "readout_text": "旧事",
            "readout_type": "raw_excerpt",
            "rank": 1,
        }]

    async def reject(*_args, **_kwargs):
        return {
            "decision": "none",
            "selected_candidate_ids": ["chunk-1"],
            "needs_raw_detail_ids": ["chunk-1"],
            "reason_code": "topic_changed",
        }

    monkeypatch.setattr(pending_module, "wide_chunk_recall", fake_wide)
    monkeypatch.setattr(pending_module, "_call_flash_lite", reject)

    async def scenario():
        now = time.time()
        config = {
            "pending_recall_enabled": True,
            "pending_join_max_wait_sec": 1,
            "pending_retrieval_timeout_sec": 20,
            "pending_selector_timeout_sec": 1,
            "pending_selector_attempts": 1,
        }
        async with aiosqlite.connect(db_path) as db:
            await db.execute("BEGIN IMMEDIATE")
            pending_id = await repository.create_queued_in_tx(
                db,
                conv_id="conv",
                origin_assistant_message_id="assistant-origin",
                intent_text="可能要找旧事",
                retrieval_deadline_at=now + 20,
                config=config,
                created_at=now,
            )
            await db.commit()
        service = PendingRecallService(repository)
        result = await service.prepare_for_user(
            conv_id="conv",
            user_message_id="user-target",
            current_user_message="已经换话题了",
            recent_messages=[],
            config_snapshot=config,
        )
        return result, await repository.get(pending_id)

    result, row = asyncio.run(scenario())

    assert result["status"] == "selected"
    assert result["items"] == []
    assert result["selection"]["decision"] == "none"
    assert row["status"] == "selected"
    assert json.loads(row["selected_json"])["items"] == []


def test_retrieval_failure_falls_back_without_binding_turn(tmp_path, monkeypatch):
    db_path = tmp_path / "retrieval-failed.db"
    asyncio.run(_create_db(db_path))
    factory = _get_db_factory(db_path)
    monkeypatch.setattr(repositories, "get_db", factory)
    repository = PendingRecallRepository()

    async def broken_wide(*_args, **_kwargs):
        raise RuntimeError("embedding unavailable")

    monkeypatch.setattr(pending_module, "wide_chunk_recall", broken_wide)

    async def scenario():
        now = time.time()
        config = {
            "pending_recall_enabled": True,
            "pending_join_max_wait_sec": 1,
            "pending_retrieval_timeout_sec": 20,
        }
        async with aiosqlite.connect(db_path) as db:
            await db.execute("BEGIN IMMEDIATE")
            pending_id = await repository.create_queued_in_tx(
                db,
                conv_id="conv",
                origin_assistant_message_id="assistant-origin",
                intent_text="找旧事",
                retrieval_deadline_at=now + 20,
                config=config,
                created_at=now,
            )
            await db.commit()
        service = PendingRecallService(repository)
        result = await service.prepare_for_user(
            conv_id="conv",
            user_message_id="user-target",
            current_user_message="继续聊天",
            recent_messages=[],
            config_snapshot=config,
        )
        return result, await repository.get(pending_id)

    result, row = asyncio.run(scenario())

    assert result["status"] == "failed"
    assert result["items"] == []
    assert row["status"] == "failed"
    assert row["target_user_message_id"] is None
    assert row["failure_reason"] == "retrieval_error:RuntimeError"


def test_assistant_pending_transition_is_atomic_and_rollback_safe(tmp_path, monkeypatch):
    db_path = tmp_path / "atomic.db"
    asyncio.run(_create_db(db_path))
    factory = _get_db_factory(db_path)
    monkeypatch.setattr(repositories, "get_db", factory)
    repository = PendingRecallRepository()

    async def scenario():
        now = time.time()
        config = {"pending_recall_enabled": True, "pending_retrieval_timeout_sec": 20}
        async with aiosqlite.connect(db_path) as db:
            await db.execute("BEGIN IMMEDIATE")
            old_id = await repository.create_queued_in_tx(
                db,
                conv_id="conv",
                origin_assistant_message_id="assistant-origin",
                intent_text="旧意图",
                retrieval_deadline_at=now + 20,
                config=config,
                created_at=now,
            )
            await db.commit()
        await repository.mark_ready(old_id, candidates=[], now=now + 0.1)
        await repository.bind_target(old_id, user_message_id="user-target", now=now + 0.2)
        await repository.save_selection(
            old_id,
            user_message_id="user-target",
            selection={"decision": "none", "items": []},
            now=now + 0.3,
        )

        service = PendingRecallService(repository)
        async with aiosqlite.connect(db_path) as db:
            await db.execute("BEGIN IMMEDIATE")
            await db.execute(
                "INSERT INTO messages (id, conv_id, role, content, created_at) VALUES (?,?,?,?,?)",
                ("assistant-rollback", "conv", "assistant", "回复", now + 1),
            )
            rolled = await service.apply_after_assistant_in_tx(
                db,
                conv_id="conv",
                assistant_message_id="assistant-rollback",
                created_at=now + 1,
                current_user_message_id="user-target",
                selected_pending_id=old_id,
                recall_intent="新意图",
                allow_new_intent=True,
                config_snapshot=config,
            )
            assert rolled["consumed"] is True
            assert rolled["created_pending_id"]
            await db.rollback()

        after_rollback = await repository.get(old_id)
        async with aiosqlite.connect(db_path) as db:
            cur = await db.execute(
                "SELECT COUNT(*) FROM messages WHERE id='assistant-rollback'"
            )
            rollback_message_count = int((await cur.fetchone())[0])
            await db.execute("BEGIN IMMEDIATE")
            await db.execute(
                "INSERT INTO messages (id, conv_id, role, content, created_at) VALUES (?,?,?,?,?)",
                ("assistant-commit", "conv", "assistant", "回复", now + 2),
            )
            committed = await service.apply_after_assistant_in_tx(
                db,
                conv_id="conv",
                assistant_message_id="assistant-commit",
                created_at=now + 2,
                current_user_message_id="user-target",
                selected_pending_id=old_id,
                recall_intent="新意图",
                allow_new_intent=True,
                config_snapshot=config,
            )
            await db.commit()
        old = await repository.get(old_id)
        active = await repository.active_for_conversation("conv")
        return after_rollback, rollback_message_count, committed, old, active

    after_rollback, rollback_message_count, committed, old, active = asyncio.run(
        scenario()
    )

    assert after_rollback["status"] == "selected"
    assert rollback_message_count == 0
    assert committed["consumed"] is True
    assert committed["created_pending_id"] == active["id"]
    assert old["status"] == "consumed"
    assert old["consumed_by_assistant_message_id"] == "assistant-commit"
    assert active["status"] == "queued"
    assert active["intent_text"] == "新意图"
