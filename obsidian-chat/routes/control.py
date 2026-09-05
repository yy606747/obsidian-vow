from __future__ import annotations

import json
from typing import Any, Literal

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from app.control import (
    ControlClaimRejected,
    ControlOwnerMismatch,
    ControlSessionNotFound,
    control_session_service,
)

router = APIRouter()

TOY_BRIDGE_DEVICE_ID = "browser_toy_bridge"
FRONTEND_TOY_DRIVER_IDS = frozenset({"sosexy", "cx492b", "sk30", "sk40"})


class ControlSessionStartBody(BaseModel):
    conv_id: str = Field(min_length=1)
    kind: Literal["dom", "whisper", "tide"]
    owner_client_id: str = Field(min_length=1)
    device_id: str | None = None
    control_resource_id: str | None = None
    safeword_set: bool = False


class ControlSessionOwnerBody(BaseModel):
    owner_client_id: str = Field(min_length=1)


class ControlSessionSnapshotBody(ControlSessionOwnerBody):
    frontend_snapshot_json: Any = None


class ControlSessionEndBody(ControlSessionOwnerBody):
    close_reason: str = Field(min_length=1)


def _payload(session):
    if session is None:
        return None
    if hasattr(session, "to_api_dict"):
        data = session.to_api_dict()
        raw_snapshot = getattr(session, "frontend_snapshot_json", None)
        try:
            parsed_snapshot = json.loads(raw_snapshot) if raw_snapshot else {}
        except Exception:
            parsed_snapshot = {}
        data["frontend_snapshot"] = parsed_snapshot if isinstance(parsed_snapshot, dict) else {}
        return data
    return session


def _raise_session_error(exc: Exception) -> None:
    if isinstance(exc, ControlSessionNotFound):
        raise HTTPException(status_code=404, detail="session_not_found") from exc
    if isinstance(exc, ControlOwnerMismatch):
        raise HTTPException(status_code=403, detail="owner_mismatch") from exc
    if isinstance(exc, ControlClaimRejected):
        raise HTTPException(status_code=409, detail="claim_rejected") from exc
    raise exc


def _normalize_control_device_id(kind: str, device_id: str | None) -> str | None:
    value = str(device_id or "").strip()
    if kind == "tide":
        return value or "muse"
    if not value:
        return None
    if kind in {"dom", "whisper"} and value in FRONTEND_TOY_DRIVER_IDS:
        return TOY_BRIDGE_DEVICE_ID
    return value


def _normalize_control_resource_id(kind: str, device_id: str | None, control_resource_id: str | None) -> str | None:
    value = str(control_resource_id or "").strip()
    if value:
        return value
    if kind != "tide":
        return None
    device = str(device_id or "muse").strip().lower() or "muse"
    return f"toy:{device}"


@router.post("/api/control/sessions/start")
async def start_control_session(body: ControlSessionStartBody):
    session = await control_session_service.start(
        conv_id=body.conv_id,
        kind=body.kind,
        owner_client_id=body.owner_client_id,
        device_id=_normalize_control_device_id(body.kind, body.device_id),
        control_resource_id=_normalize_control_resource_id(body.kind, body.device_id, body.control_resource_id),
        safeword_set=body.safeword_set,
    )
    return _payload(session)


@router.post("/api/control/sessions/{session_id}/heartbeat")
async def heartbeat_control_session(session_id: str, body: ControlSessionOwnerBody):
    try:
        return _payload(await control_session_service.heartbeat(
            session_id=session_id,
            owner_client_id=body.owner_client_id,
        ))
    except Exception as exc:
        _raise_session_error(exc)


@router.post("/api/control/sessions/{session_id}/snapshot")
async def snapshot_control_session(session_id: str, body: ControlSessionSnapshotBody):
    try:
        return _payload(await control_session_service.snapshot(
            session_id=session_id,
            owner_client_id=body.owner_client_id,
            frontend_snapshot_json=body.frontend_snapshot_json,
        ))
    except Exception as exc:
        _raise_session_error(exc)


@router.post("/api/control/sessions/{session_id}/end")
async def end_control_session(session_id: str, body: ControlSessionEndBody):
    try:
        return _payload(await control_session_service.end(
            session_id=session_id,
            owner_client_id=body.owner_client_id,
            close_reason=body.close_reason,
        ))
    except Exception as exc:
        _raise_session_error(exc)


@router.get("/api/control/sessions/tide/current")
async def get_current_tide_control_session():
    return _payload(await control_session_service.get_current_tide())


@router.post("/api/control/sessions/tide/{session_id}/claim")
async def claim_tide_control_session(session_id: str, body: ControlSessionOwnerBody):
    try:
        return _payload(await control_session_service.claim_tide_session(
            session_id=session_id,
            owner_client_id=body.owner_client_id,
        ))
    except Exception as exc:
        _raise_session_error(exc)


@router.get("/api/control/sessions/current")
async def get_current_control_session(conv_id: str):
    return _payload(await control_session_service.get_current(conv_id=conv_id))
