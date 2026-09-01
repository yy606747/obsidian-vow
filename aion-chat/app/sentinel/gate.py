"""Pure hard-boundary Gate for Sentinel wake decisions."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from .attention_config import resolve_attention_config
from .eval import RUNTIME_MODE_DRY_RUN
from .judgment import REQUIRED_SENTINEL_JUDGMENT_FIELDS, normalize_sentinel_judgment


SENTINEL_GATE_RESULT_SCHEMA_VERSION = "sentinel_gate_result.v1"
WAKE_BOUNDARY_RESULT_SCHEMA_VERSION = "wake_boundary_result.v1"
GATE_STATUS_PASSED = "passed"
GATE_STATUS_BLOCKED = "blocked"
GATE_STATUS_NOT_REQUESTED = "not_requested"
GATE_ACTION_ALLOW_CORE_WAKE = "allow_core_wake"
GATE_ACTION_BLOCK_CORE_WAKE = "block_core_wake"
GATE_ACTION_OBSERVE = "observe"

GATE_REASON_CHAT_COOLDOWN = "chat_cooldown"
GATE_REASON_WAKE_COOLDOWN = "wake_cooldown"
GATE_REASON_QUIET_HOURS = "quiet_hours"
GATE_REASON_CLEAR_SLEEP = "clear_sleep"
GATE_REASON_LOW_CONFIDENCE = "low_confidence"
GATE_REASON_DEVICE_GATE = "device_gate"


def evaluate_sentinel_gate(
    judgment: Mapping[str, Any],
    *,
    context: Mapping[str, Any] | None = None,
    config: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Evaluate only hard wake boundaries for a normalized Sentinel judgment."""
    normalized = _normalize_gate_judgment(judgment)
    context = _validated_context(context)
    config = resolve_attention_config(config)

    if not normalized["wake_intent"]:
        return _gate_result(
            status=GATE_STATUS_NOT_REQUESTED,
            action=GATE_ACTION_OBSERVE,
            wake_requested=False,
            wake_allowed=False,
            blocked_reasons=[],
            judgment=normalized,
        )

    wake_cooldown_sec = (
        config["high_score_wake_cooldown_sec"]
        if normalized["score"] >= 9
        else config["wake_cooldown_sec"]
    )
    boundaries = evaluate_wake_boundaries(
        context=context,
        event_confidence=normalized["confidence"],
        config=config,
        wake_cooldown_sec=wake_cooldown_sec,
    )
    blocked_reasons = boundaries["blocked_reasons"]
    wake_allowed = not blocked_reasons
    return _gate_result(
        status=GATE_STATUS_PASSED if wake_allowed else GATE_STATUS_BLOCKED,
        action=GATE_ACTION_ALLOW_CORE_WAKE if wake_allowed else GATE_ACTION_BLOCK_CORE_WAKE,
        wake_requested=True,
        wake_allowed=wake_allowed,
        blocked_reasons=blocked_reasons,
        judgment=normalized,
    )


def evaluate_wake_boundaries(
    *,
    context: Mapping[str, Any] | None = None,
    event_confidence: float | int = 1.0,
    config: Mapping[str, Any] | None = None,
    wake_cooldown_sec: float | int | None = None,
) -> dict[str, Any]:
    """Evaluate shared wake boundaries without inventing a Sentinel judgment."""

    context = _validated_context(context)
    config = resolve_attention_config(config)
    confidence = _validated_confidence(event_confidence)
    cooldown = (
        float(config["wake_cooldown_sec"])
        if wake_cooldown_sec is None
        else _validated_positive_number(wake_cooldown_sec, key="wake_cooldown_sec")
    )
    reasons: list[str] = []
    if _enabled(context, "quiet_hours_active"):
        reasons.append(GATE_REASON_QUIET_HOURS)
    if _enabled(context, "clear_sleep") and not _enabled(context, "urgent_risk"):
        reasons.append(GATE_REASON_CLEAR_SLEEP)

    last_user_message_age_sec = context.get("last_user_message_age_sec")
    if (
        last_user_message_age_sec is not None
        and last_user_message_age_sec < config["chat_cooldown_sec"]
    ):
        reasons.append(GATE_REASON_CHAT_COOLDOWN)

    last_wake_age_sec = context.get("last_wake_age_sec")
    if last_wake_age_sec is not None and last_wake_age_sec < cooldown:
        reasons.append(GATE_REASON_WAKE_COOLDOWN)

    if confidence < config["low_confidence_threshold"]:
        reasons.append(GATE_REASON_LOW_CONFIDENCE)
    if _enabled(context, "device_effect_requested") and not _enabled(context, "device_effect_allowed"):
        reasons.append(GATE_REASON_DEVICE_GATE)
    return {
        "schema_version": WAKE_BOUNDARY_RESULT_SCHEMA_VERSION,
        "runtime_mode": RUNTIME_MODE_DRY_RUN,
        "status": GATE_STATUS_PASSED if not reasons else GATE_STATUS_BLOCKED,
        "wake_allowed": not reasons,
        "blocked_reasons": reasons,
        "side_effects": [],
    }


