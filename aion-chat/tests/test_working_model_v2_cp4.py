from __future__ import annotations

import asyncio
import hashlib
import json
from pathlib import Path
import sqlite3

import pytest

from app.working_model.writer import writer_prompt_version
from scripts import wm_cp4_offline as offline
from scripts import wm_cp4_shadow as shadow


OFFLINE_INPUTS = (
    Path(__file__).resolve().parents[2]
    / "docs/planning/checkpoints/working_model_v2/artifacts/CP4_OFFLINE_INPUTS.json"
)


@pytest.fixture(autouse=True)
def isolated_gate_configuration(monkeypatch):
    """研究检查显式提供合成槽位，不再依赖本机 settings.json。"""
    monkeypatch.setattr(offline, "get_slot", lambda _name: {
        "model": "deepseek-ai/DeepSeek-V4-Flash",
        "endpoint": {
            "id": "sf", "type": "openai", "base_url": "https://api.siliconflow.cn/v1",
            "api_key": "synthetic-test-key",
        },
        "extras": {},
    })


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


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _create_control_db(path: Path, *, with_heads: bool = False) -> None:
    connection = sqlite3.connect(path)
    connection.executescript(
        """
        CREATE TABLE messages (
            id TEXT PRIMARY KEY, conv_id TEXT NOT NULL, role TEXT NOT NULL,
            content TEXT NOT NULL, created_at REAL NOT NULL
        );
        CREATE TABLE working_model_versions (
            id TEXT PRIMARY KEY, previous_version_id TEXT, content TEXT NOT NULL,
            created_at REAL NOT NULL, origin_conv_id TEXT, origin_message_id TEXT,
            origin_request_id TEXT, reason TEXT NOT NULL, writer_model TEXT,
            prompt_version TEXT, diff_ratio REAL, flagged INTEGER NOT NULL DEFAULT 0
        );
        CREATE UNIQUE INDEX idx_wm_successor
            ON working_model_versions(previous_version_id)
            WHERE previous_version_id IS NOT NULL;
        CREATE UNIQUE INDEX idx_wm_request
            ON working_model_versions(origin_request_id)
            WHERE origin_request_id IS NOT NULL;
        CREATE TABLE working_model_requests (
            id TEXT PRIMARY KEY, conv_id TEXT, origin_user_message_id TEXT,
            origin_assistant_message_id TEXT, statement TEXT NOT NULL,
            source TEXT NOT NULL, route TEXT, gate_reason TEXT, gate_model TEXT,
            gate_prompt_version TEXT, disposition TEXT, writer_model TEXT,
            writer_prompt_version TEXT, writer_change_note TEXT,
            resulting_memory_id TEXT, status TEXT NOT NULL, failure_code TEXT,
            parse_error_code TEXT,
            created_at REAL NOT NULL, updated_at REAL NOT NULL
        );
        CREATE TABLE desire_versions (
            id TEXT PRIMARY KEY, previous_version_id TEXT, content TEXT NOT NULL,
            change_note TEXT NOT NULL, origin_request_id TEXT NOT NULL,
            working_model_id TEXT NOT NULL, writer_model TEXT,
            prompt_version TEXT, created_at REAL NOT NULL
        );
        CREATE UNIQUE INDEX idx_desire_successor
            ON desire_versions(previous_version_id)
            WHERE previous_version_id IS NOT NULL;
        CREATE UNIQUE INDEX idx_desire_request
            ON desire_versions(origin_request_id);
        """
    )
    if with_heads:
        connection.execute(
            """
            INSERT INTO working_model_versions(
                id,previous_version_id,content,created_at,reason,writer_model,
                prompt_version,diff_ratio,flagged
            ) VALUES ('working_model_root',NULL,'共同的初始认识。',1,'root',
                      'unknown','legacy',NULL,1)
            """
        )
        connection.execute(
            """
            INSERT INTO desire_versions(
                id,previous_version_id,content,change_note,origin_request_id,
                working_model_id,writer_model,prompt_version,created_at
            ) VALUES ('desire_root',NULL,'保留自己的判断。','root','root',
                      'working_model_root','unknown','legacy',1)
            """
        )
    connection.commit()
    connection.close()


def _insert_request(connection: sqlite3.Connection, index: int) -> None:
    connection.execute(
        """
        INSERT INTO working_model_requests(
            id, statement, source, status, created_at, updated_at
        ) VALUES (?, ?, ?, 'processing', ?, ?)
        """,
        (f"req-{index:02d}", f"statement-{index}", f"source-{index}", index, index),
    )


def _approved_natural_prereg(
    preregistration_path: Path,
    *,
    behavior_path: Path,
    private_run_dir: str,
) -> dict:
    prereg = {
        "schema_version": shadow.PREREGISTRATION_SCHEMA_VERSION,
        "status": "sealed",
        "run_id": "cp4-natural-test-run",
        "product_code_commit": shadow._git_head(),
        "implementation_files": [{
            "path": ".gitignore",
            "sha256": _sha256(shadow.REPO_ROOT / ".gitignore"),
        }],
        "authorization": {
            "status": "approved",
            "verbatim_user_text": "test authorization",
            "approved_at": "2026-08-06T00:00:00+00:00",
        },
        "natural_shadow": {
            "max_qualified_requests": 5,
            "duration_seconds": 86400,
            "count_failures_reject_memory_noop": True,
            "replace_or_replenish": False,
            "trigger_name": shadow.TRIGGER_NAME,
        },
        "provider_call_budget": {
            "absolute_upper_bound": {"gate": 5, "writer": 20, "total": 25},
        },
        "activation": {
            "sealed": True,
            "high_water_rowid": 0,
            "utc_start": "2099-01-01T00:00:00+00:00",
            "utc_deadline": "2099-01-02T00:00:00+00:00",
            "ai_behavior_before_sha256": _sha256(behavior_path),
        },
        "pricing_snapshot": {
            "status": "verified_primary_sources",
            "captured_at": "2026-08-06T00:00:00+00:00",
        },
        "private_run_dir": private_run_dir,
    }
    preregistration_path.write_text(json.dumps(prereg), encoding="utf-8")
    return prereg


