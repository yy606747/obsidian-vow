#!/usr/bin/env python3
"""Bounded CP1 integration smoke for the Working Model V2 gate.

Running without ``--execute-paid-run`` is read-only preflight.  Paid execution
requires a frozen approved authorization in the preregistration, validates all
hashes before the first call, writes a durable ``started`` ledger row before
each attempt, and never retries or replaces a failed/indeterminate case.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import math
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


CHAT_ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = CHAT_ROOT.parent
if str(CHAT_ROOT) not in sys.path:
    sys.path.insert(0, str(CHAT_ROOT))

from ai_providers import call_slot_chat  # noqa: E402
from config import get_slot  # noqa: E402
from app.working_model.gate import (  # noqa: E402
    WORKING_MODEL_GATE_PROMPT_VERSION,
    WORKING_MODEL_GATE_MAX_TOKENS,
    WORKING_MODEL_GATE_SLOT,
    WORKING_MODEL_GATE_TEMPERATURE,
    WORKING_MODEL_GATE_TIMEOUT_SEC,
    gate_prompt_sha256,
    run_working_model_gate,
)


ARTIFACT_ROOT = (
    REPO_ROOT / "docs/planning/checkpoints/working_model_v2/artifacts"
)
DEFAULT_PREREGISTRATION = ARTIFACT_ROOT / "CP1_PREREGISTRATION.json"
RESULTS_SCHEMA_VERSION = "working_model_gate_cp1_results.v1"
USAGE_KEYS = (
    "prompt_tokens",
    "completion_tokens",
    "total_tokens",
    "cache_read_tokens",
    "cache_write_tokens",
)


class SmokeContractError(RuntimeError):
    pass


def _now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def _sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _sha256_file(path: Path) -> str:
    return _sha256_bytes(path.read_bytes())


def _load_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise SmokeContractError(f"expected JSON object: {path}")
    return value


def _atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def _resolve_repo_path(value: object, *, field: str) -> Path:
    text = str(value or "")
    if not text or Path(text).is_absolute() or ".." in Path(text).parts:
        raise SmokeContractError(f"{field} must be a repository-relative path")
    path = (REPO_ROOT / text).resolve()
    if REPO_ROOT not in path.parents:
        raise SmokeContractError(f"{field} escapes repository root")
    return path


def _assert_hash(path: Path, expected: object, *, label: str) -> None:
    actual = _sha256_file(path)
    if actual != expected:
        raise SmokeContractError(
            f"{label} hash mismatch: expected {expected}, got {actual}"
        )


def _validate_preregistration(
    preregistration_path: Path,
    *,
    require_authorization: bool,
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any], Path]:
    prereg = _load_json(preregistration_path)
    if prereg.get("schema_version") != "working_model_gate_cp1_preregistration.v1":
        raise SmokeContractError("unknown preregistration schema")
    budget = prereg.get("budget") or {}
    if budget.get("case_count") != 20 or budget.get("max_provider_calls") != 20:
        raise SmokeContractError("CP1 budget must remain exactly 20 cases / max 20 calls")
    if budget.get("calls_per_case") != 1:
        raise SmokeContractError("CP1 must remain one provider call per case")
    if budget.get("retry_failed") is not False or budget.get("replace_failed") is not False:
        raise SmokeContractError("CP1 failures must not be retried or replaced")
    if prereg.get("temperature") != WORKING_MODEL_GATE_TEMPERATURE:
        raise SmokeContractError("runtime temperature differs from preregistration")
    if prereg.get("timeout_sec") != WORKING_MODEL_GATE_TIMEOUT_SEC:
        raise SmokeContractError("runtime timeout differs from preregistration")
    if prereg.get("max_tokens") != WORKING_MODEL_GATE_MAX_TOKENS:
        raise SmokeContractError("runtime max_tokens differs from preregistration")
    if prereg.get("prompt_version") != WORKING_MODEL_GATE_PROMPT_VERSION:
        raise SmokeContractError("runtime prompt version differs from preregistration")
    if prereg.get("prompt_sha256") != gate_prompt_sha256():
        raise SmokeContractError("runtime prompt text differs from preregistration")

    cases_path = _resolve_repo_path(prereg.get("cases_file"), field="cases_file")
    _assert_hash(
        cases_path,
        prereg.get("cases_sha256"),
        label="frozen cases",
    )
    cases_payload = _load_json(cases_path)
    cases = cases_payload.get("cases")
    if not isinstance(cases, list) or len(cases) != budget["case_count"]:
        raise SmokeContractError("frozen case count differs from preregistration")
    if len({case.get("id") for case in cases}) != len(cases):
        raise SmokeContractError("frozen case ids are not unique")
    if [case.get("order") for case in cases] != list(range(1, 21)):
        raise SmokeContractError("frozen cases are not in the exact registered order")

    implementation_files = prereg.get("implementation_files") or []
    if not implementation_files:
        raise SmokeContractError("implementation file hashes are missing")
    for entry in implementation_files:
        path = _resolve_repo_path(entry.get("path"), field="implementation_files.path")
        _assert_hash(path, entry.get("sha256"), label=str(entry.get("path")))

    slot = get_slot(WORKING_MODEL_GATE_SLOT)
    if slot is None:
        raise SmokeContractError("working_model_gate slot is not configured")
    if slot.get("model") != prereg.get("model"):
        raise SmokeContractError("configured gate model differs from preregistration")
    endpoint = slot.get("endpoint") or {}
    endpoint_contract = prereg.get("endpoint_contract") or {}
    if endpoint.get("id") != endpoint_contract.get("endpoint_id"):
        raise SmokeContractError("configured gate endpoint differs from preregistration")
    if endpoint.get("type") != endpoint_contract.get("endpoint_type"):
        raise SmokeContractError("configured gate endpoint type differs from preregistration")
    expected_host = str(endpoint_contract.get("base_url_host") or "")
    if expected_host not in str(endpoint.get("base_url") or ""):
        raise SmokeContractError("configured gate endpoint host differs from preregistration")
    if not endpoint.get("api_key"):
        raise SmokeContractError("configured gate endpoint has no usable credential")

    if require_authorization:
        authorization = prereg.get("authorization") or {}
        if authorization.get("status") != "approved":
            raise SmokeContractError("paid run has no frozen user authorization")
        if not str(authorization.get("verbatim_user_text") or "").strip():
            raise SmokeContractError("paid run authorization text is empty")
        if not str(authorization.get("approved_at") or "").strip():
            raise SmokeContractError("paid run authorization timestamp is empty")

    output_path = _resolve_repo_path(prereg.get("results_file"), field="results_file")
    return prereg, cases_payload, slot, output_path


def _nearest_rank(values: list[int], percentile: float) -> int | None:
    if not values:
        return None
    ordered = sorted(values)
    index = max(0, math.ceil(percentile * len(ordered)) - 1)
    return ordered[index]


def _usage_view(meta: dict[str, Any]) -> dict[str, int | None]:
    return {
        key: int(meta[key]) if isinstance(meta.get(key), (int, float)) else None
        for key in USAGE_KEYS
    }


def _estimated_cost_cny(
    usage: dict[str, int | None], pricing: dict[str, Any]
) -> float | None:
    prompt_tokens = usage.get("prompt_tokens")
    completion_tokens = usage.get("completion_tokens")
    if prompt_tokens is None or completion_tokens is None:
        return None
    cached_tokens = min(prompt_tokens, usage.get("cache_read_tokens") or 0)
    uncached_tokens = prompt_tokens - cached_tokens
    cost = (
        uncached_tokens * float(pricing["input_cny_per_million_tokens"])
        + cached_tokens * float(pricing["cache_hit_cny_per_million_tokens"])
        + completion_tokens * float(pricing["output_cny_per_million_tokens"])
    ) / 1_000_000
    return round(cost, 8)


def _summarize(rows: list[dict[str, Any]], pricing: dict[str, Any]) -> dict[str, Any]:
    completed = [row for row in rows if row.get("status") == "completed"]
    successful = [row for row in completed if not row.get("failure_code")]
    all_latencies = [int(row["latency_ms"]) for row in completed]
    successful_latencies = [int(row["latency_ms"]) for row in successful]
    failure_counts: dict[str, int] = {}
    for row in completed:
        failure_code = row.get("failure_code")
        if failure_code:
            failure_counts[str(failure_code)] = failure_counts.get(str(failure_code), 0) + 1
    known_costs = [
        float(row["estimated_cost_cny"])
        for row in completed
        if row.get("estimated_cost_cny") is not None
    ]
    route_counts: dict[str, int] = {}
    for row in completed:
        route = str(row.get("actual_route") or "")
        route_counts[route] = route_counts.get(route, 0) + 1
    return {
        "attempted_case_count": len(rows),
        "completed_case_count": len(completed),
        "indeterminate_started_case_count": sum(
            row.get("status") == "started" for row in rows
        ),
        "provider_call_upper_bound": len(rows),
        "successful_call_count": len(successful),
        "failure_counts": failure_counts,
        "actual_route_counts": route_counts,
        "expected_route_match_count": sum(
            row.get("route_matches_expected") is True for row in completed
        ),
        "latency_ms": {
            "all_completed": {
                "p50_nearest_rank": _nearest_rank(all_latencies, 0.50),
                "p95_nearest_rank": _nearest_rank(all_latencies, 0.95),
            },
            "successful": {
                "p50_nearest_rank": _nearest_rank(successful_latencies, 0.50),
                "p95_nearest_rank": _nearest_rank(successful_latencies, 0.95),
            },
        },
        "estimated_cost_cny": (
            round(sum(known_costs), 8) if len(known_costs) == len(completed) else None
        ),
        "costed_completed_call_count": len(known_costs),
        "pricing_snapshot": pricing,
        "interpretation": "integration smoke only; route comparison is not an accuracy claim",
    }


def _new_results(
    *,
    prereg: dict[str, Any],
    preregistration_path: Path,
) -> dict[str, Any]:
    return {
        "schema_version": RESULTS_SCHEMA_VERSION,
        "run_id": prereg["run_id"],
        "started_at": _now_iso(),
        "finished_at": None,
        "preregistration_file": str(preregistration_path.relative_to(REPO_ROOT)),
        "preregistration_sha256": _sha256_file(preregistration_path),
        "cases_file": prereg["cases_file"],
        "cases_sha256": prereg["cases_sha256"],
        "prompt_version": prereg["prompt_version"],
        "prompt_sha256": prereg["prompt_sha256"],
        "model": prereg["model"],
        "temperature": prereg["temperature"],
        "max_provider_calls": prereg["budget"]["max_provider_calls"],
        "rows": [],
        "summary": {},
    }


async def _execute(
    preregistration_path: Path,
    prereg: dict[str, Any],
    cases_payload: dict[str, Any],
    slot: dict[str, Any],
    output_path: Path,
) -> dict[str, Any]:
    if output_path.exists():
        results = _load_json(output_path)
        if results.get("schema_version") != RESULTS_SCHEMA_VERSION:
            raise SmokeContractError("unknown existing results schema")
        if results.get("run_id") != prereg.get("run_id"):
            raise SmokeContractError("existing results belong to another run")
        if results.get("preregistration_sha256") != _sha256_file(preregistration_path):
            raise SmokeContractError("preregistration changed after the run began")
    else:
        results = _new_results(
            prereg=prereg,
            preregistration_path=preregistration_path,
        )
        _atomic_write_json(output_path, results)

    rows = results["rows"]
    by_case_id = {row["case_id"]: row for row in rows}
    max_calls = int(prereg["budget"]["max_provider_calls"])
    pricing = prereg["pricing_snapshot"]

    for case in cases_payload["cases"]:
        case_id = case["id"]
        if case_id in by_case_id:
            continue
        if len(rows) >= max_calls:
            break

        row = {
            "attempt_number": len(rows) + 1,
            "case_id": case_id,
            "case_order": case["order"],
            "expected_route": case["expected_route"],
            "status": "started",
            "started_at": _now_iso(),
            "finished_at": None,
            "actual_route": None,
            "route_matches_expected": None,
            "gate_reason": None,
            "failure_code": None,
            "latency_ms": None,
            "model": prereg["model"],
            "prompt_version": prereg["prompt_version"],
            "usage": {key: None for key in USAGE_KEYS},
            "estimated_cost_cny": None,
        }
        rows.append(row)
        by_case_id[case_id] = row
        results["summary"] = _summarize(rows, pricing)
        _atomic_write_json(output_path, results)

        usage_meta: dict[str, Any] = {}

        async def paid_provider(messages: list[dict[str, str]]) -> str:
            return await call_slot_chat(
                WORKING_MODEL_GATE_SLOT,
                messages=messages,
                expect_json=True,
                timeout=WORKING_MODEL_GATE_TIMEOUT_SEC,
                temperature=WORKING_MODEL_GATE_TEMPERATURE,
                scope=f"working_model:gate:cp1:{case_id}",
                usage_meta=usage_meta,
                max_tokens=WORKING_MODEL_GATE_MAX_TOKENS,
            )

        result = await run_working_model_gate(
            statement=case["statement"],
            source=case["source"],
            latest_user_message=case["latest_user_message"],
            provider=paid_provider,
            model=slot["model"],
        )
        usage = _usage_view(usage_meta)
        row.update(
            {
                "status": "completed",
                "finished_at": _now_iso(),
                "actual_route": result.route,
                "route_matches_expected": (
                    result.failure_code is None
                    and result.route == case["expected_route"]
                ),
                "gate_reason": result.reason,
                "failure_code": result.failure_code,
                "latency_ms": result.latency_ms,
                "model": result.model,
                "prompt_version": result.prompt_version,
                "usage": usage,
                "estimated_cost_cny": _estimated_cost_cny(usage, pricing),
            }
        )
        results["summary"] = _summarize(rows, pricing)
        _atomic_write_json(output_path, results)

    results["summary"] = _summarize(rows, pricing)
    if len(rows) == len(cases_payload["cases"]) and all(
        row.get("status") in {"completed", "started"} for row in rows
    ):
        results["finished_at"] = _now_iso()
    _atomic_write_json(output_path, results)
    return results


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--preregistration",
        type=Path,
        default=DEFAULT_PREREGISTRATION,
    )
    parser.add_argument(
        "--execute-paid-run",
        action="store_true",
        help="perform the preregistered provider calls; absent means read-only preflight",
    )
    args = parser.parse_args()
    preregistration_path = args.preregistration.resolve()
    prereg, cases_payload, slot, output_path = _validate_preregistration(
        preregistration_path,
        require_authorization=args.execute_paid_run,
    )
    if not args.execute_paid_run:
        print(
            json.dumps(
                {
                    "ok": True,
                    "mode": "read_only_preflight",
                    "run_id": prereg["run_id"],
                    "case_count": len(cases_payload["cases"]),
                    "max_provider_calls": prereg["budget"]["max_provider_calls"],
                    "model": slot["model"],
                    "endpoint_id": slot["endpoint"].get("id"),
                    "authorization_status": (prereg.get("authorization") or {}).get(
                        "status"
                    ),
                    "provider_calls_made": 0,
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0

    started = time.monotonic()
    results = asyncio.run(
        _execute(
            preregistration_path,
            prereg,
            cases_payload,
            slot,
            output_path,
        )
    )
    print(
        json.dumps(
            {
                "ok": True,
                "run_id": results["run_id"],
                "results_file": str(output_path.relative_to(REPO_ROOT)),
                "wall_time_sec": round(time.monotonic() - started, 3),
                "summary": results["summary"],
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except SmokeContractError as exc:
        print(f"CP1 smoke contract error: {exc}", file=sys.stderr)
        raise SystemExit(2)
