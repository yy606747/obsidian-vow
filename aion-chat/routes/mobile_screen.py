"""Mobile screen check routes used by the Android poll loop and settings UI.

Parallel to routes/pc_screen.py but every request is routed by target device:
``GET /pending`` requires a ``device_id`` query so a request meant for the
tablet is never served to the phone.
"""

from __future__ import annotations

import json

from fastapi import APIRouter, File, UploadFile
from fastapi.responses import Response
from pydantic import BaseModel

from app.mobile_screen import mobile_screen_service as service
from config import load_worldbook
from ws import manager


router = APIRouter()


class MobileScreenDecision(BaseModel):
    decision: str
    reject_reason: str = ""


class MobileScreenConfigUpdate(BaseModel):
    mobile_screen_capture_enabled: bool


@router.get("/api/mobile-screen/pending")
async def pending_mobile_screen_request(device_id: str, timeout: float = 30):
    request = await service.wait_pending(device_id, timeout)
    if not request:
        return Response(status_code=204)
    ai_name = str(load_worldbook().get("ai_name") or "AI").strip() or "AI"
    return request.to_pending(ai_name)


@router.post("/api/mobile-screen/{request_id}/decision")
async def mobile_screen_decision(request_id: str, body: MobileScreenDecision):
    try:
        request = await service.mark_decision(request_id, body.decision, body.reject_reason)
    except ValueError as exc:
        return Response(
            content=json.dumps({"error": str(exc)}),
            status_code=400,
            media_type="application/json",
        )
    if not request:
        return Response(
            content=json.dumps({"error": "not_found"}),
            status_code=404,
            media_type="application/json",
        )
    return {"ok": True, "request": request.to_public()}


@router.post("/api/mobile-screen/{request_id}/upload")
async def upload_mobile_screen(request_id: str, screenshot: UploadFile = File(...)):
    request = await service.save_uploaded_screenshot(request_id, await screenshot.read())
    if not request:
        return Response(
            content=json.dumps({"error": "not_found"}),
            status_code=404,
            media_type="application/json",
        )
    if request.status != "completed":
        return Response(
            content=json.dumps({"error": request.reject_reason or "not_approved"}),
            status_code=409,
            media_type="application/json",
        )
    return {"ok": True, "request": request.to_public()}


@router.get("/api/mobile-screen/status")
async def mobile_screen_status():
    return service.status_payload()


@router.get("/api/mobile-screen/config")
async def get_mobile_screen_config():
    return {"mobile_screen_capture_enabled": service.is_enabled()}


@router.put("/api/mobile-screen/config")
async def put_mobile_screen_config(body: MobileScreenConfigUpdate):
    service.set_enabled(body.mobile_screen_capture_enabled)
    data = {"mobile_screen_capture_enabled": service.is_enabled()}
    await manager.broadcast({"type": "mobile_screen_capture_config_changed", "data": data})
    return {"ok": True, **data}


__all__ = ["router"]
