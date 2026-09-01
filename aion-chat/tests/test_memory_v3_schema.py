import asyncio
import hashlib
import json
import sqlite3
import sys
import types

import aiosqlite

from app.memory_v3 import config as memory_v3_config
from app.memory_v3.config import normalize_memory_v3_config
from app.memory_v3.schema import init_memory_v3_tables, inspect_memory_v3_schema
from scripts import memory_v3_preflight


async def _base_schema(db) -> None:
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


def test_memory_v3_config_is_inert_by_default_and_clamped():
    default = normalize_memory_v3_config({})

    assert default["relational_cards_enabled"] is False
    assert default["relational_card_generation_enabled"] is False
    assert default["relational_card_v2_generation_enabled"] is False
    assert default["relational_card_generation_cutoff_ts"] == 0.0
    assert default["pending_recall_enabled"] is False
    assert default["timeline_enabled"] is False
    assert default["card_readout_mode"] == "current_raw"
    assert default["relational_card_relationship_register"] == ""

    normalized = normalize_memory_v3_config(
        {
            "relational_card_v2_generation_enabled": "yes",
            "relational_card_generation_cutoff_ts": 99999999999,
            "pending_candidate_k": 999,
            "pending_join_max_wait_sec": -1,
            "pending_selector_timeout_sec": 99,
            "relational_card_failure_retry_delay_sec": 1,
            "timeline_max_chars": 9999,
            "timeline_generation_min_interval_sec": -1,
            "timeline_generation_timeout_sec": 99,
            "timeline_generation_attempts": 99,
            "card_readout_mode": "made-up",
            "relational_card_relationship_register": "  既有   语域  " + "长" * 500,
        }
    )
    assert normalized["pending_candidate_k"] == 100
    assert normalized["relational_card_v2_generation_enabled"] is True
    assert normalized["relational_card_generation_cutoff_ts"] == 4102444800.0
    assert normalized["pending_join_max_wait_sec"] == 0.0
    assert normalized["pending_selector_timeout_sec"] == 30.0
    assert normalized["relational_card_failure_retry_delay_sec"] == 60.0
    assert normalized["timeline_max_chars"] == 2000
    assert normalized["timeline_generation_min_interval_sec"] == 0.0
    assert normalized["timeline_generation_timeout_sec"] == 60.0
    assert normalized["timeline_generation_attempts"] == 2
    assert normalized["card_readout_mode"] == "current_raw"
    assert normalized["relational_card_relationship_register"].startswith("既有 语域 长")
    assert len(normalized["relational_card_relationship_register"]) == 400


def test_generation_cutoff_resets_on_every_combined_off_to_on_transition(monkeypatch):
    persisted = []
    runtime_config = types.ModuleType("config")
    runtime_config.SETTINGS = {"memory_v3": {}}
    runtime_config.save_settings = lambda data: persisted.append(dict(data["memory_v3"]))
    monkeypatch.setitem(sys.modules, "config", runtime_config)

    monkeypatch.setattr(memory_v3_config.time, "time", lambda: 1234.5678)
    first = memory_v3_config.save_memory_v3_config(
        {
            "relational_card_generation_enabled": True,
            "relational_card_v2_generation_enabled": True,
            "relational_card_generation_cutoff_ts": 1.0,
        }
    )
    assert first["relational_card_generation_cutoff_ts"] == 1234.568

    disabled = memory_v3_config.save_memory_v3_config(
        {"relational_card_generation_enabled": False}
    )
    assert disabled["relational_card_generation_cutoff_ts"] == 1234.568

    monkeypatch.setattr(memory_v3_config.time, "time", lambda: 2345.6789)
    reenabled = memory_v3_config.save_memory_v3_config(
        {"relational_card_generation_enabled": True}
    )
    assert reenabled["relational_card_generation_cutoff_ts"] == 2345.679

    explicit_backfill_cutoff = memory_v3_config.save_memory_v3_config(
        {"relational_card_generation_cutoff_ts": 42.0}
    )
    assert explicit_backfill_cutoff["relational_card_generation_cutoff_ts"] == 42.0
    assert len(persisted) == 4


def test_memory_v3_schema_is_idempotent_and_backfills_origins(tmp_path):
    db_path = tmp_path / "schema.db"

    async def scenario():
        async with aiosqlite.connect(db_path) as db:
            await _base_schema(db)
            await db.executemany(
                "INSERT INTO memory_items (id, legacy_memory_id, metadata_json) VALUES (?,?,?)",
                [
                    ("ai", "legacy-ai", '{"legacy_type":"ai_note"}'),
                    ("digest", None, '{"source":"digest.multi_note"}'),
                    ("manual", None, '{}'),
                    ("legacy", "legacy-1", '{}'),
                ],
            )
            before = await inspect_memory_v3_schema(db)
            first = await init_memory_v3_tables(db)
            second = await init_memory_v3_tables(db)
            await db.commit()
            after = await inspect_memory_v3_schema(db)
            cur = await db.execute(
                "SELECT id, origin_type FROM memory_items ORDER BY id"
            )
            origins = dict(await cur.fetchall())
        return before, first, second, after, origins

    before, first, second, after, origins = asyncio.run(scenario())

    assert before["missing_new_tables"]
    assert first["added_memory_chunk_columns"] == [
        "source_hash",
        "status",
        "retired_at",
        "card_generation_hash",
        "card_generation_status",
        "card_generation_reason",
        "card_generation_prompt_version",
        "card_generation_attempted_at",
    ]
    assert first["added_memory_item_columns"] == ["origin_type"]
    assert second["added_memory_chunk_columns"] == []
    assert second["added_memory_item_columns"] == []
    assert not any(after.values())
    assert origins == {
        "ai": "ai_note",
        "digest": "auto_digest",
        "legacy": "legacy",
        "manual": "manual",
    }