def _gate_result(
    *,
    status: str,
    action: str,
    wake_requested: bool,
    wake_allowed: bool,
    blocked_reasons: list[str],
    judgment: Mapping[str, Any],
) -> dict[str, Any]:
    return {
        "schema_version": SENTINEL_GATE_RESULT_SCHEMA_VERSION,
        "runtime_mode": RUNTIME_MODE_DRY_RUN,
        "status": status,
        "action": action,
        "wake_requested": wake_requested,
        "wake_allowed": wake_allowed,
        "blocked_reasons": blocked_reasons,
        "side_effects": [],
        "fallback_used": False,
        "fallback_reason": "",
        "suggested_next_check_sec": judgment["suggested_next_check_sec"],
        "judgment": dict(judgment),
    }


def _normalize_gate_judgment(judgment: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(judgment, Mapping):
        raise ValueError("sentinel gate judgment must be an object")
    missing = sorted(REQUIRED_SENTINEL_JUDGMENT_FIELDS.difference(judgment.keys()))
    if missing:
        raise ValueError(f"sentinel gate judgment missing fields: {missing!r}")
    return normalize_sentinel_judgment({
        field: judgment[field]
        for field in REQUIRED_SENTINEL_JUDGMENT_FIELDS
    })


def _validated_context(context: Mapping[str, Any] | None) -> dict[str, Any]:
    if context is None:
        return {}
    if not isinstance(context, Mapping):
        raise ValueError("sentinel gate context must be an object")

    normalized = dict(context)
    for key in (
        "clear_sleep",
        "device_effect_allowed",
        "device_effect_requested",
        "quiet_hours_active",
        "urgent_risk",
    ):
        if key in normalized and not isinstance(normalized[key], bool):
            raise ValueError(f"sentinel gate context {key} must be a boolean")
    for key in ("last_user_message_age_sec", "last_wake_age_sec"):
        value = normalized.get(key)
        if value is None:
            continue
        if isinstance(value, bool) or not isinstance(value, int | float):
            raise ValueError(f"sentinel gate context {key} must be a number")
        if value < 0:
            raise ValueError(f"sentinel gate context {key} must be non-negative")
        normalized[key] = float(value)
    return normalized


def _enabled(context: Mapping[str, Any], key: str) -> bool:
    return context.get(key) is True


def _validated_confidence(value: Any) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise ValueError("wake boundary event_confidence must be a number")
    confidence = float(value)
    if not 0.0 <= confidence <= 1.0:
        raise ValueError("wake boundary event_confidence must be between 0 and 1")
    return confidence


def _validated_positive_number(value: Any, *, key: str) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise ValueError(f"wake boundary {key} must be a number")
    number = float(value)
    if number <= 0:
        raise ValueError(f"wake boundary {key} must be positive")
    return number


__all__ = [
    "GATE_ACTION_ALLOW_CORE_WAKE",
    "GATE_ACTION_BLOCK_CORE_WAKE",
    "GATE_ACTION_OBSERVE",
    "GATE_REASON_CHAT_COOLDOWN",
    "GATE_REASON_CLEAR_SLEEP",
    "GATE_REASON_DEVICE_GATE",
    "GATE_REASON_LOW_CONFIDENCE",
    "GATE_REASON_QUIET_HOURS",
    "GATE_REASON_WAKE_COOLDOWN",
    "GATE_STATUS_BLOCKED",
    "GATE_STATUS_NOT_REQUESTED",
    "GATE_STATUS_PASSED",
    "SENTINEL_GATE_RESULT_SCHEMA_VERSION",
    "WAKE_BOUNDARY_RESULT_SCHEMA_VERSION",
    "evaluate_sentinel_gate",
    "evaluate_wake_boundaries",
]
