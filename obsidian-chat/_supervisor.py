"""
后台线程稳定性工具：
- run_forever_safe: 包一个回调函数，让循环永远不退出（每轮自己 try/except）
- Watchdog: 注册若干后台线程引用，定期检查是否还活着，挂了就重启

设计目标：宁可吞掉异常打 log，也别让线程整个死掉。
"""

from __future__ import annotations

import asyncio
import threading
import time
import traceback
from typing import Callable


def run_forever_safe(
    name: str,
    body_fn: Callable[[], None],
    interval: float = 0.0,
    stop_event: threading.Event | None = None,
    max_backoff: float = 60.0,
):
    """
    在当前线程里循环调用 body_fn()，每轮 try/except。
    body_fn 抛异常时记录并退避重试（指数退避，最长 max_backoff）。
    body_fn 正常返回时按 interval 间隔继续。

    用法：threading.Thread(target=run_forever_safe, args=("voice", tick), daemon=True).start()
    """
    backoff = 1.0
    while True:
        if stop_event is not None and stop_event.is_set():
            print(f"[Supervisor:{name}] stop_event set, exiting")
            return
        try:
            body_fn()
            backoff = 1.0
            if interval > 0:
                _sleep_interruptible(interval, stop_event)
        except Exception as e:
            print(f"[Supervisor:{name}] iteration crashed: {e}")
            traceback.print_exc()
            _sleep_interruptible(backoff, stop_event)
            backoff = min(backoff * 2, max_backoff)


def _sleep_interruptible(seconds: float, stop_event: threading.Event | None):
    if stop_event is None:
        time.sleep(seconds)
        return
    end = time.time() + seconds
    while time.time() < end:
        if stop_event.is_set():
            return
        time.sleep(min(0.5, end - time.time()))


class Watchdog:
    """
    注册 (name, alive_fn, restart_fn) 三元组。
    alive_fn() -> bool：返回 False 时调用 restart_fn() 重启。
    """

    def __init__(self, poll_interval: float = 30.0):
        self._entries: list[tuple[str, Callable[[], bool], Callable[[], None]]] = []
        self._poll = poll_interval
        self._stopped = False

    def register(self, name: str, alive_fn: Callable[[], bool], restart_fn: Callable[[], None]):
        self._entries.append((name, alive_fn, restart_fn))
        print(f"[Watchdog] registered: {name}")

    def stop(self):
        self._stopped = True

    async def run(self):
        """主循环——挂在 lifespan 里 asyncio.create_task() 起来"""
        print(f"[Watchdog] started, poll={self._poll}s, watching {len(self._entries)} task(s)")
        while not self._stopped:
            await asyncio.sleep(self._poll)
            for name, alive_fn, restart_fn in self._entries:
                try:
                    if not alive_fn():
                        print(f"[Watchdog] {name} is DEAD, restarting...")
                        try:
                            restart_fn()
                            print(f"[Watchdog] {name} restarted")
                        except Exception as e:
                            print(f"[Watchdog] {name} restart failed: {e}")
                            traceback.print_exc()
                except Exception as e:
                    print(f"[Watchdog] check {name} failed: {e}")


def log_future_exception(name: str):
    """
    给 asyncio.run_coroutine_threadsafe 返回的 Future 挂 done_callback，
    异常不再被吞掉。

    用法：
        fut = asyncio.run_coroutine_threadsafe(coro(), loop)
        fut.add_done_callback(log_future_exception("voice_send"))
    """
    def _cb(fut):
        try:
            exc = fut.exception()
            if exc:
                print(f"[Future:{name}] exception: {exc}")
                traceback.print_exception(type(exc), exc, exc.__traceback__)
        except Exception:
            pass
    return _cb
