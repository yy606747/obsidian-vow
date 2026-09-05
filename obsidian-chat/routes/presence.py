"""Authenticated polling, acknowledgement, and sprite-sync API."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query, status
from fastapi.responses import Response
from pydantic import BaseModel, Field

from app.background_tasks import create_tracked_task

from app.presence.service import (
    PresenceDeliveryError,
    PresenceInvalidTransition,
    PresenceNotFound,
    presence_service,
)
from app.presence.sprites import SpriteLibraryError, sprite_library
from app.presence.summon import (
    SummonEventError,
    normalize_summon_id,
    resolve_summon_target,
    summon_coordinator,
    summon_event_repository,
)
from config import load_ai_behavior


router = APIRouter(prefix="/api/presence", tags=["presence"])


class PresenceAck(BaseModel):
    status: str
    reason: str = ""
    actual_playback_ms: int | None = None
    device_id: str = "pc"


class SpriteSyncAck(BaseModel):
    device_id: str = "pc"


class PresenceSummonRequest(BaseModel):
    summon_id: str = Field(min_length=1, max_length=80)
    device_id: str = Field(default="pc", min_length=1, max_length=64)


@router.post("/summon", status_code=status.HTTP_202_ACCEPTED)
async def summon_presence(body: PresenceSummonRequest):
    if not load_ai_behavior().get("presence_summon_enabled", False):
        raise HTTPException(status_code=404, detail="presence_summon_disabled")
    try:
        summon_id = normalize_summon_id(body.summon_id)
    except SummonEventError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    target = await resolve_summon_target()
    if target is None:
        raise HTTPException(status_code=409, detail="presence_summon_no_target")
    try:
        inserted = await summon_event_repository.insert(
            summon_id=summon_id,
            conv_id=str(target["conv_id"]),
            device_id=body.device_id,
        )
    except SummonEventError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    if inserted["inserted"]:
        create_tracked_task(
            summon_coordinator.process(
                summon_id=summon_id,
                target=target,
                device_id=body.device_id,
            ),
            name=f"presence_summon:{summon_id}",
        )
    return {"accepted": True, "summon_id": summon_id}


@router.get("/pending")
async def pending_presence(
    timeout: float = Query(default=30.0, ge=0.0, le=30.0),
    device_id: str = "pc",
):
    try:
        event = await presence_service.poll_pending(
            timeout=timeout, device_id=device_id
        )
    except PresenceDeliveryError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if event is None:
        return Response(status_code=204)
    return event


@router.post("/{event_id}/ack")
async def acknowledge_presence(event_id: str, body: PresenceAck):
    try:
        return await presence_service.ack(
            event_id,
            status=body.status,
            reason=body.reason,
            actual_playback_ms=body.actual_playback_ms,
            device_id=body.device_id,
        )
    except PresenceNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except PresenceInvalidTransition as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except PresenceDeliveryError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/sprites/manifest")
async def sprite_manifest(device_id: str = "pc"):
    await presence_service.touch_agent(device_id=device_id)
    return {
        "device_id": device_id,
        "sprites": await sprite_library.manifest(device_id=device_id),
    }


@router.get("/sprites/{sprite_hash}")
async def download_sprite(sprite_hash: str):
    try:
        data, row = await sprite_library.file_for_hash(sprite_hash)
    except SpriteLibraryError as exc:
        status = 404 if str(exc) == "presence_sprite_not_found" else 400
        raise HTTPException(status_code=status, detail=str(exc)) from exc
    return Response(
        content=data,
        media_type="image/png",
        headers={
            "ETag": f'"{str(row["sprite_hash"])[7:]}"',
            "Cache-Control": "private, max-age=31536000, immutable",
        },
    )


@router.post("/sprites/{sprite_hash}/synced")
async def acknowledge_sprite_sync(sprite_hash: str, body: SpriteSyncAck):
    try:
        result = await sprite_library.mark_synced(
            sprite_hash, device_id=body.device_id
        )
        await presence_service.touch_agent(device_id=body.device_id)
        return {"ok": True, **result}
    except SpriteLibraryError as exc:
        status = 404 if str(exc) == "presence_sprite_not_found" else 400
        raise HTTPException(status_code=status, detail=str(exc)) from exc


__all__ = ["router"]