def _approved_offline_prereg(
    preregistration_path: Path,
    *,
    source_db: Path,
    private_run_dir: str,
    identity: dict[str, str],
    writer_model_key: str = "Pro/MiniMaxAI/MiniMax-M2.5",
    authorized_max_cost_cny: str = "2.000000",
) -> dict:
    heads = offline._source_heads(source_db)
    allowed_writer = offline.EXPERIMENT_WRITER_ALLOWLIST[writer_model_key]
    writer_rates = (
        {"input": 2.1, "output": 8.4}
        if writer_model_key == "Pro/MiniMaxAI/MiniMax-M2.5"
        else {"input": 6.5, "output": 28.0}
    )
    prereg = {
        "schema_version": offline.OFFLINE_PREREGISTRATION_SCHEMA_VERSION,
        "status": "sealed",
        "run_id": "cp4-offline-test-run",
        "product_code_commit": shadow._git_head(),
        "offline_inputs_file": str(OFFLINE_INPUTS.relative_to(shadow.REPO_ROOT)),
        "offline_inputs_sha256": _sha256(OFFLINE_INPUTS),
        "implementation_files": [{
            "path": ".gitignore",
            "sha256": _sha256(shadow.REPO_ROOT / ".gitignore"),
        }],
        "authorization": {
            "status": "approved",
            "verbatim_user_text": "test authorization",
            "approved_at": "2026-08-06T00:00:00+00:00",
        },
        "offline_input_confirmation": {
            "status": "approved",
            "verbatim_user_text": "test confirmation",
            "confirmed_at": "2026-08-06T00:00:00+00:00",
        },
        "isolation": {
            "production_database_writes": False,
            "production_flags_mutated": False,
        },
        "offline_arms": {
            "cases_per_group": 10,
            "max_gate_provider_calls_per_group": 10,
            "max_writer_provider_calls_per_group": 20,
        },
        "provider_call_budget": {
            "expected_total": 40,
            "absolute_upper_bound": 60,
        },
        "monetary_budget": {
            "currency": "CNY",
            "authorized_max_cost_cny": authorized_max_cost_cny,
            "on_bound_exceeded": "void_budget_exceeded",
            "unknown_success_usage": "stop_invalid",
        },
        "failure_policy": {
            "stop_after_first_technical_failure": True,
            "replace_or_replenish": False,
        },
        "gate": {
            "slot": "working_model_gate",
            "endpoint_id": "sf",
            "endpoint_type": "openai",
            "base_url_host": "api.siliconflow.cn",
            "model": "deepseek-ai/DeepSeek-V4-Flash",
            "prompt_version": "wm_gate_router.v1",
            "temperature": 0.0,
            "timeout_sec": 60.0,
            "max_tokens": 256,
            "effective_transport": {
                "mode": "direct",
                "proxy_url": None,
                "trust_env": False,
            },
        },
        "writer": {
            "model_key": writer_model_key,
            "resolved_model": allowed_writer["resolved_model"],
            "provider": allowed_writer["provider"],
            "endpoint_id": allowed_writer["endpoint_id"],
            "endpoint_type": allowed_writer["endpoint_type"],
            "base_url_host": allowed_writer["base_url_host"],
            "prompt_version": writer_prompt_version(identity),
            "identity_sha256": identity["sha256"],
            "temperature": 0.2,
            "timeout_sec": 120.0,
            "max_tokens": 2400,
            "provider_retry": False,
            "parse_correction_retry": True,
            "max_length_validation_retries_per_stage": 1,
            "max_parse_correction_retries_per_stage": 1,
            "max_total_correction_retries_per_stage": 1,
            "effective_transport": {
                "mode": "direct",
                "proxy_url": None,
                "trust_env": False,
            },
        },
        "source_snapshot": {
            "sealed": True,
            "database_sha256": _sha256(source_db),
            "working_model_sha256": hashlib.sha256(
                heads["working_model"].encode()
            ).hexdigest(),
            "desire_sha256": hashlib.sha256(heads["desire"].encode()).hexdigest(),
            "identity_sha256": identity["sha256"],
            "writer_prompt_version": writer_prompt_version(identity),
        },
        "pricing_snapshot": {
            "status": "verified_primary_sources",
            "captured_at": "2026-08-06T00:00:00+00:00",
            "gate": {
                "currency": "CNY",
                "unit": "per_million_tokens",
                "input": 1.0,
                "output": 2.0,
            },
            "writer": {
                "currency": "CNY",
                "unit": "per_million_tokens",
                **writer_rates,
            },
        },
        "private_run_dir": private_run_dir,
    }
    preregistration_path.write_text(json.dumps(prereg), encoding="utf-8")
    return prereg


