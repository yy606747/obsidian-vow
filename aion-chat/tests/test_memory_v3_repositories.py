import asyncio
from contextlib import asynccontextmanager
import json

import aiosqlite
import pytest

import app.memory_v3.repository as repositories
from app.memory_v2.v2_repository import MemoryRepository
from app.memory_v3.provenance import source_hash_for_messages
from app.memory_v3.relational_cards import (
    RelationalCardContractError,
    validate_relational_card,
)
from app.memory_v3.schema import init_memory_v3_tables


async def _create_db(path) -> None:
    async with aiosqlite.connect(path) as db:
        await db.execute("""
            CREATE TABLE messages (
                id TEXT PRIMARY KEY,
                conv_id TEXT NOT NULL,
                role TEXT NOT NULL,
                content TEXT NOT NULL,
                created_at REAL NOT NULL
            )
        """)
        await db.execute("""
            CREATE TABLE memory_chunks (
                id TEXT PRIMARY KEY,
                conv_id TEXT NOT NULL,
                message_ids_json TEXT NOT NULL DEFAULT '[]',
                content TEXT NOT NULL,
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL,
                embedding BLOB,
                keywords_json TEXT NOT NULL DEFAULT '[]',
                metadata_json TEXT NOT NULL DEFAULT '{}'
            )
        """)
        await db.execute("""
            CREATE TABLE memory_items (
                id TEXT PRIMARY KEY,
                legacy_memory_id TEXT,
                metadata_json TEXT NOT NULL DEFAULT '{}'
            )
        """)
        await init_memory_v3_tables(db)
        await db.commit()


def _get_db_factory(path):
    @asynccontextmanager
    async def factory():
        async with aiosqlite.connect(path) as db:
            yield db

    return factory


def _messages() -> list[dict]:
    return [
        {
            "id": "m1",
            "conv_id": "conv",
            "role": "user",
            "content": "我其实很怕重要的事被随口略过。",
            "created_at": 1.0,
        },
        {
            "id": "m2",
            "conv_id": "conv",
            "role": "assistant",
            "content": "我会先停下来听你说完，也不会把你的边界当成气话。",
            "created_at": 2.0,
        },
    ]


def test_relational_card_contract_allows_assistant_sources_but_blocks_voice_copy():
    messages = _messages()
    accepted = validate_relational_card(
        {
            "decision": "create",
            "kind": "relational_reading",
            "note": "这次互动显出，对边界先听清再回应对双方都很重要。",
            "source_message_ids": ["m1", "m2"],
            "quotes": [
                {"source_message_id": "m1", "quote": "很怕重要的事被随口略过"},
                {"source_message_id": "m2", "quote": "不会把你的边界当成气话"},
            ],
        },
        messages,
    )

    assert accepted["decision"] == "create"
    assert accepted["source_message_ids"] == ["m1", "m2"]
    assert accepted["source_hash"] == source_hash_for_messages(messages)
    assert accepted["longitudinal_marker_warning"] == []

    with pytest.raises(RelationalCardContractError) as exc:
        validate_relational_card(
            {
                "decision": "create",
                "kind": "relational_reading",
                "note": "我会先停下来听你说完，也不会把你的边界当成气话。",
                "source_message_ids": ["m2"],
                "quotes": [{"source_message_id": "m2", "quote": "先停下来听你说完"}],
            },
            messages,
        )
    assert exc.value.code == "assistant_verbatim_copy"


def test_relational_card_contract_blocks_copy_from_undeclared_assistant_message():
    messages = _messages()
    with pytest.raises(RelationalCardContractError) as exc:
        validate_relational_card(
            {
                "decision": "create",
                "kind": "relational_reading",
                "note": "我会先停下来听你说完，也不会把你的边界当成气话。",
                "source_message_ids": ["m1"],
                "quotes": [
                    {"source_message_id": "m1", "quote": "很怕重要的事被随口略过"}
                ],
            },
            messages,
        )

    assert exc.value.code == "assistant_verbatim_copy"


