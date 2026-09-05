"""Offline replay/eval contracts for Sentinel Judgment layer."""

from __future__ import annotations

import json
from collections import Counter, defaultdict
from collections.abc import Mapping, Sequence
from typing import Any

from .eval import RUNTIME_MODE_DRY_RUN, SnapshotBuilder, attention_snapshot_builder
from .handoff import build_layer2_handoff
from .judgment import (
    SENTINEL_JUDGMENT_SCHEMA_VERSION,
    build_sentinel_judgment_messages,
    parse_sentinel_judgment,
)


SENTINEL_JUDGMENT_EVAL_SCHEMA_VERSION = "sentinel_judgment_eval.v1"
DEFAULT_SENTINEL_JUDGMENT_TRACE_DIR = "data/sentinel_judgment_traces"


def evaluate_judgment_case(
    case: Mapping[str, Any],
    *,
    attention_cases_by_id: Mapping[str, Mapping[str, Any]],
    snapshot_builder: SnapshotBuilder = attention_snapshot_builder,
) -> dict[str, Any]:
    case_id = _required_text(case, "id", subject="judgment case")
    category = str(case.get("category") or "uncategorized")
    attention_case_id = _required_text(case, "attention_case_id", subject=f"judgment case {case_id}")
    if attention_case_id not in attention_cases_by_id:
        raise ValueError(f"judgment case {case_id} references unknown attention_case_id {attention_case_id!r}")
    if not isinstance(case.get("expect"), Mapping):
        raise ValueError(f"judgment case {case_id} requires expect object")

    failures: list[str] = []
    snapshot: Mapping[str, Any] | None = None
    handoff: Mapping[str, Any] | None = None
    messages: list[dict[str, str]] = []
    judgment: Mapping[str, Any] | None = None

    try:
        snapshot = snapshot_builder(attention_cases_by_id[attention_case_id])
        handoff = build_layer2_handoff(snapshot)
        context = case.get("context", {})
        if context is None:
            context = {}
        if not isinstance(context, Mapping):
            raise ValueError(f"judgment case {case_id} context must be an object")
        messages = build_sentinel_judgment_messages(handoff, context=context)
        raw_output = _fixture_output_text(case)
        judgment = parse_sentinel_judgment(raw_output)
    except Exception as exc:
        failures.append(f"judgment_eval_failed: {type(exc).__name__}: {exc}")

    if judgment is not None:
        failures.extend(evaluate_judgment_expectations(case, judgment, messages))

    return {
        "id": case_id,
        "category": category,
        "attention_case_id": attention_case_id,
        "ok": not failures,
        "failures": failures,
        "actual": actual_judgment_payload(judgment),
        "trace": {
            "trace_id": f"sentinel_judgment_replay:{case_id}",
            "runtime_mode": RUNTIME_MODE_DRY_RUN,
            "side_effects": [],
            "fallback_used": False,
            "fallback_reason": "",
            "attention_snapshot": dict(snapshot or {}),
            "handoff": dict(handoff or {}),
            "messages": messages,
            "judgment": dict(judgment or {}),
            "failures": failures,
        },
    }


def evaluate_judgment_cases(
    cases: Sequence[Mapping[str, Any]],
    *,
    attention_cases: Sequence[Mapping[str, Any]],
    snapshot_builder: SnapshotBuilder = attention_snapshot_builder,
    trace_dir: str = DEFAULT_SENTINEL_JUDGMENT_TRACE_DIR,
) -> dict[str, Any]:
    attention_cases_by_id = {
        _required_text(case, "id", subject="attention case"): case
        for case in attention_cases
    }
    records = [
        evaluate_judgment_case(
            case,
            attention_cases_by_id=attention_cases_by_id,
            snapshot_builder=snapshot_builder,
        )
        for case in cases
    ]
    total = len(records)
    passed = sum(1 for record in records if record["ok"])
    by_category = defaultdict(lambda: {"total": 0, "passed": 0, "failed": 0})
    for record in records:
        bucket = by_category[record["category"]]
        bucket["total"] += 1
        if record["ok"]:
            bucket["passed"] += 1
        else:
            bucket["failed"] += 1

    return {
        "schema_version": SENTINEL_JUDGMENT_EVAL_SCHEMA_VERSION,
        "runtime_mode": RUNTIME_MODE_DRY_RUN,
        "judgment_schema_version": SENTINEL_JUDGMENT_SCHEMA_VERSION,
        "trace_dir": str(trace_dir),
        "side_effects": [],
        "metrics": {
            "total": total,
            "passed": passed,
            "failed": total - passed,
            "pass_rate": round(passed / total, 4) if total else 0.0,
            "by_category": dict(by_category),
            "category_counts": dict(Counter(record["category"] for record in records)),
        },
        "records": records,
    }


def _fixture_output_text(case: Mapping[str, Any]) -> str:
    output = case.get("fixture_model_output")
    if isinstance(output, str):
        return output
    if isinstance(output, Mapping):
        return json.dumps(output, ensure_ascii=False)
    raise ValueError(f"judgment case {case.get('id') or '<unknown>'} requires fixture_model_output")


