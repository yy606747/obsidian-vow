from __future__ import annotations

import asyncio
import logging
import threading
import time
from datetime import datetime, timedelta

from ws import manager

from . import store, trigger

log = logging.getLogger("schedule")
_last_missed_summary: dict | None = None


def get_last_missed_summary() -> dict | None:
    global _last_missed_summary
    out = _last_missed_summary
    _last_missed_summary = None
    return out


async def catch_up_missed_alarms(grace_seconds: int = 120) -> int:
    cutoff = (datetime.now() - timedelta(seconds=grace_seconds)).strftime("%Y-%m-%d %H:%M")
    missed = await store.list_missed_candidates(cutoff)
    if not missed:
        return 0
    await store.mark_missed([item["id"] for item in missed])
    summary = {
        "count": len(missed),
        "items": [
            {"id": item["id"], "type": item["type"], "trigger_at": item["trigger_at"], "content": item["content"]}
            for item in missed[:10]
        ],
    }
    global _last_missed_summary
    _last_missed_summary = summary
    log.info("catch-up: marked %d missed", len(missed))
    try:
        await manager.broadcast({"type": "missed_alarms", "data": summary})
    except Exception as exc:
        log.warning("broadcast missed_alarms failed: %s", exc)
    return len(missed)


class ScheduleManager:
    def __init__(self):
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._running = False
        self._tick_in_flight = False

    def set_event_loop(self, loop: asyncio.AbstractEventLoop):
        self._loop = loop

    def start(self):
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(target=self._check_loop, daemon=True)
        self._thread.start()
        log.info("ScheduleManager started")

    def stop(self):
        self._running = False

    def _check_loop(self):
        while self._running:
            try:
                if not self._loop:
                    pass
                elif self._tick_in_flight:
                    log.warning("previous tick still in flight, skipping")
                else:
                    self._tick_in_flight = True
                    fut = asyncio.run_coroutine_threadsafe(self._tick_safe(), self._loop)
                    fut.add_done_callback(self._on_tick_done)
            except Exception as exc:
                log.error("schedule loop error: %s", exc)
            for _ in range(60):
                if not self._running:
                    return
                time.sleep(0.5)

    def _on_tick_done(self, fut):
        self._tick_in_flight = False
        try:
            exc = fut.exception()
            if exc:
                log.error("schedule tick exception: %s", exc)
        except Exception:
            pass

    async def _tick_safe(self):
        try:
            await asyncio.wait_for(self._tick(), timeout=120)
        except asyncio.TimeoutError:
            log.error("schedule tick timeout (>120s); next tick will run")
        except Exception as exc:
            log.error("schedule tick failed: %s", exc)

    async def _tick(self):
        due = await store.list_due(datetime.now().strftime("%Y-%m-%d %H:%M"))
        if not due:
            return
        for item in due:
            await store.mark_triggered(item["id"])
        await trigger.fire_due_items(due)

    async def _fire_alarm(self, item: dict):
        await store.mark_triggered(item["id"])
        await trigger.fire_due_items([item])

    async def _fire_monitor(self, item: dict):
        await store.mark_triggered(item["id"])
        await trigger.fire_due_items([item])


schedule_mgr = ScheduleManager()
