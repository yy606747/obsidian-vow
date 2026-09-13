"""Authenticated by the application's existing API middleware."""
import asyncio
import time
from typing import Literal

from fastapi import APIRouter, HTTPException
from fastapi.responses import Response
from pydantic import BaseModel, Field

from app.look_at_me.repository import repository
from app.look_at_me.service import configuration, configure, public_request

router = APIRouter(prefix='/api/look-at-me')


class ConfigBody(BaseModel):
    enabled: bool
    device_id: str


class Event(BaseModel):
    event_id: str = Field(min_length=1,max_length=100)
    sequence: int = Field(ge=1)
    type: Literal['notified','offered','accepted','rejected','timed_out','started','finished','expired','delivery_failed','start_failed','cancelled']
    occurred_at: float = Field(gt=0,allow_inf_nan=False)
    payload: dict = Field(default_factory=dict)


class EventsBody(BaseModel):
    device_id: str
    events: list[Event] = Field(min_length=1,max_length=100)


@router.get('/config')
async def config():
    return configuration()


@router.put('/config')
async def put_config(body: ConfigBody):
    try:
        data = configure(**body.model_dump())
    except ValueError as e:
        raise HTTPException(400,str(e))
    from ws import manager
    await manager.broadcast({'type':'look_at_me_config','data':data})
    return data


@router.get('/pending')
async def pending(device_id: str, timeout: float = 30):
    until = time.monotonic() + max(0,min(timeout,30))
    while True:
        cfg = configuration()
        if not cfg['enabled'] or cfg['device_id'] != device_id:
            return {'enabled':False}
        r = await repository.pending(device_id)
        if r:
            from app.chat.worldbook import load_worldbook_names
            from app.daily_chat.service import daily_chat
            state = await daily_chat.ensure_current()
            return {'enabled':True,'request':{**public_request(r),'created_at':r['created_at'],
                    'conv_id':state.get('active_conv_id'),'ai_name':load_worldbook_names()[1]},'server_now':time.time()}
        if time.monotonic() >= until:
            return Response(status_code=204)
        await asyncio.sleep(.5)


@router.post('/{request_id}/events')
async def events(request_id: str, body: EventsBody):
    from app.daily_chat.service import daily_chat, calendar_day
    state = await daily_chat.ensure_current()
    try:
        r = await repository.events(request_id,body.device_id,[e.model_dump() for e in body.events],
                                    day_of=lambda at:calendar_day(at,daily_chat.timezone())[0],current_conv=state.get('active_conv_id'),
                                    valid_convs=daily_chat.scope(state.get('active_conv_id') or ''))
    except (ValueError,TypeError,OverflowError) as e:
        raise HTTPException(403 if str(e)=='device_mismatch' else 400,str(e))
    from ws import manager
    await manager.broadcast({'type':'look_at_me','data':public_request(r)})
    return {'ok':True,'processed_seq':r['processed_seq'],'request':public_request(r)}


@router.get('/status')
async def status(device_id: str = ''):
    import aiosqlite
    cfg = configuration()
    async with repository.db() as db:
        db.row_factory = aiosqlite.Row
        r = await (await db.execute('SELECT * FROM look_at_me_requests WHERE device_id=? ORDER BY created_at DESC LIMIT 1', (device_id or cfg['device_id'],))).fetchone()
    from app.daily_chat.service import daily_chat
    state = await daily_chat.ensure_current()
    return {**cfg,'conv_id':state.get('active_conv_id'),'server_now':time.time(),'request':public_request(dict(r)) if r else None}
