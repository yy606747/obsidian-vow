"""Recoverable memory writes and at-most-once acceptance dispatch."""
import asyncio
import logging
import time
import aiosqlite

from .repository import repository

log = logging.getLogger(__name__)


async def run_pending(*, lane=None, spawn=False):
    from app.chat.worldbook import load_worldbook_names
    from app.daily_chat.service import daily_chat
    from app.memory_v2.service import memory_service
    from .prompts import memory
    await daily_chat.ensure_current()
    async with repository.db() as db:
        db.row_factory = aiosqlite.Row
        rows = await (await db.execute("SELECT * FROM look_at_me_requests WHERE (status='active' AND core_state='pending') OR (outcome IS NOT NULL AND memory_state='pending')")).fetchall()
    for raw in rows:
        r = dict(raw)
        if lane == 'core' and r['outcome'] or lane == 'memory' and not r['outcome']:
            continue
        if r['outcome']:
            async with repository.db() as db:
                claim = await db.execute("UPDATE look_at_me_requests SET memory_state='running' WHERE id=? AND memory_state='pending'", (r['id'],))
                await db.commit()
            if not claim.rowcount:
                continue
            try:
                user_name, ai_name = load_worldbook_names()
                mem = await memory_service.create_memory(
                    memory(r,user_name=user_name,ai_name=ai_name,timezone=daily_chat.timezone()),
                    'event',source_conv=r['origin_conv_id'],source_start_ts=r['started_at'],source_end_ts=r['ended_at'],
                    idempotency_key='look_at_me:'+r['id'])
                async with repository.db() as db:
                    await db.execute("UPDATE look_at_me_requests SET memory_state='done',memory_id=? WHERE id=?", (mem['id'],r['id']))
                    await db.commit()
            except Exception:
                log.exception('Look at me memory write failed')
                async with repository.db() as db:
                    await db.execute("UPDATE look_at_me_requests SET memory_state='pending' WHERE id=?", (r['id'],))
                    await db.commit()
        elif r['expected_end_at'] > time.time():
            async with repository.db() as db:
                claim = await db.execute("UPDATE look_at_me_requests SET core_state='running' WHERE id=? AND status='active' AND core_state='pending' AND expected_end_at>?", (r['id'],time.time()))
                await db.commit()
            if not claim.rowcount:
                continue
            if spawn:
                from app.background_tasks import create_tracked_task
                create_tracked_task(_dispatch_acceptance(r), name='look_at_me_accept:'+r['id'])
            else:
                await _dispatch_acceptance(r)



async def run_loop():
    while True:
        try:
            await run_pending(lane='memory')
        except Exception:
            log.exception('Look at me task recovery failed')
        await asyncio.sleep(3)


async def run_core_loop():
    while True:
        try:
            await run_pending(lane='core', spawn=True)
        except Exception:
            log.exception('Look at me Core dispatch deferred')
        await asyncio.sleep(1)


async def _dispatch_acceptance(r):
    from app.daily_chat.service import daily_chat
    try:
        from opportunity import run_autonomous_turn
        from config import DEFAULT_MODEL
        await run_autonomous_turn(kind='look_at_me_accept',now=time.time(),target={
            'conv_id':daily_chat.state['active_conv_id'],'model_key':r['model_key'] or DEFAULT_MODEL,
            'last_user_ts':r['accepted_at'],'look_at_me_request_id':r['id']})
    except Exception:
        log.exception('Look at me acceptance turn failed')
    finally:
        async with repository.db() as db:
            await db.execute("UPDATE look_at_me_requests SET core_state='done' WHERE id=?", (r['id'],))
            await db.commit()
