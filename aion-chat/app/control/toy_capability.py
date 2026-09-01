"""Shared live capability snapshot for session-bound toy commands."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping


TOY_BRIDGE_DEVICE_ID = "browser_toy_bridge"
TOY_COMMAND_CAPABILITY = "toy.legacy_command"
TOY_CONTROL_KINDS = frozenset({"dom", "whisper"})
FRONTEND_TOY_DRIVER_IDS = frozenset({"sosexy", "cx492b", "sk30", "sk40"})


@dataclass(frozen=True)
class ToyCapabilitySnapshot:
    """A frozen answer used by both prompt construction and command delivery."""

    allowed: bool
    reason: str
    conv_id: str
    control_session_id: str = ""
    control_kind: str = ""
    control_status: str = ""
    control_epoch: int | None = None
    owner_client_id: str = ""
    control_device_id: str = ""
    session: Any | None = field(default=None, repr=False, compare=False)

    def to_execution_context(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "toy_capability_allowed": self.allowed,
            "toy_capability_reason": self.reason,
        }
        if self.control_session_id:
            payload.update({
                "control_session_id": self.control_session_id,
                "control_kind": self.control_kind,
                "control_status": self.control_status,
                "control_epoch": self.control_epoch,
                "owner_client_id": self.owner_client_id,
                "control_device_id": self.control_device_id,
            })
        return payload


async def resolve_toy_capability_snapshot(
    *,
    conv_id: str,
    session_service: Any | None = None,
    device_service_adapter: Any | None = None,
    expected_session_id: str | None = None,
    expected_epoch: Any = None,
    expected_owner_client_id: str | None = None,
    expected_device_id: str | None = None,
) -> ToyCapabilitySnapshot:
    """Resolve current authorization and device availability, failing closed.

    Device freshness is deliberately delegated to ``DeviceService``.  In
    particular, the browser bridge's existing 45-second expiry remains the
    single freshness threshold.
    """
    conv_id = str(conv_id or "").strip()
    if not conv_id:
        return ToyCapabilitySnapshot(False, "missing_conversation", conv_id)

    if session_service is None:
        from .service import control_session_service

        session_service = control_session_service
    get_current = getattr(session_service, "get_current", None)
    if not callable(get_current):
        raise ValueError("toy capability session service must provide get_current")
    try:
        session = await get_current(conv_id=conv_id)
    except Exception:
        return ToyCapabilitySnapshot(False, "session_lookup_failed", conv_id)
    if session is None:
        return ToyCapabilitySnapshot(False, "no_active_session", conv_id)

    snapshot = _session_snapshot(conv_id, session)
    if snapshot.control_status != "active":
        return _denied(snapshot, f"session_{snapshot.control_status or 'invalid'}")
    if snapshot.control_kind not in TOY_CONTROL_KINDS:
        return _denied(snapshot, "unsupported_control_kind")
    if not snapshot.control_session_id or snapshot.control_epoch is None or not snapshot.owner_client_id:
        return _denied(snapshot, "invalid_control_metadata")
    if str(getattr(session, "conv_id", conv_id) or "").strip() != conv_id:
        return _denied(snapshot, "conversation_mismatch")

    if expected_session_id is not None and str(expected_session_id or "").strip() != snapshot.control_session_id:
        return _denied(snapshot, "session_mismatch")
    if expected_epoch is not None and not _epoch_matches(expected_epoch, snapshot.control_epoch):
        return _denied(snapshot, "epoch_mismatch")
    if expected_owner_client_id is not None and str(expected_owner_client_id or "").strip() != snapshot.owner_client_id:
        return _denied(snapshot, "owner_mismatch")
    if expected_device_id is not None:
        expected_device = normalize_toy_device_id(expected_device_id)
        if expected_device != snapshot.control_device_id:
            return _denied(snapshot, "device_mismatch")

    if device_service_adapter is None:
        from app.devices import device_service

        device_service_adapter = device_service
    get_device = getattr(device_service_adapter, "get_device", None)
    if not callable(get_device):
        raise ValueError("toy capability device service must provide get_device")
    try:
        result = await get_device(snapshot.control_device_id)
    except Exception:
        return _denied(snapshot, "device_lookup_failed")
    if not isinstance(result, Mapping) or result.get("ok") is not True:
        return _denied(snapshot, "device_not_found")
    device = result.get("device")
    if not isinstance(device, Mapping):
        return _denied(snapshot, "device_invalid")
    if str(device.get("device_id") or "").strip() != snapshot.control_device_id:
        return _denied(snapshot, "device_mismatch")
    if str(device.get("status") or "").strip().lower() != "online":
        return _denied(snapshot, "device_offline")

    capabilities = device.get("capabilities")
    if not isinstance(capabilities, (list, tuple, set, frozenset)) or TOY_COMMAND_CAPABILITY not in capabilities:
        return _denied(snapshot, "missing_toy_capability")
    metadata = device.get("metadata")
    if not isinstance(metadata, Mapping):
        return _denied(snapshot, "device_session_mismatch")
    if str(metadata.get("control_session_id") or "").strip() != snapshot.control_session_id:
        return _denied(snapshot, "device_session_mismatch")
    if not _epoch_matches(metadata.get("control_epoch"), snapshot.control_epoch):
        return _denied(snapshot, "device_epoch_mismatch")
    if str(metadata.get("owner_client_id") or "").strip() != snapshot.owner_client_id:
        return _denied(snapshot, "device_owner_mismatch")
    if str(metadata.get("control_kind") or "").strip() != snapshot.control_kind:
        return _denied(snapshot, "device_kind_mismatch")
    return ToyCapabilitySnapshot(
        True,
        "allowed",
        snapshot.conv_id,
        snapshot.control_session_id,
        snapshot.control_kind,
        snapshot.control_status,
        snapshot.control_epoch,
        snapshot.owner_client_id,
        snapshot.control_device_id,
        snapshot.session,
    )


def normalize_toy_device_id(value: Any) -> str:
    device_id = str(value or "").strip() or TOY_BRIDGE_DEVICE_ID
    return TOY_BRIDGE_DEVICE_ID if device_id in FRONTEND_TOY_DRIVER_IDS else device_id


def _session_snapshot(conv_id: str, session: Any) -> ToyCapabilitySnapshot:
    raw_epoch = getattr(session, "control_epoch", None)
    epoch = None if isinstance(raw_epoch, bool) or not isinstance(raw_epoch, int) else raw_epoch
    return ToyCapabilitySnapshot(
        False,
        "unresolved",
        conv_id,
        str(getattr(session, "session_id", "") or "").strip(),
        str(getattr(session, "kind", "") or "").strip(),
        str(getattr(session, "status", "") or "").strip(),
        epoch,
        str(getattr(session, "owner_client_id", "") or "").strip(),
        normalize_toy_device_id(getattr(session, "device_id", None)),
        session,
    )


def _denied(snapshot: ToyCapabilitySnapshot, reason: str) -> ToyCapabilitySnapshot:
    return ToyCapabilitySnapshot(
        False,
        reason,
        snapshot.conv_id,
        snapshot.control_session_id,
        snapshot.control_kind,
        snapshot.control_status,
        snapshot.control_epoch,
        snapshot.owner_client_id,
        snapshot.control_device_id,
        snapshot.session,
    )


def _epoch_matches(expected: Any, current: int | None) -> bool:
    if current is None:
        return False
    try:
        return int(expected) == int(current)
    except (TypeError, ValueError):
        return False


__all__ = [
    "FRONTEND_TOY_DRIVER_IDS",
    "TOY_BRIDGE_DEVICE_ID",
    "TOY_COMMAND_CAPABILITY",
    "TOY_CONTROL_KINDS",
    "ToyCapabilitySnapshot",
    "normalize_toy_device_id",
    "resolve_toy_capability_snapshot",
]
