"""Pure runtime context contracts for the future Sentinel chain."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from copy import deepcopy
from typing import Any

from app.chat.worldbook import resolve_worldbook_names
from app.context_delivery import ContextDeliveryProjection
from app.context_delivery.contracts import SCHEMA_VERSION as CONTEXT_DELIVERY_SCHEMA_VERSION

from .eval import RUNTIME_MODE_DRY_RUN


LEGACY_SENTINEL_RUNTIME_CONTEXT_SCHEMA_VERSION = "sentinel_runtime_context.v1"
SENTINEL_RUNTIME_CONTEXT_SCHEMA_VERSION = "sentinel_runtime_context.v2"
SUPPORTED_SENTINEL_RUNTIME_CONTEXT_SCHEMA_VERSIONS = frozenset({
    LEGACY_SENTINEL_RUNTIME_CONTEXT_SCHEMA_VERSION,
    SENTINEL_RUNTIME_CONTEXT_SCHEMA_VERSION,
})

DEFAULT_RUNTIME_RECENT_CHAT_LIMIT = 10
DEFAULT_RUNTIME_SENTINEL_LOG_LIMIT = 20
DEFAULT_RUNTIME_CONTEXT_ITEM_MAX_CHARS = 240

_ALLOWED_RUNTIME_CONTEXT_KEYS = frozenset({
    "ai_name",
    "clear_sleep",
    "context_projection",
    "device_effect_allowed",
    "device_effect_requested",
    "last_user_chat_time",
    "last_user_message_age_sec",
    "last_wake_age_sec",
    "now",
    "quiet_hours_active",
    "recent_chat",
    "recent_sentinel_logs",
    "sentinel_call_core_criteria",
    "urgent_risk",
    "user_name",
})

_GATE_BOOLEAN_KEYS = (
    "clear_sleep",
    "device_effect_allowed",
    "device_effect_requested",
    "quiet_hours_active",
    "urgent_risk",
)
_GATE_AGE_KEYS = (
    "last_user_message_age_sec",
    "last_wake_age_sec",
)


def build_sentinel_runtime_context(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Normalize runtime context material into prompt, gate and wake-package inputs."""
    if not isinstance(payload, Mapping):
        raise ValueError("sentinel runtime context payload must be an object")

    unknown = sorted(set(payload).difference(_ALLOWED_RUNTIME_CONTEXT_KEYS))
    if unknown:
        raise ValueError(f"sentinel runtime context unknown fields: {unknown!r}")

    raw_user_name = _optional_text(payload.get("user_name"), default="她", key="user_name")
    raw_ai_name = _optional_text(payload.get("ai_name"), default="我", key="ai_name")
    user_name, ai_name = resolve_worldbook_names({
        "user_name": raw_user_name,
        "ai_name": raw_ai_name,
    })
    recent_chat = _formatted_recent_chat(payload.get("recent_chat"), user_name=user_name, ai_name=ai_name)
    recent_sentinel_logs = _formatted_recent_sentinel_logs(payload.get("recent_sentinel_logs"))
    context_projection = _normalized_projection(payload.get("context_projection"))
    gate_context = _gate_context(payload)

    judgment_context = {
        "now": _optional_text(payload.get("now"), default="未知", key="now"),
        "user_name": user_name,
        "ai_name": ai_name,
        "last_user_chat_time": _optional_text(
            payload.get("last_user_chat_time"),
            default="未知",
            key="last_user_chat_time",
        ),
        "recent_chat": recent_chat,
        "recent_sentinel_logs": recent_sentinel_logs,
        "sentinel_call_core_criteria": _optional_text(
            payload.get("sentinel_call_core_criteria"),
            default="score >= 7 只是参考；你必须自己判断 wake_intent。",
            key="sentinel_call_core_criteria",
        ),
        "context_projection": deepcopy(context_projection),
    }
    wake_context = {
        "recent_sentinel_logs": list(recent_sentinel_logs),
        "recent_chat": list(recent_chat),
        "context_projection": deepcopy(context_projection),
    }
    return {
        "schema_version": SENTINEL_RUNTIME_CONTEXT_SCHEMA_VERSION,
        "runtime_mode": RUNTIME_MODE_DRY_RUN,
        "side_effects": [],
        "fallback_used": False,
        "fallback_reason": "",
        "judgment_context": judgment_context,
        "gate_context": gate_context,
        "wake_context": wake_context,
        "metrics": {
            "recent_chat_count": len(recent_chat),
            "recent_sentinel_log_count": len(recent_sentinel_logs),
            "gate_context_keys": sorted(gate_context),
            "context_projection_schema_version": context_projection["schema_version"],
        },
    }