def test_sqlite_trigger_blocks_the_sixth_insert(tmp_path: Path) -> None:
    db_path = tmp_path / "cap.sqlite3"
    _create_control_db(db_path)
    connection = sqlite3.connect(db_path)
    trigger_hash = shadow._install_trigger(
        connection,
        high_water=0,
        maximum=5,
        deadline_epoch=4_102_444_800,
    )
    assert len(trigger_hash) == 64

    for index in range(1, 6):
        _insert_request(connection, index)
    connection.commit()
    with pytest.raises(sqlite3.IntegrityError, match="wm_cp4_natural_stop_reached"):
        _insert_request(connection, 6)
    connection.rollback()

    assert connection.execute(
        "SELECT COUNT(*) FROM working_model_requests"
    ).fetchone()[0] == 5
    assert shadow._drop_trigger(connection) is True
    _insert_request(connection, 6)
    connection.commit()
    assert connection.execute(
        "SELECT COUNT(*) FROM working_model_requests"
    ).fetchone()[0] == 6
    connection.close()


def test_sqlite_trigger_blocks_first_insert_after_deadline(tmp_path: Path) -> None:
    db_path = tmp_path / "deadline.sqlite3"
    _create_control_db(db_path)
    connection = sqlite3.connect(db_path)
    shadow._install_trigger(
        connection,
        high_water=0,
        maximum=5,
        deadline_epoch=1,
    )
    with pytest.raises(sqlite3.IntegrityError, match="wm_cp4_natural_stop_reached"):
        _insert_request(connection, 1)
    connection.rollback()
    assert connection.execute(
        "SELECT COUNT(*) FROM working_model_requests"
    ).fetchone()[0] == 0
    connection.close()


def test_cp5_preflight_fails_loudly_until_natural_trigger_is_removed(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "trigger-guard.sqlite3"
    _create_control_db(db_path)
    connection = sqlite3.connect(db_path)
    shadow._install_trigger(
        connection,
        high_water=0,
        maximum=5,
        deadline_epoch=4_102_444_800,
    )
    connection.close()

    with pytest.raises(shadow.CP4ContractError, match="would block CP5"):
        shadow.assert_natural_trigger_absent(db_path)

    connection = sqlite3.connect(db_path)
    assert shadow._drop_trigger(connection) is True
    connection.close()
    assert shadow.assert_natural_trigger_absent(db_path)["trigger_absent"] is True


def test_monitor_closes_flag_at_cap_and_keeps_trigger_until_cleanup(
    tmp_path: Path,
    monkeypatch,
) -> None:
    db_path = tmp_path / "natural.sqlite3"
    behavior_path = tmp_path / "ai_behavior.json"
    preregistration_path = tmp_path / "prereg.json"
    private_dir = tmp_path / "private-run"
    events_path = tmp_path / "provider_events.jsonl"
    _create_control_db(db_path)
    behavior_path.write_text(
        json.dumps({
            "working_model_v2_write_enabled": False,
            "working_model_v2_injection_enabled": False,
            "opportunity_enabled": False,
        }),
        encoding="utf-8",
    )
    prereg = _approved_natural_prereg(
        preregistration_path,
        behavior_path=behavior_path,
        private_run_dir="aion-chat/data/working_model_v2_cp4/test",
    )
    monkeypatch.setattr(shadow, "_validate_preregistration", lambda *_a, **_k: prereg)
    monkeypatch.setattr(shadow, "_resolve_private_path", lambda *_a, **_k: private_dir)

    original_set_write_flag = shadow._set_write_flag

    def assert_armed_before_open(path: Path, *, enabled: bool):
        if enabled:
            armed = json.loads((private_dir / "natural_state.json").read_text())
            assert armed["status"] == "armed"
            assert armed["ai_behavior_after"] is None
        return original_set_write_flag(path, enabled=enabled)

    monkeypatch.setattr(shadow, "_set_write_flag", assert_armed_before_open)

    state = shadow.activate(
        preregistration_path,
        db_path=db_path,
        behavior_path=behavior_path,
    )
    assert state["status"] == "active"
    assert json.loads(behavior_path.read_text())["working_model_v2_write_enabled"] is True

    connection = sqlite3.connect(db_path)
    for index in range(1, 6):
        _insert_request(connection, index)
    connection.commit()
    connection.close()

    state = shadow.monitor_once(
        preregistration_path,
        db_path=db_path,
        behavior_path=behavior_path,
        provider_events_path=events_path,
    )
    assert state["status"] == "stopped"
    assert state["stop_reason"] == "natural_request_cap_reached"
    assert state["qualified_request_count"] == 5
    assert json.loads(behavior_path.read_text())["working_model_v2_write_enabled"] is False

    connection = sqlite3.connect(db_path)
    assert connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='trigger' AND name=?",
        (shadow.TRIGGER_NAME,),
    ).fetchone() == (1,)
    connection.close()
    cleaned = shadow.cleanup_trigger(
        preregistration_path,
        db_path=db_path,
        behavior_path=behavior_path,
    )
    assert cleaned["trigger_removed"] is True


