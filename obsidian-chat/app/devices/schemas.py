"""DeviceService contracts for Phase 7."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Mapping


def _as_dict(value: Mapping[str, Any] | None) -> dict[str, Any]:
    return dict(value or {})


def _as_tuple(values) -> tuple[str, ...]:
    return tuple(str(value) for value in (values or ()) if str(value))


class DeviceStatus(str, Enum):
    UNKNOWN = "unknown"
    ONLINE = "online"
    OFFLINE = "offline"


class DeviceCommandStatus(str, Enum):
    EXECUTED = "executed"
    FAILED = "failed"
    SKIPPED = "skipped"
    QUEUED = "queued"


@dataclass(frozen=True)
class DeviceState:
    device_id: str
    name: str
    kind: str
    status: DeviceStatus = DeviceStatus.UNKNOWN
    driver_id: str = "unknown"
    capabilities: tuple[str, ...] = ()
    battery: int | None = None
    last_seen_at: float | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self):
        object.__setattr__(self, "status", DeviceStatus(self.status))
        object.__setattr__(self, "capabilities", _as_tuple(self.capabilities))
        object.__setattr__(self, "metadata", _as_dict(self.metadata))

    def to_dict(self) -> dict[str, Any]:
        return {
            "device_id": self.device_id,
            "name": self.name,
            "kind": self.kind,
            "status": self.status.value,
            "driver_id": self.driver_id,
            "capabilities": list(self.capabilities),
            "battery": self.battery,
            "last_seen_at": self.last_seen_at,
            "metadata": dict(self.metadata),
        }


@dataclass(frozen=True)
class DeviceCommandResult:
    device_id: str
    command: str
    status: DeviceCommandStatus
    driver_id: str = "unknown"
    message: str = ""
    result: Mapping[str, Any] | None = None
    audit_event_id: str | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self):
        object.__setattr__(self, "status", DeviceCommandStatus(self.status))
        object.__setattr__(self, "result", None if self.result is None else _as_dict(self.result))
        object.__setattr__(self, "metadata", _as_dict(self.metadata))

    @property
    def ok(self) -> bool:
        return self.status is DeviceCommandStatus.EXECUTED

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "device_id": self.device_id,
            "command": self.command,
            "status": self.status.value,
            "driver_id": self.driver_id,
            "message": self.message,
            "result": None if self.result is None else dict(self.result),
            "audit_event_id": self.audit_event_id,
            "metadata": dict(self.metadata),
        }


__all__ = [
    "DeviceCommandResult",
    "DeviceCommandStatus",
    "DeviceState",
    "DeviceStatus",
]
