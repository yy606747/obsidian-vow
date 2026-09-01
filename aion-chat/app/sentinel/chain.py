"""Pure end-to-end dry-run chain for Sentinel Attention -> Core wake package."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from .attention import build_attention_snapshot
from .eval import RUNTIME_MODE_DRY_RUN
from .gate import evaluate_sentinel_gate
from .handoff import build_layer2_handoff
from .judgment_runner import SentinelJudgmentProvider, run_sentinel_judgment_dry_run
from .runtime_context import validate_sentinel_runtime_context
from .core_wake_orchestrator import build_core_wake_preflight
from .wake_package import build_core_wake_package


SENTINEL_CHAIN_DRY_RUN_SCHEMA_VERSION = "sentinel_chain_dry_run.v1"


async def run_sentinel_chain_dry_run(
    input_payload: Mapping[str, Any],
    *,
    judgment_provider: SentinelJudgmentProvider,
    judgment_context: Mapping[str, Any] | None = None,
    gate_context: Mapping[str, Any] | None = None,
    wake_context: Mapping[str, Any] | None = None,
    core_execution_context: Mapping[str, Any] | None = None,
    runtime_context: Mapping[str, Any] | None = None,
    config: Mapping[str, Any] | None = None,
    request_id: str = "",
) -> dict[str, Any]:
    """Run the future Sentinel chain without executing Core or other side effects."""
    if not isinstance(input_payload, Mapping):
        raise ValueError("sentinel chain input_payload must be an object")
    normalized_runtime_context = None
    if runtime_context is not None:
        if judgment_context is not None or gate_context is not None or wake_context is not None:
            raise ValueError("sentinel chain runtime_context cannot be combined with explicit contexts")
        normalized_runtime_context = validate_sentinel_runtime_context(runtime_context)
        judgment_context = normalized_runtime_context["judgment_context"]
        gate_context = normalized_runtime_context["gate_context"]
        wake_context = normalized_runtime_context["wake_context"]

    attention_snapshot = build_attention_snapshot(input_payload)
    handoff = build_layer2_handoff(attention_snapshot)
    judgment_run = await run_sentinel_judgment_dry_run(
        handoff,
        provider=judgment_provider,
        context=judgment_context,
        request_id=request_id,
    )
    judgment = judgment_run["judgment"]
    gate_result = evaluate_sentinel_gate(
        judgment,
        context=gate_context,
        config=config,
    )
    wake_package = None
    core_wake_preflight = None
    if gate_result["wake_allowed"]:
        wake_package = build_core_wake_package(
            handoff=handoff,
            judgment=judgment,
            gate_result=gate_result,
            context=wake_context,
        )
        if core_execution_context is not None:
            core_wake_preflight = build_core_wake_preflight(
                wake_package=wake_package,
                execution_context=core_execution_context,
            )

    return {
        "schema_version": SENTINEL_CHAIN_DRY_RUN_SCHEMA_VERSION,
        "runtime_mode": RUNTIME_MODE_DRY_RUN,
        "request_id": request_id,
        "side_effects": [],
        "fallback_used": False,
        "fallback_reason": "",
        "attention_snapshot": attention_snapshot,
        "handoff": handoff,
        "runtime_context": normalized_runtime_context,
        "judgment_run": judgment_run,
        "gate_result": gate_result,
        "wake_package": wake_package,
        "core_wake_preflight": core_wake_preflight,
        "metrics": {
            "wake_requested": gate_result["wake_requested"],
            "wake_allowed": gate_result["wake_allowed"],
            "wake_package_created": wake_package is not None,
            "blocked_reasons": list(gate_result["blocked_reasons"]),
        },
    }


__all__ = [
    "SENTINEL_CHAIN_DRY_RUN_SCHEMA_VERSION",
    "run_sentinel_chain_dry_run",
]
