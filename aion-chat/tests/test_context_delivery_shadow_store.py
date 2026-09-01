import pytest

from app.context_delivery.shadow_rules import (
    EVALUATION_MATCHED,
    EVALUATION_NOT_MATCHED,
    ShadowRuleEvaluation,
)
from app.context_delivery.shadow_store import ContextTriggerShadowStore


def _evaluation(
    *,
    source_event_id="event-1",
    status=EVALUATION_MATCHED,
    occurred_at=1000.0,
):
    return ShadowRuleEvaluation(
        rule="location_region_transition",
        source_event_id=source_event_id,
        occurred_at=occurred_at,
        evaluation_status=status,
        evaluation_reason="test_reason",
        features={"distance_m": 570.0},
    )


def _projection(at=1000.0):
    return {
        "schema_version": "context_delivery_projection.v2",
        "generated_at": at,
        "observations": [],
        "device_derived": [],
        "recent_events": [],
        "baseline_deviations": [],
        "availability": [],
        "metrics": {},
    }


def _gate(allowed=True):
    return {
        "schema_version": "wake_boundary_result.v1",
        "runtime_mode": "dry_run",
        "status": "passed" if allowed else "blocked",
        "wake_allowed": allowed,
        "blocked_reasons": [],
        "side_effects": [],
    }


def test_store_is_idempotent_by_rule_and_source_event(tmp_path):
    store = ContextTriggerShadowStore(tmp_path / "context_delivery.db")

    first = store.record_evaluation(
        _evaluation(),
        projection=_projection(),
        gate=_gate(),
    )
    second = store.record_evaluation(
        _evaluation(),
        projection=_projection(),
        gate=_gate(),
    )

    assert first["id"] == second["id"]
    assert first["features"] == {"distance_m": 570.0}
    assert first["projection"]["schema_version"] == "context_delivery_projection.v2"
    assert store.stats()["total"] == 1


def test_gate_is_present_only_for_matched_rows(tmp_path):
    store = ContextTriggerShadowStore(tmp_path / "context_delivery.db")

    with pytest.raises(ValueError, match="requires gate"):
        store.record_evaluation(_evaluation(), projection=_projection(), gate=None)
    with pytest.raises(ValueError, match="cannot carry gate"):
        store.record_evaluation(
            _evaluation(status=EVALUATION_NOT_MATCHED),
            projection=_projection(),
            gate=_gate(),
        )

    row = store.record_evaluation(
        _evaluation(status=EVALUATION_NOT_MATCHED),
        projection=_projection(),
        gate=None,
    )
    assert row["gate"] is None


def test_only_matched_rows_accept_owner_labels(tmp_path):
    store = ContextTriggerShadowStore(tmp_path / "context_delivery.db", now=lambda: 2000.0)
    matched = store.record_evaluation(
        _evaluation(source_event_id="matched"),
        projection=_projection(),
        gate=_gate(),
    )
    not_matched = store.record_evaluation(
        _evaluation(source_event_id="not", status=EVALUATION_NOT_MATCHED),
        projection=_projection(),
        gate=None,
    )

    labeled = store.set_owner_label(matched["id"], "right")

    assert labeled["owner_label"] == "right"
    assert labeled["labeled_at"] == 2000.0
    with pytest.raises(ValueError, match="only matched"):
        store.set_owner_label(not_matched["id"], "wrong")


def test_outcome_backfill_waits_for_full_owner_window_and_is_one_way(tmp_path):
    store = ContextTriggerShadowStore(tmp_path / "context_delivery.db")
    row = store.record_evaluation(
        _evaluation(occurred_at=1000.0),
        projection=_projection(),
        gate=_gate(),
    )

    assert store.due_outcomes(reference_time=2799.0) == []
    assert [item["id"] for item in store.due_outcomes(reference_time=2800.0)] == [row["id"]]

    updated = store.record_outcome(
        row["id"],
        legacy_sentinel_woke_within_5m=True,
        owner_message_within_30m=False,
        evaluated_at=2800.0,
    )
    unchanged = store.record_outcome(
        row["id"],
        legacy_sentinel_woke_within_5m=False,
        owner_message_within_30m=True,
        evaluated_at=2900.0,
    )

    assert updated["legacy_sentinel_woke_within_5m"] is True
    assert updated["owner_message_within_30m"] is False
    assert unchanged["legacy_sentinel_woke_within_5m"] is True
    assert unchanged["owner_message_within_30m"] is False
    assert store.due_outcomes(reference_time=9999.0) == []


def test_stats_keep_unavailable_out_of_not_matched_denominator(tmp_path):
    from app.context_delivery.shadow_rules import EVALUATION_UNAVAILABLE

    store = ContextTriggerShadowStore(tmp_path / "context_delivery.db")
    for source_id, status in (
        ("matched", EVALUATION_MATCHED),
        ("not", EVALUATION_NOT_MATCHED),
        ("missing", EVALUATION_UNAVAILABLE),
    ):
        store.record_evaluation(
            _evaluation(source_event_id=source_id, status=status),
            projection=_projection(),
            gate=_gate() if status == EVALUATION_MATCHED else None,
        )

    stats = store.stats()

    assert stats["total"] == 3
    assert stats["by_status"] == {
        "matched": 1,
        "not_matched": 1,
        "unavailable": 1,
    }
    assert stats["unavailable_ratio"] == pytest.approx(1 / 3)
