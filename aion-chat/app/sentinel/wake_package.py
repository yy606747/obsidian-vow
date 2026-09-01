"""Pure Core wake package builder for Sentinel Layer 3 foundation."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from copy import deepcopy
from typing import Any

from app.context_delivery import ContextDeliveryProjection
from app.context_delivery.contracts import SCHEMA_VERSION as CONTEXT_DELIVERY_SCHEMA_VERSION
from app.context_delivery.safety import (
    DEVICE_PROXY_HARD_LIMITS,
    TRIGGER_CONTEXT_HARD_LIMIT,
)

from .eval import RUNTIME_MODE_DRY_RUN
from .gate import (
    GATE_STATUS_PASSED,
    SENTINEL_GATE_RESULT_SCHEMA_VERSION,
)
from .handoff import LAYER2_HANDOFF_ALLOWED_FIELDS, LAYER2_HANDOFF_SCHEMA_VERSION
from .judgment import REQUIRED_SENTINEL_JUDGMENT_FIELDS, normalize_sentinel_judgment


LEGACY_CORE_WAKE_PACKAGE_SCHEMA_VERSION = "sentinel_core_wake_package.v1"
CORE_WAKE_PACKAGE_SCHEMA_VERSION = "sentinel_core_wake_package.v2"
SUPPORTED_CORE_WAKE_PACKAGE_SCHEMA_VERSIONS = frozenset({
    LEGACY_CORE_WAKE_PACKAGE_SCHEMA_VERSION,
    CORE_WAKE_PACKAGE_SCHEMA_VERSION,
})
DEFAULT_RECENT_SENTINEL_LOG_LIMIT = 3
DEFAULT_RECENT_CHAT_LIMIT = 6
DEFAULT_CONTEXT_ITEM_MAX_CHARS = 240

CORE_WAKE_HARD_LIMITS = (
    "只基于本轮注入的上下文、唤醒包和最近聊天说话；不要自行补造未提供的长期记忆。",
    "不要编造她没说过的事、没发生过的场景或没有证据的状态。",
    "不确定的证据必须用不确定口吻，不要说死。",
    "这些线索是你自己留意到的，不要当成确凿的事实讲给她听。",
    *DEVICE_PROXY_HARD_LIMITS,
    TRIGGER_CONTEXT_HARD_LIMIT,
    "不要复述数据表；就用你自己的口吻说话。",
)


def build_core_wake_package(
    *,
    handoff: Mapping[str, Any],
    judgment: Mapping[str, Any],
    gate_result: Mapping[str, Any],
    context: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Build the short package that Wake Orchestrator may pass to Core."""
    handoff = _validated_handoff(handoff)
    judgment = _validated_judgment(judgment)
    gate_result = _validated_gate_result(gate_result)
    context = _validated_context(context)

    hypothesis_labels = [
        str(item.get("label")).strip()
        for item in handoff["hypotheses"]
        if isinstance(item, Mapping) and str(item.get("label") or "").strip()
    ]
    package = {
        "schema_version": CORE_WAKE_PACKAGE_SCHEMA_VERSION,
        "runtime_mode": RUNTIME_MODE_DRY_RUN,
        "trigger": "sentinel",
        "side_effects": [],
        "fallback_used": False,
        "fallback_reason": "",
        "wake_reason": judgment["core_reason"],
        "sentinel": {
            "monitoringlog": judgment["monitoringlog"],
            "summary": judgment["summary"],
            "score": judgment["score"],
            "confidence": judgment["confidence"],
            "core_reason": judgment["core_reason"],
            "tone_hint": judgment["tone_hint"],
            "uncertainty": judgment["uncertainty"],
            "suggested_next_check_sec": judgment["suggested_next_check_sec"],
        },
        "attention": {
            "compact_text": handoff["compact_text"],
            "world_state": deepcopy(handoff["world_state"]),
            "attention_targets": list(handoff["attention_targets"]),
            "hypothesis_labels": hypothesis_labels,
            "suggested_next_check_sec": handoff["suggested_next_check_sec"],
        },
        "context": context,
        "hard_limits": list(CORE_WAKE_HARD_LIMITS),
        "gate": {
            "schema_version": gate_result["schema_version"],
            "status": gate_result["status"],
            "action": gate_result["action"],
            "wake_requested": gate_result["wake_requested"],
            "wake_allowed": gate_result["wake_allowed"],
            "blocked_reasons": list(gate_result["blocked_reasons"]),
        },
    }
    return package


