"""PC screen check routes used by the remote PC agent and settings UI."""

from __future__ import annotations

import json

from fastapi import APIRouter, File, UploadFile
from fastapi.responses import Response
from pydantic import BaseModel

from app.pc_screen import service
from config import load_worldbook
from ws import manager


router = APIRouter()


class ScreenDecision(BaseModel):
    decision: str
    reject_reason: str = ""


class ScreenConfigUpdate(BaseModel):
    screen_capture_enabled: bool


@router.get("/api/pc-screen/pending")
async def pending_screen_request(timeout: float = 30):
    request = await service.wait_pending_request(timeout)
    if not request:
        return Response(status_code=204)
    ai_name = str(load_worldbook().get("ai_name") or "AI").strip() or "AI"
    return {"request_id": request.request_id, "reason": request.reason, "ai_name": ai_name}


@router.post("/api/pc-screen/{request_id}/decision")
async def screen_decision(request_id: str, body: ScreenDecision):
    try:
        request = await service.mark_decision(
            request_id,
            body.decision,
            body.reject_reason,
        )
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


@router.post("/api/pc-screen/{request_id}/upload")
async def upload_screen(request_id: str, screenshot: UploadFile = File(...)):
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


@router.get("/api/pc-screen/status")
async def screen_status():
    return service.status_payload()


@router.get("/api/pc-screen/config")
async def get_screen_config():
    return {"screen_capture_enabled": service.is_screen_capture_enabled()}


@router.put("/api/pc-screen/config")
async def put_screen_config(body: ScreenConfigUpdate):
    service.set_screen_capture_enabled(body.screen_capture_enabled)
    data = {"screen_capture_enabled": service.is_screen_capture_enabled()}
    await manager.broadcast({"type": "screen_capture_config_changed", "data": data})
    return {"ok": True, **data}
