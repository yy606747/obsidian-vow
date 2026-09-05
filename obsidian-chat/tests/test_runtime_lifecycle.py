import asyncio
from contextlib import asynccontextmanager
import time
import threading

import httpx
import pytest

import database
import main
from app.background_tasks import (
    begin_task_lifecycle, create_tracked_task, run_tracked_threadsafe, shutdown_tracked_tasks,
)
from app.lifecycle import RuntimeResources


def test_tracked_tasks_are_cancelled_and_awaited_together():
    finished = []

    async def scenario():
        begin_task_lifecycle()

        async def worker(index):
            try:
                await asyncio.Event().wait()
            finally:
                await asyncio.sleep(0.01)
                finished.append(index)

        tasks = [create_tracked_task(worker(index), name=f"worker:{index}") for index in range(3)]
        await asyncio.sleep(0)
        result = await shutdown_tracked_tasks(timeout=0.2)
        assert result == {"cancelled": 3, "pending": []}
        assert all(task.done() for task in tasks)

    asyncio.run(scenario())
    assert sorted(finished) == [0, 1, 2]


def test_task_failure_is_logged_and_shutdown_does_not_replay(caplog):
    calls = []

    async def scenario():
        begin_task_lifecycle()

        async def failed():
            calls.append("device_action")
            raise RuntimeError("合成设备故障")

        task = create_tracked_task(failed(), name="failed-device-action")
        await asyncio.gather(task, return_exceptions=True)
        await shutdown_tracked_tasks()

    asyncio.run(scenario())
    assert calls == ["device_action"]
    assert "failed-device-action" in caplog.text


def test_task_shutdown_timeout_is_total_and_reports_remaining(caplog):
    async def scenario():
        begin_task_lifecycle()
        release = asyncio.Event()

        async def slow():
            try:
                await asyncio.Event().wait()
            finally:
                await release.wait()

        tasks = [create_tracked_task(slow(), name=f"slow:{index}") for index in range(3)]
        await asyncio.sleep(0)
        started = time.monotonic()
        result = await shutdown_tracked_tasks(timeout=0.03)
        elapsed = time.monotonic() - started
        release.set()
        await asyncio.gather(*tasks, return_exceptions=True)
        return result, elapsed

    result, elapsed = asyncio.run(scenario())
    assert result["pending"] == ["slow:0", "slow:1", "slow:2"]
    assert elapsed < 0.2
    assert "收尾超时" in caplog.text


def test_shutdown_rejects_new_background_work_until_next_lifespan():
    executed = []

    async def worker():
        executed.append(True)

    async def scenario():
        begin_task_lifecycle()
        await shutdown_tracked_tasks()
        task = create_tracked_task(worker())
        await asyncio.gather(task, return_exceptions=True)
        assert task.cancelled()
        begin_task_lifecycle()
        await create_tracked_task(worker())

    asyncio.run(scenario())
    assert executed == [True]


def test_resource_failure_does_not_skip_other_cleanup(caplog):
    calls = []

    def failed_stop():
        raise RuntimeError("合成停止故障")

    async def scenario():
        begin_task_lifecycle()
        resources = RuntimeResources()
        resources.add_stop("good", lambda: calls.append("stopped"))
        resources.add_stop("bad", failed_stop)
        assert (await resources.shutdown(timeout=0.2))["pending"] == []

    asyncio.run(scenario())
    assert calls == ["stopped"]
    assert "资源停止失败" in caplog.text


