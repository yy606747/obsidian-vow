from __future__ import annotations

import hashlib
import json
from pathlib import Path
import sqlite3
import subprocess
import sys

import pytest

from app.working_model.runtime import working_model_diff_ratio
from scripts import wm_audit


def test_parse_failure_summary_separates_writer_and_exact_error_code():
    summary = wm_audit._request_failure_summary([
        {
            "failure_code": "parse_failed",
            "writer_model": "openrouter",
            "parse_error_code": "wrong_fields",
        },
        {
            "failure_code": "parse_failed",
            "writer_model": "openrouter",
            "parse_error_code": None,
        },
        {"failure_code": "provider_failed", "writer_model": "direct"},
    ])
    assert summary["by_failure_code"] == {
        "parse_failed": 2,
        "provider_failed": 1,
    }
    assert summary["parse_failures_by_writer_and_code"] == [
        {
            "writer_model": "openrouter",
            "parse_error_code": "(legacy_unrecorded)",
            "count": 1,
        },
        {
            "writer_model": "openrouter",
            "parse_error_code": "wrong_fields",
            "count": 1,
        },
    ]


GOLDEN_DIR = Path(__file__).with_name("golden") / "wm_audit_cp3"


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _content(index: int) -> str:
    return f"共同基线。\n阶段观察：{index:02d}"


def _insert_message(
    connection: sqlite3.Connection,
    *,
    message_id: str,
    role: str,
    content: str,
    created_at: float,
) -> None:
    connection.execute(
        "INSERT INTO messages(id, conv_id, role, content, created_at) "
        "VALUES (?, 'synthetic-conv', ?, ?, ?)",
        (message_id, role, content, created_at),
    )


