"""补充检索的全库范围、代际一致性和消息修改入口。"""

import asyncio
import importlib
import json
import sqlite3

import pytest

import database
from app.chat import crud_routes, streaming
from app.chat.models import MsgUpdate
from app.memory_v2 import chunks, memory_service
from app.memory_v2.embedding import pack_embedding
from app.memory_v3.config import normalize_memory_v3_config


hybrid = importlib.import_module("app.memory_v2.hybrid_recall")


@pytest.fixture
def corpus(tmp_path, monkeypatch):
    path = tmp_path / "recall.db"
    monkeypatch.setattr(database, "DB_PATH", path)
    asyncio.run(database.init_db())
    hybrid.clear_full_corpus_cache()
    monkeypatch.setattr(chunks, "load_worldbook", lambda: {"user_name": "小栀", "ai_name": "阿澈"})

    async def no_external(*_args, **_kwargs):
        return None

    async def query_embedding(_text):
        return [1.0, 0.0]

    async def forbidden_embedding(*_args, **_kwargs):
        raise AssertionError("消息修改不应触发向量化")

    monkeypatch.setattr(hybrid.embedding, "get_embedding", query_embedding)
    monkeypatch.setattr(chunks.embedding, "get_embeddings_batch", forbidden_embedding)
    monkeypatch.setattr(crud_routes, "export_conversation", no_external)
    monkeypatch.setattr(streaming, "export_conversation", no_external)
    monkeypatch.setattr(crud_routes.manager, "broadcast", no_external)
    import routes.files
    monkeypatch.setattr(routes.files, "delete_exported_file", lambda _id: None)
    with sqlite3.connect(path) as db:
        db.execute("INSERT INTO conversations (id,title,created_at,updated_at) VALUES ('conv','测试',1,1)")
    yield path
    hybrid.clear_full_corpus_cache()


def add_chunk(db, chunk_id, *, ts=1, vector=(1.0, 0.0), content="旧日海边约定", status="active", message_id=None):
    db.execute(
        "INSERT INTO memory_chunks (id,conv_id,message_ids_json,content,source_hash,"
        "created_at,updated_at,embedding,metadata_json,status) VALUES (?,'conv',?,?,?,?,?,?,?,?)",
        (chunk_id, json.dumps([message_id or chunk_id]), content, f"hash:{chunk_id}",
         ts, ts, pack_embedding(vector), json.dumps({"source_start_ts": ts, "source_end_ts": ts}), status),
    )


async def recall(**kwargs):
    return await hybrid.wide_chunk_recall(
        "海边约定", top_k=kwargs.pop("top_k", 1), candidate_limit=1000,
        as_of_ts=kwargs.pop("as_of_ts", 2000), **kwargs,
    )


def test_old_relevant_record_outside_latest_1000_is_selected_and_can_revert(corpus):
    with sqlite3.connect(corpus) as db:
        add_chunk(db, "old")
        for index in range(1001):
            add_chunk(db, f"new-{index}", ts=index + 2, vector=(0, 1), content="今日菜单")

    async def scenario():
        selected = await recall()
        legacy = await recall(full_corpus_enabled=False)
        return selected, legacy

    selected, legacy = asyncio.run(scenario())
    assert selected[0]["candidate_id"] == "old"
    assert selected[0]["semantic_similarity"] == 1.0
    assert legacy[0]["candidate_id"] != "old"


def test_time_origin_and_retired_filters_run_before_ranking(corpus):
    with sqlite3.connect(corpus) as db:
        add_chunk(db, "future", ts=100)
        add_chunk(db, "excluded", message_id="origin")
        add_chunk(db, "retired", status="retired")
        add_chunk(db, "cold", status="cold")
    result = asyncio.run(recall(as_of_ts=50, exclude_message_id="origin", top_k=20))
    assert [item["candidate_id"] for item in result] == ["cold"]


