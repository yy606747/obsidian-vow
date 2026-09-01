import json
from pathlib import Path

from app.sentinel.eval import (
    ATTENTION_SNAPSHOT_SCHEMA_VERSION,
    FORBIDDEN_LAYER1_DECISION_FIELDS,
    REQUIRED_LAYER1_DEBUG_TRACE_FIELDS,
    REQUIRED_LAYER1_SNAPSHOT_FIELDS,
    RUNTIME_MODE_DRY_RUN,
    attention_snapshot_builder,
    evaluate_case,
    evaluate_cases,
)


CASES_PATH = Path(__file__).resolve().parents[1] / "app" / "sentinel" / "eval_cases.json"


def _load_cases():
    return json.loads(CASES_PATH.read_text(encoding="utf-8"))


def _attention_result():
    return evaluate_cases(
        _load_cases(),
        snapshot_builder=attention_snapshot_builder,
        snapshot_builder_name="attention",
    )


def test_layer1_foundation_replay_corpus_is_closed():
    result = _attention_result()

    assert result["runtime_mode"] == RUNTIME_MODE_DRY_RUN
    assert result["snapshot_builder"] == "attention"
    assert result["side_effects"] == []
    assert result["metrics"]["total"] >= 20
    assert result["metrics"]["failed"] == 0
    assert result["metrics"]["passed"] == result["metrics"]["total"]
    assert {
        "anti_overclaim",
        "location_transition",
        "opportunity",
        "relationship",
        "restraint",
        "routine_risk",
        "source_missing",
    }.issubset(result["metrics"]["category_counts"])


def test_layer1_attention_snapshot_shape_is_stable_for_every_replay_case():
    cases = _load_cases()
    case_by_id = {case["id"]: case for case in cases}
    result = _attention_result()

    for record in result["records"]:
        snapshot = record["trace"]["snapshot"]
        debug_trace = snapshot["debug_trace"]
        feature_tags = debug_trace["feature_tags"]
        case = case_by_id[record["id"]]

        assert record["ok"] is True
        assert REQUIRED_LAYER1_SNAPSHOT_FIELDS.issubset(snapshot)
        assert REQUIRED_LAYER1_DEBUG_TRACE_FIELDS.issubset(debug_trace)
        assert FORBIDDEN_LAYER1_DECISION_FIELDS.isdisjoint(snapshot)
        assert snapshot["schema_version"] == ATTENTION_SNAPSHOT_SCHEMA_VERSION
        assert isinstance(snapshot["world_state"], dict)
        assert isinstance(snapshot["hypotheses"], list)
        assert isinstance(snapshot["attention_targets"], list)
        assert isinstance(snapshot["suggested_next_check_sec"], int)
        assert isinstance(feature_tags, list)
        assert debug_trace["raw_signal_count"] == len(case["input"]["raw_signals"])
        assert debug_trace["recent_chat_count"] == len(case["input"].get("recent_chat", []))
        if debug_trace["raw_signal_count"] > 0:
            assert any(tag.startswith("kind:") for tag in feature_tags)
            assert any(tag.startswith("source:") for tag in feature_tags)
            assert any(tag.startswith("source_family:") for tag in feature_tags)


def test_layer1_disabled_sources_stay_visible_without_claiming_evidence():
    cases = {case["id"]: case for case in _load_cases()}

    camera_record = evaluate_case(
        cases["camera_disabled_no_visual_claim"],
        snapshot_builder=attention_snapshot_builder,
    )
    pc_record = evaluate_case(
        cases["pc_source_disabled_no_work_claim"],
        snapshot_builder=attention_snapshot_builder,
    )

    camera_snapshot = camera_record["trace"]["snapshot"]
    pc_snapshot = pc_record["trace"]["snapshot"]

    assert camera_record["ok"] is True
    assert pc_record["ok"] is True
    assert "camera_disabled" in camera_snapshot["debug_trace"]["feature_tags"]
    assert "pc_disabled" in pc_snapshot["debug_trace"]["feature_tags"]
    assert "source_status:disabled" in camera_snapshot["debug_trace"]["feature_tags"]
    assert "source_status:disabled" in pc_snapshot["debug_trace"]["feature_tags"]
    assert "当前没有画面" in camera_snapshot["compact_text"]
    assert "当前没有电脑前台窗口" in pc_snapshot["compact_text"]
    assert "摄像头看到" not in camera_snapshot["compact_text"]
    assert "PC正在" not in pc_snapshot["compact_text"]


def test_layer1_input_decision_fields_fail_loud_before_snapshot_generation():
    base_case = next(case for case in _load_cases() if case["id"] == "late_night_unlock_not_sleeping")

    for field in FORBIDDEN_LAYER1_DECISION_FIELDS:
        bad_case = {
            **base_case,
            "input": {
                **base_case["input"],
                field: True,
            },
        }
        record = evaluate_case(bad_case, snapshot_builder=attention_snapshot_builder)

        assert record["ok"] is False
        assert record["trace"]["snapshot"] == {}
        assert record["failures"] == [
            f"snapshot_builder_failed: ValueError: input contains forbidden decision fields: ['{field}']"
        ]
