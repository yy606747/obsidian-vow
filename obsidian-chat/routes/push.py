from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from app.web_push import repository
from app.web_push.keys import application_server_key


router = APIRouter(prefix="/api/push", tags=["push"])


class PushKeys(BaseModel):
    p256dh: str = Field(min_length=1)
    auth: str = Field(min_length=1)


class PushSubscription(BaseModel):
    endpoint: str = Field(min_length=1)
    keys: PushKeys


class PushUnsubscribe(BaseModel):
    endpoint: str = Field(min_length=1)


def _normalized_endpoint(value: str) -> str:
    endpoint = value.strip()
    if not endpoint.startswith("https://"):
        raise HTTPException(status_code=422, detail="push endpoint must use https")
    return endpoint


@router.get("/public-key")
async def public_key():
    return {"public_key": application_server_key()}


@router.post("/subscribe")
async def subscribe(body: PushSubscription):
    await repository.upsert_subscription(
        endpoint=_normalized_endpoint(body.endpoint),
        p256dh=body.keys.p256dh.strip(),
        auth=body.keys.auth.strip(),
    )
    return {"ok": True}


@router.post("/unsubscribe")
async def unsubscribe(body: PushUnsubscribe):
    await repository.delete_subscription(_normalized_endpoint(body.endpoint))
    return {"ok": True}