def test_relational_card_contract_rejects_untraceable_quote_and_source():
    messages = _messages()
    with pytest.raises(RelationalCardContractError) as exc:
        validate_relational_card(
            {
                "decision": "create",
                "kind": "relational_reading",
                "note": "一段理解",
                "source_message_ids": ["outside"],
                "quotes": [{"source_message_id": "outside", "quote": "不存在"}],
            },
            messages,
        )
    assert exc.value.code == "source_outside_chunk"

    with pytest.raises(RelationalCardContractError) as exc:
        validate_relational_card(
            {
                "decision": "create",
                "kind": "relational_reading",
                "note": "一段理解",
                "source_message_ids": ["m1"],
                "quotes": [{"source_message_id": "m1", "quote": "原文里没有"}],
            },
            messages,
        )
    assert exc.value.code == "quote_not_in_message"

    with pytest.raises(RelationalCardContractError) as exc:
        validate_relational_card(
            {
                "decision": "create",
                "kind": "relational_reading",
                "note": "第一段\n第二段",
                "source_message_ids": ["m1"],
                "quotes": [{"source_message_id": "m1", "quote": "重要的事"}],
            },
            messages,
        )
    assert exc.value.code == "note_not_single_paragraph"


def test_card_repository_versions_subset_sources_and_rejects_source_drift(
    tmp_path, monkeypatch
):
    db_path = tmp_path / "cards.db"
    asyncio.run(_create_db(db_path))
    monkeypatch.setattr(repositories, "get_db", _get_db_factory(db_path))
    messages = _messages()

    async def scenario():
        async with aiosqlite.connect(db_path) as db:
            await db.executemany(
                "INSERT INTO messages VALUES (?,?,?,?,?)",
                [
                    (m["id"], m["conv_id"], m["role"], m["content"], m["created_at"])
                    for m in messages
                ],
            )
            await db.execute(
                "INSERT INTO memory_chunks "
                "(id, conv_id, message_ids_json, content, source_hash, status, "
                "created_at, updated_at) VALUES (?,?,?,?,?,'active',?,?)",
                (
                    "chunk",
                    "conv",
                    json.dumps(["m1", "m2"]),
                    "raw",
                    source_hash_for_messages(messages),
                    1.0,
                    2.0,
                ),
            )
            await db.commit()

        repository = repositories.RelationalCardRepository()
        first_hash = source_hash_for_messages([messages[0]])
        first = await repository.append_active(
            source_chunk_id="chunk",
            content="第一版",
            source_message_ids=["m1"],
            evidence=[{"source_message_id": "m1", "quote": "很怕重要的事"}],
            source_hash=first_hash,
            prompt_version="v1",
        )
        second_hash = source_hash_for_messages([messages[1]])
        second = await repository.append_active(
            source_chunk_id="chunk",
            content="第二版",
            source_message_ids=["m2"],
            evidence=[{"source_message_id": "m2", "quote": "先停下来"}],
            source_hash=second_hash,
            prompt_version="v2",
        )
        active = await repository.active_for_chunk("chunk")

        async with aiosqlite.connect(db_path) as db:
            await db.execute("UPDATE messages SET content='已经修改' WHERE id='m2'")
            await db.commit()
        drift_rejected = False
        try:
            await repository.append_active(
                source_chunk_id="chunk",
                content="过期版本",
                source_message_ids=["m2"],
                evidence=[],
                source_hash=second_hash,
                prompt_version="v3",
            )
        except ValueError:
            drift_rejected = True

        async with aiosqlite.connect(db_path) as db:
            db.row_factory = aiosqlite.Row
            rows = await (
                await db.execute(
                    "SELECT id, version, status, supersedes_card_id "
                    "FROM memory_relational_cards ORDER BY version"
                )
            ).fetchall()
        return first, second, active, drift_rejected, [dict(row) for row in rows]

    first, second, active, drift_rejected, rows = asyncio.run(scenario())

    assert first["version"] == 1
    assert second["version"] == 2
    assert second["supersedes_card_id"] == first["id"]
    assert active["id"] == second["id"]
    assert drift_rejected is True
    assert [row["status"] for row in rows] == ["superseded", "active"]