def validate_sentinel_runtime_context(context: Mapping[str, Any]) -> dict[str, Any]:
    """Validate a normalized runtime context pack before the chain consumes it."""
    if not isinstance(context, Mapping):
        raise ValueError("sentinel runtime context must be an object")
    schema_version = context.get("schema_version")
    if schema_version not in SUPPORTED_SENTINEL_RUNTIME_CONTEXT_SCHEMA_VERSIONS:
        raise ValueError(
            "sentinel runtime context schema_version must be one of "
            f"{sorted(SUPPORTED_SENTINEL_RUNTIME_CONTEXT_SCHEMA_VERSIONS)!r}"
        )
    if schema_version == SENTINEL_RUNTIME_CONTEXT_SCHEMA_VERSION:
        judgment_context = context.get("judgment_context")
        wake_context = context.get("wake_context")
        if not isinstance(judgment_context, Mapping) or not isinstance(wake_context, Mapping):
            raise ValueError("sentinel_runtime_context.v2 requires judgment_context and wake_context objects")
        if "context_projection" not in judgment_context or "context_projection" not in wake_context:
            raise ValueError("sentinel_runtime_context.v2 requires both context_projection copies")
        judgment_projection = _validated_v2_projection(
            judgment_context.get("context_projection"),
            key="judgment_context.context_projection",
        )
        wake_projection = _validated_v2_projection(
            wake_context.get("context_projection"),
            key="wake_context.context_projection",
        )
        if judgment_projection != wake_projection:
            raise ValueError("sentinel_runtime_context.v2 projection copies must match")
    return dict(context)


def _normalized_projection(value: Any) -> dict[str, Any]:
    if value is None:
        return ContextDeliveryProjection(generated_at=0.0).to_dict()
    if not isinstance(value, Mapping):
        raise ValueError("sentinel runtime context context_projection must be an object")
    projection = ContextDeliveryProjection.from_dict(value)
    payload = projection.to_dict()
    if payload["schema_version"] != CONTEXT_DELIVERY_SCHEMA_VERSION:
        payload["schema_version"] = CONTEXT_DELIVERY_SCHEMA_VERSION
        projection = ContextDeliveryProjection.from_dict(payload)
    return projection.to_dict()


