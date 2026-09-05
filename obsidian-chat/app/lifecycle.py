"""应用资源的启动登记与有界退出。"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from collections.abc import Callable

from app.background_tasks import begin_task_shutdown, shutdown_tracked_tasks


logger = logging.getLogger(__name__)


class RuntimeResources:
    def __init__(self):
        self._stops: list[tuple[str, Callable[[], None]]] = []

    def add_stop(self, name: str, callback: Callable[[], None]) -> None:
        # 在启动资源之前登记，部分启动失败也能清理。
        self._stops.append((name, callback))

    async def shutdown(self, *, timeout: float = 5.0) -> dict:
        deadline = time.monotonic() + max(0.0, timeout)
        begin_task_shutdown()
        # 先通知生产者停止，避免退出过程中继续生成任务。
        # 停止接口是同步函数，放入线程执行，不能卡住服务事件循环。
        async def stop(name, callback):
            loop = asyncio.get_running_loop()
            completed = loop.create_future()

            def finish():
                if not completed.done():
                    completed.set_result(None)

            def invoke():
                try:
                    callback()
                except Exception:
                    logger.exception("资源停止失败：%s", name)
                finally:
                    try:
                        loop.call_soon_threadsafe(finish)
                    except RuntimeError:
                        pass

            # 不占默认线程池：回调卡住时，解释器不会在关闭线程池时再次无界等待。
            threading.Thread(target=invoke, name=f"shutdown:{name}", daemon=True).start()
            await completed

        stops = {asyncio.create_task(stop(name, callback), name=f"shutdown:{name}")
                 for name, callback in reversed(self._stops)}
        self._stops.clear()
        background = asyncio.create_task(
            shutdown_tracked_tasks(timeout=max(0.0, deadline - time.monotonic() - 0.001)),
            name="shutdown:background",
        )
        all_tasks = {*stops, background}
        _, pending = await asyncio.wait(all_tasks, timeout=max(0.0, deadline - time.monotonic()))
        for task in pending:
            task.cancel()
        names = sorted(task.get_name() for task in pending)
        if background.done() and not background.cancelled():
            names.extend(background.result()["pending"])
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        if names:
            logger.warning("应用收尾超时：%s", names)
        return {"pending": names}
