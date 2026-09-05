from __future__ import annotations

import asyncio
import importlib
import sqlite3

import aiosqlite

from app.working_model import repository as wm_repository
from app.working_model.schema import init_working_model_tables
from scripts import wm_repair_failed_memories as repair


async def _with_heartbeat(awaitable):
    async def heartbeat():
        while True:
            await asyncio.sleep(0.001)

    task = asyncio.create_task(heartbeat())
    try:
        return await awaitable
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


def _run(awaitable):
    return asyncio.run(_with_heartbeat(awaitable))


async def _init_production_shape(path) -> None:
    async with aiosqlite.connect(path) as db:
        await db.execute(
            "CREATE TABLE memories ("
            "id TEXT PRIMARY KEY, content TEXT NOT NULL, type TEXT, created_at REAL, "
            "source_conv TEXT, embedding BLOB, keywords TEXT DEFAULT '', importance REAL, "
            "source_start_ts REAL, source_end_ts REAL, unresolved INTEGER DEFAULT 0)"
        )
        await db.execute(
            "CREATE TABLE memory_items ("
            "id TEXT PRIMARY KEY, legacy_memory_id TEXT UNIQUE, "
            "origin_type TEXT NOT NULL DEFAULT 'legacy', "
            "kind TEXT NOT NULL DEFAULT 'episode', "
            "namespace TEXT NOT NULL DEFAULT 'normal', content TEXT NOT NULL, "
            "subject TEXT NOT NULL DEFAULT '', "
            "entities_json TEXT NOT NULL DEFAULT '[]', "
            "emotion TEXT NOT NULL DEFAULT '', importance REAL NOT NULL DEFAULT 0.5, "
            "confidence REAL NOT NULL DEFAULT 0.7, status TEXT NOT NULL DEFAULT 'active', "
            "visibility TEXT NOT NULL DEFAULT 'prompt', embedding BLOB, "
            "keywords_json TEXT NOT NULL DEFAULT '[]', source_conv TEXT, "
            "source_start_ts REAL, source_end_ts REAL, created_at REAL NOT NULL, "
            "updated_at REAL NOT NULL, last_seen_at REAL, last_used_at REAL, expires_at REAL, "
            "metadata_json TEXT NOT NULL DEFAULT '{}')"
        )
        await db.execute(
            "CREATE TABLE memory_links ("
            "memory_id TEXT NOT NULL, target_id TEXT NOT NULL, target_type TEXT NOT NULL, "
            "relation TEXT NOT NULL, created_at REAL NOT NULL, "
            "PRIMARY KEY (memory_id,target_id,target_type,relation))"
        )
        await init_working_model_tables(db)
        await wm_repository.insert_request(
            db,
            request_id="wmreq_gate_memory",
            conv_id="conv",
            origin_user_message_id="u1",
            origin_assistant_message_id="a1",
            statement="一次具体边界事实",
            source="用户原话",
            status="failed",
            created_at=10.0,
            route="memory",
            gate_reason="具体事件",
            gate_model="gate",
            gate_prompt_version="gate.v1",
            failure_code=repair.FAILURE_CODE,
        )
        await wm_repository.insert_request(
            db,
            request_id="wmreq_writer_memory",
            conv_id="conv",
            origin_user_message_id="u2",
            origin_assistant_message_id="a2",
            statement="已有模式的一次新印证",
            source="用户原话二",
            status="failed",
            created_at=20.0,
            route="working_model",
            gate_reason="属于用户理解",
            gate_model="gate",
            gate_prompt_version="gate.v1",
            writer_model="core",
            writer_prompt_version="writer.v6",
            writer_change_note="现有认识已覆盖，保存为记忆",
            failure_code=repair.FAILURE_CODE,
        )
        await db.commit()


def test_repair_is_provider_free_complete_and_idempotent(tmp_path, monkeypatch):
    db_path = tmp_path / "chat.db"
    _run(_init_production_shape(db_path))

    dry_run = repair.inspect_repair(db_path=db_path)
    assert dry_run["provider_calls"] == 0
    assert dry_run["candidate_count"] == 2
    with sqlite3.connect(db_path) as db:
        assert db.execute("SELECT COUNT(*) FROM memories").fetchone()[0] == 0

    invalidations = []
    hybrid_recall = importlib.import_module("app.memory_v2.hybrid_recall")

    monkeypatch.setattr(
        hybrid_recall,
        "invalidate_full_corpus_cache",
        lambda **kwargs: invalidations.append(kwargs),
    )
    applied = _run(repair.apply_repair(db_path=db_path))
    assert applied["ok"] is True
    assert applied["provider_calls"] == 0
    assert applied["repaired_count"] == 2
    assert applied["remaining_candidate_count"] == 0
    assert invalidations == [{"notes": True}]

    with sqlite3.connect(db_path) as db:
        db.row_factory = sqlite3.Row
        requests = {
            row["id"]: dict(row)
            for row in db.execute(
                "SELECT * FROM working_model_requests ORDER BY id"
            )
        }
        assert requests["wmreq_gate_memory"]["status"] == "routed"
        assert requests["wmreq_gate_memory"]["disposition"] is None
        assert requests["wmreq_writer_memory"]["status"] == "routed"
        assert requests["wmreq_writer_memory"]["disposition"] == "memory"
        assert all(row["failure_code"] is None for row in requests.values())
        assert all(row["parse_error_code"] is None for row in requests.values())
        mirrors = db.execute(
            "SELECT subject,entities_json,origin_type,embedding FROM memory_items "
            "ORDER BY id"
        ).fetchall()
        assert len(mirrors) == 2
        assert all(row["subject"] == "" for row in mirrors)
        assert all(row["entities_json"] == "[]" for row in mirrors)
        assert all(row["origin_type"] == "ai_note" for row in mirrors)
        assert all(row["embedding"] is None for row in mirrors)
        assert db.execute("SELECT COUNT(*) FROM memory_links").fetchone()[0] == 2

    rerun = _run(repair.apply_repair(db_path=db_path))
    assert rerun["ok"] is True
    assert rerun["repaired_count"] == 0
    assert rerun["started_candidate_count"] == 0
    with sqlite3.connect(db_path) as db:
        assert db.execute("SELECT COUNT(*) FROM memories").fetchone()[0] == 2


def test_repair_refuses_working_model_failure_without_writer_provenance(tmp_path):
    db_path = tmp_path / "chat.db"
    _run(_init_production_shape(db_path))
    with sqlite3.connect(db_path) as db:
        db.execute(
            "UPDATE working_model_requests SET writer_prompt_version=NULL "
            "WHERE id='wmreq_writer_memory'"
        )
        db.commit()

    try:
        repair.inspect_repair(db_path=db_path)
    except repair.WorkingModelMemoryRepairError as exc:
        assert "lacks writer provenance" in str(exc)
    else:
        raise AssertionError("repair must fail closed when provenance is ambiguous")
