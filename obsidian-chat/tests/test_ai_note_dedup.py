import asyncio
import json
import sqlite3

import pytest

from app.memory_v2 import ai_note_dedup as dedup, repository, v2_repository
from app.memory_v2.embedding import pack_embedding
from app.working_model import runtime
from scripts import dedupe_ai_notes as cleanup
from test_working_model_v2_cp2 import (
    _db_factory, _gate, _init_db, _no_broadcast, _pipeline_input, _run,
)


@pytest.fixture
def note_db(tmp_path, monkeypatch):
    path = tmp_path / "notes.db"
    _run(_init_db(path))
    factory = _db_factory(path)
    monkeypatch.setattr(repository, "get_db", factory)
    monkeypatch.setattr(v2_repository, "get_db", factory)

    async def embedding(_content):
        return [1.0, 0.0]

    monkeypatch.setattr(repository, "get_document_embedding", embedding)
    from app.chat import worldbook
    monkeypatch.setattr(worldbook, "load_worldbook_names", lambda: ("小栀", "阿澈"))
    return path, factory


def count(path, table="memories"):
    with sqlite3.connect(path) as db:
        return db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]


async def keep(_messages):
    return {"duplicate_of": None, "reason": "新增事实"}


async def duplicate(messages):
    candidate = json.loads(messages[-1]["content"])["existing_notes"][0]
    return {"duplicate_of": candidate["id"], "new_information": "", "reason": "同一件事的改述"}


def test_exact_duplicate_reuses_record_and_preserves_original_date(note_db, monkeypatch):
    path, _ = note_db
    monkeypatch.setattr(dedup, "_judge", lambda _: pytest.fail("exact duplicate needs no model"))

    async def scenario():
        first = await repository.create_memory("小栀喜欢苹果。", "ai_note", source_conv="first")
        second = await repository.create_memory("小栀喜欢苹果", "ai_note", source_conv="later")
        assert second["id"] == first["id"]
        assert second["created_at"] == first["created_at"]
        assert second["source_conv"] == "first"
        assert second["deduplicated"]

    _run(scenario())
    assert count(path) == count(path, "memory_items") == 1
    with sqlite3.connect(path) as db:
        row = db.execute("SELECT metadata_json,last_seen_at,last_used_at FROM memory_items").fetchone()
    assert json.loads(row[0])["dedup_hits"] == 1
    assert row[1] is not None and row[2] is None


def test_paraphrase_uses_existing_note_but_changes_and_new_details_survive(note_db, monkeypatch):
    path, _ = note_db

    async def scenario():
        first = await repository.create_memory("小栀喜欢苹果。", "ai_note")
        monkeypatch.setattr(dedup, "_judge", duplicate)
        paraphrase = await repository.create_memory("苹果是小栀爱吃的水果。", "ai_note")
        assert paraphrase["id"] == first["id"]
        monkeypatch.setattr(dedup, "_judge", keep)
        for text in ("小栀现在不喜欢苹果了。", "小栀喜欢苹果，也喜欢梨。"):
            new = await repository.create_memory(text, "ai_note")
            assert new["id"] != first["id"]
            assert not new.get("deduplicated")

    _run(scenario())
    assert count(path) == 3


@pytest.mark.parametrize("answer", [{"duplicate_of": "invented-id", "new_information": ""}, {}, "bad-json", TimeoutError()])
def test_judge_failure_or_unrecognized_target_keeps_new_note(note_db, monkeypatch, answer):
    path, _ = note_db

    async def judge(_messages):
        if isinstance(answer, Exception):
            raise answer
        return answer

    monkeypatch.setattr(dedup, "_judge", judge)

    async def scenario():
        await repository.create_memory("小栀喜欢苹果。", "ai_note")
        new = await repository.create_memory("苹果是小栀爱吃的水果。", "ai_note")
        assert not new.get("deduplicated")

    _run(scenario())
    assert count(path) == 2


