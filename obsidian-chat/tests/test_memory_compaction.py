import asyncio
import importlib
import json
import sqlite3
import time

import pytest

from app.memory_v2 import compaction, compaction_config, resident_recall
from app.memory_v2.embedding import pack_embedding
from test_pending_full_corpus import corpus, add_chunk


hybrid = importlib.import_module("app.memory_v2.hybrid_recall")
NOW = time.time()
OLD = NOW - 40 * 86400


def run(awaitable):
    return asyncio.run(awaitable)


def add_note(db, key, *, origin="auto_digest", ts=OLD, content=None, vector=(1, 0)):
    db.execute("INSERT INTO memory_items(id,origin_type,content,created_at,updated_at,source_start_ts,"
               "source_end_ts,embedding,metadata_json) VALUES(?,?,?,?,?,?,?,?,?)",
               (key, origin, content or "小栀赶项目又熬到凌晨两点，第二天精神不好，原计划还没完成。",
                ts, ts, ts, ts, pack_embedding(vector), '{}'))


@pytest.fixture
def archive(corpus, tmp_path, monkeypatch):
    monkeypatch.setattr(compaction_config, "config_path", lambda: tmp_path / "compaction.json")
    compaction_config.save_config({"enabled": True, "cache_mib": 1})
    from app.chat import worldbook
    monkeypatch.setattr(worldbook, "load_worldbook_names", lambda: ("小栀", "阿澈"))

    async def embed(_):
        return [1.0, 0.0]

    monkeypatch.setattr(compaction.embedding, "get_document_embedding", embed)
    return corpus


async def one_note(messages):
    data = json.loads(messages[1]["content"])
    return {"notes": [{"content": "小栀那段时间赶项目，连续熬夜。", "kind": "episode",
                        "source_ids": [r["id"] for r in data]}]}


def seed_old(path):
    with sqlite3.connect(path) as db:
        add_chunk(db, "old", ts=OLD, content="小栀赶项目到凌晨两点，连着几个晚上睡不好，白天还要上课。")
        add_note(db, "old-note")
        add_note(db, "ai", origin="ai_note", content="小栀希望有矛盾时把话说开。")


def snapshot(path):
    with sqlite3.connect(path) as db:
        chunks = db.execute("SELECT id,content,embedding,status FROM memory_chunks ORDER BY id").fetchall()
        notes = db.execute("SELECT id,content,embedding,status FROM memory_items WHERE origin_type!='compacted' ORDER BY id").fetchall()
    return chunks, notes


def test_old_details_leave_ram_but_deliberate_recall_reads_disk(archive):
    with sqlite3.connect(archive) as db:
        add_chunk(db, "old", ts=OLD)
        add_chunk(db, "recent", ts=NOW, content="今天吃饭", vector=(0, 1))
        add_chunk(db, "retired", ts=OLD, status="retired")
        add_note(db, "old-auto")
        add_note(db, "old-ai", origin="ai_note")
        add_note(db, "old-manual", origin="manual")
    before = snapshot(archive)

    async def scenario():
        entry = await hybrid._full_corpus_candidates(include_cards=True, ai_note_lane_enabled=True)
        assert {r["id"] for r in entry.chunks} == {"recent"}
        assert {r["id"] for r in entry.ai_notes} == {"old-ai"}
        assert {r["id"] for r in entry.notes} == {"old-manual"}
        result = await hybrid.wide_chunk_recall("海边约定", top_k=1, candidate_limit=1, as_of_ts=NOW + 1)
        assert result[0]["candidate_id"] == "old"
        assert {r["id"] for r in entry.chunks} == {"recent"}

    run(scenario())
    assert snapshot(archive) == before


def test_budget_includes_text_vectors_and_only_keeps_one_cache_variant(archive):
    with sqlite3.connect(archive) as db:
        for i in range(160):
            add_chunk(db, f"chunk-{i}", ts=NOW, content="普通日常" * 800, vector=[1.0] * 768)

    async def scenario():
        entry = await hybrid._full_corpus_candidates(include_cards=False, ai_note_lane_enabled=False)
        assert 0 < len(entry.chunks) < 160
        assert entry.residency["budget_excluded_rows"] > 0
        assert entry.residency["estimated_bytes"] <= 1024 * 1024
        await hybrid._full_corpus_candidates(include_cards=True, ai_note_lane_enabled=True)
        assert len(hybrid._full_corpus_cache) == 1
    run(scenario())


def test_over_budget_protected_note_can_still_be_recalled_from_disk(archive):
    with sqlite3.connect(archive) as db:
        for i in range(150):
            add_note(db, f"new-{i}", origin="ai_note", ts=NOW, content="做饭讨论" * 800, vector=(0, 1))
        add_note(db, "important", origin="ai_note", content="小栀和阿澈约好一起去海边看日出。")

    async def scenario():
        plan = await hybrid.hybrid_recall("海边日出", full_corpus=True, ai_note_lane_enabled=True)
        assert plan["candidate_cache"]["note_budget_excluded_rows"] > 0
        assert "important" in {item["id"] for item in plan["selected"]}
    run(scenario())


