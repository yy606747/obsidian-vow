"""Offline replay/eval contracts for the future Sentinel chain.

This module is intentionally pure: no file I/O, no network, no database, no
Core calls, and no websocket broadcasts. Callers provide cases and decide where
to persist traces.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from collections.abc import Callable, Mapping, Sequence
from typing import Any

from .attention import ATTENTION_SNAPSHOT_SCHEMA_VERSION, build_attention_snapshot_from_case


SENTINEL_REPLAY_EVAL_SCHEMA_VERSION = "sentinel_replay_eval.v1"
DEFAULT_SENTINEL_REPLAY_TRACE_DIR = "data/sentinel_replay_traces"
RUNTIME_MODE_DRY_RUN = "dry_run"

FORBIDDEN_LAYER1_DECISION_FIELDS = frozenset({
    "call_core",
    "core_reason",
    "score",
    "wake_intent",
})
REQUIRED_LAYER1_SNAPSHOT_FIELDS = frozenset({
    "attention_targets",
    "compact_text",
    "debug_trace",
    "hypotheses",
    "schema_version",
    "suggested_next_check_sec",
    "world_state",
})
REQUIRED_LAYER1_DEBUG_TRACE_FIELDS = frozenset({
    "against",
    "missing",
    "source_records",
    "support",
})

SnapshotBuilder = Callable[[Mapping[str, Any]], Mapping[str, Any]]
attention_snapshot_builder = build_attention_snapshot_from_case


def fixture_snapshot_builder(case: Mapping[str, Any]) -> Mapping[str, Any]:
    """Return the hand-authored fixture snapshot for initial runner smoke tests."""
    snapshot = case.get("fixture_snapshot")
    if not isinstance(snapshot, Mapping):
        raise ValueError(f"case {case.get('id') or '<unknown>'} requires fixture_snapshot")
    return snapshot


def evaluate_case(
    case: Mapping[str, Any],
    *,
    snapshot_builder: SnapshotBuilder = fixture_snapshot_builder,
) -> dict[str, Any]:
    """Evaluate one replay case against an Attention snapshot builder."""
    case_id = _required_text(case, "id", subject="case")
    category = str(case.get("category") or "uncategorized")
    if not isinstance(case.get("input"), Mapping):
        raise ValueError(f"case {case_id} requires input object")
    if not isinstance(case.get("expect"), Mapping):
        raise ValueError(f"case {case_id} requires expect object")

    failures: list[str] = []
    snapshot: Mapping[str, Any] | None = None
    try:
        snapshot = snapshot_builder(case)
    except Exception as exc:
        failures.append(f"snapshot_builder_failed: {type(exc).__name__}: {exc}")

    if snapshot is not None:
        failures.extend(_validate_snapshot(snapshot))
        failures.extend(_evaluate_expectations(case, snapshot))

    trace = _build_trace(case_id=case_id, snapshot=snapshot, failures=failures)
    return {
        "id": case_id,
        "category": category,
        "ok": not failures,
        "failures": failures,
        "actual": _actual_payload(snapshot),
        "trace": trace,
    }


def evaluate_cases(
    cases: Sequence[Mapping[str, Any]],
    *,
    snapshot_builder: SnapshotBuilder = fixture_snapshot_builder,
    trace_dir: str = DEFAULT_SENTINEL_REPLAY_TRACE_DIR,
    snapshot_builder_name: str = "",
) -> dict[str, Any]:
    """Evaluate a batch of replay cases and return a serializable dry-run report."""
    records = [
        evaluate_case(case, snapshot_builder=snapshot_builder)
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

    categories = Counter(record["category"] for record in records)
    return {
        "schema_version": SENTINEL_REPLAY_EVAL_SCHEMA_VERSION,
        "runtime_mode": RUNTIME_MODE_DRY_RUN,
        "snapshot_builder": snapshot_builder_name or _snapshot_builder_name(snapshot_builder),
        "trace_dir": str(trace_dir),
        "side_effects": [],
        "metrics": {
            "total": total,
            "passed": passed,
            "failed": total - passed,
            "pass_rate": round(passed / total, 4) if total else 0.0,
            "by_category": dict(by_category),
            "category_counts": dict(categories),
        },
        "records": records,
    }


def _validate_snapshot(snapshot: Mapping[str, Any]) -> list[str]:
    failures: list[str] = []
    schema_version = snapshot.get("schema_version")
    if schema_version != ATTENTION_SNAPSHOT_SCHEMA_VERSION:
        failures.append(
            f"schema_version: expected {ATTENTION_SNAPSHOT_SCHEMA_VERSION!r}, got {schema_version!r}"
        )
    if not isinstance(snapshot.get("compact_text"), str) or not snapshot.get("compact_text", "").strip():
        failures.append("compact_text is required")
    if not isinstance(snapshot.get("world_state"), Mapping):
        failures.append("world_state object is required")
    hypotheses = snapshot.get("hypotheses")
    if not isinstance(hypotheses, list):
        failures.append("hypotheses list is required")
    else:
        failures.extend(_validate_hypotheses(hypotheses))
    if not _is_text_list(snapshot.get("attention_targets")):
        failures.append("attention_targets text list is required")
    suggested_next_check_sec = snapshot.get("suggested_next_check_sec")
    if isinstance(suggested_next_check_sec, bool) or not isinstance(suggested_next_check_sec, int):
        failures.append("suggested_next_check_sec integer is required")
    debug_trace = snapshot.get("debug_trace")
    if not isinstance(debug_trace, Mapping):
        failures.append("debug_trace object is required")
    else:
        missing_trace_fields = sorted(REQUIRED_LAYER1_DEBUG_TRACE_FIELDS.difference(debug_trace.keys()))
        if missing_trace_fields:
            failures.append(f"debug_trace missing required fields: {missing_trace_fields!r}")
        for key in sorted(REQUIRED_LAYER1_DEBUG_TRACE_FIELDS.intersection(debug_trace.keys())):
            if not _is_text_list(debug_trace.get(key)):
                failures.append(f"debug_trace.{key} text list is required")
    forbidden_present = sorted(FORBIDDEN_LAYER1_DECISION_FIELDS.intersection(snapshot.keys()))
    if forbidden_present:
        failures.append(f"layer1_forbidden_decision_fields: {forbidden_present!r}")
    return failures


def _validate_hypotheses(hypotheses: list[Any]) -> list[str]:
    failures: list[str] = []
    for index, item in enumerate(hypotheses):
        if not isinstance(item, Mapping):
            failures.append(f"hypotheses[{index}] object is required")
            continue
        label = item.get("label")
        if not isinstance(label, str) or not label.strip():
            failures.append(f"hypotheses[{index}].label is required")
        confidence = item.get("confidence")
        if not _is_optional_unit_number(confidence):
            failures.append(f"hypotheses[{index}].confidence must be 0.0-1.0")
        for key in ("support", "against", "missing"):
            if not _is_text_list(item.get(key)):
                failures.append(f"hypotheses[{index}].{key} text list is required")
    return failures


def _evaluate_expectations(case: Mapping[str, Any], snapshot: Mapping[str, Any]) -> list[str]:
    expect = case["expect"]
    compact_text = snapshot.get("compact_text") or ""
    debug_trace = snapshot.get("debug_trace") or {}
    failures: list[str] = []

    for expected in _as_list(expect.get("compact_text_contains")):
        if expected not in compact_text:
            failures.append(f"compact_text missing {expected!r}")
    for forbidden in _as_list(expect.get("compact_text_not_contains")):
        if forbidden in compact_text:
            failures.append(f"compact_text contains forbidden {forbidden!r}")

    attention_targets = _as_list(snapshot.get("attention_targets"))
    for expected in _as_list(expect.get("attention_targets_include")):
        if expected not in attention_targets:
            failures.append(f"attention_targets missing {expected!r}")
    for forbidden in _as_list(expect.get("attention_targets_exclude")):
        if forbidden in attention_targets:
            failures.append(f"attention_targets contains forbidden {forbidden!r}")

    hypothesis_labels = [
        item.get("label")
        for item in _as_list(snapshot.get("hypotheses"))
        if isinstance(item, Mapping)
    ]
    for expected in _as_list(expect.get("hypothesis_labels_include")):
        if expected not in hypothesis_labels:
            failures.append(f"hypotheses missing label {expected!r}")

    for key in _as_list(expect.get("debug_trace_keys")):
        if key not in debug_trace:
            failures.append(f"debug_trace missing key {key!r}")
    return failures


def _build_trace(
    *,
    case_id: str,
    snapshot: Mapping[str, Any] | None,
    failures: Sequence[str],
) -> dict[str, Any]:
    return {
        "trace_id": f"sentinel_replay:{case_id}",
        "case_id": case_id,
        "runtime_mode": RUNTIME_MODE_DRY_RUN,
        "fallback_used": False,
        "fallback_reason": "",
        "side_effects": [],
        "failures": list(failures),
        "snapshot": dict(snapshot or {}),
    }


def _actual_payload(snapshot: Mapping[str, Any] | None) -> dict[str, Any]:
    if snapshot is None:
        return {
            "compact_text": "",
            "attention_targets": [],
            "hypothesis_labels": [],
            "forbidden_decision_fields_present": [],
        }
    return {
        "compact_text": snapshot.get("compact_text") or "",
        "attention_targets": _as_list(snapshot.get("attention_targets")),
        "hypothesis_labels": [
            item.get("label")
            for item in _as_list(snapshot.get("hypotheses"))
            if isinstance(item, Mapping)
        ],
        "forbidden_decision_fields_present": sorted(
            FORBIDDEN_LAYER1_DECISION_FIELDS.intersection(snapshot.keys())
        ),
    }


def _as_list(value: Any) -> list:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    if isinstance(value, tuple):
        return list(value)
    return [value]


def _is_text_list(value: Any) -> bool:
    return isinstance(value, list) and all(
        isinstance(item, str) and item.strip()
        for item in value
    )


def _is_optional_unit_number(value: Any) -> bool:
    if value is None:
        return True
    if isinstance(value, bool) or not isinstance(value, int | float):
        return False
    return 0.0 <= float(value) <= 1.0


def _required_text(case: Mapping[str, Any], key: str, *, subject: str) -> str:
    value = str(case.get(key) or "").strip()
    if not value:
        raise ValueError(f"{subject} requires {key}")
    return value


def _snapshot_builder_name(snapshot_builder: SnapshotBuilder) -> str:
    return str(getattr(snapshot_builder, "__name__", "snapshot_builder"))


__all__ = [
    "ATTENTION_SNAPSHOT_SCHEMA_VERSION",
    "DEFAULT_SENTINEL_REPLAY_TRACE_DIR",
    "FORBIDDEN_LAYER1_DECISION_FIELDS",
    "REQUIRED_LAYER1_DEBUG_TRACE_FIELDS",
    "REQUIRED_LAYER1_SNAPSHOT_FIELDS",
    "RUNTIME_MODE_DRY_RUN",
    "SENTINEL_REPLAY_EVAL_SCHEMA_VERSION",
    "SnapshotBuilder",
    "attention_snapshot_builder",
    "evaluate_case",
    "evaluate_cases",
    "fixture_snapshot_builder",
]
