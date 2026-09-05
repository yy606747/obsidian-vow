import json
from pathlib import Path

import pytest

from app.sentinel.judgment_eval import (
    SENTINEL_JUDGMENT_EVAL_SCHEMA_VERSION,
    evaluate_judgment_case,
    evaluate_judgment_cases,
    evaluate_judgment_expectations,
)
from app.sentinel.eval import RUNTIME_MODE_DRY_RUN
from sentinel_judgment_eval import build_markdown, load_cases


ROOT = Path(__file__).resolve().parents[1]
ATTENTION_CASES_PATH = ROOT / "app" / "sentinel" / "eval_cases.json"
JUDGMENT_CASES_PATH = ROOT / "app" / "sentinel" / "judgment_eval_cases.json"


def _load_attention_cases():
    return json.loads(ATTENTION_CASES_PATH.read_text(encoding="utf-8"))


def _load_judgment_cases():
    return json.loads(JUDGMENT_CASES_PATH.read_text(encoding="utf-8"))


def test_judgment_eval_cases_pass_and_are_side_effect_free():
    result = evaluate_judgment_cases(
        _load_judgment_cases(),
        attention_cases=_load_attention_cases(),
    )

    assert result["schema_version"] == SENTINEL_JUDGMENT_EVAL_SCHEMA_VERSION
    assert result["runtime_mode"] == RUNTIME_MODE_DRY_RUN
    assert result["side_effects"] == []
    assert result["metrics"]["total"] >= 8
    assert result["metrics"]["failed"] == 0
    assert result["metrics"]["passed"] == result["metrics"]["total"]
    assert all(record["ok"] for record in result["records"])
    assert all(record["trace"]["side_effects"] == [] for record in result["records"])
    assert all(record["trace"]["fallback_used"] is False for record in result["records"])


def test_judgment_eval_prompt_never_receives_debug_trace():
    result = evaluate_judgment_cases(
        _load_judgment_cases(),
        attention_cases=_load_attention_cases(),
    )

    for record in result["records"]:
        prompt_text = "\n".join(message["content"] for message in record["trace"]["messages"])

        assert "debug_trace" not in prompt_text
        assert "feature_tags" not in prompt_text
        assert "source_records" not in prompt_text
        assert "raw_signal_count" not in prompt_text


def test_judgment_eval_fails_loud_on_missing_fixture_output():
    case = {
        "id": "missing_fixture",
        "category": "contract",
        "attention_case_id": "late_night_unlock_not_sleeping",
        "expect": {},
    }
    attention_cases = {case["id"]: case for case in _load_attention_cases()}

    record = evaluate_judgment_case(case, attention_cases_by_id=attention_cases)

    assert record["ok"] is False
    assert record["trace"]["judgment"] == {}
    assert record["failures"] == [
        "judgment_eval_failed: ValueError: judgment case missing_fixture requires fixture_model_output"
    ]


def test_judgment_eval_fails_loud_on_unknown_attention_case():
    with pytest.raises(ValueError, match="unknown attention_case_id"):
        evaluate_judgment_case(
            {
                "id": "bad_reference",
                "category": "contract",
                "attention_case_id": "missing_attention_case",
                "fixture_model_output": {},
                "expect": {},
            },
            attention_cases_by_id={},
        )


def test_judgment_expectations_support_semantic_contains_any():
    case = {
        "expect": {
            "restraint_reason_contains_any": ["忙碌声明优先", "明确说了要专心", "尊重她的专注边界"],
            "core_reason_empty": True,
        }
    }
    judgment = {
        "restraint_reason": "她明确说了要专心，我这时候出现只会显得不尊重她的节奏。",
        "core_reason": "",
    }

    assert evaluate_judgment_expectations(case, judgment, []) == []


def test_judgment_expectations_support_any_of_alternatives():
    case = {
        "expect": {
            "prompt_not_contains": ["debug_trace"],
            "any_of": [
                {"wake_intent": True, "core_reason_contains": ["关系未收束"]},
                {
                    "wake_intent": False,
                    "core_reason_empty": True,
                    "restraint_reason_contains_any": ["尊重边界", "冷却期"],
                },
            ],
        }
    }
    judgment = {
        "wake_intent": False,
        "core_reason": "",
        "restraint_reason": "先尊重边界，等冷却期过去再出现。",
    }

    assert evaluate_judgment_expectations(case, judgment, [{"content": "clean prompt"}]) == []


def test_judgment_expectations_support_cross_field_text_contains_any():
    case = {
        "expect": {
            "judgment_text_contains_any": ["作息承诺", "承诺风险窗口"],
        }
    }
    judgment = {
        "monitoringlog": "用户一小时前承诺今晚早睡。",
        "summary": "当前处于承诺风险窗口。",
        "core_reason": "需要温和提醒。",
        "restraint_reason": "",
        "uncertainty": "",
        "tone_hint": "",
    }

    assert evaluate_judgment_expectations(case, judgment, []) == []


def test_judgment_cli_helpers_load_cases_and_render_markdown():
    judgment_cases = load_cases(JUDGMENT_CASES_PATH)
    attention_cases = load_cases(ATTENTION_CASES_PATH)
    result = evaluate_judgment_cases(judgment_cases[:1], attention_cases=attention_cases)
    markdown = build_markdown(result)

    assert judgment_cases[0]["id"] == "judgment_sleep_uncertain_restrain"
    assert "# Sentinel Judgment Eval" in markdown
    assert "sentinel_judgment_eval.v1" in markdown
    assert "judgment_sleep_uncertain_restrain" in markdown