def test_compaction_keeps_original_bytes_and_never_takes_ai_notes_as_inputs(archive):
    seed_old(archive)
    before = snapshot(archive)
    result = run(compaction.compact_once(generator=one_note, now=NOW, timezone="UTC"))
    assert result["status"] == "applied"
    assert result["source_count"] == 2
    assert result["note_count"] == 1
    assert snapshot(archive) == before

    async def scenario():
        entry = await hybrid._full_corpus_candidates(include_cards=False, ai_note_lane_enabled=True)
        assert len(entry.notes) == 1
        assert entry.notes[0]["origin_type"] == "compacted"
        assert entry.notes[0]["source_end_ts"] == OLD
        assert entry.ai_notes[0]["id"] == "ai"
        assert await compaction.select_batch(compaction_config.load_config(), now=NOW, timezone="UTC") == []
    run(scenario())


def test_disable_restores_original_recall_and_rollback_removes_only_derived_notes(archive):
    seed_old(archive)
    before = snapshot(archive)
    assert run(compaction.compact_once(generator=one_note, now=NOW, timezone="UTC"))["status"] == "applied"
    run(hybrid._full_corpus_candidates(include_cards=False, ai_note_lane_enabled=True))
    compaction_config.save_config({"enabled": False})
    entry = run(hybrid._full_corpus_candidates(include_cards=False, ai_note_lane_enabled=True))
    assert {r["id"] for r in entry.chunks} == {"old"}
    assert {r["id"] for r in entry.notes} == {"old-note"}
    assert run(compaction.rollback())["batches"] == 1
    assert snapshot(archive) == before


def test_empty_summary_is_valid_and_does_not_repeat_paid_work(archive):
    seed_old(archive)

    async def empty(_):
        return {"notes": []}

    assert run(compaction.compact_once(generator=empty, now=NOW, timezone="UTC"))["note_count"] == 0
    assert run(compaction.compact_once(generator=empty, now=NOW, timezone="UTC"))["status"] == "empty"


def test_already_short_durable_fact_does_not_block_following_batches(archive):
    with sqlite3.connect(archive) as db:
        add_note(db, "short", content="小栀不吃香菜。")

    async def unchanged(messages):
        row = json.loads(messages[1]["content"])[0]
        return {"notes": [{"content": row["content"], "kind": "preference", "source_ids": [row["id"]]}]}

    assert run(compaction.compact_once(generator=unchanged, now=NOW, timezone="UTC"))["status"] == "applied"
    assert run(compaction.compact_once(generator=unchanged, now=NOW, timezone="UTC"))["status"] == "empty"


@pytest.mark.parametrize("bad", [{}, {"notes": "wrong"}, {"notes": [{"content": "新造出来的长期约定", "kind": "commitment", "source_ids": ["fake"]}]}, TimeoutError()])
def test_provider_failure_keeps_sources_and_does_not_commit_batch(archive, bad):
    seed_old(archive)
    before = snapshot(archive)

    async def broken(_):
        if isinstance(bad, Exception):
            raise bad
        return bad

    assert run(compaction.compact_once(generator=broken, now=NOW, timezone="UTC"))["status"] == "failed"
    assert snapshot(archive) == before
    assert run(compaction.status())["applied_batches"] == 0


def test_embedding_failure_does_not_commit(archive, monkeypatch):
    seed_old(archive)

    async def no_vector(_):
        return None

    monkeypatch.setattr(compaction.embedding, "get_document_embedding", no_vector)
    assert run(compaction.compact_once(generator=one_note, now=NOW, timezone="UTC"))["status"] == "failed"
    assert run(compaction.status())["applied_batches"] == 0


def test_concurrent_edit_cannot_commit_stale_summary(archive):
    seed_old(archive)

    async def edit(messages):
        with sqlite3.connect(archive) as db:
            db.execute("UPDATE memory_chunks SET content='改过的内容' WHERE id='old'")
        return await one_note(messages)

    assert run(compaction.compact_once(generator=edit, now=NOW, timezone="UTC"))["status"] == "source_changed"
    assert run(compaction.status())["applied_batches"] == 0


def test_disabling_during_provider_call_prevents_late_apply(archive):
    seed_old(archive)

    async def disable(messages):
        compaction_config.save_config({"enabled": False})
        return await one_note(messages)

    assert run(compaction.compact_once(generator=disable, now=NOW, timezone="UTC"))["status"] == "disabled"
    assert run(compaction.status())["applied_batches"] == 0