def test_card_repository_owner_invalidation_is_audited_and_not_regenerated(
    tmp_path, monkeypatch
):
    db_path = tmp_path / "card-invalidation.db"
    asyncio.run(_create_db(db_path))
    monkeypatch.setattr(repositories, "get_db", _get_db_factory(db_path))
    messages = _messages()

    async def scenario():
        async with aiosqlite.connect(db_path) as db:
            await db.executemany(
                "INSERT INTO messages VALUES (?,?,?,?,?)",
                [
                    (m["id"], m["conv_id"], m["role"], m["content"], m["created_at"])
                    for m in messages
                ],
            )
            await db.execute(
                "INSERT INTO memory_chunks "
                "(id, conv_id, message_ids_json, content, source_hash, status, "
                "created_at, updated_at) VALUES (?,?,?,?,?,'active',?,?)",
                (
                    "chunk",
                    "conv",
                    json.dumps(["m1", "m2"]),
                    "raw",
                    "chunk-hash",
                    1.0,
                    2.0,
                ),
            )
            await db.commit()

        repository = repositories.RelationalCardRepository()
        card = await repository.append_active(
            source_chunk_id="chunk",
            content="一条后来被 owner 判定为误读的卡",
            source_message_ids=["m1"],
            evidence=[{"source_message_id": "m1", "quote": "重要的事"}],
            source_hash=source_hash_for_messages([messages[0]]),
            prompt_version="relational-card-v5.1",
            metadata={"kind": "relational_reading"},
        )
        invalidated = await repository.invalidate_active(
            card["id"],
            reason="把一次抱怨误读成长期要求",
        )
        repeated = await repository.invalidate_active(
            card["id"],
            reason="重复请求不应改写审计记录",
        )
        active = await repository.active_for_chunk("chunk")

        async with aiosqlite.connect(db_path) as db:
            db.row_factory = aiosqlite.Row
            card_row = await (
                await db.execute(
                    "SELECT status, metadata_json FROM memory_relational_cards WHERE id=?",
                    (card["id"],),
                )
            ).fetchone()
            chunk_row = await (
                await db.execute(
                    "SELECT source_hash, card_generation_hash, card_generation_status, "
                    "card_generation_reason, card_generation_prompt_version "
                    "FROM memory_chunks WHERE id='chunk'"
                )
            ).fetchone()
        return invalidated, repeated, active, dict(card_row), dict(chunk_row)

    invalidated, repeated, active, card_row, chunk_row = asyncio.run(scenario())

    metadata = json.loads(card_row["metadata_json"])
    assert invalidated["invalidated"] is True
    assert repeated["invalidated"] is False
    assert repeated["already_invalid"] is True
    assert active is None
    assert card_row["status"] == "invalid"
    assert metadata["kind"] == "relational_reading"
    assert metadata["invalidation"]["actor"] == "owner_api"
    assert metadata["invalidation"]["reason"] == "把一次抱怨误读成长期要求"
    assert chunk_row["card_generation_hash"] == chunk_row["source_hash"]
    assert chunk_row["card_generation_status"] == "invalid"
    assert chunk_row["card_generation_prompt_version"] == "relational-card-v5.1"
    assert chunk_row["card_generation_reason"].startswith("owner_invalidated:")


def test_card_repository_invalidation_rejects_missing_and_superseded_cards(
    tmp_path, monkeypatch
):
    db_path = tmp_path / "card-invalidation-errors.db"
    asyncio.run(_create_db(db_path))
    monkeypatch.setattr(repositories, "get_db", _get_db_factory(db_path))
    messages = _messages()

    async def scenario():
        async with aiosqlite.connect(db_path) as db:
            await db.executemany(
                "INSERT INTO messages VALUES (?,?,?,?,?)",
                [
                    (m["id"], m["conv_id"], m["role"], m["content"], m["created_at"])
                    for m in messages
                ],
            )
            await db.execute(
                "INSERT INTO memory_chunks "
                "(id, conv_id, message_ids_json, content, source_hash, status, "
                "created_at, updated_at) VALUES (?,?,?,?,?,'active',?,?)",
                (
                    "chunk",
                    "conv",
                    json.dumps(["m1", "m2"]),
                    "raw",
                    "chunk-hash",
                    1.0,
                    2.0,
                ),
            )
            await db.commit()

        repository = repositories.RelationalCardRepository()
        first = await repository.append_active(
            source_chunk_id="chunk",
            content="第一版",
            source_message_ids=["m1"],
            evidence=[{"source_message_id": "m1", "quote": "重要的事"}],
            source_hash=source_hash_for_messages([messages[0]]),
            prompt_version="v1",
        )
        await repository.append_active(
            source_chunk_id="chunk",
            content="第二版",
            source_message_ids=["m2"],
            evidence=[{"source_message_id": "m2", "quote": "先停下来"}],
            source_hash=source_hash_for_messages([messages[1]]),
            prompt_version="v2",
        )

        with pytest.raises(KeyError):
            await repository.invalidate_active("missing", reason="不存在")
        with pytest.raises(ValueError, match="status=superseded"):
            await repository.invalidate_active(first["id"], reason="旧版本")

    asyncio.run(scenario())


