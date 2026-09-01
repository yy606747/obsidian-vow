"""DeviceService read/command API."""

from __future__ import annotations

from typing import Any, Optional

from fastapi import APIRouter
from pydantic import BaseModel, Field

from app.devices import DeviceStatus, device_service


router = APIRouter()


class DeviceCommandBody(BaseModel):
    command: str = Field(min_length=1)
    params: dict[str, Any] = Field(default_factory=dict)
    request_id: Optional[str] = None


class DeviceStateReportBody(BaseModel):
    status: DeviceStatus
    name: Optional[str] = None
    kind: Optional[str] = None
    capabilities: list[str] = Field(default_factory=list)
    battery: Optional[int] = None
    metadata: dict[str, Any] = Field(default_factory=dict)


@router.get("/api/devices")
async def list_devices():
    return await device_service.list_devices()


@router.get("/api/devices/{device_id}")
async def get_device(device_id: str):
    return await device_service.get_device(device_id)


@router.post("/api/devices/{device_id}/commands")
async def execute_device_command(device_id: str, body: DeviceCommandBody):
    return await device_service.execute_command(
        device_id,
        body.command,
        body.params,
        request_id=body.request_id,
    )


@router.post("/api/devices/{device_id}/state")
async def report_device_state(device_id: str, body: DeviceStateReportBody):
    return await device_service.report_state(
        device_id,
        status=body.status,
        name=body.name,
        kind=body.kind,
        capabilities=body.capabilities,
        battery=body.battery,
        metadata=body.metadata,
    )