def test_relative_dates_on_different_days_cannot_be_discarded_as_identical():
    rows = [{"id": "old", "content": "今天一起看了电影", "created_at": 86400, "embedding": None}]
    assert _run(dedup.find_duplicate(rows[0]["content"], None, 172800, rows=rows,
                                     judge=lambda _: pytest.fail("different event"))) is None


def test_unrelated_notes_skip_judge_and_malformed_vectors_are_ignored():
    rows = [{"id": "old", "content": "火星轨道参数", "created_at": 1, "embedding": b"bad"}]
    result = _run(dedup.find_duplicate("周末想去买菜", pack_embedding([1, 0]), 2,
                                     rows=rows, judge=lambda _: pytest.fail("unrelated")))
    assert result is None


def test_parallel_writes_do_not_create_two_notes(note_db):
    path, _ = note_db

    async def scenario():
        a, b = await asyncio.gather(*(
            repository.create_memory("小栀喜欢苹果。", "ai_note") for _ in range(2)
        ))
        assert a["id"] == b["id"]

    _run(scenario())
    assert count(path) == 1


def test_wm_request_reuses_legacy_note_and_keeps_provenance(note_db, monkeypatch):
    path, factory = note_db
    monkeypatch.setattr(dedup, "_judge", duplicate)

    async def scenario():
        first = await repository.create_memory("小栀喜欢苹果。", "ai_note")

        async def embedding(_):
            return pack_embedding([1, 0])

        result = await runtime.run_working_model_pipeline(
            _pipeline_input(statement="苹果是小栀爱吃的水果。"),
            db_factory=factory, gate_provider=_gate("memory"),
            memory_prepare=embedding, memory_broadcast=_no_broadcast,
        )
        assert result["request"]["status"] == "routed"
        assert result["request"]["resulting_memory_id"] == first["id"]
        return result["request"]["id"]

    request_id = _run(scenario())
    assert count(path) == 1
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT COUNT(*) FROM memory_links WHERE target_id=? AND relation='generated_from'", (request_id,)).fetchone()[0] == 1


def test_legacy_path_reuses_wm_note(note_db):
    path, factory = note_db

    async def scenario():
        async def embedding(_):
            return pack_embedding([1, 0])

        result = await runtime.run_working_model_pipeline(
            _pipeline_input(statement="小栀喜欢苹果。"), db_factory=factory,
            gate_provider=_gate("memory"), memory_prepare=embedding,
            memory_broadcast=_no_broadcast,
        )
        second = await repository.create_memory("小栀喜欢苹果。", "ai_note")
        assert second["id"] == result["request"]["resulting_memory_id"]
        assert second["deduplicated"]

    _run(scenario())
    assert count(path) == 1


def test_changed_candidate_is_rechecked_before_reuse(note_db):
    path, factory = note_db

    async def scenario():
        await repository.create_memory("小栀喜欢苹果。", "ai_note")
        async with factory() as db:
            old = (await dedup.fetch_notes(db))[0]
            await db.execute("UPDATE memories SET content='小栀不喜欢苹果。'")
            await db.commit()
            assert await dedup.reuse_in_tx(db, old, seen_at=10) is None

    _run(scenario())
    assert count(path) == 1


def test_cleanup_archives_both_list_views_and_can_restore(note_db, monkeypatch):
    path, factory = note_db
    monkeypatch.setattr(dedup, "_judge", keep)

    async def scenario():
        for text in ("小栀喜欢苹果。", "苹果是小栀爱吃的水果。", "小栀爱吃苹果。"):
            await repository.create_memory(text, "ai_note")
        plan = await cleanup.plan_cleanup(db_factory=factory, judge=duplicate)
        assert len(plan["pairs"]) == 2
        assert len({pair["duplicate_of"] for pair in plan["pairs"]}) == 1
        applied = await cleanup.apply_plan(plan, db_factory=factory)
        assert len(applied["archived"]) == 2
        assert len(await repository.list_memories()) == 1
        assert len((await repository.list_memories_page())["items"]) == 1
        async with factory() as db:
            assert len(await dedup.fetch_notes(db)) == 1
        assert (await cleanup.apply_plan(plan, db_factory=factory))["archived"] == []
        assert len((await cleanup.restore_plan(plan, db_factory=factory))["restored"]) == 2
        assert len(await repository.list_memories()) == 3

    _run(scenario())
    # Text and old request IDs remain available, including after archival.
    assert count(path) == count(path, "memory_items") == 3


