import json
from pathlib import Path

import pytest

from app.sentinel import (
    ATTENTION_SNAPSHOT_SCHEMA_VERSION,
    LAYER2_HANDOFF_ALLOWED_FIELDS,
    LAYER2_HANDOFF_SCHEMA_VERSION,
    attention_snapshot_builder,
    build_layer2_handoff,
    evaluate_case,
)
from app.sentinel.eval import FORBIDDEN_LAYER1_DECISION_FIELDS


CASES_PATH = Path(__file__).resolve().parents[1] / "app" / "sentinel" / "eval_cases.json"


def _load_cases():
    return json.loads(CASES_PATH.read_text(encoding="utf-8"))


def _snapshot_for(case_id: str) -> dict:
    case = next(item for item in _load_cases() if item["id"] == case_id)
    record = evaluate_case(case, snapshot_builder=attention_snapshot_builder)

    assert record["ok"] is True
    return record["trace"]["snapshot"]


def _all_attention_snapshots():
    for case in _load_cases():
        record = evaluate_case(case, snapshot_builder=attention_snapshot_builder)
        assert record["ok"] is True
        yield case["id"], record["trace"]["snapshot"]


def test_layer2_handoff_contains_only_prompt_safe_attention_fields():
    for case_id, snapshot in _all_attention_snapshots():
        handoff = build_layer2_handoff(snapshot)
        serialized = json.dumps(handoff, ensure_ascii=False)

        assert set(handoff) == LAYER2_HANDOFF_ALLOWED_FIELDS
        assert handoff["schema_version"] == LAYER2_HANDOFF_SCHEMA_VERSION
        assert handoff["attention_schema_version"] == ATTENTION_SNAPSHOT_SCHEMA_VERSION
        assert handoff["compact_text"] == snapshot["compact_text"]
        assert handoff["suggested_next_check_sec"] == snapshot["suggested_next_check_sec"]
        assert FORBIDDEN_LAYER1_DECISION_FIELDS.isdisjoint(handoff)
        assert "debug_trace" not in handoff
        assert "generated_at" not in handoff

        assert "debug_trace" not in serialized, case_id
        assert "feature_tags" not in serialized, case_id
        assert "source_records" not in serialized, case_id
        assert "raw_signal_count" not in serialized, case_id
        assert "recent_chat_count" not in serialized, case_id


def test_layer2_handoff_keeps_uncertainty_and_missing_source_cautions():
    expected_cautions = {
        "late_night_unlock_not_sleeping": "不能确认已经睡着",
        "low_confidence_sleep_high_salience": "证据不足以确认睡着",
        "camera_disabled_no_visual_claim": "不能推断视觉状态",
        "pc_source_disabled_no_work_claim": "不能确认电脑前状态",
        "source_conflict_active_vs_busy": "优先尊重忙碌声明",
    }

    for case_id, expected in expected_cautions.items():
        handoff = build_layer2_handoff(_snapshot_for(case_id))

        assert expected in handoff["compact_text"]


def test_layer2_handoff_deep_copies_mutable_snapshot_fields():
    snapshot = _snapshot_for("outside_transition_strong_signal")
    handoff = build_layer2_handoff(snapshot)

    handoff["world_state"]["mutated"] = True
    handoff["hypotheses"][0]["support"].append("mutated")
    handoff["attention_targets"].append("mutated")

    assert "mutated" not in snapshot["world_state"]
    assert "mutated" not in snapshot["hypotheses"][0]["support"]
    assert "mutated" not in snapshot["attention_targets"]


def test_layer2_handoff_fails_loud_on_missing_or_malformed_snapshot_fields():
    with pytest.raises(ValueError, match="schema_version must be"):
        build_layer2_handoff({})

    with pytest.raises(ValueError, match="missing handoff fields: \\['attention_targets'\\]"):
        build_layer2_handoff({
            "schema_version": ATTENTION_SNAPSHOT_SCHEMA_VERSION,
            "compact_text": "只有证据摘要。",
            "world_state": {},
            "hypotheses": [],
            "suggested_next_check_sec": 1200,
        })

    with pytest.raises(ValueError, match="attention_targets must be a text list"):
        build_layer2_handoff({
            "schema_version": ATTENTION_SNAPSHOT_SCHEMA_VERSION,
            "compact_text": "只有证据摘要。",
            "world_state": {},
            "hypotheses": [],
            "attention_targets": [""],
            "suggested_next_check_sec": 1200,
        })


def test_layer2_handoff_rejects_layer1_decision_fields():
    snapshot = _snapshot_for("late_night_unlock_not_sleeping")

    for field in FORBIDDEN_LAYER1_DECISION_FIELDS:
        with pytest.raises(ValueError, match=f"forbidden decision fields: \\['{field}'\\]"):
            build_layer2_handoff({
                **snapshot,
                field: True,
            })