def test_startup_failure_cleans_already_registered_resources(monkeypatch):
    from app.presence import sprite_library
    from app.web_search import web_search_service
    calls = []
    tasks = []

    async def noop(*_args, **_kwargs):
        return None

    async def resumed():
        tasks.append(create_tracked_task(asyncio.Event().wait(), name="startup-resumed"))

    def fail_start():
        calls.append("start")
        raise RuntimeError("合成启动故障")

    monkeypatch.setattr(main, "TEST_MODE", False)
    monkeypatch.setattr(main, "init_db", noop)
    monkeypatch.setattr(sprite_library, "ensure_seed_sprites", noop)
    monkeypatch.setattr(sprite_library, "reconcile_seed_lifecycle", noop)
    monkeypatch.setattr(web_search_service, "resume_queued", resumed)
    monkeypatch.setattr(main, "reconcile_daily_signals", lambda: None)
    monkeypatch.setattr(main, "run_startup_cleanup", lambda: None)
    monkeypatch.setattr("app.pc_screen.service.cleanup_expired_files", lambda: None)
    monkeypatch.setattr("app.mobile_screen.mobile_screen_service.cleanup_expired_files", lambda: None)
    monkeypatch.setattr(main.sentinel_runtime, "set_event_loop", lambda _loop: None)
    monkeypatch.setattr(main.sentinel_runtime, "start_monitoring", fail_start)
    monkeypatch.setattr(main.sentinel_runtime, "stop_monitoring", lambda: calls.append("stop"))

    async def scenario():
        with pytest.raises(RuntimeError, match="合成启动故障"):
            async with main.lifespan(main.app):
                pytest.fail("启动失败后不能进入服务状态")
        assert not main.app.state.ready
        assert all(task.done() for task in tasks)

    asyncio.run(scenario())
    assert calls == ["start", "stop"]


def test_health_checks_initialization_and_database_without_auth_or_models(tmp_path, monkeypatch):
    monkeypatch.setattr(database, "DB_PATH", tmp_path / "health.db")
    monkeypatch.setattr(main, "_AUTH_TOKEN", "synthetic-private-token")

    async def scenario():
        main.app.state.ready = False
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app), base_url="http://test") as client:
            assert (await client.get("/healthz")).status_code == 503
            async with main.lifespan(main.app):
                result = await client.get("/healthz")
                assert result.status_code == 200
                assert result.json() == {"status": "ok"}
                assert result.headers["cache-control"] == "no-store"
                assert (await client.get("/api/conversations")).status_code == 401
            assert (await client.get("/healthz")).status_code == 503

    asyncio.run(scenario())


def test_health_failure_discloses_no_internal_details(monkeypatch, caplog):
    @asynccontextmanager
    async def failed_db(**_kwargs):
        raise RuntimeError("private/path secret-key")
        yield

    monkeypatch.setattr(main, "get_db", failed_db)
    monkeypatch.setattr(main.app.state, "ready", True)
    result = asyncio.run(main.healthz())
    assert result.status_code == 503
    assert result.body == b'{"status":"unavailable"}'
    assert "secret-key" not in caplog.text
    assert "RuntimeError" in caplog.text


def test_health_does_not_create_a_missing_database(tmp_path, monkeypatch):
    path = tmp_path / "missing.db"
    monkeypatch.setattr(database, "DB_PATH", path)
    monkeypatch.setattr(main.app.state, "ready", True)
    assert asyncio.run(main.healthz()).status_code == 503
    assert not path.exists()


def test_blocked_stop_callback_does_not_extend_total_shutdown_budget():
    release = threading.Event()

    async def scenario():
        begin_task_lifecycle()
        resources = RuntimeResources()
        resources.add_stop("blocked", lambda: release.wait(0.5))
        started = time.monotonic()
        result = await resources.shutdown(timeout=0.03)
        elapsed = time.monotonic() - started
        release.set()
        return result, elapsed

    result, elapsed = asyncio.run(scenario())
    assert result["pending"] == ["shutdown:blocked"]
    assert elapsed < 0.2


def test_threadsafe_work_keeps_future_contract_and_participates_in_shutdown():
    async def scenario():
        begin_task_lifecycle()
        loop = asyncio.get_running_loop()
        started = asyncio.Event()
        completed = []

        async def work():
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                completed.append(True)

        future = run_tracked_threadsafe(work(), loop, name="thread-submitted")
        await started.wait()
        await shutdown_tracked_tasks(timeout=0.2)
        await asyncio.sleep(0)
        assert future.cancelled()
        assert completed == [True]

        async def forbidden():
            pytest.fail("退出后提交的任务不得执行")

        stopped = run_tracked_threadsafe(forbidden(), loop)
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wrap_future(stopped)
        begin_task_lifecycle()
        assert await asyncio.wrap_future(run_tracked_threadsafe(asyncio.sleep(0, result=7), loop)) == 7

    asyncio.run(scenario())
