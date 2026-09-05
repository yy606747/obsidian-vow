"""Disabled legacy local camera API.

The cloud-oriented monitor no longer exposes local webcam control. Sentinel
status and logs live under /api/sentinel/*.
"""

from __future__ import annotations

from fastapi import APIRouter
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel

from camera import CAMERA_DISABLED_REASON


router = APIRouter()


class CamConfigUpdate(BaseModel):
    camera_index: int | None = None
    auto_interval_min: int | None = None
    auto_interval_max: int | None = None
    max_screenshots: int | None = None
    quiet_hours_enabled: bool | None = None
    quiet_hours_start: str | None = None
    quiet_hours_end: str | None = None


class CropUpdate(BaseModel):
    zoom: float = 1.0
    cx: float = 0.5
    cy: float = 0.5


def _disabled_payload() -> dict:
    return {
        "ok": False,
        "enabled": False,
        "error": CAMERA_DISABLED_REASON,
        "message": "旧本地摄像头监控已禁用；后续摄像头输入会作为 Sentinel evidence adapter 重新接入。",
        "sentinel_status_url": "/api/sentinel/status",
        "sentinel_logs_url": "/api/sentinel/logs",
    }


def _disabled_response() -> JSONResponse:
    return JSONResponse(status_code=410, content=_disabled_payload())


@router.get("/api/cam/status")
async def cam_status():
    payload = _disabled_payload()
    payload.update({
        "camera_open": False,
        "monitoring": False,
        "camera_index": None,
        "next_capture_in": 0,
    })
    return payload


@router.get("/api/cam/cameras")
async def list_cameras():
    payload = _disabled_payload()
    payload.update({"cameras": [], "current": None})
    return payload


@router.post("/api/cam/open")
async def cam_open(camera_index: int = 0):
    return _disabled_response()


@router.post("/api/cam/close")
async def cam_close():
    return _disabled_response()


@router.post("/api/cam/monitor/start")
async def cam_monitor_start():
    return _disabled_response()


@router.post("/api/cam/monitor/stop")
async def cam_monitor_stop():
    return _disabled_response()


@router.post("/api/cam/screenshot")
async def cam_screenshot():
    return _disabled_response()


@router.put("/api/cam/config")
async def update_cam_config(body: CamConfigUpdate):
    return _disabled_response()


@router.get("/api/cam/frame")
async def cam_frame():
    return Response(status_code=410)


@router.get("/api/cam/crop")
async def get_crop():
    payload = _disabled_payload()
    payload.update({"zoom": 1.0, "cx": 0.5, "cy": 0.5})
    return payload


@router.put("/api/cam/crop")
async def set_crop(body: CropUpdate):
    return _disabled_response()


@router.get("/api/cam/logs")
async def list_log_dates():
    return _disabled_response()


@router.get("/api/cam/logs/today/entries")
async def get_today_logs():
    return _disabled_response()


@router.get("/api/cam/logs/{date_str}")
async def get_log_entries(date_str: str):
    return _disabled_response()
