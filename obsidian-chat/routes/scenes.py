from typing import Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from app.scenes.session import SceneConflict, SceneOwnerMismatch, get_scene_service

router = APIRouter(prefix="/api/scenes", tags=["scenes"])


class OwnerBody(BaseModel):
    owner_id: str = Field(min_length=1, max_length=200)


class ActionBody(OwnerBody):
    generation: int | None = Field(default=None, ge=0)


class StartBody(OwnerBody):
    request_id: str = Field(min_length=1, max_length=200)
    conv_id: str = Field(min_length=1, max_length=200)
    device_ids: list[str] = Field(min_length=1, max_length=16)
    safeword: str = Field(min_length=1, max_length=64)


class MessageBody(OwnerBody):
    text: str = Field(min_length=1, max_length=8000)


class BridgeBody(OwnerBody):
    connected: list[str] = Field(default_factory=list, max_length=16)


class OutputBody(BridgeBody):
    generation: int = Field(ge=0)
    adopted: dict = Field(default_factory=dict)


class FeedbackBody(BaseModel):
    rating: Literal["up", "down"] | None = None
    reason: str = Field(default="", max_length=4000)


async def invoke(coro):
    try:
        return await coro
    except SceneOwnerMismatch as exc:
        raise HTTPException(403, detail=str(exc)) from exc
    except SceneConflict as exc:
        raise HTTPException(409, detail=str(exc)) from exc
    except LookupError as exc:
        raise HTTPException(404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(422, detail=str(exc)) from exc


@router.get("/config")
async def get_config(service=Depends(get_scene_service)):
    from config import get_slot
    slot = get_slot("scene_compiler")
    return {**service.config.load(), "compiler_ready": bool(slot and slot.get("model"))}


@router.put("/config")
async def put_config(body: dict, service=Depends(get_scene_service)):
    return await invoke(service.configure(body))


@router.get("/devices")
async def devices(service=Depends(get_scene_service)):
    return service.device_list()


@router.get("/hardware")
async def hardware():
    from app.scenes.hardware import devices
    return devices()


@router.post("/bridge")
async def bridge(body: BridgeBody, service=Depends(get_scene_service)):
    return await invoke(service.report_bridge(**body.model_dump()))


@router.post("/sessions/{session_id}/output")
async def output(session_id: str, body: OutputBody, service=Depends(get_scene_service)):
    return await invoke(service.output(session_id, **body.model_dump()))


@router.post("/devices/{device_id}/{action}")
async def connect(device_id: str, action: Literal["connect", "disconnect"], service=Depends(get_scene_service)):
    return await invoke(service.connect(device_id, action == "connect"))


@router.get("/sessions/current")
async def current(service=Depends(get_scene_service)):
    return await service.current()


@router.get("/sessions")
async def history(service=Depends(get_scene_service)):
    return await service.history()


@router.post("/sessions")
async def start(body: StartBody, service=Depends(get_scene_service)):
    return await invoke(service.start(**body.model_dump()))


@router.get("/sessions/{session_id}")
async def get_session(session_id: str, service=Depends(get_scene_service)):
    return await invoke(service.get(session_id))


@router.post("/sessions/{session_id}/messages")
async def message(session_id: str, body: MessageBody, service=Depends(get_scene_service)):
    return await invoke(service.message(session_id, **body.model_dump()))


@router.post("/sessions/{session_id}/heartbeat")
async def heartbeat(session_id: str, body: OwnerBody, service=Depends(get_scene_service)):
    return await invoke(service.heartbeat(session_id, **body.model_dump()))


@router.post("/sessions/{session_id}/{action}")
async def action(session_id: str, action: Literal["pause", "resume", "end", "finish"], body: ActionBody, service=Depends(get_scene_service)):
    return await invoke(service.action(session_id, action=action, **body.model_dump()))


@router.put("/sessions/{session_id}/feedback")
async def feedback(session_id: str, body: FeedbackBody, service=Depends(get_scene_service)):
    return await invoke(service.feedback(session_id, **body.model_dump()))


@router.post("/panic")
async def panic(service=Depends(get_scene_service)):
    await service.panic()
    return {"stopped": True}