def test_pending_timeline_and_injection_repositories_are_append_only(
    tmp_path, monkeypatch
):
    db_path = tmp_path / "other-repositories.db"
    asyncio.run(_create_db(db_path))
    monkeypatch.setattr(repositories, "get_db", _get_db_factory(db_path))

    async def scenario():
        async with aiosqlite.connect(db_path) as db:
            first_pending = await repositories.PendingRecallRepository.create_queued_in_tx(
                db,
                conv_id="conv",
                origin_assistant_message_id="a1",
                intent_text="找第一件事",
                retrieval_deadline_at=21.0,
                config={"candidate_k": 20},
                created_at=1.0,
            )
            second_pending = await repositories.PendingRecallRepository.create_queued_in_tx(
                db,
                conv_id="conv",
                origin_assistant_message_id="a2",
                intent_text="找第二件事",
                retrieval_deadline_at=22.0,
                config={"candidate_k": 20},
                created_at=2.0,
            )
            await db.commit()

        pending = await repositories.PendingRecallRepository().active_for_conversation("conv")
        timeline_repository = repositories.TimelineRepository()
        timeline1 = await timeline_repository.append_active(
            window_start_ts=1,
            window_end_ts=2,
            entries=[{"text": "第一段", "source_message_ids": ["m1"]}],
            source_message_ids=["m1"],
            source_hash="h1",
            prompt_version="v1",
        )
        timeline2 = await timeline_repository.append_active(
            window_start_ts=2,
            window_end_ts=3,
            entries=[{"text": "第二段", "source_message_ids": ["m2"]}],
            source_message_ids=["m2"],
            source_hash="h2",
            prompt_version="v1",
        )
        active_timeline = await timeline_repository.active()
        event_id = await repositories.InjectionEventRepository().record(
            {
                "conv_id": "conv",
                "route": "ordinary",
                "candidate_id": "chunk",
                "source_chunk_id": "chunk",
                "outcome": "injected",
                "metadata": {"card_readout": False},
            }
        )

        async with aiosqlite.connect(db_path) as db:
            pending_rows = await (
                await db.execute(
                    "SELECT id, status FROM memory_pending_recalls ORDER BY created_at"
                )
            ).fetchall()
            timeline_rows = await (
                await db.execute(
                    "SELECT id, status FROM memory_timeline_versions ORDER BY version"
                )
            ).fetchall()
            event = await (
                await db.execute(
                    "SELECT id, metadata_json FROM memory_injection_events WHERE id=?",
                    (event_id,),
                )
            ).fetchone()
        return (
            first_pending,
            second_pending,
            pending,
            timeline1,
            timeline2,
            active_timeline,
            pending_rows,
            timeline_rows,
            event,
        )

    result = asyncio.run(scenario())
    first_pending, second_pending, pending = result[:3]
    timeline1, timeline2, active_timeline = result[3:6]
    pending_rows, timeline_rows, event = result[6:]

    assert pending["id"] == second_pending
    assert pending_rows == [(first_pending, "superseded"), (second_pending, "queued")]
    assert timeline1["version"] == 1
    assert timeline2["version"] == 2
    assert active_timeline["id"] == timeline2["id"]
    assert timeline_rows == [(timeline1["id"], "superseded"), (timeline2["id"], "active")]
    assert event[0].startswith("memory_injection_")
    assert json.loads(event[1]) == {"card_readout": False}


def test_origin_type_inference_is_explicit_and_backward_compatible():
    assert MemoryRepository._origin_type({"origin_type": "ai_note"}) == "ai_note"
    assert (
        MemoryRepository._origin_type({"metadata_json": '{"source":"digest.multi_note"}'})
        == "auto_digest"
    )
    assert (
        MemoryRepository._origin_type({"metadata_json": '{"source":"remember_cmd"}'})
        == "ai_note"
    )
    assert MemoryRepository._origin_type({"legacy_memory_id": "old"}) == "legacy"
    assert MemoryRepository._origin_type({}) == "manual"