def test_judge_prompt_uses_runtime_names_and_preserves_comparison_text():
    messages = dedup.build_messages("小栀现在不喜欢苹果", 2, [
        {"id": "a", "content": "小栀喜欢苹果", "created_at": 1},
    ], user_name="小栀", ai_name="阿澈")
    assert "小栀" in messages[0]["content"] and "阿澈" in messages[0]["content"]
    assert "否定或纠正" in messages[0]["content"]
    assert "主观理解和推测也属于信息" in messages[0]["content"]
    assert json.loads(messages[1]["content"])["new_note"]["content"] == "小栀现在不喜欢苹果"


def test_judge_cannot_drop_note_after_identifying_new_information():
    rows = [{"id": "old", "content": "小栀喜欢苹果", "created_at": 1, "embedding": pack_embedding([1, 0])}]

    async def judge(_):
        return {"duplicate_of": "old", "new_information": "还喜欢梨"}

    assert _run(dedup.find_duplicate("小栀喜欢苹果和梨", pack_embedding([1, 0]), 2,
                                     rows=rows, judge=judge)) is None


def test_unmirrored_legacy_note_is_reused_and_indexed_once(note_db, monkeypatch):
    path, factory = note_db
    with sqlite3.connect(path) as db:
        db.execute("INSERT INTO memories (id,content,type,created_at,embedding) VALUES (?,?,?,?,?)",
                   ("pre_v2", "小栀喜欢苹果。", "ai_note", 1.0, pack_embedding([1, 0])))
    invalidations = []
    import importlib
    from app.memory_v2 import memory_service
    hybrid_recall = importlib.import_module("app.memory_v2.hybrid_recall")
    monkeypatch.setattr(hybrid_recall, "invalidate_full_corpus_cache", lambda **kw: invalidations.append(kw))
    result = _run(memory_service.create_memory("小栀喜欢苹果。", "ai_note"))
    assert result["id"] == "pre_v2" and result["deduplicated"] and result["index_created"]
    assert count(path) == count(path, "memory_items") == 1
    assert invalidations == [{"notes": True}]
    repeated = _run(memory_service.create_memory("小栀喜欢苹果。", "ai_note"))
    assert repeated["id"] == "pre_v2" and not repeated["index_created"]
    assert invalidations == [{"notes": True}]


def test_old_unmirrored_duplicates_can_be_archived_and_restored(note_db):
    path, factory = note_db
    with sqlite3.connect(path) as db:
        for index, text in enumerate(("小栀喜欢苹果。", "苹果是小栀爱吃的水果。")):
            db.execute("INSERT INTO memories (id,content,type,created_at,embedding) VALUES (?,?,?,?,?)",
                       (f"old_{index}", text, "ai_note", index + 1.0, pack_embedding([1, 0])))

    async def scenario():
        plan = await cleanup.plan_cleanup(db_factory=factory, judge=duplicate)
        assert plan["checked"] == 2 and len(plan["pairs"]) == 1
        assert len((await cleanup.apply_plan(plan, db_factory=factory))["archived"]) == 1
        assert len(await repository.list_memories()) == 1
        assert len((await cleanup.restore_plan(plan, db_factory=factory))["restored"]) == 1
        assert len(await repository.list_memories()) == 2

    _run(scenario())
    assert count(path) == 2