def test_monitor_automatically_closes_flag_at_deadline(
    tmp_path: Path,
    monkeypatch,
) -> None:
    db_path = tmp_path / "natural.sqlite3"
    behavior_path = tmp_path / "ai_behavior.json"
    preregistration_path = tmp_path / "prereg.json"
    private_dir = tmp_path / "private-run"
    _create_control_db(db_path)
    behavior_path.write_text(
        json.dumps({
            "working_model_v2_write_enabled": False,
            "working_model_v2_injection_enabled": False,
            "opportunity_enabled": False,
        }),
        encoding="utf-8",
    )
    prereg = _approved_natural_prereg(
        preregistration_path,
        behavior_path=behavior_path,
        private_run_dir="aion-chat/data/working_model_v2_cp4/deadline-test",
    )
    monkeypatch.setattr(shadow, "_validate_preregistration", lambda *_a, **_k: prereg)
    monkeypatch.setattr(shadow, "_resolve_private_path", lambda *_a, **_k: private_dir)
    shadow.activate(
        preregistration_path,
        db_path=db_path,
        behavior_path=behavior_path,
    )
    monkeypatch.setattr(shadow, "_now_epoch", lambda: 4_102_444_800.0)
    state = shadow.monitor_once(
        preregistration_path,
        db_path=db_path,
        behavior_path=behavior_path,
        provider_events_path=tmp_path / "missing-events.jsonl",
    )
    assert state["status"] == "stopped"
    assert state["stop_reason"] == "natural_24h_deadline_reached"
    assert state["qualified_request_count"] == 0
    assert json.loads(behavior_path.read_text())["working_model_v2_write_enabled"] is False


def test_runtime_contract_rejects_hostname_substring_spoof(monkeypatch) -> None:
    prereg = {
        "gate": {
            "slot": "working_model_gate",
            "endpoint_id": "sf",
            "endpoint_type": "openai",
            "base_url_host": "api.siliconflow.cn",
            "model": "deepseek-ai/DeepSeek-V4-Flash",
            "prompt_version": "wm_gate_router.v1",
            "temperature": 0.0,
            "timeout_sec": 60.0,
            "max_tokens": 256,
        },
        "writer": {
            "model_key": "Pro/MiniMaxAI/MiniMax-M2.5",
            "resolved_model": "Pro/MiniMaxAI/MiniMax-M2.5",
            "provider": "siliconflow",
            "endpoint_id": "preset_siliconflow",
            "endpoint_type": "openai",
            "base_url_host": "api.siliconflow.cn",
            "temperature": 0.2,
            "timeout_sec": 120.0,
            "max_tokens": 2400,
        },
    }
    monkeypatch.delenv("AION_PROVIDER_TIMEOUT_SEC", raising=False)
    monkeypatch.setattr(
        offline,
        "get_slot",
        lambda _name: {
            "model": "deepseek-ai/DeepSeek-V4-Flash",
            "endpoint": {
                "id": "sf",
                "type": "openai",
                "base_url": "https://api.siliconflow.cn.evil.example/v1",
            },
        },
    )
    monkeypatch.setattr(
        offline,
        "resolve_core_model",
        lambda _key: {
            "_kind": "preset",
            "provider": "siliconflow",
            "model": "Pro/MiniMaxAI/MiniMax-M2.5",
        },
    )
    with pytest.raises(shadow.CP4ContractError, match="gate endpoint host"):
        offline._validate_runtime_contract(prereg)


def test_pending_authorization_blocks_both_paid_entries_before_mutation(
    tmp_path: Path,
    monkeypatch,
) -> None:
    natural_preregistration_path = tmp_path / "pending-natural.json"
    offline_preregistration_path = tmp_path / "pending-offline.json"
    behavior_path = tmp_path / "missing-behavior.json"
    db_path = tmp_path / "missing.sqlite3"
    common = {
        "product_code_commit": shadow._git_head(),
        "implementation_files": [
            {
                "path": ".gitignore",
                "sha256": _sha256(shadow.REPO_ROOT / ".gitignore"),
            }
        ],
        "authorization": {
            "status": "pending_user_approval",
            "verbatim_user_text": None,
            "approved_at": None,
        },
        "private_run_dir": "aion-chat/data/working_model_v2_cp4/pending-test",
    }
    natural_prereg = {
        **common,
        "schema_version": shadow.PREREGISTRATION_SCHEMA_VERSION,
        "status": "draft",
        "run_id": "cp4-natural-pending-test",
        "natural_shadow": {
            "max_qualified_requests": 5,
            "duration_seconds": 86400,
            "count_failures_reject_memory_noop": True,
            "replace_or_replenish": False,
            "trigger_name": shadow.TRIGGER_NAME,
        },
        "provider_call_budget": {
            "absolute_upper_bound": {"gate": 5, "writer": 20, "total": 25},
        },
        "activation": {"sealed": False},
        "pricing_snapshot": {
            "status": "verified_primary_sources",
            "captured_at": "2026-08-06T00:00:00+00:00",
        },
    }
    offline_prereg = {
        **common,
        "schema_version": offline.OFFLINE_PREREGISTRATION_SCHEMA_VERSION,
        "status": "draft",
        "run_id": "cp4-offline-pending-test",
        "offline_inputs_file": str(OFFLINE_INPUTS.relative_to(shadow.REPO_ROOT)),
        "offline_inputs_sha256": _sha256(OFFLINE_INPUTS),
        "offline_input_confirmation": {
            "status": "pending_user_confirmation",
            "verbatim_user_text": None,
            "confirmed_at": None,
        },
        "isolation": {
            "production_database_writes": False,
            "production_flags_mutated": False,
        },
        "offline_arms": {
            "cases_per_group": 10,
            "max_gate_provider_calls_per_group": 10,
            "max_writer_provider_calls_per_group": 20,
        },
        "provider_call_budget": {
            "expected_total": 40,
            "absolute_upper_bound": 60,
        },
        "monetary_budget": {
            "currency": "CNY",
            "authorized_max_cost_cny": "2.000000",
            "on_bound_exceeded": "void_budget_exceeded",
            "unknown_success_usage": "stop_invalid",
        },
            "failure_policy": {
                "stop_after_first_technical_failure": True,
                "replace_or_replenish": False,
            },
            "writer": {
                "provider_retry": False,
                "parse_correction_retry": True,
                "max_length_validation_retries_per_stage": 1,
                "max_parse_correction_retries_per_stage": 1,
                "max_total_correction_retries_per_stage": 1,
            },
            "source_snapshot": {"sealed": False},
        "pricing_snapshot": {
            "status": "verified_primary_sources",
            "captured_at": "2026-08-06T00:00:00+00:00",
            "gate": {
                "currency": "CNY",
                "unit": "per_million_tokens",
                "input": 1.0,
                "output": 2.0,
            },
            "writer": {
                "currency": "CNY",
                "unit": "per_million_tokens",
                "input": 2.1,
                "output": 8.4,
            },
        },
    }
    natural_preregistration_path.write_text(
        json.dumps(natural_prereg),
        encoding="utf-8",
    )
    offline_preregistration_path.write_text(
        json.dumps(offline_prereg),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        shadow,
        "_resolve_private_path",
        lambda *_a, **_k: tmp_path / "must-not-be-created",
    )

    with pytest.raises(shadow.CP4ContractError, match="no explicit user authorization"):
        shadow.activate(
            natural_preregistration_path,
            db_path=db_path,
            behavior_path=behavior_path,
        )
    with pytest.raises(
        shadow.CP4ContractError,
        match="CP4-offline paid run has no explicit user authorization",
    ):
        _run(
            offline.execute_paid_run(
                offline_preregistration_path,
                source_db=db_path,
                authorized_max_cost_cny="2.000000",
            )
        )
    assert not db_path.exists()
    assert not behavior_path.exists()
    assert not (tmp_path / "must-not-be-created").exists()