def evaluate_judgment_expectations(
    case: Mapping[str, Any],
    judgment: Mapping[str, Any],
    messages: Sequence[Mapping[str, str]],
) -> list[str]:
    """Evaluate a parsed judgment against the conservative local replay expectations."""
    expect = case["expect"]
    if not isinstance(expect, Mapping):
        return ["expect must be an object"]
    alternatives = expect.get("any_of")
    common_expect = {key: value for key, value in expect.items() if key != "any_of"}
    if alternatives:
        common_failures = _evaluate_expectation_block(common_expect, judgment, messages)
        if common_failures:
            return common_failures
        alternative_failures = []
        for index, alternative in enumerate(_as_list(alternatives), start=1):
            if not isinstance(alternative, Mapping):
                alternative_failures.append(f"alternative {index} is not an object")
                continue
            failures = _evaluate_expectation_block(
                {**common_expect, **dict(alternative)},
                judgment,
                messages,
            )
            if not failures:
                return []
            alternative_failures.append(f"alternative {index}: {'; '.join(failures)}")
        return ["no expectation alternative matched", *alternative_failures]
    return _evaluate_expectation_block(common_expect, judgment, messages)


def _evaluate_expectation_block(
    expect: Mapping[str, Any],
    judgment: Mapping[str, Any],
    messages: Sequence[Mapping[str, str]],
) -> list[str]:
    failures: list[str] = []
    if "wake_intent" in expect and judgment.get("wake_intent") is not expect["wake_intent"]:
        failures.append(f"wake_intent: expected {expect['wake_intent']!r}, got {judgment.get('wake_intent')!r}")
    if "call_core" in expect and judgment.get("call_core") is not expect["call_core"]:
        failures.append(f"call_core: expected {expect['call_core']!r}, got {judgment.get('call_core')!r}")

    score = judgment.get("score")
    if "score_min" in expect and score < expect["score_min"]:
        failures.append(f"score below min {expect['score_min']!r}: {score!r}")
    if "score_max" in expect and score > expect["score_max"]:
        failures.append(f"score above max {expect['score_max']!r}: {score!r}")
    confidence = judgment.get("confidence")
    if "confidence_min" in expect and confidence < expect["confidence_min"]:
        failures.append(f"confidence below min {expect['confidence_min']!r}: {confidence!r}")

    text_fields = ("monitoringlog", "summary", "core_reason", "restraint_reason", "uncertainty", "tone_hint")
    judgment_text = "\n".join(str(judgment.get(field) or "") for field in text_fields)
    for expected in _as_list(expect.get("judgment_text_contains")):
        if expected not in judgment_text:
            failures.append(f"judgment_text missing {expected!r}")
    judgment_contains_any = _as_list(expect.get("judgment_text_contains_any"))
    if judgment_contains_any and not any(expected in judgment_text for expected in judgment_contains_any):
        failures.append(f"judgment_text missing any of {judgment_contains_any!r}")

    for field in text_fields:
        text = str(judgment.get(field) or "")
        for expected in _as_list(expect.get(f"{field}_contains")):
            if expected not in text:
                failures.append(f"{field} missing {expected!r}")
        contains_any = _as_list(expect.get(f"{field}_contains_any"))
        if contains_any and not any(expected in text for expected in contains_any):
            failures.append(f"{field} missing any of {contains_any!r}")
        if expect.get(f"{field}_empty") is True and text.strip():
            failures.append(f"{field} expected empty")

    prompt_text = "\n".join(message.get("content", "") for message in messages)
    for forbidden in _as_list(expect.get("prompt_not_contains")):
        if forbidden in prompt_text:
            failures.append(f"prompt contains forbidden {forbidden!r}")
    return failures


def actual_judgment_payload(judgment: Mapping[str, Any] | None) -> dict[str, Any]:
    """Return the compact judgment fields used in eval reports."""
    if judgment is None:
        return {
            "wake_intent": None,
            "call_core": None,
            "score": None,
            "confidence": None,
        }
    return {
        "wake_intent": judgment.get("wake_intent"),
        "call_core": judgment.get("call_core"),
        "score": judgment.get("score"),
        "confidence": judgment.get("confidence"),
        "tone_hint": judgment.get("tone_hint"),
    }


def _as_list(value: Any) -> list:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    if isinstance(value, tuple):
        return list(value)
    return [value]


def _required_text(case: Mapping[str, Any], key: str, *, subject: str) -> str:
    value = str(case.get(key) or "").strip()
    if not value:
        raise ValueError(f"{subject} requires {key}")
    return value


__all__ = [
    "DEFAULT_SENTINEL_JUDGMENT_TRACE_DIR",
    "SENTINEL_JUDGMENT_EVAL_SCHEMA_VERSION",
    "actual_judgment_payload",
    "evaluate_judgment_case",
    "evaluate_judgment_cases",
    "evaluate_judgment_expectations",
]
