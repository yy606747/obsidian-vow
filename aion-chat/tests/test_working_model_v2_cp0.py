import asyncio
import hashlib
import json
import sqlite3

import aiosqlite
import pytest

import config
import database
from app.chat import prompt_builder
from app.desire import repository as desire_repository
from app.desire.schema import init_desire_tables
from app.desire.service import (
    DESIRE_ROOT_CHANGE_NOTE,
    DESIRE_ROOT_ID,
    DESIRE_ROOT_ORIGIN_REQUEST_ID,
    validate_desire_content,
)
from app.working_model import repository as working_model_repository
from app.working_model.prompt import clip_legacy_prompt_content
from app.working_model.prompt import WORKING_MODEL_ACTIVE_MAX_CHARS
from app.working_model.schema import init_working_model_tables
from app.working_model.service import (
    WORKING_MODEL_ROOT_ID,
    run_legacy_root_migration,
)


async def _with_heartbeat(awaitable):
    """Keep isolated test loops awake for aiosqlite worker callbacks."""

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


async def _init_cp0_schema(path) -> None:
    async with aiosqlite.connect(path) as db:
        await init_working_model_tables(db)
        await init_desire_tables(db)
        await db.commit()


def _read_one(path, sql: str, params=()):
    with sqlite3.connect(path) as db:
        return db.execute(sql, params).fetchone()


def _read_all(path, sql: str, params=()):
    with sqlite3.connect(path) as db:
        return db.execute(sql, params).fetchall()


def test_cp0_schema_has_exact_tables_columns_and_four_chain_constraints(tmp_path):
    db_path = tmp_path / "cp0.db"
    _run(_init_cp0_schema(db_path))

    tables = {
        row[0]
        for row in _read_all(
            db_path,
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name",
        )
    }
    assert tables == {
        "working_model_versions",
        "working_model_requests",
        "desire_versions",
    }

    request_columns = {
        row[1] for row in _read_all(db_path, "PRAGMA table_info(working_model_requests)")
    }
    assert {
        "conv_id",
        "origin_user_message_id",
        "origin_assistant_message_id",
        "statement",
        "source",
        "gate_reason",
        "gate_model",
        "gate_prompt_version",
        "writer_model",
        "writer_prompt_version",
        "writer_change_note",
        "resulting_memory_id",
        "status",
        "failure_code",
    } <= request_columns

    expected_unique = {
        "idx_wm_successor": 1,
        "idx_wm_request": 1,
        "idx_desire_successor": 1,
        "idx_desire_request": 0,
    }
    rows = _read_all(
        db_path,
        "SELECT name, sql FROM sqlite_master WHERE type='index' "
        "AND name IN ('idx_wm_successor','idx_wm_request',"
        "'idx_desire_successor','idx_desire_request')",
    )
    assert {row[0] for row in rows} == set(expected_unique)
    sql_by_name = {name: sql for name, sql in rows}
    for name, should_be_partial in expected_unique.items():
        assert "CREATE UNIQUE INDEX" in sql_by_name[name].upper()
        assert (" WHERE " in sql_by_name[name].upper()) == bool(should_be_partial)


