"""Live SiliconFlow dry-run harness for the new Sentinel chain.

This CLI calls the Sentinel judgment model, then runs Gate and wake-package
dry-run locally. It never calls Core, writes runtime logs, broadcasts, TTS, or
device effects.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import time
from collections.abc import Awaitable, Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

import httpx

from app.sentinel import (
    DEFAULT_SENTINEL_JUDGMENT_TRACE_DIR,
    actual_judgment_payload,
    evaluate_judgment_expectations,
    run_sentinel_chain_dry_run,
)
from config import MODELS, get_endpoint, get_key, get_slot
from sentinel_judgment_eval import (
    DEFAULT_ATTENTION_CASES_PATH,
    DEFAULT_JUDGMENT_CASES_PATH,
    load_cases,
)


SENTINEL_SILICONFLOW_DRY_RUN_SCHEMA_VERSION = "sentinel_siliconflow_dry_run.v1"
RUNTIME_MODE_PROVIDER_DRY_RUN = "provider_dry_run"
DEFAULT_SILICONFLOW_BASE_URL = "https://api.siliconflow.cn/v1"
DEFAULT_SILICONFLOW_MODEL = MODELS.get("Kimi-K2.5", {}).get("model", "Pro/moonshotai/Kimi-K2.5")
DEFAULT_TIMEOUT_SEC = 120.0
DEFAULT_TEMPERATURE = 0.2

LiveProvider = Callable[[list[dict[str, str]]], Awaitable[str] | str]

_REDACT_PATTERNS = (
    (re.compile(r"(Bearer\s+)[A-Za-z0-9_\-\.]{6,}", re.I), r"\1***"),
    (re.compile(r"(sk-[A-Za-z0-9_\-]{4})[A-Za-z0-9_\-]{4,}"), r"\1***"),
)


def resolve_siliconflow_endpoint(
    *,
    model: str = "",
    base_url: str = "",
    endpoint_id: str = "",
    slot_name: str = "",
) -> dict[str, Any]:
    """Resolve a SiliconFlow/OpenAI-compatible endpoint without exposing the key."""
    resolved_model = model.strip()
    endpoint: dict[str, Any] | None = None
    source = "siliconflow_default"

    if slot_name:
        slot = get_slot(slot_name)
        if not slot:
            raise ValueError(f"slot {slot_name!r} is not configured")
        endpoint = dict(slot["endpoint"])
        resolved_model = resolved_model or str(slot.get("model") or "").strip()
        source = f"slot:{slot_name}"
    elif endpoint_id:
        endpoint = get_endpoint(endpoint_id)
        if not endpoint:
            raise ValueError(f"endpoint {endpoint_id!r} is not configured")
        endpoint = dict(endpoint)
        source = f"endpoint:{endpoint_id}"

    if endpoint is None:
        api_key, key_source = _resolve_siliconflow_key()
        endpoint = {
            "id": "preset_siliconflow",
            "name": "硅基流动",
            "type": "openai",
            "base_url": base_url.strip() or DEFAULT_SILICONFLOW_BASE_URL,
            "api_key": api_key,
        }
        source = f"siliconflow_default:{key_source}"
    else:
        endpoint.setdefault("type", "openai")
        endpoint["base_url"] = base_url.strip() or str(endpoint.get("base_url") or "").strip()
        if not endpoint.get("base_url"):
            raise ValueError(f"{source} missing base_url")
        if not endpoint.get("api_key"):
            api_key, key_source = _resolve_siliconflow_key()
            endpoint["api_key"] = api_key
            source = f"{source}:{key_source}"

    if endpoint.get("type") != "openai":
        raise ValueError(f"{source} must be an OpenAI-compatible endpoint")
    if not endpoint.get("api_key"):
        raise ValueError("missing SiliconFlow API key: set AION_SILICONFLOW_KEY or data/settings.json siliconflow_key")

    resolved_model = resolved_model or DEFAULT_SILICONFLOW_MODEL
    return {
        "source": source,
        "endpoint": endpoint,
        "model": resolved_model,
        "public": {
            "source": source,
            "endpoint_id": endpoint.get("id") or "",
            "endpoint_name": endpoint.get("name") or "",
            "type": endpoint.get("type") or "",
            "base_url": endpoint.get("base_url") or "",
            "model": resolved_model,
            "has_api_key": bool(endpoint.get("api_key")),
        },
    }


def build_siliconflow_provider(
    endpoint: Mapping[str, Any],
    *,
    model: str,
    temperature: float = DEFAULT_TEMPERATURE,
    timeout_sec: float = DEFAULT_TIMEOUT_SEC,
) -> LiveProvider:
    """Create a strict JSON SiliconFlow chat-completions provider."""
    base_url = str(endpoint.get("base_url") or "").rstrip("/")
    api_key = str(endpoint.get("api_key") or "")
    if not base_url:
        raise ValueError("SiliconFlow endpoint requires base_url")
    if not api_key:
        raise ValueError("SiliconFlow endpoint requires api_key")
    if not model.strip():
        raise ValueError("SiliconFlow provider requires model")

    async def provider(messages: list[dict[str, str]]) -> str:
        start = time.monotonic()
        payload = {
            "model": model,
            "messages": messages,
            "stream": False,
            "temperature": temperature,
            "response_format": {"type": "json_object"},
        }
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }
        async with httpx.AsyncClient(timeout=timeout_sec) as client:
            response = await client.post(
                f"{base_url}/chat/completions",
                json=payload,
                headers=headers,
            )
        elapsed_ms = int((time.monotonic() - start) * 1000)
        if response.status_code != 200:
            detail = _redact(response.text[:500])
            raise RuntimeError(f"SiliconFlow HTTP {response.status_code} after {elapsed_ms}ms: {detail}")
        try:
            data = response.json()
            return data["choices"][0]["message"]["content"]
        except Exception as exc:
            raise ValueError(f"SiliconFlow response missing choices[0].message.content after {elapsed_ms}ms") from exc

    return provider


async def run_live_judgment_cases(
    judgment_cases: Sequence[Mapping[str, Any]],
    *,
    attention_cases: Sequence[Mapping[str, Any]],
    provider: LiveProvider,
    provider_info: Mapping[str, Any],
    case_ids: Sequence[str] | None = None,
    limit: int | None = None,
    repeat: int = 1,
    trace_dir: str = DEFAULT_SENTINEL_JUDGMENT_TRACE_DIR,
) -> dict[str, Any]:
    """Call a live provider for selected fixture cases and evaluate the result."""
    if repeat <= 0:
        raise ValueError("repeat must be positive")
    attention_cases_by_id = {
        _required_text(case, "id", subject="attention case"): case
        for case in attention_cases
    }
    selected_cases = _select_cases(judgment_cases, case_ids=case_ids, limit=limit)
    records = []
    for iteration in range(1, repeat + 1):
        for index, case in enumerate(selected_cases, start=1):
            case_id = case.get("id") or index
            request_id = f"sentinel_siliconflow_dry_run:{case_id}"
            if repeat > 1:
                request_id = f"{request_id}:r{iteration}"
            records.append(await _run_live_case(
                case,
                attention_cases_by_id=attention_cases_by_id,
                provider=provider,
                request_id=request_id,
                iteration=iteration,
            ))

    total = len(records)
    passed = sum(1 for record in records if record["ok"])
    failed = total - passed
    return {
        "schema_version": SENTINEL_SILICONFLOW_DRY_RUN_SCHEMA_VERSION,
        "runtime_mode": RUNTIME_MODE_PROVIDER_DRY_RUN,
        "trace_dir": str(trace_dir),
        "provider": dict(provider_info),
        "side_effects": ["siliconflow_chat_completion"] if total else [],
        "production_side_effects": [],
        "fallback_used": False,
        "fallback_reason": "",
        "metrics": {
            "total": total,
            "unique_cases": len(selected_cases),
            "repeat": repeat,
            "passed": passed,
            "failed": failed,
            "pass_rate": round(passed / total, 4) if total else 0.0,
            "model_calls": total,
            "wake_intent_true": sum(1 for record in records if record["actual"].get("wake_intent") is True),
            "wake_allowed": sum(1 for record in records if record["actual"].get("gate_status") == "passed"),
            "wake_package_created": sum(1 for record in records if record["actual"].get("wake_package_created") is True),
            "case_pass_rates": _case_pass_rates(records),
        },
        "records": records,
    }


def build_markdown(result: Mapping[str, Any]) -> str:
    """Render a compact human-review report for live provider dry-runs."""
    provider = result.get("provider") or {}
    lines = [
        "# Sentinel SiliconFlow Dry Run",
        "",
        f"- schema_version: `{result['schema_version']}`",
        f"- runtime_mode: `{result['runtime_mode']}`",
        f"- provider: `{provider.get('endpoint_name') or provider.get('endpoint_id') or 'SiliconFlow'}`",
        f"- model: `{provider.get('model') or ''}`",
        f"- total: `{result['metrics']['total']}`",
        f"- unique_cases: `{result['metrics'].get('unique_cases')}`",
        f"- repeat: `{result['metrics'].get('repeat')}`",
        f"- passed: `{result['metrics']['passed']}`",
        f"- failed: `{result['metrics']['failed']}`",
        f"- production_side_effects: `{result['production_side_effects']}`",
        "",
        "## Cases",
        "",
    ]
    for record in result["records"]:
        status = "PASS" if record["ok"] else "FAIL"
        actual = record["actual"]
        lines.extend([
            f"### {record['id']} - {status}",
            "",
            f"- category: `{record['category']}`",
            f"- iteration: `{record.get('iteration', 1)}`",
            f"- expectation_mode: `{record.get('expectation_mode', 'expect')}`",
            f"- wake_intent: `{actual.get('wake_intent')}`",
            f"- score: `{actual.get('score')}`",
            f"- confidence: `{actual.get('confidence')}`",
            f"- gate_status: `{actual.get('gate_status')}`",
            f"- wake_package_created: `{actual.get('wake_package_created')}`",
        ])
        if actual.get("summary"):
            lines.append(f"- summary: {actual['summary']}")
        if actual.get("core_reason"):
            lines.append(f"- core_reason: {actual['core_reason']}")
        if actual.get("restraint_reason"):
            lines.append(f"- restraint_reason: {actual['restraint_reason']}")
        if record["failures"]:
            lines.append("- failures:")
            for failure in record["failures"]:
                lines.append(f"  - `{failure}`")
        lines.append("")
    return "\n".join(lines)


def _resolve_siliconflow_key() -> tuple[str, str]:
    env_key = os.environ.get("AION_SILICONFLOW_KEY", "").strip()
    if env_key:
        return env_key, "env:AION_SILICONFLOW_KEY"
    key = get_key("siliconflow")
    if key:
        return key, "settings:siliconflow_key"
    return "", "missing"


async def _run_live_case(
    case: Mapping[str, Any],
    *,
    attention_cases_by_id: Mapping[str, Mapping[str, Any]],
    provider: LiveProvider,
    request_id: str,
    iteration: int = 1,
) -> dict[str, Any]:
    case_id = _required_text(case, "id", subject="judgment case")
    category = str(case.get("category") or "uncategorized")
    attention_case_id = _required_text(case, "attention_case_id", subject=f"judgment case {case_id}")
    if attention_case_id not in attention_cases_by_id:
        raise ValueError(f"judgment case {case_id} references unknown attention_case_id {attention_case_id!r}")
    if not isinstance(case.get("expect"), Mapping):
        raise ValueError(f"judgment case {case_id} requires expect object")
    evaluation_case = _provider_evaluation_case(case)
    expectation_mode = "provider_expect" if isinstance(case.get("provider_expect"), Mapping) else "expect"

    failures: list[str] = []
    chain: Mapping[str, Any] = {}
    judgment: Mapping[str, Any] | None = None
    captured: dict[str, str] = {}

    async def captured_provider(messages: list[dict[str, str]]) -> str:
        raw_result = provider(messages)
        raw_text = await raw_result if hasattr(raw_result, "__await__") else raw_result
        captured["raw_output"] = str(raw_text)
        return raw_text

    try:
        context = _case_context(case)
        attention_input = attention_cases_by_id[attention_case_id].get("input")
        if not isinstance(attention_input, Mapping):
            raise ValueError(f"attention case {attention_case_id!r} requires input object")
        chain = await run_sentinel_chain_dry_run(
            attention_input,
            judgment_provider=captured_provider,
            judgment_context=context,
            gate_context=_case_gate_context(case),
            wake_context=_case_wake_context(context),
            request_id=request_id,
        )
        judgment = chain["judgment_run"]["judgment"]
        failures.extend(evaluate_judgment_expectations(
            evaluation_case,
            judgment,
            chain["judgment_run"]["messages"],
        ))
    except Exception as exc:
        failures.append(f"provider_dry_run_failed: {type(exc).__name__}: {_redact(exc)}")

    actual = actual_judgment_payload(judgment)
    if chain:
        gate = chain.get("gate_result") or {}
        actual.update({
            "summary": (judgment or {}).get("summary"),
            "core_reason": (judgment or {}).get("core_reason"),
            "restraint_reason": (judgment or {}).get("restraint_reason"),
            "gate_status": gate.get("status"),
            "wake_allowed": gate.get("wake_allowed"),
            "wake_package_created": chain.get("wake_package") is not None,
        })
    return {
        "id": case_id,
        "category": category,
        "attention_case_id": attention_case_id,
        "iteration": iteration,
        "expectation_mode": expectation_mode,
        "ok": not failures,
        "failures": failures,
        "actual": actual,
        "trace": {
            "trace_id": request_id,
            "runtime_mode": RUNTIME_MODE_PROVIDER_DRY_RUN,
            "side_effects": ["siliconflow_chat_completion"],
            "production_side_effects": [],
            "fallback_used": False,
            "fallback_reason": "",
            "expectation_mode": expectation_mode,
            "chain": dict(chain),
            "raw_output": captured.get("raw_output", ""),
            "failures": failures,
        },
    }


def _case_context(case: Mapping[str, Any]) -> Mapping[str, Any]:
    context = case.get("context", {})
    if context is None:
        return {}
    if not isinstance(context, Mapping):
        raise ValueError(f"judgment case {case.get('id') or '<unknown>'} context must be an object")
    return context


def _provider_evaluation_case(case: Mapping[str, Any]) -> Mapping[str, Any]:
    provider_expect = case.get("provider_expect")
    if provider_expect is None:
        return case
    if not isinstance(provider_expect, Mapping):
        raise ValueError(f"judgment case {case.get('id') or '<unknown>'} provider_expect must be an object")
    base_expect = case.get("expect")
    if not isinstance(base_expect, Mapping):
        raise ValueError(f"judgment case {case.get('id') or '<unknown>'} requires expect object")
    inherited_expect = {}
    for key in ("prompt_not_contains",):
        if key in base_expect and key not in provider_expect:
            inherited_expect[key] = base_expect[key]
    evaluation_case = dict(case)
    evaluation_case["expect"] = {**inherited_expect, **dict(provider_expect)}
    return evaluation_case


def _case_gate_context(case: Mapping[str, Any]) -> Mapping[str, Any]:
    value = case.get("gate_context", {})
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise ValueError(f"judgment case {case.get('id') or '<unknown>'} gate_context must be an object")
    return value


def _case_wake_context(context: Mapping[str, Any]) -> dict[str, list[str]]:
    return {
        "recent_chat": _text_list(context.get("recent_chat")),
        "recent_sentinel_logs": _text_list(context.get("recent_sentinel_logs")),
        "memories": _text_list(context.get("memories")),
    }


def _select_cases(
    cases: Sequence[Mapping[str, Any]],
    *,
    case_ids: Sequence[str] | None = None,
    limit: int | None = None,
) -> list[Mapping[str, Any]]:
    selected = list(cases)
    if case_ids:
        wanted = list(case_ids)
        by_id = {_required_text(case, "id", subject="judgment case"): case for case in selected}
        missing = [case_id for case_id in wanted if case_id not in by_id]
        if missing:
            raise ValueError(f"unknown case_id(s): {missing!r}")
        selected = [by_id[case_id] for case_id in wanted]
    if limit is not None:
        if limit < 0:
            raise ValueError("limit must be non-negative")
        selected = selected[:limit]
    return selected


def _case_pass_rates(records: Sequence[Mapping[str, Any]]) -> dict[str, dict[str, Any]]:
    by_case: dict[str, dict[str, Any]] = {}
    for record in records:
        case_id = str(record.get("id") or "")
        bucket = by_case.setdefault(case_id, {"total": 0, "passed": 0, "failed": 0, "pass_rate": 0.0})
        bucket["total"] += 1
        if record.get("ok"):
            bucket["passed"] += 1
        else:
            bucket["failed"] += 1
    for bucket in by_case.values():
        total = bucket["total"]
        bucket["pass_rate"] = round(bucket["passed"] / total, 4) if total else 0.0
    return by_case


def _text_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value] if value.strip() else []
    if not isinstance(value, Sequence):
        raise ValueError("context list value must be a sequence")
    return [str(item).strip() for item in value if str(item).strip()]


def _required_text(case: Mapping[str, Any], key: str, *, subject: str) -> str:
    value = str(case.get(key) or "").strip()
    if not value:
        raise ValueError(f"{subject} requires {key}")
    return value


def _redact(value: Any) -> str:
    text = str(value)
    for pattern, repl in _REDACT_PATTERNS:
        text = pattern.sub(repl, text)
    return text


def _write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description="Run live SiliconFlow Sentinel dry-run eval")
    parser.add_argument("--cases", type=Path, default=DEFAULT_JUDGMENT_CASES_PATH)
    parser.add_argument("--attention-cases", type=Path, default=DEFAULT_ATTENTION_CASES_PATH)
    parser.add_argument("--case-id", action="append", default=[], help="Run one case id; repeatable")
    parser.add_argument("--limit", type=int, help="Run only the first N selected cases")
    parser.add_argument("--repeat", type=int, default=1, help="Repeat the selected cases N times")
    parser.add_argument("--model", default="", help=f"Default: {DEFAULT_SILICONFLOW_MODEL}")
    parser.add_argument("--base-url", default=DEFAULT_SILICONFLOW_BASE_URL)
    parser.add_argument("--endpoint-id", default="", help="Use a configured OpenAI-compatible endpoint id")
    parser.add_argument("--slot", default="", help="Use a configured slot, for example sentinel")
    parser.add_argument("--temperature", type=float, default=DEFAULT_TEMPERATURE)
    parser.add_argument("--timeout-sec", type=float, default=DEFAULT_TIMEOUT_SEC)
    parser.add_argument("--trace-dir", default=DEFAULT_SENTINEL_JUDGMENT_TRACE_DIR)
    parser.add_argument("--output-json", type=Path)
    parser.add_argument("--output-md", type=Path)
    parser.add_argument("--quiet", action="store_true", help="Do not print the full JSON report to stdout")
    parser.add_argument("--no-fail", action="store_true", help="Always exit 0 after printing report")
    args = parser.parse_args()

    endpoint = resolve_siliconflow_endpoint(
        model=args.model,
        base_url=args.base_url,
        endpoint_id=args.endpoint_id,
        slot_name=args.slot,
    )
    provider = build_siliconflow_provider(
        endpoint["endpoint"],
        model=endpoint["model"],
        temperature=args.temperature,
        timeout_sec=args.timeout_sec,
    )
    result = asyncio.run(run_live_judgment_cases(
        load_cases(args.cases),
        attention_cases=load_cases(args.attention_cases),
        provider=provider,
        provider_info=endpoint["public"],
        case_ids=args.case_id,
        limit=args.limit,
        repeat=args.repeat,
        trace_dir=args.trace_dir,
    ))
    output = json.dumps(result, ensure_ascii=False, indent=2)
    if not args.quiet:
        print(output)
    if args.output_json:
        _write_text(args.output_json, output + "\n")
    if args.output_md:
        _write_text(args.output_md, build_markdown(result) + "\n")
    if result["metrics"]["failed"] and not args.no_fail:
        return 1
    return 0


__all__ = [
    "DEFAULT_SILICONFLOW_BASE_URL",
    "DEFAULT_SILICONFLOW_MODEL",
    "RUNTIME_MODE_PROVIDER_DRY_RUN",
    "SENTINEL_SILICONFLOW_DRY_RUN_SCHEMA_VERSION",
    "build_markdown",
    "build_siliconflow_provider",
    "resolve_siliconflow_endpoint",
    "run_live_judgment_cases",
]


if __name__ == "__main__":
    raise SystemExit(main())
