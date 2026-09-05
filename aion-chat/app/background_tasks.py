from __future__ import annotations

import asyncio
import logging
import time
import weakref
from collections.abc import Awaitable
from typing import Any
from app.turn_diagnostics import current_turn


_BACKGROUND_TASKS: set[asyncio.Task[Any]] = set()
_CLOSING_LOOPS = weakref.WeakSet()
logger = logging.getLogger(__name__)


def begin_task_lifecycle() -> None:
    _CLOSING_LOOPS.discard(asyncio.get_running_loop())


def begin_task_shutdown() -> None:
    _CLOSING_LOOPS.add(asyncio.get_running_loop())


def _register(task: asyncio.Task[Any]) -> None:
    if task not in _BACKGROUND_TASKS:
        _BACKGROUND_TASKS.add(task)
        task.add_done_callback(_on_done)
    if task.get_loop() in _CLOSING_LOOPS:
        task.cancel()


def run_tracked_threadsafe(coro, loop, *, name: str | None = None):
    """保留跨线程 Future 契约，同时让哨兵、日程和语音任务参加统一收尾。"""
    entered = False

    async def run():
        nonlocal entered
        entered = True
        task = asyncio.current_task()
        if loop in _CLOSING_LOOPS:
            coro.close()
            raise asyncio.CancelledError
        if name:
            task.set_name(name)
        _register(task)
        return await coro

    wrapper = run()
    try:
        future = asyncio.run_coroutine_threadsafe(wrapper, loop)
    except BaseException:
        wrapper.close()
        coro.close()
        raise

    def close_unstarted(_future):
        if not entered:
            coro.close()

    future.add_done_callback(close_unstarted)
    return future


def create_tracked_task(coro: Awaitable[Any], *, name: str | None = None) -> asyncio.Task[Any]:
    trace = current_turn.get()
    original = coro
    entered = False
    if trace is not None:
        async def observed():
            nonlocal entered
            entered = True
            started = time.perf_counter()
            outcome = "succeeded"
            error_type = None
            try:
                return await original
            except BaseException as exc:
                outcome = "cancelled" if isinstance(exc, asyncio.CancelledError) else "failed"
                error_type = type(exc).__name__
                raise
            finally:
                await trace.record("background", outcome=outcome, metadata={
                    "task_name": name, "elapsed_ms": round((time.perf_counter() - started) * 1000, 3),
                    "error_type": error_type,
                })
        coro = observed()
    task = asyncio.create_task(coro, name=name) if name else asyncio.create_task(coro)
    _register(task)
    if trace is not None:
        def close_unstarted(_task):
            if not entered and hasattr(original, "close"):
                original.close()
        task.add_done_callback(close_unstarted)
    return task


def _on_done(task: asyncio.Task[Any]) -> None:
    _BACKGROUND_TASKS.discard(task)
    try:
        exc = task.exception()
    except asyncio.CancelledError:
        return
    if exc is None:
        return
    logger.error("后台任务异常：%s", task.get_name(), exc_info=(type(exc), exc, exc.__traceback__))


async def shutdown_tracked_tasks(*, timeout: float = 5.0) -> dict:
    """同一事件循环的任务合计有界收尾，不逐个等待五秒，也不重放动作。"""
    begin_task_shutdown()
    loop = asyncio.get_running_loop()
    current = asyncio.current_task()
    tasks = {task for task in _BACKGROUND_TASKS
             if task is not current and task.get_loop() is loop and not task.done()}
    for task in tasks:
        task.cancel()
    pending = set()
    if tasks:
        _, pending = await asyncio.wait(tasks, timeout=max(0.0, timeout))
    names = sorted(task.get_name() for task in pending)
    if names:
        logger.warning("后台收尾超时，未完成任务：%s", names)
    return {"cancelled": len(tasks), "pending": names}