def test_cards_change_readout_but_never_raw_ranking(corpus):
    from app.memory_v3.card_versions import READABLE_RELATIONAL_CARD_PROMPT_VERSIONS
    with sqlite3.connect(corpus) as db:
        add_chunk(db, "match")
        add_chunk(db, "other", vector=(0, 1), content="今日菜单")
        for chunk_id in ("match", "other"):
            db.execute(
                "INSERT INTO memory_relational_cards (id,source_chunk_id,version,content,"
                "source_hash,status,prompt_version,created_at,updated_at) VALUES (?,?,1,?,?,'active',?,1,1)",
                (f"card:{chunk_id}", chunk_id, "海边约定" if chunk_id == "other" else "简短展示", f"hash:{chunk_id}",
                 sorted(READABLE_RELATIONAL_CARD_PROMPT_VERSIONS)[0]),
            )
    result = asyncio.run(recall(relational_cards_enabled=True))
    assert result[0]["candidate_id"] == "match"
    assert result[0]["readout_type"] == "relational_card"
    assert result[0]["readout_text"] == "简短展示"


def test_cache_hit_and_inflight_refresh_keep_one_matrix_generation(corpus, monkeypatch):
    with sqlite3.connect(corpus) as db:
        add_chunk(db, "old")
        add_chunk(db, "other", vector=(0, 1), content="今日菜单")
    fetch = hybrid._fetch_chunks
    calls = []

    async def counted_fetch(limit, **kwargs):
        calls.append(limit)
        return await fetch(limit, **kwargs)

    changed = False

    async def refresh_during_embedding(_text):
        nonlocal changed
        if not changed:
            changed = True
            async with database.get_db() as db:
                await db.execute("UPDATE memory_chunks SET embedding=? WHERE id='old'", (pack_embedding([0, 1]),))
                await db.execute("UPDATE memory_chunks SET embedding=? WHERE id='other'", (pack_embedding([1, 0]),))
                await db.commit()
            hybrid.invalidate_full_corpus_cache(chunks=True)
            await hybrid._full_corpus_candidates(include_cards=False, ai_note_lane_enabled=False)
        return [1.0, 0.0]

    monkeypatch.setattr(hybrid, "_fetch_chunks", counted_fetch)
    monkeypatch.setattr(hybrid.embedding, "get_embedding", refresh_during_embedding)

    async def scenario():
        first = await recall()
        second = await recall()
        third = await recall()
        return first, second, third

    first, second, third = asyncio.run(scenario())
    assert first[0]["candidate_id"] == "old"
    assert first[0]["semantic_similarity"] == 1.0
    assert second[0]["candidate_id"] == third[0]["candidate_id"] == "other"
    assert calls == [None, None]


@pytest.mark.parametrize("operation", ["edit", "delete", "conversation", "regenerate"])
def test_message_mutation_reconciles_cached_chunks_without_model_calls(corpus, operation):
    with sqlite3.connect(corpus) as db:
        db.execute("INSERT INTO messages (id,conv_id,role,content,created_at) VALUES ('source','conv',?,'海边约定',1)",
                   ("assistant" if operation == "regenerate" else "user",))

    async def scenario():
        await memory_service.ensure_conversation_chunks("conv", embed=False)
        before = await recall()
        assert before and "海边约定" in before[0]["raw_content"]
        if operation == "edit":
            assert (await crud_routes.update_message("source", MsgUpdate(content="改成山间散步")))["ok"]
        elif operation == "delete":
            assert (await crud_routes.delete_message("source"))["ok"]
        elif operation == "conversation":
            assert (await crud_routes.delete_conversation("conv"))["ok"]
        else:
            await streaming.replace_message_and_freeze_vow_context("conv", "source")
        return await recall()

    after = asyncio.run(scenario())
    assert not any("海边约定" in item["raw_content"] for item in after)
    if operation == "edit":
        assert "改成山间散步" in after[0]["raw_content"]
        with sqlite3.connect(corpus) as db:
            assert db.execute("SELECT embedding FROM memory_chunks WHERE status='active'").fetchone()[0] is None
    else:
        assert after == []


