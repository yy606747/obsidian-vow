from __future__ import annotations

from fastapi import APIRouter
from pydantic import BaseModel, Field

from .service import daily_chat

router = APIRouter(prefix='/api/chat/daily')


class DailyChatSettings(BaseModel):
    inherit_count: int | None = Field(default=None, ge=1, le=100)
    initial_count: int | None = Field(default=None, ge=1, le=100)
    enabled: bool | None = None


@router.get('')
async def current_daily_chat():
    return await daily_chat.ensure_current()


@router.put('')
async def configure_daily_chat(body: DailyChatSettings):
    return await daily_chat.configure(**body.model_dump(exclude_none=True))
