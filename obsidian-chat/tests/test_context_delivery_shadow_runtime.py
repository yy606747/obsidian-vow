import ast
import json
import sqlite3
from pathlib import Path

from app.context_delivery import ContextDeliveryProjection
from app.context_delivery.shadow_rules import ContextTriggerShadowRuleEngine
from app.context_delivery.shadow_store import ContextTriggerShadowStore
from app.events import EvidenceRecord
from context_delivery_shadow_runtime import ContextTriggerShadowRuntime


def _record(
    event_id: str,
    *,
    kind: str,
    observed_at: float,
    payload=None,
    source="android.sensing",
):
    return EvidenceRecord(
        id=event_id,
        kind=kind,
        source=source,
        observed_at=observed_at,
        received_at=observed_at + 0.1,
        payload=payload or {},
    )


def _location(event_id="location-1", observed_at=1000.0):
    return _record(
        event_id,
        kind="location.state",
        source="location.v2",
        observed_at=observed_at,
        payload={
            "payload_schema": "location_geofence.v1",
            "event_type": "transition",
            "geofence_direction": "inside_to_outside",
            "boundary_side": "outside",
            "distance_m": 570.0,
            "accuracy_m": 30.0,
            "configured_enter_m": 400.0,
            "configured_exit_m": 560.0,
        },
    )


def _runtime(tmp_path, **kwargs):
    kwargs.setdefault("boundary_context_reader", lambda _at: {})
    return ContextTriggerShadowRuntime(
        store=ContextTriggerShadowStore(tmp_path / "context_delivery.db"),
        behavior_loader=lambda: {"context_trigger_shadow_enabled": True},
        projection_reader=lambda **reader_kwargs: ContextDeliveryProjection(
            generated_at=reader_kwargs["reference_time"]
        ),
        **kwargs,
    )


def test_matched_event_records_projection_and_shared_boundary_result(tmp_path):
    runtime = _runtime(
        tmp_path,
        boundary_context_reader=lambda _at: {"last_user_message_age_sec": 60},
    )

    row = runtime.process_evidence(_location())

    assert row["evaluation_status"] == "matched"
    assert row["projection"]["schema_version"] == "context_delivery_projection.v2"
    assert row["gate"]["wake_allowed"] is False
    assert row["gate"]["blocked_reasons"] == ["chat_cooldown"]
    assert "judgment" not in row["gate"]
    assert "score" not in row["gate"]


def test_runtime_flag_off_has_no_store_or_rule_side_effect(tmp_path):
    store = ContextTriggerShadowStore(tmp_path / "context_delivery.db")
    runtime = ContextTriggerShadowRuntime(
        store=store,
        behavior_loader=lambda: {"context_trigger_shadow_enabled": False},
    )

    assert runtime.process_evidence(
        _record("unlock-1", kind="sensing.unlock", observed_at=1000)
    ) is None
    assert not store.db_path.exists()


def test_non_whitelisted_signal_does_not_even_read_shadow_config(tmp_path):
    runtime = ContextTriggerShadowRuntime(
        store=ContextTriggerShadowStore(tmp_path / "context_delivery.db"),
        behavior_loader=lambda: (_ for _ in ()).throw(
            AssertionError("notification must not enter trigger shadow")
        ),
    )

    assert runtime.process_evidence(
        _record(
            "notification-1",
            kind="sensing.notification",
            observed_at=1000,
            payload={"app": "群聊"},
        )
    ) is None
    assert not runtime.store.db_path.exists()


def test_runtime_is_idempotent_for_replayed_source_event(tmp_path):
    runtime = _runtime(tmp_path)
    record = _location()

    first = runtime.process_evidence(record)
    second = runtime.process_evidence(record)

    assert first["id"] == second["id"]
    assert runtime.store.stats()["total"] == 1


def test_runtime_deduplicates_transport_retry_with_new_evidence_id(tmp_path):
    runtime = _runtime(tmp_path)

    first = runtime.process_evidence(_location(event_id="evidence-a"))
    second = runtime.process_evidence(_location(event_id="evidence-b"))

    assert first["id"] == second["id"]
    assert first["features"]["evidence_id"] == "evidence-a"
    assert runtime.store.stats()["total"] == 1


def test_restart_keeps_durable_rows_but_not_previous_interaction_state(tmp_path):
    store = ContextTriggerShadowStore(tmp_path / "context_delivery.db")
    common = {
        "store": store,
        "behavior_loader": lambda: {"context_trigger_shadow_enabled": True},
        "projection_reader": lambda **kw: ContextDeliveryProjection(
            generated_at=kw["reference_time"]
        ),
        "boundary_context_reader": lambda _at: {},
    }
    before = ContextTriggerShadowRuntime(
        rule_engine=ContextTriggerShadowRuleEngine(),
        **common,
    )
    first = before.process_evidence(
        _record("unlock-before", kind="sensing.unlock", observed_at=1000)
    )

    after = ContextTriggerShadowRuntime(
        rule_engine=ContextTriggerShadowRuleEngine(),
        **common,
    )
    second = after.process_evidence(
        _record("unlock-after", kind="sensing.unlock", observed_at=20_000)
    )

    assert first["evaluation_reason"] == "missing_previous_interaction"
    assert second["evaluation_status"] == "unavailable"
    assert second["evaluation_reason"] == "missing_previous_interaction"
    assert store.stats()["total"] == 2


