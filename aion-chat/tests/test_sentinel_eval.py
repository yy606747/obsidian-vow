import json
from pathlib import Path

import pytest

from app.sentinel.eval import (
    ATTENTION_SNAPSHOT_SCHEMA_VERSION,
    DEFAULT_SENTINEL_REPLAY_TRACE_DIR,
    RUNTIME_MODE_DRY_RUN,
    SENTINEL_REPLAY_EVAL_SCHEMA_VERSION,
    attention_snapshot_builder,
    evaluate_case,
    evaluate_cases,
)
from sentinel_replay_eval import build_markdown, load_cases


CASES_PATH = Path(__file__).resolve().parents[1] / "app" / "sentinel" / "eval_cases.json"


def _load_cases():
    return json.loads(CASES_PATH.read_text(encoding="utf-8"))


def test_fixture_eval_cases_pass_and_are_side_effect_free():
    result = evaluate_cases(_load_cases())

    assert result["schema_version"] == SENTINEL_REPLAY_EVAL_SCHEMA_VERSION
    assert result["runtime_mode"] == RUNTIME_MODE_DRY_RUN
    assert result["snapshot_builder"] == "fixture_snapshot_builder"
    assert result["trace_dir"] == DEFAULT_SENTINEL_REPLAY_TRACE_DIR
    assert result["side_effects"] == []
    assert result["metrics"]["total"] >= 20
    assert result["metrics"]["failed"] == 0
    assert result["metrics"]["passed"] == result["metrics"]["total"]
    assert all(record["ok"] for record in result["records"])
    assert all(record["trace"]["fallback_used"] is False for record in result["records"])
    assert all(record["trace"]["side_effects"] == [] for record in result["records"])


def test_attention_builder_eval_cases_pass_and_are_side_effect_free():
    result = evaluate_cases(
        _load_cases(),
        snapshot_builder=attention_snapshot_builder,
        snapshot_builder_name="attention",
    )

    assert result["schema_version"] == SENTINEL_REPLAY_EVAL_SCHEMA_VERSION
    assert result["runtime_mode"] == RUNTIME_MODE_DRY_RUN
    assert result["snapshot_builder"] == "attention"
    assert result["side_effects"] == []
    assert result["metrics"]["total"] >= 20
    assert result["metrics"]["failed"] == 0
    assert result["metrics"]["passed"] == result["metrics"]["total"]
    assert all(record["ok"] for record in result["records"])
    assert all(record["trace"]["fallback_used"] is False for record in result["records"])
    assert all(record["trace"]["side_effects"] == [] for record in result["records"])


def test_attention_builder_exposes_normalized_feature_tags():
    case = next(
        item
        for item in _load_cases()
        if item["id"] == "source_conflict_active_vs_busy"
    )

    record = evaluate_case(case, snapshot_builder=attention_snapshot_builder)
    feature_tags = record["trace"]["snapshot"]["debug_trace"]["feature_tags"]

    assert record["ok"] is True
    assert "busy_declared" in feature_tags
    assert "phone_active" in feature_tags
    assert "conflict_busy_active" in feature_tags
    assert record["actual"]["attention_targets"] == ["conflict_busy_active", "restraint_busy"]


def test_eval_case_requires_contract_fields():
    with pytest.raises(ValueError, match="case requires id"):
        evaluate_case({})

    with pytest.raises(ValueError, match="requires input object"):
        evaluate_case({"id": "missing_input", "expect": {}})

    with pytest.raises(ValueError, match="requires expect object"):
        evaluate_case({"id": "missing_expect", "input": {}})


def test_eval_case_fails_loud_when_snapshot_builder_fails():
    case = {
        "id": "builder_failure",
        "category": "contract",
        "input": {},
        "expect": {},
    }

    record = evaluate_case(case)

    assert record["ok"] is False
    assert record["failures"] == [
        "snapshot_builder_failed: ValueError: case builder_failure requires fixture_snapshot"
    ]
    assert record["trace"]["failures"] == record["failures"]


def test_attention_builder_fails_loud_on_malformed_raw_signal():
    case = {
        "id": "bad_attention_input",
        "category": "contract",
        "input": {
            "raw_signals": [
                {"kind": "sensing.screen", "source": "android.sensing"},
            ],
            "recent_chat": [],
        },
        "expect": {},
    }

    record = evaluate_case(case, snapshot_builder=attention_snapshot_builder)

    assert record["ok"] is False
    assert record["failures"] == [
        "snapshot_builder_failed: ValueError: raw_signals[0] requires text"
    ]
    assert record["trace"]["snapshot"] == {}


def test_layer1_snapshot_cannot_emit_decision_fields():
    case = {
        "id": "forbidden_decision_field",
        "category": "contract",
        "input": {},
        "fixture_snapshot": {
            "schema_version": ATTENTION_SNAPSHOT_SCHEMA_VERSION,
            "compact_text": "只有证据摘要。",
            "debug_trace": {},
            "wake_intent": True,
        },
        "expect": {},
    }

    record = evaluate_case(case)

    assert record["ok"] is False
    assert record["actual"]["forbidden_decision_fields_present"] == ["wake_intent"]
    assert "layer1_forbidden_decision_fields: ['wake_intent']" in record["failures"]


def test_custom_snapshot_builder_can_be_injected():
    case = {
        "id": "custom_builder",
        "category": "contract",
        "input": {"raw_signals": []},
        "expect": {
            "compact_text_contains": ["没有可用证据"],
            "debug_trace_keys": ["source_records", "support", "against", "missing"],
        },
    }

    def builder(_case):
        return {
            "schema_version": ATTENTION_SNAPSHOT_SCHEMA_VERSION,
            "compact_text": "没有可用证据，不能推断用户状态。",
            "world_state": {"availability": "unknown"},
            "hypotheses": [],
            "attention_targets": [],
            "suggested_next_check_sec": 1200,
            "debug_trace": {
                "source_records": [],
                "support": [],
                "against": [],
                "missing": ["sensing", "activity", "location"],
            },
        }

    record = evaluate_case(case, snapshot_builder=builder)

    assert record["ok"] is True
    assert record["trace"]["runtime_mode"] == RUNTIME_MODE_DRY_RUN
    assert record["trace"]["snapshot"]["compact_text"] == "没有可用证据，不能推断用户状态。"


def test_batch_report_preserves_trace_dir_override():
    result = evaluate_cases(_load_cases()[:1], trace_dir="/tmp/sentinel-traces")

    assert result["trace_dir"] == "/tmp/sentinel-traces"
    assert result["metrics"]["total"] == 1
    assert result["records"][0]["trace"]["trace_id"].startswith("sentinel_replay:")


def test_cli_helpers_load_cases_and_render_markdown():
    cases = load_cases(CASES_PATH)
    result = evaluate_cases(
        cases[:1],
        snapshot_builder=attention_snapshot_builder,
        snapshot_builder_name="attention",
    )
    markdown = build_markdown(result)

    assert cases[0]["id"] == "late_night_unlock_not_sleeping"
    assert "# Sentinel Replay Eval" in markdown
    assert "late_night_unlock_not_sleeping" in markdown
    assert "runtime_mode: `dry_run`" in markdown
    assert "snapshot_builder: `attention`" in markdown
