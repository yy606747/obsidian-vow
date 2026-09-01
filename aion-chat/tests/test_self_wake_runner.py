import asyncio

from app.self_wake.service import SelfWakeRunner


def _run(awaitable):
    return asyncio.run(awaitable)


class _Repo:
    def __init__(self, batches):
        self.batches = list(batches)
        self.claim_calls = 0
        self.finishes = []
        self._lock = asyncio.Lock()

    async def claim_due_batch(self):
        async with self._lock:
            self.claim_calls += 1
            return self.batches.pop(0) if self.batches else []

    async def finish_trigger(self, wake_id, **kwargs):
        self.finishes.append((wake_id, kwargs))
        return True


def test_scan_once_launches_independent_task_without_waiting_provider():
    async def run():
        repo = _Repo([[{"id": "wake_1"}]])
        started = asyncio.Event()
        release = asyncio.Event()

        async def trigger(_wake):
            started.set()
            await release.wait()

        runner = SelfWakeRunner(repository=repo, trigger=trigger)
        assert await asyncio.wait_for(runner.scan_once(), timeout=0.2) == 1
        await asyncio.wait_for(started.wait(), timeout=0.2)
        assert len(runner.in_flight) == 1
        release.set()
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        assert not runner.in_flight

    _run(run())


def test_run_loop_scans_immediately_before_first_sleep():
    async def run():
        repo = _Repo([[{"id": "wake_startup"}]])
        fired = asyncio.Event()

        async def trigger(_wake):
            fired.set()

        runner = SelfWakeRunner(repository=repo, trigger=trigger)
        task = asyncio.create_task(runner.run_scan_loop(interval_sec=60))
        await asyncio.wait_for(fired.wait(), timeout=0.2)
        assert repo.claim_calls == 1
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    _run(run())


def test_concurrent_scans_do_not_launch_same_claim_twice():
    async def run():
        repo = _Repo([[{"id": "wake_once"}], []])
        calls = []

        async def trigger(wake):
            calls.append(wake["id"])

        runner = SelfWakeRunner(repository=repo, trigger=trigger)
        counts = await asyncio.gather(runner.scan_once(), runner.scan_once())
        assert sum(counts) == 1
        await asyncio.sleep(0)
        assert calls == ["wake_once"]

    _run(run())


def test_shutdown_cancels_inflight_and_writes_terminal_outcome():
    async def run():
        repo = _Repo([[{"id": "wake_slow"}]])
        started = asyncio.Event()

        async def trigger(_wake):
            started.set()
            await asyncio.Event().wait()

        runner = SelfWakeRunner(repository=repo, trigger=trigger)
        await runner.scan_once()
        await asyncio.wait_for(started.wait(), timeout=0.2)
        await runner.shutdown()
        assert not runner.in_flight
        assert repo.finishes[-1] == (
            "wake_slow",
            {"outcome": "cancelled_on_shutdown", "error": ""},
        )

    _run(run())


def test_empty_claim_batch_never_calls_provider():
    async def run():
        repo = _Repo([[]])
        calls = []

        async def trigger(wake):
            calls.append(wake)

        runner = SelfWakeRunner(repository=repo, trigger=trigger)
        assert await runner.scan_once() == 0
        assert calls == []

    _run(run())