def test_memory_v3_active_uniqueness_constraints(tmp_path):
    db_path = tmp_path / "constraints.db"

    async def scenario():
        async with aiosqlite.connect(db_path) as db:
            await db.execute("PRAGMA foreign_keys=ON")
            await _base_schema(db)
            await init_memory_v3_tables(db)
            await db.execute(
                "INSERT INTO memory_chunks "
                "(id, conv_id, content, created_at, updated_at) VALUES ('chunk','conv','x',1,1)"
            )
            await db.execute(
                "INSERT INTO memory_relational_cards "
                "(id, source_chunk_id, version, content, source_hash, status, prompt_version, created_at, updated_at) "
                "VALUES ('card-1','chunk',1,'x','hash','active','v1',1,1)"
            )
            card_duplicate_failed = False
            try:
                await db.execute(
                    "INSERT INTO memory_relational_cards "
                    "(id, source_chunk_id, version, content, source_hash, status, prompt_version, created_at, updated_at) "
                    "VALUES ('card-2','chunk',2,'y','hash','active','v1',2,2)"
                )
            except sqlite3.IntegrityError:
                card_duplicate_failed = True

            await db.execute(
                "INSERT INTO memory_pending_recalls "
                "(id, conv_id, origin_assistant_message_id, intent_text, status, created_at, retrieval_deadline_at, updated_at) "
                "VALUES ('p1','conv','a1','找旧事','queued',1,21,1)"
            )
            pending_duplicate_failed = False
            try:
                await db.execute(
                    "INSERT INTO memory_pending_recalls "
                    "(id, conv_id, origin_assistant_message_id, intent_text, status, created_at, retrieval_deadline_at, updated_at) "
                    "VALUES ('p2','conv','a2','另一件','ready',2,22,2)"
                )
            except sqlite3.IntegrityError:
                pending_duplicate_failed = True
        return card_duplicate_failed, pending_duplicate_failed

    assert asyncio.run(scenario()) == (True, True)


def test_preflight_is_read_only_and_separates_config_layers(tmp_path):
    db_path = tmp_path / "preflight.db"
    settings_path = tmp_path / "settings.json"
    repo_root = tmp_path / "repo"
    (repo_root / "aion-chat").mkdir(parents=True)
    (repo_root / "deploy").mkdir()
    (repo_root / "aion-chat/main.py").write_text("uvicorn.run('main:app')", encoding="utf-8")
    (repo_root / "aion-chat/Dockerfile").write_text(
        'CMD ["python", "-u", "main.py"]', encoding="utf-8"
    )
    (repo_root / "deploy/docker-compose.prod.yml").write_text("services: {}", encoding="utf-8")
    settings_path.write_text(
        json.dumps(
            {
                "memory_v2_recall": {"top_k": 5, "prompt_min_score": 0.45},
                "memory_v3": {"pending_recall_enabled": True},
            }
        ),
        encoding="utf-8",
    )

    async def prepare():
        async with aiosqlite.connect(db_path) as db:
            await _base_schema(db)
            await db.execute(
                "CREATE TABLE messages (id TEXT PRIMARY KEY, content TEXT)"
            )
            await db.execute(
                "CREATE TABLE memory_usage (id TEXT PRIMARY KEY, reason TEXT)"
            )
            await db.commit()

    asyncio.run(prepare())
    before = hashlib.sha256(db_path.read_bytes()).hexdigest()
    args = memory_v3_preflight.parse_args(
        [
            "--db",
            str(db_path),
            "--settings",
            str(settings_path),
            "--repo-root",
            str(repo_root),
        ]
    )
    result = asyncio.run(memory_v3_preflight.run(args))
    after = hashlib.sha256(db_path.read_bytes()).hexdigest()

    assert before == after
    assert result["provider_calls"] == 0
    assert result["business_writes"] == 0
    assert result["config_layers"]["code_default_memory_v2"]["top_k"] == 8
    assert result["config_layers"]["workspace_memory_v2"]["top_k"] == 5
    assert result["config_layers"]["workspace_memory_v3"]["pending_recall_enabled"] is True
    assert result["worker_model"]["static_single_worker_consistent"] is True
