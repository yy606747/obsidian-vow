from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

ControlKind = Literal["dom", "whisper", "tide"]
ControlStatus = Literal["active", "stale", "ended"]
ControlContextSource = Literal["control_session", "legacy_body", "safety_tombstone", "none"]


@dataclass(frozen=True)
class ControlSession:
    session_id: str
    conv_id: str
    kind: ControlKind
    status: ControlStatus
    owner_client_id: str
    device_id: str | None
    started_at: float
    last_heartbeat_at: float
    last_snapshot_at: float | None
    ended_at: float | None
    close_reason: str | None
    control_epoch: int
    safeword_set: bool
    control_resource_id: str | None = None
    frontend_snapshot_json: str | None = None
    metadata_json: str = "{}"

    @classmethod
    def from_row(cls, row: Any) -> "ControlSession":
        data = dict(row)
        return cls(
            session_id=data["session_id"],
            conv_id=data["conv_id"],
            kind=data["kind"],
            status=data["status"],
            owner_client_id=data["owner_client_id"],
            device_id=data.get("device_id"),
            control_resource_id=data.get("control_resource_id"),
            started_at=float(data["started_at"]),
            last_heartbeat_at=float(data["last_heartbeat_at"]),
            last_snapshot_at=data.get("last_snapshot_at"),
            ended_at=data.get("ended_at"),
            close_reason=data.get("close_reason"),
            control_epoch=int(data["control_epoch"] or 0),
            safeword_set=bool(data["safeword_set"]),
            frontend_snapshot_json=data.get("frontend_snapshot_json"),
            metadata_json=data.get("metadata_json") or "{}",
        )

    def to_api_dict(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "conv_id": self.conv_id,
            "kind": self.kind,
            "status": self.status,
            "owner_client_id": self.owner_client_id,
            "device_id": self.device_id,
            "control_resource_id": self.control_resource_id,
            "started_at": self.started_at,
            "last_heartbeat_at": self.last_heartbeat_at,
            "last_snapshot_at": self.last_snapshot_at,
            "ended_at": self.ended_at,
            "close_reason": self.close_reason,
            "control_epoch": self.control_epoch,
            "safeword_set": self.safeword_set,
        }


@dataclass(frozen=True)
class ControlPromptContext:
    session_id: str | None = None
    kind: ControlKind | None = None
    active: bool = False
    source: ControlContextSource = "none"
    owner_client_id: str | None = None
    control_epoch: int | None = None
    control_resource_id: str | None = None
    safeword_set: bool = False
    dom_history: list[str] = field(default_factory=list)
    cnc_enabled: bool = False
    cnc_weakness: list[str] = field(default_factory=list)
    resist_hits: int = 0
    short_streak: int = 0
    reply_delay_ms: int = 0
    compliance_streak: int = 0
    session_elapsed: int = 0
    scene_name: str | None = None
    scene_elapsed: int = 0
    since_last_punish: int | None = None
    ratchet_valley: int = 0
    debt: float = 0.0
    stubborn_streak: int = 0
    hidden_agenda_brief: str | None = None
    hidden_agenda_stance: str | None = None
    hidden_agenda_status: str = "none"
    hidden_agenda_source_refs: list[str] = field(default_factory=list)
    aftercare_active: bool = False
    safety_close_reason: str | None = None
    safety_closed_at: float | None = None