def _validated_handoff(handoff: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(handoff, Mapping):
        raise ValueError("core wake package handoff must be an object")
    if handoff.get("schema_version") != LAYER2_HANDOFF_SCHEMA_VERSION:
        raise ValueError(f"core wake package handoff schema_version must be {LAYER2_HANDOFF_SCHEMA_VERSION!r}")
    return dict(handoff)


def _validated_judgment(judgment: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(judgment, Mapping):
        raise ValueError("core wake package judgment must be an object")
    missing = sorted(REQUIRED_SENTINEL_JUDGMENT_FIELDS.difference(judgment.keys()))
    if missing:
        raise ValueError(f"core wake package judgment missing fields: {missing!r}")
    normalized = normalize_sentinel_judgment({
        field: judgment[field]
        for field in REQUIRED_SENTINEL_JUDGMENT_FIELDS
    })
    if not normalized["wake_intent"]:
        raise ValueError("core wake package requires judgment wake_intent true")
    return normalized


def _validated_gate_result(gate_result: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(gate_result, Mapping):
        raise ValueError("core wake package gate_result must be an object")
    if gate_result.get("schema_version") != SENTINEL_GATE_RESULT_SCHEMA_VERSION:
        raise ValueError(
            "core wake package gate_result schema_version must be "
            f"{SENTINEL_GATE_RESULT_SCHEMA_VERSION!r}"
        )
    if gate_result.get("status") != GATE_STATUS_PASSED:
        raise ValueError("core wake package requires passed gate status")
    if gate_result.get("wake_allowed") is not True:
        raise ValueError("core wake package requires gate wake_allowed true")
    return dict(gate_result)


def _validated_context(context: Mapping[str, Any] | None) -> dict[str, Any]:
    if context is None:
        context = {}
    if not isinstance(context, Mapping):
        raise ValueError("core wake package context must be an object")
    allowed = {"context_projection", "recent_chat", "recent_sentinel_logs"}
    unknown = sorted(set(context).difference(allowed))
    if unknown:
        raise ValueError(f"core wake package context unknown fields: {unknown!r}")
    return {
        "recent_sentinel_logs": _limited_text_list(
            context.get("recent_sentinel_logs"),
            key="recent_sentinel_logs",
            limit=DEFAULT_RECENT_SENTINEL_LOG_LIMIT,
        ),
        "recent_chat": _limited_text_list(
            context.get("recent_chat"),
            key="recent_chat",
            limit=DEFAULT_RECENT_CHAT_LIMIT,
        ),
        "context_projection": _normalized_projection(context.get("context_projection")),
    }


def _normalized_projection(value: Any) -> dict[str, Any]:
    if value is None:
        return ContextDeliveryProjection(generated_at=0.0).to_dict()
    if not isinstance(value, Mapping):
        raise ValueError("core wake package context_projection must be an object")
    projection = ContextDeliveryProjection.from_dict(value)
    payload = projection.to_dict()
    if payload["schema_version"] != CONTEXT_DELIVERY_SCHEMA_VERSION:
        payload["schema_version"] = CONTEXT_DELIVERY_SCHEMA_VERSION
        projection = ContextDeliveryProjection.from_dict(payload)
    return projection.to_dict()


def _limited_text_list(value: Any, *, key: str, limit: int) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, Sequence) or isinstance(value, str | bytes):
        raise ValueError(f"core wake package context {key} must be a list")
    result: list[str] = []
    for index, item in enumerate(value):
        if not isinstance(item, str) or not item.strip():
            raise ValueError(f"core wake package context {key}[{index}] must be non-empty text")
        text = item.strip()
        if len(text) > DEFAULT_CONTEXT_ITEM_MAX_CHARS:
            text = text[:DEFAULT_CONTEXT_ITEM_MAX_CHARS].rstrip()
        result.append(text)
    return result[-limit:]



__all__ = [
    "CORE_WAKE_HARD_LIMITS",
    "CORE_WAKE_PACKAGE_SCHEMA_VERSION",
    "DEFAULT_CONTEXT_ITEM_MAX_CHARS",
    "DEFAULT_RECENT_CHAT_LIMIT",
    "DEFAULT_RECENT_SENTINEL_LOG_LIMIT",
    "LEGACY_CORE_WAKE_PACKAGE_SCHEMA_VERSION",
    "SUPPORTED_CORE_WAKE_PACKAGE_SCHEMA_VERSIONS",
    "build_core_wake_package",
]