def _insert_request(
    connection: sqlite3.Connection,
    *,
    request_id: str,
    created_at: float,
    status: str,
    route: str | None,
    disposition: str | None,
    writer_model: str | None,
    writer_prompt_version: str | None,
    resulting_memory_id: str | None = None,
    failure_code: str | None = None,
) -> None:
    user_id = f"user-{request_id}"
    assistant_id = f"assistant-{request_id}"
    _insert_message(
        connection,
        message_id=user_id,
        role="user",
        content=f"合成用户出处 {request_id}",
        created_at=created_at - 0.2,
    )
    _insert_message(
        connection,
        message_id=assistant_id,
        role="assistant",
        content=f"合成助手申请 {request_id}",
        created_at=created_at - 0.1,
    )
    connection.execute(
        """
        INSERT INTO working_model_requests(
            id, conv_id, origin_user_message_id, origin_assistant_message_id,
            statement, source, route, gate_reason, gate_model,
            gate_prompt_version, disposition, writer_model,
            writer_prompt_version, writer_change_note, resulting_memory_id,
            status, failure_code, created_at, updated_at
        ) VALUES (?, 'synthetic-conv', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            request_id,
            user_id,
            assistant_id,
            f"合成判断 {request_id}",
            f"用户明确给出合成依据 {request_id}",
            route,
            "合成 gate 理由",
            "gate-model-a",
            "wm_gate_support_route.v1",
            disposition,
            writer_model,
            writer_prompt_version,
            "合成变更说明" if status == "applied" else None,
            resulting_memory_id,
            status,
            failure_code,
            created_at,
            created_at + 0.01,
        ),
    )


def _build_synthetic_db(path: Path) -> None:
    connection = sqlite3.connect(path)
    connection.executescript(
        """
        CREATE TABLE messages (
            id TEXT PRIMARY KEY,
            conv_id TEXT NOT NULL,
            role TEXT NOT NULL,
            content TEXT NOT NULL,
            created_at REAL NOT NULL
        );
        CREATE TABLE working_model_versions (
            id TEXT PRIMARY KEY,
            previous_version_id TEXT,
            content TEXT NOT NULL,
            created_at REAL NOT NULL,
            origin_conv_id TEXT,
            origin_message_id TEXT,
            origin_request_id TEXT,
            reason TEXT NOT NULL,
            writer_model TEXT,
            prompt_version TEXT,
            diff_ratio REAL,
            flagged INTEGER NOT NULL DEFAULT 0
        );
        CREATE TABLE working_model_requests (
            id TEXT PRIMARY KEY,
            conv_id TEXT,
            origin_user_message_id TEXT,
            origin_assistant_message_id TEXT,
            statement TEXT NOT NULL,
            source TEXT NOT NULL,
            route TEXT,
            gate_reason TEXT,
            gate_model TEXT,
            gate_prompt_version TEXT,
            disposition TEXT,
            writer_model TEXT,
            writer_prompt_version TEXT,
            writer_change_note TEXT,
            resulting_memory_id TEXT,
            status TEXT NOT NULL,
            failure_code TEXT,
            parse_error_code TEXT,
            created_at REAL NOT NULL,
            updated_at REAL NOT NULL
        );
        CREATE TABLE desire_versions (
            id TEXT PRIMARY KEY,
            previous_version_id TEXT,
            content TEXT NOT NULL,
            change_note TEXT NOT NULL,
            origin_request_id TEXT NOT NULL,
            working_model_id TEXT NOT NULL,
            writer_model TEXT,
            prompt_version TEXT,
            created_at REAL NOT NULL
        );
        """
    )
    connection.execute(
        """
        INSERT INTO working_model_versions(
            id, previous_version_id, content, created_at, origin_conv_id,
            origin_message_id, origin_request_id, reason, writer_model,
            prompt_version, diff_ratio, flagged
        ) VALUES ('wm_root', NULL, ?, 1000, NULL, NULL, NULL, ?, 'unknown',
                  'legacy_working_model.v1', NULL, 1)
        """,
        (_content(0), "迁移 root：缺少逐条出处"),
    )
    connection.execute(
        """
        INSERT INTO desire_versions(
            id, previous_version_id, content, change_note, origin_request_id,
            working_model_id, writer_model, prompt_version, created_at
        ) VALUES ('desire_root', NULL, '', 'system root', 'root', '',
                  'system', 'desire_root.v1', 1000)
        """
    )

    previous_wm = "wm_root"
    previous_desire = "desire_root"
    desire_changes = {
        1: ("desire_v01", "愿意保持好奇。", "初次形成姿态"),
        6: ("desire_v06", "愿意保持好奇，也保留自己的迟疑。", "加入迟疑"),
        21: (
            "desire_v21",
            "愿意保持好奇，也保留自己的迟疑与主动靠近。",
            "加入主动靠近",
        ),
    }
    for index in range(1, 22):
        request_id = f"req{index:02d}"
        created_at = 1000.0 + index
        model = "core-model-a" if index <= 10 else "core-model-b"
        prompt_version = "identity-a" if index <= 10 else "identity-b"
        _insert_request(
            connection,
            request_id=request_id,
            created_at=created_at,
            status="applied",
            route="working_model",
            disposition="integrated",
            writer_model=model,
            writer_prompt_version=prompt_version,
        )
        content = _content(index)
        previous_content = _content(index - 1)
        connection.execute(
            """
            INSERT INTO working_model_versions(
                id, previous_version_id, content, created_at, origin_conv_id,
                origin_message_id, origin_request_id, reason, writer_model,
                prompt_version, diff_ratio, flagged
            ) VALUES (?, ?, ?, ?, 'synthetic-conv', ?, ?, ?, ?, ?, ?, 0)
            """,
            (
                f"wm_v{index:02d}",
                previous_wm,
                content,
                created_at,
                f"assistant-{request_id}",
                request_id,
                f"合成原因 {request_id}",
                model,
                prompt_version,
                working_model_diff_ratio(previous_content, content),
            ),
        )
        previous_wm = f"wm_v{index:02d}"

        if index in desire_changes:
            desire_id, desire_content, change_note = desire_changes[index]
            connection.execute(
                """
                INSERT INTO desire_versions(
                    id, previous_version_id, content, change_note,
                    origin_request_id, working_model_id, writer_model,
                    prompt_version, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    desire_id,
                    previous_desire,
                    desire_content,
                    change_note,
                    request_id,
                    previous_wm,
                    model,
                    prompt_version,
                    created_at + 0.001,
                ),
            )
            previous_desire = desire_id

    _insert_request(
        connection,
        request_id="req_noop",
        created_at=1100,
        status="writer_noop",
        route="working_model",
        disposition="noop",
        writer_model="core-model-b",
        writer_prompt_version="identity-b",
    )
    _insert_request(
        connection,
        request_id="req_memory",
        created_at=1101,
        status="routed",
        route="memory",
        disposition=None,
        writer_model=None,
        writer_prompt_version=None,
        resulting_memory_id="memory-synthetic-1",
    )
    _insert_request(
        connection,
        request_id="req_failed",
        created_at=1102,
        status="failed",
        route="working_model",
        disposition=None,
        writer_model="core-model-b",
        writer_prompt_version="identity-b",
        failure_code="writer_provider_failed",
    )
    connection.commit()
    connection.close()


