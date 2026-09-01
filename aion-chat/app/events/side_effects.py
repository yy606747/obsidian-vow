"""Side-effect gateway contracts for future backend modules."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Mapping


def _as_dict(value: Mapping[str, Any] | None) -> dict[str, Any]:
    return dict(value or {})


def _clean_text(value: str, *, field_name: str) -> str:
    text = str(value or "").strip()
    if not text:
        raise ValueError(f"{field_name} is required")
    return text


class SideEffectKind(str, Enum):
    MESSAGE_WRITE = "message.write"
    WS_BROADCAST = "ws.broadcast"
    MONITOR_LOG = "monitor.log"
    CORE_WAKE = "core.wake"
    DEVICE_COMMAND = "device.command"
    MEMORY_WRITE = "memory.write"


class SideEffectStatus(str, Enum):
    PLANNED = "planned"
    REJECTED = "rejected"


@dataclass(frozen=True)
class SideEffectRequest:
    id: str
    kind: SideEffectKind
    source: str
    payload: Mapping[str, Any] = field(default_factory=dict)
    reason: str = ""
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self):
        object.__setattr__(self, "id", _clean_text(self.id, field_name="id"))
        object.__setattr__(self, "kind", SideEffectKind(self.kind))
        object.__setattr__(self, "source", _clean_text(self.source, field_name="source"))
        object.__setattr__(self, "payload", _as_dict(self.payload))
        object.__setattr__(self, "reason", str(self.reason or ""))
        object.__setattr__(self, "metadata", _as_dict(self.metadata))

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "kind": self.kind.value,
            "source": self.source,
            "payload": dict(self.payload),
            "reason": self.reason,
            "metadata": dict(self.metadata),
        }


@dataclass(frozen=True)
class SideEffectPlan:
    request: SideEffectRequest
    status: SideEffectStatus = SideEffectStatus.PLANNED
    executor: str = "gateway_required"
    message: str = ""

    def __post_init__(self):
        object.__setattr__(self, "status", SideEffectStatus(self.status))

    def to_dict(self) -> dict[str, Any]:
        return {
            "request": self.request.to_dict(),
            "status": self.status.value,
            "executor": self.executor,
            "message": self.message,
        }


class SideEffectGateway:
    """Validation-only gateway. It does not execute effects in Phase 8.0."""

    def __init__(self, *, allowed_kinds: set[SideEffectKind] | None = None):
        self._allowed_kinds = set(allowed_kinds or SideEffectKind)

    def plan(
        self,
        *,
        request_id: str,
        kind: SideEffectKind | str,
        source: str,
        payload: Mapping[str, Any] | None = None,
        reason: str = "",
        metadata: Mapping[str, Any] | None = None,
    ) -> SideEffectPlan:
        request = SideEffectRequest(
            id=request_id,
            kind=SideEffectKind(kind),
            source=source,
            payload=payload or {},
            reason=reason,
            metadata=metadata or {},
        )
        if request.kind not in self._allowed_kinds:
            return SideEffectPlan(
                request=request,
                status=SideEffectStatus.REJECTED,
                message="side_effect_kind_not_allowed",
            )
        return SideEffectPlan(request=request)

    def allowed_kinds_payload(self) -> list[str]:
        return sorted(kind.value for kind in self._allowed_kinds)


side_effect_gateway = SideEffectGateway()


__all__ = [
    "SideEffectGateway",
    "SideEffectKind",
    "SideEffectPlan",
    "SideEffectRequest",
    "SideEffectStatus",
    "side_effect_gateway",
]