def test_reconciliation_failure_rolls_back_message_and_index_together(corpus, monkeypatch):
    with sqlite3.connect(corpus) as db:
        db.execute("INSERT INTO messages (id,conv_id,role,content,created_at) VALUES ('source','conv','user','原文',1)")

    async def fail(*_args, **_kwargs):
        raise RuntimeError("合成对账故障")

    monkeypatch.setattr(memory_service, "reconcile_conversation_chunks_in_tx", fail)
    result = asyncio.run(crud_routes.delete_message("source"))
    assert result == {"ok": False, "error": "delete_failed"}
    with sqlite3.connect(corpus) as db:
        assert db.execute("SELECT content FROM messages WHERE id='source'").fetchone()[0] == "原文"


@pytest.mark.parametrize("operation", ["edit", "delete"])
def test_pending_embedding_rejects_source_changed_during_request(corpus, monkeypatch, operation):
    with sqlite3.connect(corpus) as db:
        db.execute("INSERT INTO messages (id,conv_id,role,content,created_at) VALUES ('source','conv','user','旧的海边约定',1)")

    async def scenario():
        await memory_service.ensure_conversation_chunks("conv", embed=False)
        started, resume = asyncio.Event(), asyncio.Event()
        inputs = []

        async def delayed_embedding(texts):
            inputs.extend(texts)
            started.set()
            await resume.wait()
            return [[1.0, 0.0] for _ in texts]

        monkeypatch.setattr(chunks.embedding, "get_embeddings_batch", delayed_embedding)
        task = asyncio.create_task(chunks.embed_pending_chunks())
        try:
            await asyncio.wait_for(started.wait(), timeout=2)
            assert "旧的海边约定" in inputs[0]
            if operation == "edit":
                assert (await crud_routes.update_message("source", MsgUpdate(content="新的山间散步")))["ok"]
            else:
                assert (await crud_routes.delete_message("source"))["ok"]
        finally:
            resume.set()
        stats = await asyncio.wait_for(task, timeout=2)
        assert stats["embedded"] == 0 and stats["skipped"] == 1
        with sqlite3.connect(corpus) as db:
            content, vector, status = db.execute("SELECT content,embedding,status FROM memory_chunks").fetchone()
        assert vector is None
        if operation == "edit":
            assert "新的山间散步" in content and status == "active"
            await memory_service.ensure_conversation_chunks("conv", embed=False)
            assert (await chunks.embed_pending_chunks())["embedded"] == 1
            assert "新的山间散步" in inputs[-1]
        else:
            assert status == "retired"

    asyncio.run(scenario())


def test_full_corpus_switch_defaults_on_and_has_independent_rollback():
    from routes.memories import MemoryV3ConfigUpdate
    assert normalize_memory_v3_config({})["pending_full_corpus_enabled"] is True
    assert normalize_memory_v3_config({"pending_full_corpus_enabled": "false"})["pending_full_corpus_enabled"] is False
    assert MemoryV3ConfigUpdate(pending_full_corpus_enabled=False).model_dump(exclude_none=True) == {
        "pending_full_corpus_enabled": False,
    }


def test_retrieval_uses_frozen_full_corpus_configuration(corpus, monkeypatch):
    import time
    import app.memory_v3.pending_recall as pending
    from app.memory_v3.repository import PendingRecallRepository
    calls = []

    async def capture(_query, **kwargs):
        calls.append(kwargs)
        return []

    monkeypatch.setattr(pending, "wide_chunk_recall", capture)

    async def scenario():
        now = time.time()
        repository = PendingRecallRepository()
        async with database.get_db() as db:
            pending_id = await repository.create_queued_in_tx(
                db, conv_id="conv", origin_assistant_message_id="origin", intent_text="旧事",
                retrieval_deadline_at=now + 10, created_at=now,
                config={"pending_full_corpus_enabled": False, "ai_note_lane_enabled": True},
            )
            await db.commit()
        await pending.PendingRecallService(repository)._retrieve(pending_id)
        assert (await repository.get(pending_id))["status"] == "ready"

    asyncio.run(scenario())
    assert len(calls) == 1
    assert calls[0]["full_corpus_enabled"] is False
    assert calls[0]["ai_note_lane_enabled"] is True