def test_projection_failure_is_audited_without_reviving_legacy_text(tmp_path):
    runtime = ContextTriggerShadowRuntime(
        store=ContextTriggerShadowStore(tmp_path / "context_delivery.db"),
        behavior_loader=lambda: {"context_trigger_shadow_enabled": True},
        projection_reader=lambda **_kw: (_ for _ in ()).throw(RuntimeError("reader down")),
        boundary_context_reader=lambda _at: {},
    )

    row = runtime.process_evidence(_location())

    assert row["features"]["projection_capture_error"] == "RuntimeError"
    assert row["projection"]["schema_version"] == "context_delivery_projection.v2"
    assert row["projection"]["observations"] == []


def test_due_outcomes_are_backfilled_from_injected_observers(tmp_path):
    calls = {"owner": [], "sentinel": []}
    runtime = _runtime(
        tmp_path,
        owner_message_checker=lambda start, end: calls["owner"].append((start, end)) or True,
        legacy_wake_checker=lambda start, end: calls["sentinel"].append((start, end)) or False,
        now=lambda: 2800.0,
    )
    row = runtime.process_evidence(_location(observed_at=1000.0))

    assert runtime.refresh_due_outcomes(reference_time=2800.0) == 1
    updated = runtime.store.list_evaluations(limit=10)[0]

    assert updated["id"] == row["id"]
    assert updated["legacy_sentinel_woke_within_5m"] is False
    assert updated["owner_message_within_30m"] is True
    assert calls["sentinel"] == [(1000.0, 1300.0)]
    assert calls["owner"] == [(1000.0, 2800.0)]


def test_runtime_source_contains_no_model_or_wake_execution_call():
    text = (Path(__file__).resolve().parents[1] / "context_delivery_shadow_runtime.py").read_text(
        encoding="utf-8"
    )
    tree = ast.parse(text)
    calls = set()
    imports = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            if isinstance(node.func, ast.Name):
                calls.add(node.func.id)
            elif isinstance(node.func, ast.Attribute):
                calls.add(node.func.attr)
        elif isinstance(node, ast.Import):
            imports.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imports.add(node.module)

    assert calls.isdisjoint({"stream_ai", "run_core_wake", "call_provider"})
    assert all(not name.startswith("ai_providers") for name in imports)


def test_legacy_evidence_adapter_forwards_successful_records_to_shadow(monkeypatch):
    from app.legacy_adapters import evidence as evidence_adapter
    import context_delivery_shadow_runtime as shadow_runtime_module

    calls = []
    fake_runtime = type("FakeRuntime", (), {
        "process_evidence_safely": lambda _self, record: calls.append(record),
    })()
    record = _record("unlock-hook", kind="sensing.unlock", observed_at=1000)
    monkeypatch.setattr(
        shadow_runtime_module,
        "context_trigger_shadow_runtime",
        fake_runtime,
    )
    monkeypatch.setattr(
        evidence_adapter,
        "_safe_record",
        lambda *_args: record,
    )

    result = evidence_adapter.record_sensing_entry_safely({"type": "unlock"})

    assert result is record
    assert calls == [record]


def test_default_owner_message_outcome_reader_uses_bounded_user_window(monkeypatch, tmp_path):
    import context_delivery_shadow_runtime as runtime_module

    db_path = tmp_path / "chat.db"
    with sqlite3.connect(db_path) as conn:
        conn.execute("CREATE TABLE messages (role TEXT, created_at REAL)")
        conn.executemany(
            "INSERT INTO messages(role, created_at) VALUES (?, ?)",
            [
                ("assistant", 1100.0),
                ("user", 999.0),
                ("user", 1500.0),
                ("user", 2900.0),
            ],
        )
    monkeypatch.setattr(runtime_module, "DB_PATH", db_path)

    assert runtime_module._last_user_message_at_or_before(1400.0) == 999.0
    assert runtime_module._owner_message_between(1000.0, 2800.0) is True
    assert runtime_module._owner_message_between(1500.0, 2800.0) is False


def test_default_legacy_wake_outcome_reader_uses_call_core_field(monkeypatch, tmp_path):
    import context_delivery_shadow_runtime as runtime_module

    log_dir = tmp_path / "monitor_logs"
    log_dir.mkdir()
    path = log_dir / "1970-01-01.jsonl"
    path.write_text(
        "\n".join(json.dumps(item) for item in (
            {"timestamp": 1100.0, "call_core": False},
            {"timestamp": 1200.0, "call_core": True},
            {"timestamp": 1400.0, "call_core": True},
        )),
        encoding="utf-8",
    )
    monkeypatch.setattr(runtime_module, "MONITOR_LOGS_DIR", log_dir)

    assert runtime_module._legacy_wake_between(1000.0, 1300.0) is True
    assert runtime_module._legacy_wake_between(1200.0, 1300.0) is False
    assert runtime_module._last_legacy_wake_at_or_before(1300.0) == 1200.0