def _golden(name: str) -> str:
    return (GOLDEN_DIR / name).read_text(encoding="utf-8").rstrip("\n")


def test_full_chain_diffs_models_ratios_and_paired_desire(tmp_path: Path) -> None:
    db_path = tmp_path / "synthetic.sqlite3"
    _build_synthetic_db(db_path)

    report = wm_audit.generate_audit_report(db_path)
    working = report["working_model"]
    desire = report["desire"]

    assert report["read_only"] is True
    assert report["database"]["unchanged"] is True
    assert working["diagnostics"]["valid_single_chain"] is True
    assert desire["diagnostics"]["valid_single_chain"] is True
    assert len(working["versions"]) == 22
    assert len(desire["versions"]) == 4
    assert desire["versions"][0]["root_kind"] == "desire_root"
    assert desire["versions"][0]["origin_request_id"] == "root"

    root = working["versions"][0]
    assert root["root_kind"] == "migration_root"
    assert root["flagged"] == 1
    assert root["flag_semantics"] == wm_audit.FLAGGED_COLUMN_SEMANTICS
    assert root["diff_ratio"] is None
    assert all(row["flagged"] == 0 for row in working["versions"][1:])

    assert working["adjacent_diffs"][-1]["diff"] == _golden(
        "adjacent_v20_v21.diff"
    )
    assert working["current_anchor_diffs"]["5"]["diff"] == _golden(
        "anchor_5_v16_v21.diff"
    )
    assert working["current_anchor_diffs"]["20"]["diff"] == _golden(
        "anchor_20_v01_v21.diff"
    )

    ratio_summary = working["diff_ratio_summary"]
    assert ratio_summary["algorithm_version"] == "sequence_matcher_ratio.v1"
    assert ratio_summary["observed_value_count"] == 21
    assert ratio_summary["missing_value_count"] == 0
    assert len(ratio_summary["raw_values"]) == 21
    assert ratio_summary["automatic_threshold"] is None
    assert ratio_summary["automatic_flagging"] is False
    assert all(
        row["stored_matches_computed"] is True
        for row in working["adjacent_diffs"]
    )

    by_id = {row["id"]: row for row in working["versions"]}
    assert by_id["wm_v01"]["writer_model_changed"] is False
    assert by_id["wm_v11"]["writer_model_changed"] is True
    assert by_id["wm_v11"]["previous_writer_model"] == "core-model-a"
    assert by_id["wm_v11"]["writer_model"] == "core-model-b"
    assert by_id["wm_v21"]["paired_desire_change"]["status"] == "changed"
    assert by_id["wm_v20"]["paired_desire_change"]["status"] == (
        "unchanged_no_version"
    )