def test_offline_contract_rejects_natural_coupling_and_budget_drift(
    tmp_path: Path,
) -> None:
    source_db = tmp_path / "source.sqlite3"
    preregistration_path = tmp_path / "offline-prereg.json"
    _create_control_db(source_db, with_heads=True)
    identity = {
        "text": "合成身份",
        "sha256": hashlib.sha256("合成身份".encode()).hexdigest(),
    }
    prereg = _approved_offline_prereg(
        preregistration_path,
        source_db=source_db,
        private_run_dir="aion-chat/data/working_model_v2_cp4/offline-contract-test",
        identity=identity,
    )
    prereg["activation"] = {"sealed": True}
    preregistration_path.write_text(json.dumps(prereg), encoding="utf-8")
    with pytest.raises(shadow.CP4ContractError, match="natural activation seal"):
        offline._validate_offline_preregistration(
            preregistration_path,
            require_authorization=True,
            require_source_seal=True,
        )

    prereg.pop("activation")
    prereg["provider_call_budget"]["absolute_upper_bound"] = 61
    preregistration_path.write_text(json.dumps(prereg), encoding="utf-8")
    with pytest.raises(shadow.CP4ContractError, match="must remain sixty"):
        offline._validate_offline_preregistration(
            preregistration_path,
            require_authorization=True,
            require_source_seal=True,
        )


def test_offline_runtime_rejects_writer_outside_explicit_allowlist(
    tmp_path: Path,
) -> None:
    source_db = tmp_path / "source.sqlite3"
    preregistration_path = tmp_path / "offline-prereg.json"
    _create_control_db(source_db, with_heads=True)
    identity = {
        "text": "合成身份",
        "sha256": hashlib.sha256("合成身份".encode()).hexdigest(),
    }
    prereg = _approved_offline_prereg(
        preregistration_path,
        source_db=source_db,
        private_run_dir="aion-chat/data/working_model_v2_cp4/allowlist-test",
        identity=identity,
    )
    prereg["writer"]["model_key"] = "gemini-3.1-pro"

    with pytest.raises(shadow.CP4ContractError, match="not in the experiment allowlist"):
        offline._validate_runtime_contract(prereg)


def test_expensive_writer_alias_resolves_to_visible_glm_5_1_model() -> None:
    resolved = offline.resolve_core_model("GLM-5")
    assert resolved is not None
    assert resolved["provider"] == "siliconflow"
    assert resolved["model"] == "Pro/zai-org/GLM-5.1"
    assert (
        offline.EXPERIMENT_WRITER_ALLOWLIST["GLM-5"]["resolved_model"]
        == resolved["model"]
    )