@pytest.mark.parametrize("mutation", ["edit", "delete", "retire"])
def test_later_source_change_excludes_derived_summary(archive, mutation):
    seed_old(archive)
    assert run(compaction.compact_once(generator=one_note, now=NOW, timezone="UTC"))["status"] == "applied"
    with sqlite3.connect(archive) as db:
        db.execute({"edit": "UPDATE memory_chunks SET content='纠正后的内容' WHERE id='old'",
                    "delete": "DELETE FROM memory_chunks WHERE id='old'",
                    "retire": "UPDATE memory_chunks SET status='retired' WHERE id='old'"}[mutation])
    entry = run(hybrid._full_corpus_candidates(include_cards=False, ai_note_lane_enabled=True))
    assert entry.notes == []
    assert run(compaction.status())["applied_batches"] == 0


def test_batch_limits_and_daily_call_limit_survive_new_invocations(archive):
    seed_old(archive)
    policy = {**compaction_config.load_config(), "daily_batches": 1}

    async def invalid(_):
        return {}

    assert run(compaction.compact_once(policy=policy, generator=invalid, now=NOW, timezone="UTC"))["status"] == "failed"
    assert run(compaction.compact_once(policy=policy, generator=one_note, now=NOW, timezone="UTC"))["status"] == "daily_limit"


def test_owner_pinned_and_high_importance_notes_stay_independent(archive):
    with sqlite3.connect(archive) as db:
        add_note(db, "pinned")
        add_note(db, "important")
        add_note(db, "ordinary")
        db.execute("UPDATE memory_items SET metadata_json=? WHERE id='pinned'", ('{"ui_unresolved":true}',))
        db.execute("UPDATE memory_items SET importance=0.95 WHERE id='important'")
    rows = run(compaction.select_batch(compaction_config.load_config(), now=NOW, timezone="UTC"))
    assert {r["id"] for r in rows} == {"ordinary"}
    entry = run(hybrid._full_corpus_candidates(include_cards=False, ai_note_lane_enabled=True))
    assert {r["id"] for r in entry.notes} == {"pinned", "important"}


def test_write_during_cache_build_forces_following_refresh(archive, monkeypatch):
    with sqlite3.connect(archive) as db:
        add_chunk(db, "recent", ts=NOW)
    original = resident_recall.build_entry

    async def changed(*args, **kwargs):
        result = await original(*args, **kwargs)
        hybrid.invalidate_full_corpus_cache(notes=True)
        return result

    monkeypatch.setattr(resident_recall, "build_entry", changed)
    entry = run(hybrid._full_corpus_candidates(include_cards=False, ai_note_lane_enabled=True))
    assert entry.notes_dirty and entry.chunks_dirty


def test_cold_scan_pages_and_excludes_future_origin_and_retired_rows(archive, monkeypatch):
    with sqlite3.connect(archive) as db:
        for i in range(180):
            add_chunk(db, f"ordinary-{i:04}", ts=OLD, content="普通菜单", vector=(0, 1))
        add_chunk(db, "zz-match", ts=OLD)
        add_chunk(db, "future", ts=NOW + 5000)
        add_chunk(db, "origin", ts=OLD, message_id="excluded")
        add_chunk(db, "retired", ts=OLD, status="retired")
    original = hybrid._batch_similarities
    sizes = []

    def track(rows, vector):
        sizes.append(len(rows))
        return original(rows, vector)

    monkeypatch.setattr(hybrid, "_batch_similarities", track)
    result = run(hybrid.wide_chunk_recall("海边约定", top_k=1, candidate_limit=1,
                 as_of_ts=NOW, exclude_message_id="excluded"))
    assert result[0]["candidate_id"] == "zz-match"
    assert len(sizes) >= 3
    assert max(sizes) <= resident_recall.PAGE_ROWS
    assert hybrid._full_corpus_cache == {}


@pytest.mark.parametrize("names", [("小栀", "阿澈"), ("青禾", "临川")])
def test_compaction_prompt_uses_runtime_names(names):
    body = compaction.build_messages([], user_name=names[0], ai_name=names[1], timezone="UTC")[0]["content"]
    assert all(name in body for name in names)
    assert all(generic not in body for generic in ("用户", "AI", "对方", "TA", "你"))


def test_historical_agreement_does_not_become_an_unresolved_task():
    rows = [{"key": "note:old", "content": "小栀和阿澈明确约好了工作日晚上11点提醒休息。"}]
    result = compaction.parse_notes({"notes": [{"content": rows[0]["content"], "kind": "commitment", "source_ids": ["note:old"]}]}, rows)
    assert result[0]["kind"] == "rule"
    body = compaction.build_messages([], user_name="小栀", ai_name="阿澈", timezone="UTC")[0]["content"]
    assert "导入时间" in body and "不生成新待办" in body