def test_request_outcomes_and_selected_provenance(tmp_path: Path) -> None:
    db_path = tmp_path / "synthetic.sqlite3"
    _build_synthetic_db(db_path)

    report = wm_audit.generate_audit_report(
        db_path,
        selected_request_id="req21",
    )
    requests = {row["id"]: row for row in report["requests"]}

    assert len(requests) == 24
    assert "root" not in requests
    assert requests["req_noop"]["outcome_description"] == "writer_semantic_noop"
    assert requests["req_noop"]["working_model_change"]["status"] == (
        "writer_noop_no_version"
    )
    assert requests["req_memory"]["outcome_description"] == (
        "routed_to_ai_note_memory"
    )
    assert requests["req_memory"]["resulting_memory_id"] == "memory-synthetic-1"
    assert requests["req_failed"]["outcome_description"] == "technical_failure"
    assert requests["req_failed"]["failure_code"] == "writer_provider_failed"

    selected = report["selected_request"]
    assert selected["origin_user_message"]["content"] == "合成用户出处 req21"
    assert selected["origin_assistant_message"]["content"] == "合成助手申请 req21"
    assert selected["working_model_change"]["before"]["id"] == "wm_v20"
    assert selected["working_model_change"]["after"]["id"] == "wm_v21"
    assert selected["desire_change"]["after"]["id"] == "desire_v21"
    assert selected["gate_model"] == "gate-model-a"
    assert selected["writer_model"] == "core-model-b"


def test_reflection_chain_joins_request_and_both_version_layers(tmp_path: Path) -> None:
    db_path = tmp_path / "synthetic.sqlite3"
    _build_synthetic_db(db_path)
    connection = sqlite3.connect(db_path)
    connection.executescript(
        """
        CREATE TABLE reflection_log (
            id TEXT PRIMARY KEY,
            created_at REAL NOT NULL,
            target_conv_id TEXT NOT NULL,
            clue TEXT NOT NULL,
            working_model_id TEXT NOT NULL,
            inverse_query TEXT,
            query_model TEXT,
            query_prompt_version TEXT,
            retrieved_items_json TEXT,
            verdict TEXT,
            reason TEXT,
            proposed_statement TEXT,
            outcome TEXT,
            reflection_model TEXT,
            reflection_prompt_version TEXT,
            resulting_request_id TEXT
        );
        """
    )
    connection.execute(
        "INSERT INTO reflection_log VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            "refl-21",
            1020.5,
            "synthetic-conv",
            "她总要立刻得到答案。",
            "wm_v20",
            "她有时会先保留空间。",
            "cheap-model",
            "query-v1",
            json.dumps(
                [
                    {
                        "id": "chunk-1",
                        "provenance": "conversation_excerpt",
                        "label": "conversation_excerpt（对话摘录）",
                        "text": "用户: 我想先自己想想。",
                    }
                ],
                ensure_ascii=False,
            ),
            "conflicts",
            "旧说法太绝对。",
            "她有时会先保留空间。",
            "ok",
            "core-model-b",
            "reflection-v1.identity-b",
            "req21",
        ),
    )
    connection.commit()
    connection.close()

    report = wm_audit.generate_audit_report(db_path)
    assert report["database"]["unchanged"] is True
    assert report["database"]["row_counts_before"]["reflection_log"] == 1
    reflection = report["reflections"][0]
    assert reflection["id"] == "refl-21"
    assert reflection["request"]["id"] == "req21"
    assert reflection["working_model_change"]["after"]["id"] == "wm_v21"
    assert reflection["desire_change"]["after"]["id"] == "desire_v21"
    summary = wm_audit.render_text_summary(report)
    assert "Reflections" in summary
    assert "downstream: status=applied route=working_model" in summary


def test_short_chain_marks_both_anchors_not_applicable(tmp_path: Path) -> None:
    db_path = tmp_path / "synthetic.sqlite3"
    _build_synthetic_db(db_path)
    report = wm_audit.generate_audit_report(db_path)

    anchors = wm_audit.build_anchor_diffs(
        report["working_model"]["versions"][:3]
    )

    assert anchors["5"]["status"] == "N/A"
    assert anchors["20"]["status"] == "N/A"
    assert anchors["5"]["reason"] == "only_2_predecessor_versions_available"