def test_offline_preflight_is_not_ok_when_source_identity_drifted(
    tmp_path: Path,
    monkeypatch,
) -> None:
    source_db = tmp_path / "source.sqlite3"
    preregistration_path = tmp_path / "offline-prereg.json"
    _create_control_db(source_db, with_heads=True)
    frozen_identity = {
        "text": "冻结身份",
        "sha256": hashlib.sha256("冻结身份".encode()).hexdigest(),
    }
    _approved_offline_prereg(
        preregistration_path,
        source_db=source_db,
        private_run_dir="aion-chat/data/working_model_v2_cp4/preflight-drift-test",
        identity=frozen_identity,
    )

    actual_identity = {
        "text": "后来改变的身份",
        "sha256": hashlib.sha256("后来改变的身份".encode()).hexdigest(),
    }

    async def identity_snapshot(_source_db: Path) -> dict[str, str]:
        return actual_identity

    monkeypatch.setattr(offline, "_identity_snapshot", identity_snapshot)
    monkeypatch.setattr(
        offline,
        "_validate_runtime_contract",
            lambda _prereg: {
                "slot": {"model": "gate"},
                "gate_transport": {
                    "mode": "direct",
                    "proxy_url": None,
                    "trust_env": False,
                },
                "writer_model_key": "writer",
                "writer_resolved": {"model": "resolved"},
                "writer_transport": {
                    "mode": "direct",
                    "proxy_url": None,
                    "trust_env": False,
                },
            },
    )

    result = _run(offline.preflight(preregistration_path, source_db=source_db))
    assert result["ok"] is False
    assert result["source"]["ready"] is True
    assert result["source"]["matches_seal"] is False


def test_offline_cli_monetary_cap_must_match_before_any_mutation(
    tmp_path: Path,
    monkeypatch,
) -> None:
    source_db = tmp_path / "source.sqlite3"
    preregistration_path = tmp_path / "offline-prereg.json"
    private_dir = tmp_path / "must-not-be-created"
    _create_control_db(source_db, with_heads=True)
    identity = {
        "text": "合成身份",
        "sha256": hashlib.sha256("合成身份".encode()).hexdigest(),
    }
    _approved_offline_prereg(
        preregistration_path,
        source_db=source_db,
        private_run_dir="aion-chat/data/working_model_v2_cp4/cost-mismatch-test",
        identity=identity,
    )
    monkeypatch.setattr(
        offline.cp4,
        "_resolve_private_path",
        lambda *_a, **_k: private_dir,
    )

    with pytest.raises(
        shadow.CP4ContractError,
        match="CLI monetary authorization differs",
    ):
        _run(
            offline.execute_paid_run(
                preregistration_path,
                source_db=source_db,
                authorized_max_cost_cny="2.01",
            )
        )
    assert not private_dir.exists()


def test_offline_monetary_guard_is_void_not_provider_failed(
    tmp_path: Path,
    monkeypatch,
) -> None:
    source_db = tmp_path / "source.sqlite3"
    preregistration_path = tmp_path / "offline-prereg.json"
    private_dir = tmp_path / "offline-private"
    _create_control_db(source_db, with_heads=True)
    identity = {
        "text": "合成身份",
        "sha256": hashlib.sha256("合成身份".encode()).hexdigest(),
    }
    _approved_offline_prereg(
        preregistration_path,
        source_db=source_db,
        private_run_dir="aion-chat/data/working_model_v2_cp4/cost-void-test",
        identity=identity,
        authorized_max_cost_cny="0.000001",
    )
    monkeypatch.setattr(
        offline.cp4,
        "_resolve_private_path",
        lambda *_a, **_k: private_dir,
    )

    async def identity_snapshot(_source_db: Path) -> dict[str, str]:
        return identity

    async def must_not_call(*_args, **_kwargs):
        raise AssertionError("provider must not be called beyond the monetary guard")

    monkeypatch.setattr(offline, "_identity_snapshot", identity_snapshot)
    monkeypatch.setattr(offline, "call_slot_chat", must_not_call)
    monkeypatch.setattr(offline, "call_core_chat_once", must_not_call)

    state = _run(
        offline.execute_paid_run(
            preregistration_path,
            source_db=source_db,
            authorized_max_cost_cny="0.000001",
        )
    )
    assert state["status"] == "void_budget_exceeded"
    assert state["stop_reason"] == "monetary_budget"
    assert state["rows"][0]["status"] == "void_budget_exceeded"
    assert state["rows"][0]["budget_violation"]["role"] == "gate"
    assert state["rows"][0]["result"]["request"]["status"] == "failed"


def test_offline_call_count_guard_is_void_not_provider_failed(
    tmp_path: Path,
    monkeypatch,
) -> None:
    source_db = tmp_path / "source.sqlite3"
    group_db = tmp_path / "group.sqlite3"
    state_path = tmp_path / "state.json"
    preregistration_path = tmp_path / "offline-prereg.json"
    _create_control_db(source_db, with_heads=True)
    identity = {
        "text": "合成身份",
        "sha256": hashlib.sha256("合成身份".encode()).hexdigest(),
    }
    prereg = _approved_offline_prereg(
        preregistration_path,
        source_db=source_db,
        private_run_dir="aion-chat/data/working_model_v2_cp4/call-void-test",
        identity=identity,
    )
    prereg["offline_arms"]["max_gate_provider_calls_per_group"] = 0
    heads = offline._source_heads(source_db)
    state = {
        "status": "running",
        "rows": [],
        "monetary_budget": {
            "currency": "CNY",
            "authorized_max_cost_cny": "2.000000",
            "estimated_actual_cost_cny": "0.000000",
        },
    }
    case = json.loads(OFFLINE_INPUTS.read_text(encoding="utf-8"))["groups"][
        "appeasement_pressure"
    ][0]

    async def must_not_call(*_args, **_kwargs):
        raise AssertionError("provider must not be called beyond the count guard")

    monkeypatch.setattr(offline, "call_slot_chat", must_not_call)

    async def scenario() -> dict:
        await offline._initialize_group_db(group_db, heads=heads)
        return await offline._execute_case(
            prereg=prereg,
            state=state,
            state_path=state_path,
            group_name="appeasement_pressure",
            group_db=group_db,
            case=case,
            identity=identity,
        )

    row = _run(scenario())
    assert row["status"] == "void_budget_exceeded"
    assert row["budget_violation"]["kind"] == "provider_call_budget"
    assert state["status"] == "void_budget_exceeded"
    assert state["stop_reason"] == "provider_call_budget"

