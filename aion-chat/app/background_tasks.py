from __future__ import annotations

import asyncio
import traceback
from collections.abc import Awaitable
from typing import Any


_BACKGROUND_TASKS: set[asyncio.Task[Any]] = set()


def create_tracked_task(coro: Awaitable[Any], *, name: str | None = None) -> asyncio.Task[Any]:
    task = asyncio.create_task(coro, name=name) if name else asyncio.create_task(coro)
    _BACKGROUND_TASKS.add(task)
    task.add_done_callback(_on_done)
    return task


def _on_done(task: asyncio.Task[Any]) -> None:
    _BACKGROUND_TASKS.discard(task)
    try:
        exc = task.exception()
    except asyncio.CancelledError:
        return
    if exc is None:
        return
    print(f"[BackgroundTask] {task.get_name()} failed: {type(exc).__name__}: {exc}")
    traceback.print_exception(type(exc), exc, exc.__traceback__)