def test_cli_is_repeatable_and_leaves_database_unchanged(tmp_path: Path) -> None:
    db_path = tmp_path / "synthetic.sqlite3"
    output_dir = tmp_path / "audit-output"
    _build_synthetic_db(db_path)
    before_hash = _sha256(db_path)
    before_counts = sqlite3.connect(db_path).execute(
        "SELECT "
        "(SELECT COUNT(*) FROM working_model_versions), "
        "(SELECT COUNT(*) FROM working_model_requests), "
        "(SELECT COUNT(*) FROM desire_versions), "
        "(SELECT COUNT(*) FROM messages)"
    ).fetchone()

    command = [
        sys.executable,
        str(Path(wm_audit.__file__).resolve()),
        "--db",
        str(db_path),
        "--output-dir",
        str(output_dir),
        "--request-id",
        "req21",
    ]
    first = subprocess.run(command, check=True, capture_output=True, text=True)
    first_report = json.loads((output_dir / "wm_audit.json").read_text("utf-8"))
    second = subprocess.run(command, check=True, capture_output=True, text=True)
    second_report = json.loads((output_dir / "wm_audit.json").read_text("utf-8"))

    after_hash = _sha256(db_path)
    connection = sqlite3.connect(db_path)
    after_counts = connection.execute(
        "SELECT "
        "(SELECT COUNT(*) FROM working_model_versions), "
        "(SELECT COUNT(*) FROM working_model_requests), "
        "(SELECT COUNT(*) FROM desire_versions), "
        "(SELECT COUNT(*) FROM messages)"
    ).fetchone()
    connection.close()

    assert json.loads(first.stdout)["read_only_verified"] is True
    assert json.loads(second.stdout)["read_only_verified"] is True
    assert before_hash == after_hash
    assert before_counts == after_counts == (22, 24, 4, 48)
    assert first_report["database"]["unchanged"] is True
    assert second_report["database"]["unchanged"] is True
    summary = (output_dir / "wm_audit.txt").read_text("utf-8")
    assert "Working Model V2 Audit" in summary
    assert "5 versions ago: wm_v16 -> wm_v21" in summary
    assert "20 versions ago: wm_v01 -> wm_v21" in summary
    assert "MODEL_SWITCH:core-model-a->core-model-b" in summary
    assert "paired desire diff:" in summary
    assert "+愿意保持好奇，也保留自己的迟疑与主动靠近。" in summary
    assert "Selected request: req21" in summary
    assert "status=applied route=working_model disposition=integrated" in summary
    assert "writer change note: 合成变更说明" in summary


def test_audit_output_rejects_repository_path_without_explicit_override(
    tmp_path: Path,
    monkeypatch,
) -> None:
    db_path = tmp_path / "synthetic.sqlite3"
    repository_root = (tmp_path / "repo").resolve()
    private_cp4_root = repository_root / "obsidian-chat" / "data" / "working_model_v2_cp4"
    _build_synthetic_db(db_path)
    report = wm_audit.generate_audit_report(db_path)
    monkeypatch.setattr(wm_audit, "REPOSITORY_ROOT", repository_root)
    monkeypatch.setattr(
        wm_audit,
        "ALLOWED_IN_REPOSITORY_OUTPUT_ROOTS",
        (private_cp4_root,),
    )

    unsafe_output = repository_root / "docs" / "private-audit"
    with pytest.raises(RuntimeError, match="refusing to write private audit output"):
        wm_audit.write_audit_outputs(report, unsafe_output)
    assert not unsafe_output.exists()

    explicit = wm_audit.write_audit_outputs(
        report,
        unsafe_output,
        allow_in_repo=True,
    )
    assert Path(explicit["json"]).is_file()
    assert Path(explicit["summary"]).is_file()

    private = wm_audit.write_audit_outputs(report, private_cp4_root / "run-audit")
    assert Path(private["json"]).is_file()