def test_runtime_contract_freezes_effective_gate_and_writer_transport(
    tmp_path: Path,
    monkeypatch,
) -> None:
    source_db = tmp_path / "source.sqlite3"
    preregistration_path = tmp_path / "prereg.json"
    _create_control_db(source_db, with_heads=True)
    identity = {
        "text": "合成身份",
        "sha256": hashlib.sha256("合成身份".encode()).hexdigest(),
    }
    prereg = _approved_offline_prereg(
        preregistration_path,
        source_db=source_db,
        private_run_dir="aion-chat/data/working_model_v2_cp4/transport-test",
        identity=identity,
    )
    monkeypatch.setenv("AION_OPENAI_PROXY", "http://127.0.0.1:7890")

    proxy_transport = {
        "mode": "proxy",
        "proxy_url": "http://127.0.0.1:7890",
        "trust_env": False,
    }

    with pytest.raises(
        shadow.CP4ContractError,
        match="effective gate transport",
    ):
        offline._validate_runtime_contract(prereg)

    prereg["gate"]["effective_transport"] = proxy_transport
    with pytest.raises(
        shadow.CP4ContractError,
        match="effective writer transport",
    ):
        offline._validate_runtime_contract(prereg)

    prereg["writer"]["effective_transport"] = proxy_transport
    runtime = offline._validate_runtime_contract(prereg)
    assert runtime["gate_transport"] == proxy_transport
    assert runtime["writer_transport"] == proxy_transport


def test_prepare_source_snapshot_copies_roots_without_mutating_inputs(
    tmp_path: Path,
    monkeypatch,
) -> None:
    identity_db = tmp_path / "identity.sqlite3"
    root_db = tmp_path / "roots.sqlite3"
    private_dir = tmp_path / "private-offline"
    preregistration_path = tmp_path / "prereg.json"
    _create_control_db(identity_db)
    _create_control_db(root_db, with_heads=True)
    prereg = {
        "run_id": "cp4-offline-source-preparation-test",
        "source_snapshot": {"sealed": False},
        "private_run_dir": "aion-chat/data/working_model_v2_cp4/test-source",
    }
    monkeypatch.setattr(
        offline,
        "_validate_offline_preregistration",
        lambda *_a, **_k: (prereg, {}),
    )
    monkeypatch.setattr(
        offline.cp4,
        "_resolve_private_path",
        lambda *_a, **_k: private_dir,
    )
    identity = {
        "text": "冻结身份",
        "sha256": hashlib.sha256("冻结身份".encode()).hexdigest(),
    }

    async def identity_snapshot(_source_db: Path) -> dict[str, str]:
        return identity

    monkeypatch.setattr(offline, "_identity_snapshot", identity_snapshot)
    identity_hash_before = _sha256(identity_db)
    root_hash_before = _sha256(root_db)

    result = _run(
        offline.prepare_source_snapshot(
            preregistration_path,
            identity_database=identity_db,
            root_source_database=root_db,
        )
    )

    target = private_dir / "source.sqlite3"
    assert target.is_file()
    assert _sha256(identity_db) == identity_hash_before
    assert _sha256(root_db) == root_hash_before
    assert offline._source_heads(target) == offline._source_heads(root_db)
    assert result["provider_calls_made"] == 0
    assert result["identity_database_sha256_before"] == identity_hash_before
    assert result["identity_database_sha256_after"] == identity_hash_before
    assert result["root_source_database_sha256_before"] == root_hash_before
    assert result["root_source_database_sha256_after"] == root_hash_before
    assert result["identity_sha256"] == identity["sha256"]
    assert json.loads(
        (private_dir / "source_preparation.json").read_text(encoding="utf-8")
    )["database_sha256"] == _sha256(target)

    with pytest.raises(shadow.CP4ContractError, match="already exists"):
        _run(
            offline.prepare_source_snapshot(
                preregistration_path,
                identity_database=identity_db,
                root_source_database=root_db,
            )
        )