def _validated_v2_projection(value: Any, *, key: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError(f"sentinel runtime context {key} must be an object")
    projection = ContextDeliveryProjection.from_dict(value)
    if projection.schema_version != CONTEXT_DELIVERY_SCHEMA_VERSION:
        raise ValueError(
            f"sentinel_runtime_context.v2 {key} must use "
            f"{CONTEXT_DELIVERY_SCHEMA_VERSION!r}"
        )
    return projection.to_dict()


def _formatted_recent_chat(value: Any, *, user_name: str, ai_name: str) -> list[str]:
    if value is None:
        return []
    if not _is_sequence(value):
        raise ValueError("sentinel runtime context recent_chat must be a list")
    lines = []
    for index, item in enumerate(value):
        if isinstance(item, str):
            lines.append(_limited_text(item, key=f"recent_chat[{index}]"))
            continue
        if not isinstance(item, Mapping):
            raise ValueError(f"sentinel runtime context recent_chat[{index}] must be text or object")
        allowed = {"content", "role"}
        unknown = sorted(set(item).difference(allowed))
        if unknown:
            raise ValueError(f"sentinel runtime context recent_chat[{index}] unknown fields: {unknown!r}")
        role = _required_text(item.get("role"), key=f"recent_chat[{index}].role")
        if role not in {"assistant", "user"}:
            raise ValueError(f"sentinel runtime context recent_chat[{index}].role must be user or assistant")
        content = _required_text(item.get("content"), key=f"recent_chat[{index}].content")
        name = user_name if role == "user" else ai_name
        lines.append(_limited_text(f"{name}: {content}", key=f"recent_chat[{index}]"))
    return lines[-DEFAULT_RUNTIME_RECENT_CHAT_LIMIT:]


def _formatted_recent_sentinel_logs(value: Any) -> list[str]:
    if value is None:
        return []
    if not _is_sequence(value):
        raise ValueError("sentinel runtime context recent_sentinel_logs must be a list")
    lines = []
    for index, item in enumerate(value):
        if isinstance(item, str):
            lines.append(_limited_text(item, key=f"recent_sentinel_logs[{index}]"))
            continue
        if not isinstance(item, Mapping):
            raise ValueError(f"sentinel runtime context recent_sentinel_logs[{index}] must be text or object")
        allowed = {"call_core", "monitoringlog", "score", "status", "time"}
        unknown = sorted(set(item).difference(allowed))
        if unknown:
            raise ValueError(
                f"sentinel runtime context recent_sentinel_logs[{index}] unknown fields: {unknown!r}"
            )
        monitoringlog = _required_text(
            item.get("monitoringlog"),
            key=f"recent_sentinel_logs[{index}].monitoringlog",
        )
        parts = []
        time_text = str(item.get("time") or "").strip()
        if time_text:
            parts.append(f"[{time_text}]")
        if "score" in item:
            score = _required_score(item.get("score"), key=f"recent_sentinel_logs[{index}].score")
            parts.append(f"score:{score:g}")
        if "call_core" in item:
            call_core = item.get("call_core")
            if not isinstance(call_core, bool):
                raise ValueError(f"sentinel runtime context recent_sentinel_logs[{index}].call_core must be a boolean")
            if call_core:
                parts.append("->wake")
        status = str(item.get("status") or "").strip()
        if status:
            parts.append(f"status:{status}")
        parts.append(monitoringlog)
        lines.append(_limited_text(" ".join(parts), key=f"recent_sentinel_logs[{index}]"))
    return lines[-DEFAULT_RUNTIME_SENTINEL_LOG_LIMIT:]


def _gate_context(payload: Mapping[str, Any]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key in _GATE_BOOLEAN_KEYS:
        if key not in payload:
            continue
        value = payload[key]
        if not isinstance(value, bool):
            raise ValueError(f"sentinel runtime context {key} must be a boolean")
        result[key] = value
    for key in _GATE_AGE_KEYS:
        if key not in payload:
            continue
        value = payload[key]
        if isinstance(value, bool) or not isinstance(value, int | float):
            raise ValueError(f"sentinel runtime context {key} must be a number")
        if value < 0:
            raise ValueError(f"sentinel runtime context {key} must be non-negative")
        result[key] = float(value)
    return result


def _optional_text(value: Any, *, default: str, key: str) -> str:
    if value is None:
        return default
    if not isinstance(value, str):
        raise ValueError(f"sentinel runtime context {key} must be text")
    text = value.strip()
    return text or default


def _required_text(value: Any, *, key: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"sentinel runtime context {key} must be non-empty text")
    return value.strip()


def _required_score(value: Any, *, key: str) -> int | float:
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise ValueError(f"sentinel runtime context {key} must be a number")
    score = float(value)
    if not 0.0 <= score <= 10.0:
        raise ValueError(f"sentinel runtime context {key} must be 0-10")
    return int(score) if score.is_integer() else score


def _limited_text(value: str, *, key: str) -> str:
    text = _required_text(value, key=key)
    if len(text) > DEFAULT_RUNTIME_CONTEXT_ITEM_MAX_CHARS:
        text = text[:DEFAULT_RUNTIME_CONTEXT_ITEM_MAX_CHARS].rstrip()
    return text


def _is_sequence(value: Any) -> bool:
    return isinstance(value, Sequence) and not isinstance(value, str | bytes)


__all__ = [
    "DEFAULT_RUNTIME_CONTEXT_ITEM_MAX_CHARS",
    "DEFAULT_RUNTIME_RECENT_CHAT_LIMIT",
    "DEFAULT_RUNTIME_SENTINEL_LOG_LIMIT",
    "LEGACY_SENTINEL_RUNTIME_CONTEXT_SCHEMA_VERSION",
    "SENTINEL_RUNTIME_CONTEXT_SCHEMA_VERSION",
    "SUPPORTED_SENTINEL_RUNTIME_CONTEXT_SCHEMA_VERSIONS",
    "build_sentinel_runtime_context",
    "validate_sentinel_runtime_context",
]
