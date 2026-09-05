"""Mode and capability contracts for server-side tool policy."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Iterable, Mapping


class ChatMode(str, Enum):
    NORMAL = "normal"
    WORK = "work"
    VOICE_CALL = "voice_call"
    SENTINEL = "sentinel"
    DEVICE_CONTROL = "device_control"
    INTIMATE = "intimate"
    CONTROL_SESSION = "control_session"
    MAINTENANCE = "maintenance"


def _as_tuple(values: Iterable[str] | None) -> tuple[str, ...]:
    if values is None:
        return ()
    return tuple(str(value) for value in values if str(value))


def _as_dict(value: Mapping[str, object] | None) -> dict:
    return dict(value or {})


@dataclass(frozen=True)
class ModeSnapshot:
    mode: ChatMode = ChatMode.NORMAL
    capabilities: tuple[str, ...] = ()
    source: str = "server_default"
    metadata: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self):
        object.__setattr__(self, "mode", ChatMode(self.mode))
        object.__setattr__(self, "capabilities", _as_tuple(self.capabilities))
        object.__setattr__(self, "metadata", _as_dict(self.metadata))

    def to_dict(self) -> dict:
        return {
            "mode": self.mode.value,
            "capabilities": list(self.capabilities),
            "source": self.source,
            "metadata": dict(self.metadata),
        }


__all__ = ["ChatMode", "ModeSnapshot"]