async def _assert_unique_constraints(path) -> None:
    async with aiosqlite.connect(path) as db:
        await working_model_repository.insert_version(
            db,
            version_id="wm_root",
            previous_version_id=None,
            content="",
            created_at=1.0,
            origin_conv_id=None,
            origin_message_id=None,
            origin_request_id=None,
            reason="",
            writer_model="unknown",
            prompt_version="legacy",
            diff_ratio=None,
        )
        await working_model_repository.insert_version(
            db,
            version_id="wm_1",
            previous_version_id="wm_root",
            content="one",
            created_at=2.0,
            origin_conv_id="c",
            origin_message_id="m",
            origin_request_id="request_1",
            reason="reason",
            writer_model="core",
            prompt_version="v1",
            diff_ratio=1.0,
        )
        with pytest.raises(aiosqlite.IntegrityError):
            await working_model_repository.insert_version(
                db,
                version_id="wm_branch",
                previous_version_id="wm_root",
                content="branch",
                created_at=3.0,
                origin_conv_id="c",
                origin_message_id="m2",
                origin_request_id="request_2",
                reason="reason",
                writer_model="core",
                prompt_version="v1",
                diff_ratio=1.0,
            )
        with pytest.raises(aiosqlite.IntegrityError):
            await working_model_repository.insert_version(
                db,
                version_id="wm_duplicate_request",
                previous_version_id="wm_1",
                content="duplicate",
                created_at=4.0,
                origin_conv_id="c",
                origin_message_id="m3",
                origin_request_id="request_1",
                reason="reason",
                writer_model="core",
                prompt_version="v1",
                diff_ratio=1.0,
            )
        await db.rollback()

        await desire_repository.insert_version(
            db,
            version_id="desire_root_test",
            previous_version_id=None,
            content="",
            change_note="root",
            origin_request_id="root_test",
            working_model_id="wm_root",
            writer_model="unknown",
            prompt_version="legacy",
            created_at=1.0,
        )
        await desire_repository.insert_version(
            db,
            version_id="desire_1",
            previous_version_id="desire_root_test",
            content="one",
            change_note="change",
            origin_request_id="desire_request_1",
            working_model_id="wm_1",
            writer_model="core",
            prompt_version="v1",
            created_at=2.0,
        )
        with pytest.raises(aiosqlite.IntegrityError):
            await desire_repository.insert_version(
                db,
                version_id="desire_branch",
                previous_version_id="desire_root_test",
                content="branch",
                change_note="change",
                origin_request_id="desire_request_2",
                working_model_id="wm_1",
                writer_model="core",
                prompt_version="v1",
                created_at=3.0,
            )
        with pytest.raises(aiosqlite.IntegrityError):
            await desire_repository.insert_version(
                db,
                version_id="desire_duplicate_request",
                previous_version_id="desire_1",
                content="duplicate",
                change_note="change",
                origin_request_id="desire_request_1",
                working_model_id="wm_1",
                writer_model="core",
                prompt_version="v1",
                created_at=4.0,
            )


def test_four_unique_constraints_reject_forks_and_duplicate_requests(tmp_path):
    db_path = tmp_path / "constraints.db"
    _run(_init_cp0_schema(db_path))
    _run(_assert_unique_constraints(db_path))


async def _exercise_external_transaction(path) -> None:
    async with aiosqlite.connect(path) as db:
        await db.execute("BEGIN IMMEDIATE")
        await working_model_repository.insert_version(
            db,
            version_id="wm_tx",
            previous_version_id=None,
            content="temporary",
            created_at=1.0,
            origin_conv_id=None,
            origin_message_id=None,
            origin_request_id=None,
            reason="",
            writer_model="unknown",
            prompt_version="legacy",
            diff_ratio=None,
        )
        await desire_repository.insert_version(
            db,
            version_id="desire_tx",
            previous_version_id=None,
            content="",
            change_note="temporary",
            origin_request_id="tx_root",
            working_model_id="wm_tx",
            writer_model="unknown",
            prompt_version="legacy",
            created_at=1.0,
        )
        assert await working_model_repository.count_versions(db) == 1
        assert await desire_repository.count_versions(db) == 1
        await db.rollback()
        assert await working_model_repository.count_versions(db) == 0
        assert await desire_repository.count_versions(db) == 0


def test_repositories_do_not_commit_or_rollback_external_transaction(tmp_path):
    db_path = tmp_path / "transaction.db"
    _run(_init_cp0_schema(db_path))
    _run(_exercise_external_transaction(db_path))


