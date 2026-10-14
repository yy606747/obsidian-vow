"""Owner-authenticated API; native device mutations also check session binding."""
from __future__ import annotations

import asyncio
from typing import Literal

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from app.together.service import TogetherConflict, TogetherDeviceMismatch, get_together_service

router = APIRouter(prefix="/api/together", tags=["together"])


class DeviceBody(BaseModel):
    device_id: str = Field(min_length=1, max_length=200)


class StartBody(DeviceBody):
    request_id: str = Field(min_length=1, max_length=200)
    conv_id: str = Field(min_length=1, max_length=200)
    source: Literal["screen", "camera"] = "screen"
    history_count: int = Field(default=0, ge=0, le=100)
    model_key: str | None = Field(default=None, max_length=200)


class MessageBody(DeviceBody):
    client_id: str = Field(min_length=1, max_length=200)
    text: str = Field(default="", max_length=8000)
    capture_id: str | None = Field(default=None, max_length=200)


class HeartbeatBody(DeviceBody):
    source_version: int = Field(ge=1)
    capture_state: Literal["ready", "paused", "unavailable"] = "paused"
    input_busy: bool = False


class ActionBody(DeviceBody):
    action: Literal["end", "switch_source", "pause_capture", "resume_capture", "set_model", "retry"]
    source: Literal["screen", "camera"] | None = None
    model_key: str | None = Field(default=None, max_length=200)
    message_id: str | None = Field(default=None, max_length=200)
    ended_at: float | None = Field(default=None, ge=0, allow_inf_nan=False)


async def invoke(coro):
    try:
        return await coro
    except TogetherDeviceMismatch as exc:
        raise HTTPException(403, str(exc)) from exc
    except TogetherConflict as exc:
        raise HTTPException(409, str(exc)) from exc
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from exc
    except (ValueError, TypeError) as exc:
        # No provider or transcript prose in API error details.
        detail = str(exc) if str(exc).startswith("together_") else "together_invalid_request"
        raise HTTPException(422, detail) from exc


@router.get("/config")
async def configuration(service=Depends(get_together_service)):
    from config import get_slot, list_core_models
    slot = get_slot("asr")
    user_name, ai_name = service.names_loader()
    return {**service.config.load(), "user_name": user_name, "ai_name": ai_name,
            "asr_ready": bool(slot and slot.get("model")),
            "models": [{"key": key, "image_supported": service.image_check(key)} for key in list_core_models()]}


@router.put("/config")
async def configure(body: dict, service=Depends(get_together_service)):
    return await invoke(service.configure(body))


@router.get("/sessions/current")
async def current(service=Depends(get_together_service)):
    return await service.current()


@router.post("/sessions")
async def start(body: StartBody, service=Depends(get_together_service)):
    # Consent dialogs can take arbitrary time. No model call, active accounting
    # or initiative hold begins until the native conversation actually connects.
    return await invoke(service.start(**body.model_dump(), wait_for_device=True))


@router.get("/sessions/{session_id}")
async def session(session_id: str, device_id: str | None = None, service=Depends(get_together_service)):
    return await invoke(service.get(session_id, device_id))


@router.post("/sessions/{session_id}/messages")
async def message(session_id: str, body: MessageBody, service=Depends(get_together_service)):
    return await invoke(service.message(session_id, **body.model_dump()))


@router.post("/sessions/{session_id}/heartbeat")
async def heartbeat(session_id: str, body: HeartbeatBody, service=Depends(get_together_service)):
    return await invoke(service.heartbeat(session_id, **body.model_dump()))


@router.post("/sessions/{session_id}/actions")
async def action(session_id: str, body: ActionBody, service=Depends(get_together_service)):
    return await invoke(service.action(session_id, **body.model_dump()))


@router.get("/sessions/{session_id}/events")
async def events(session_id: str, device_id: str, after: int = Query(default=0, ge=0),
                 timeout: float = Query(default=25, ge=0, le=25), service=Depends(get_together_service)):
    await invoke(service.require(session_id, device_id))
    deadline = asyncio.get_running_loop().time()+timeout
    while True:
        result = await service.repo.events(session_id, after)
        row = await service.require(session_id, device_id)
        if result["events"] or result["resync"] or row["status"] == "ended" or not service.enabled() or asyncio.get_running_loop().time() >= deadline:
            return {**result, "status": row["status"], "enabled": service.enabled(), "source_version": row["source_version"], "server_now": service.now()}
        await asyncio.sleep(.2)


@router.post("/sessions/{session_id}/captures")
async def capture(session_id: str, body: DeviceBody, service=Depends(get_together_service)):
    result = await invoke(service.request_capture(session_id, **body.model_dump()))
    return service.capture_view(result)


@router.post("/sessions/{session_id}/captures/{capture_id}/result")
async def capture_result(session_id: str, capture_id: str, device_id: str = Form(...), source_version: int = Form(...),
                         captured_at: float | None = Form(None), error: str | None = Form(None),
                         image: UploadFile | None = File(None), service=Depends(get_together_service)):
    await invoke(service.require(session_id, device_id, active=True))
    data = await image.read(service.config.load()["image_max_bytes"]+1) if image else None
    return await invoke(service.receive_capture(session_id, capture_id, device_id=device_id,
        source_version=source_version, captured_at=captured_at, error=error, data=data))


@router.get("/sessions/{session_id}/captures/{capture_id}/image")
async def capture_image(session_id: str, capture_id: str, device_id: str, service=Depends(get_together_service)):
    await invoke(service.require(session_id, device_id, active=True))
    row = await service.repo.capture(capture_id)
    if not row or row["session_id"] != session_id or not row["filename"] or row["status"] not in {"ready", "attached"}:
        raise HTTPException(404, "together_draft_unavailable")
    path = service.media.path(row["filename"])
    if not path.is_file():
        raise HTTPException(404, "together_draft_unavailable")
    return FileResponse(path, media_type="image/jpeg", headers={"Cache-Control": "no-store"})


@router.post("/sessions/{session_id}/asr")
async def asr(session_id: str, device_id: str = Form(...), recording: UploadFile = File(...), service=Depends(get_together_service)):
    await invoke(service.require(session_id, device_id, active=True))
    maximum = service.config.load()["recording_seconds"]*32000+4096
    data = await recording.read(maximum+1)
    return await invoke(service.transcribe(session_id, device_id=device_id, data=data))


@router.get("/writebacks")
async def writebacks(service=Depends(get_together_service)):
    return await service.repo.read("""SELECT id,session_id,kind,status,error,attempts FROM together_writebacks
        WHERE status!='done' ORDER BY created_at LIMIT 100""", many=True)


@router.post("/writebacks/{job_id}/retry")
async def retry_writeback(job_id: str, service=Depends(get_together_service)):
    from app.together.writeback import Writebacks
    await invoke(Writebacks(service.repo).retry(job_id))
    return {"ok": True}