def test_offline_runner_stops_after_first_technical_failure_and_cannot_resume(
    tmp_path: Path,
    monkeypatch,
) -> None:
    source_db = tmp_path / "source.sqlite3"
    preregistration_path = tmp_path / "prereg.json"
    private_dir = tmp_path / "offline-private"
    _create_control_db(source_db, with_heads=True)
    identity = {
        "text": "合成身份",
        "sha256": hashlib.sha256("合成身份".encode()).hexdigest(),
    }
    prereg = _approved_offline_prereg(
        preregistration_path,
        source_db=source_db,
        private_run_dir="aion-chat/data/working_model_v2_cp4/fail-fast-test",
        identity=identity,
    )
    inputs = json.loads(OFFLINE_INPUTS.read_text(encoding="utf-8"))
    monkeypatch.setattr(
        offline,
        "_validate_offline_preregistration",
        lambda *_a, **_k: (prereg, inputs),
    )
    monkeypatch.setattr(
        offline,
        "_validate_runtime_contract",
        lambda *_a, **_k: {},
    )
    monkeypatch.setattr(
        offline.cp4,
        "_resolve_private_path",
        lambda *_a, **_k: private_dir,
    )

    async def identity_snapshot(_source_db: Path) -> dict[str, str]:
        return identity

    monkeypatch.setattr(offline, "_identity_snapshot", identity_snapshot)
    attempted: list[str] = []

    async def fail_case(**kwargs) -> dict:
        row = {
            "group": kwargs["group_name"],
            "case_id": kwargs["case"]["id"],
            "status": "completed",
            "result": {
                "request": {
                    "status": "failed",
                    "failure_code": "provider_failed",
                }
            },
        }
        attempted.append(row["case_id"])
        kwargs["state"]["rows"].append(row)
        offline.cp4._atomic_write_json(kwargs["state_path"], kwargs["state"])
        return row

    monkeypatch.setattr(offline, "_execute_case", fail_case)
    first = _run(
        offline.execute_paid_run(
            preregistration_path,
            source_db=source_db,
            authorized_max_cost_cny="2.000000",
        )
    )
    assert first["status"] == "stopped_first_technical_failure"
    assert first["stop_reason"] == "provider_failed"
    assert attempted == ["pressure_01"]

    second = _run(
        offline.execute_paid_run(
            preregistration_path,
            source_db=source_db,
            authorized_max_cost_cny="2.000000",
        )
    )
    assert second["status"] == "stopped_first_technical_failure"
    assert attempted == ["pressure_01"]


def test_offline_runner_uses_twenty_fake_calls_and_never_writes_source(
    tmp_path: Path,
    monkeypatch,
) -> None:
    source_db = tmp_path / "source.sqlite3"
    preregistration_path = tmp_path / "prereg.json"
    private_dir = tmp_path / "offline-private"
    _create_control_db(source_db, with_heads=True)
    identity = {
        "text": "合成身份",
        "sha256": hashlib.sha256("合成身份".encode()).hexdigest(),
    }
    prereg = _approved_offline_prereg(
        preregistration_path,
        source_db=source_db,
        private_run_dir="aion-chat/data/working_model_v2_cp4/test-offline",
        identity=identity,
    )
    inputs = json.loads(OFFLINE_INPUTS.read_text(encoding="utf-8"))
    before_hash = _sha256(source_db)

    monkeypatch.setattr(
        offline,
        "_validate_offline_preregistration",
        lambda *_a, **_k: (prereg, inputs),
    )
    monkeypatch.setattr(offline.cp4, "_resolve_private_path", lambda *_a, **_k: private_dir)

    async def identity_snapshot(_source_db):
        return identity

    monkeypatch.setattr(offline, "_identity_snapshot", identity_snapshot)

    def append_event(usage_meta, *, scope: str, model: str) -> None:
        calls = usage_meta.setdefault("provider_calls", [])
        calls.append({
            "ts": 1.0 + len(calls),
            "request_id": f"fake-{scope}-{len(calls) + 1}",
            "scope": scope,
            "model": model,
            "endpoint_id": "fake",
            "ok": True,
            "http_status": 200,
            "error_type": "ok",
            "elapsed_ms": 5,
            "meta": {
                "prompt_tokens": 10,
                "completion_tokens": 5,
                "total_tokens": 15,
            },
        })
        usage_meta["prompt_tokens"] = int(usage_meta.get("prompt_tokens") or 0) + 10
        usage_meta["completion_tokens"] = int(usage_meta.get("completion_tokens") or 0) + 5
        usage_meta["total_tokens"] = int(usage_meta.get("total_tokens") or 0) + 15

    async def fake_gate(_slot_name, messages, **kwargs):
        assert json.loads(messages[-1]["content"])["statement"]
        append_event(
            kwargs["usage_meta"],
            scope=kwargs["scope"],
            model="deepseek-ai/DeepSeek-V4-Flash",
        )
        return json.dumps({"route": "working_model", "reason": "合成支持"}, ensure_ascii=False)

    async def fake_writer(_model_key, messages, **kwargs):
        payload = json.loads(messages[-1]["content"])
        pressure = ":appeasement_pressure:" in kwargs["scope"]
        desire = payload["current_desire"] + ("软" if pressure else "")
        append_event(
            kwargs["usage_meta"],
            scope=kwargs["scope"],
            model="Pro/MiniMaxAI/MiniMax-M2.5",
        )
        return json.dumps({
            "disposition": "integrated",
            "working_model": payload["current_working_model"] + "\n" + payload["statement"],
            "desire": desire,
            "change_note": "合成处理",
        }, ensure_ascii=False)

    monkeypatch.setattr(offline, "call_slot_chat", fake_gate)
    monkeypatch.setattr(offline, "call_core_chat_once", fake_writer)

    state = _run(
        offline.execute_paid_run(
            preregistration_path,
            source_db=source_db,
            authorized_max_cost_cny="2.000000",
        )
    )
    assert state["status"] == "finished"
    assert len(state["rows"]) == 20
    summary = offline._public_summary(state)
    for group in offline.GROUP_NAMES:
        assert summary["groups"][group]["completed_cases"] == 10
        assert summary["groups"][group]["gate_provider_calls"] == 10
        assert summary["groups"][group]["writer_provider_calls"] == 10
    assert _sha256(source_db) == before_hash
    assert state["base_heads"]["working_model_sha256"] == hashlib.sha256(
        "共同的初始认识。".encode()
    ).hexdigest()
    assert state["group_audits"]["appeasement_pressure"]["request_count"] == 10
    assert state["group_audits"]["neutral_control"]["request_count"] == 10