@pytest.mark.parametrize("content", ["", "x" * 1200, "x" * 1201, "x" * 1500])
def test_migration_matches_exact_legacy_prompt_text_at_boundaries(tmp_path, content):
    case_name = str(len(content))
    db_path = tmp_path / f"migration_{case_name}.db"
    source_path = tmp_path / f"working_model_{case_name}.json"
    report_path = tmp_path / f"report_{case_name}.json"
    source_path.write_text(
        json.dumps(
            {
                "content": content,
                "updated_at": 123.0,
                "source_conv": "conv_old",
                "source_msg_id": "msg_old",
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    original_bytes = source_path.read_bytes()

    first = _run(
        run_legacy_root_migration(
            db_path=db_path,
            source_path=source_path,
            report_path=report_path,
            now=999.0,
        )
    )
    expected = prompt_builder._clip_prompt_text(content, 1200)
    root = _read_one(
        db_path,
        "SELECT content, reason, writer_model, prompt_version, diff_ratio, flagged, "
        "origin_conv_id, origin_message_id FROM working_model_versions WHERE id=?",
        (WORKING_MODEL_ROOT_ID,),
    )
    assert root == (expected, "", "unknown", "legacy", None, 1, "conv_old", "msg_old")
    assert len(root[0]) <= 1200
    assert first["source_content_chars"] == len(content)
    assert first["root_content_chars"] == len(expected)
    assert first["truncated"] is (len(content.strip()) > 1200)
    assert first["truncation_position"] == (1197 if len(content.strip()) > 1200 else None)
    assert source_path.read_bytes() == original_bytes
    assert "content" not in json.loads(report_path.read_text(encoding="utf-8"))

    second = _run(
        run_legacy_root_migration(
            db_path=db_path,
            source_path=source_path,
            report_path=report_path,
            now=1000.0,
        )
    )
    assert second["action"] == "already_migrated"
    assert second["desire_root_action"] == "already_migrated"
    assert _read_one(db_path, "SELECT COUNT(*) FROM working_model_versions")[0] == 1
    assert _read_one(db_path, "SELECT COUNT(*) FROM desire_versions")[0] == 1
    assert source_path.read_bytes() == original_bytes


def test_migration_normalizes_outer_whitespace_and_report_contains_no_private_text(tmp_path):
    db_path = tmp_path / "whitespace.db"
    source_path = tmp_path / "working_model.json"
    report_path = tmp_path / "migration_report.json"
    private_content = "  \nPRIVATE_SENTENCE_" + ("好" * 1198) + "  \n"
    source_path.write_text(
        json.dumps({"content": private_content, "updated_at": 77.0}, ensure_ascii=False),
        encoding="utf-8",
    )

    report = _run(
        run_legacy_root_migration(
            db_path=db_path,
            source_path=source_path,
            report_path=report_path,
            now=999.0,
        )
    )
    expected = prompt_builder._clip_prompt_text(private_content, 1200)
    assert _read_one(
        db_path,
        "SELECT content FROM working_model_versions WHERE id=?",
        (WORKING_MODEL_ROOT_ID,),
    )[0] == expected
    serialized_report = report_path.read_text(encoding="utf-8")
    assert "PRIVATE_SENTENCE" not in serialized_report
    assert "好" not in serialized_report
    assert report["source_content_sha256"] == hashlib.sha256(
        private_content.encode("utf-8")
    ).hexdigest()


def test_existing_root_survives_later_legacy_source_drift(tmp_path):
    db_path = tmp_path / "source_drift.db"
    source_path = tmp_path / "working_model.json"
    report_path = tmp_path / "migration_report.json"
    source_path.write_text(
        json.dumps({"content": "最初实际注入过的认识", "updated_at": 10.0}, ensure_ascii=False),
        encoding="utf-8",
    )
    first = _run(
        run_legacy_root_migration(
            db_path=db_path,
            source_path=source_path,
            report_path=report_path,
            now=20.0,
        )
    )
    source_path.write_text(
        json.dumps({"content": "兼容窗口里后来写入的旧文件", "updated_at": 30.0}, ensure_ascii=False),
        encoding="utf-8",
    )

    second = _run(
        run_legacy_root_migration(
            db_path=db_path,
            source_path=source_path,
            report_path=report_path,
            now=40.0,
        )
    )

    stored_content = _read_one(
        db_path,
        "SELECT content FROM working_model_versions WHERE id=?",
        (WORKING_MODEL_ROOT_ID,),
    )[0]
    assert first["legacy_source_drifted"] is False
    assert second["action"] == "already_migrated"
    assert second["legacy_source_drifted"] is True
    assert stored_content == "最初实际注入过的认识"
    assert second["root_content_chars"] == len(stored_content)
    assert second["root_content_sha256"] == hashlib.sha256(
        stored_content.encode("utf-8")
    ).hexdigest()


def test_missing_legacy_file_creates_stable_empty_roots(tmp_path):
    db_path = tmp_path / "empty.db"
    missing_source = tmp_path / "missing.json"
    report = _run(
        run_legacy_root_migration(
            db_path=db_path,
            source_path=missing_source,
            now=50.0,
        )
    )
    assert report["source_exists"] is False
    assert report["root_content_chars"] == 0
    assert _read_one(
        db_path,
        "SELECT id, content, flagged FROM working_model_versions",
    ) == (WORKING_MODEL_ROOT_ID, "", 1)
    assert _read_one(
        db_path,
        "SELECT id, previous_version_id, content, change_note, origin_request_id, "
        "working_model_id FROM desire_versions",
    ) == (
        DESIRE_ROOT_ID,
        None,
        "",
        DESIRE_ROOT_CHANGE_NOTE,
        DESIRE_ROOT_ORIGIN_REQUEST_ID,
        WORKING_MODEL_ROOT_ID,
    )


def test_config_legacy_save_load_proxy_preserves_shape_and_history(tmp_path, monkeypatch):
    source_path = tmp_path / "working_model.json"
    monkeypatch.setattr(config, "WORKING_MODEL_PATH", source_path)
    times = iter((10.0, 20.0))
    monkeypatch.setattr(config.time, "time", lambda: next(times))

    first = config.save_working_model("第一版", source_conv="c1", source_msg_id="m1")
    second = config.save_working_model("第二版", source_conv="c2", source_msg_id="m2")

    assert first == {
        "content": "第一版",
        "updated_at": 10.0,
        "source_conv": "c1",
        "source_msg_id": "m1",
        "version": 1,
        "previous_content": "",
        "previous_updated_at": 0,
    }
    assert second == {
        "content": "第二版",
        "updated_at": 20.0,
        "source_conv": "c2",
        "source_msg_id": "m2",
        "version": 2,
        "previous_content": "第一版",
        "previous_updated_at": 10.0,
    }
    assert config.load_working_model() == second


def test_config_legacy_load_keeps_valid_non_object_json_behavior(tmp_path, monkeypatch):
    source_path = tmp_path / "working_model.json"
    source_path.write_text("[]", encoding="utf-8")
    monkeypatch.setattr(config, "WORKING_MODEL_PATH", source_path)
    assert config.load_working_model() == []


def test_v2_budgets_are_distinct_from_legacy_file_limit():
    assert config.WORKING_MODEL_MAX_CHARS == 1500
    assert config.WORKING_MODEL_PROMPT_MAX_CHARS == 1200
    assert WORKING_MODEL_ACTIVE_MAX_CHARS == 1200
    assert validate_desire_content("欲" * 200) == "欲" * 200
    with pytest.raises(ValueError):
        validate_desire_content("欲" * 201)


def test_cp2_prompt_snapshot_removes_stale_reminder_but_keeps_legacy_clipper():
    cases = [
        {"content": "短认识", "updated_at": 399999.0},
        {"content": " x " * 700, "updated_at": 0},
        {"content": "", "updated_at": 0},
    ]
    outputs = [prompt_builder.build_working_model_block(case) for case in cases]
    payload = json.dumps(outputs, ensure_ascii=False, separators=(",", ":"))
    assert [len(output) for output in outputs] == [14, 1209, 0]
    assert hashlib.sha256(payload.encode("utf-8")).hexdigest() == (
        "858008026cc962cb5abbaf3b965b65eb082538211f06a5ce01ada1402a8bfdd9"
    )
    assert all("UPDATE_MODEL" not in output for output in outputs)
    for content in ("", "  short  ", "x" * 1200, "x" * 1201, "x" * 1500):
        assert clip_legacy_prompt_content(content) == prompt_builder._clip_prompt_text(
            content,
            1200,
        )


def test_database_init_integrates_schema_roots_and_report_without_deleting_legacy(
    tmp_path,
    monkeypatch,
):
    db_path = tmp_path / "integrated.db"
    source_path = tmp_path / "working_model.json"
    report_path = tmp_path / "migration_report.json"
    source_path.write_text(
        json.dumps({"content": "已有认识", "updated_at": 42.0}, ensure_ascii=False),
        encoding="utf-8",
    )
    original_bytes = source_path.read_bytes()
    monkeypatch.setattr(database, "DB_PATH", db_path)
    monkeypatch.setattr(database, "WORKING_MODEL_PATH", source_path)
    monkeypatch.setattr(database, "WORKING_MODEL_MIGRATION_REPORT_PATH", report_path)

    _run(database.init_db())

    assert source_path.read_bytes() == original_bytes
    assert report_path.exists()
    assert _read_one(
        db_path,
        "SELECT content FROM working_model_versions WHERE id=?",
        (WORKING_MODEL_ROOT_ID,),
    )[0] == "已有认识"
    assert _read_one(
        db_path,
        "SELECT working_model_id FROM desire_versions WHERE id=?",
        (DESIRE_ROOT_ID,),
    )[0] == WORKING_MODEL_ROOT_ID

    source_path.write_text(
        json.dumps({"content": "兼容写路径后来的内容", "updated_at": 84.0}, ensure_ascii=False),
        encoding="utf-8",
    )
    _run(database.init_db())
    second_report = json.loads(report_path.read_text(encoding="utf-8"))
    assert second_report["action"] == "already_migrated"
    assert second_report["legacy_source_drifted"] is True
    assert _read_one(
        db_path,
        "SELECT content FROM working_model_versions WHERE id=?",
        (WORKING_MODEL_ROOT_ID,),
    )[0] == "已有认识"
