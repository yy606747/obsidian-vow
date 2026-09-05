import asyncio

from app.devices.gates.ring_touch import MAX_TOUCHES_PER_WINDOW, RingTouchGate
from app.devices.schemas import DeviceStatus


def _run(coro):
    return asyncio.run(coro)


def test_ring_touch_gate_blocks_disabled_stale_duplicate_and_offline():
    now = [100.0]
    enabled = [False]
    status = [DeviceStatus.ONLINE]
    gate = RingTouchGate(
        settings_reader=lambda: enabled[0],
        device_status_reader=lambda: status[0],
        now=lambda: now[0],
    )

    assert _run(gate.check({"_ring_request_id": "r1", "_ring_created_at": 100.0})).reason == "disabled"
    enabled[0] = True
    assert _run(gate.check({"_ring_request_id": "r2", "_ring_created_at": 1.0})).reason == "skipped_stale"
    first = _run(gate.check({"_ring_request_id": "r3", "_ring_created_at": 100.0}))
    assert first.passed is True
    assert _run(gate.check({"_ring_request_id": "r3", "_ring_created_at": 100.0})).reason == "duplicate_request"
    status[0] = DeviceStatus.OFFLINE
    assert _run(gate.check({"_ring_request_id": "r4", "_ring_created_at": 100.0})).reason == "device_offline"


def test_ring_touch_gate_blocks_quiet_hours():
    gate = RingTouchGate(
        settings_reader=lambda: True,
        quiet_hours_reader=lambda: True,
        device_status_reader=lambda: DeviceStatus.ONLINE,
        now=lambda: 150.0,
    )

    result = _run(gate.check({"_ring_request_id": "quiet", "_ring_created_at": 150.0}))

    assert result.passed is False
    assert result.reason == "quiet_hours"


def test_ring_touch_gate_rate_limits_and_clamps_haptics():
    now = [200.0]
    gate = RingTouchGate(
        settings_reader=lambda: True,
        device_status_reader=lambda: DeviceStatus.ONLINE,
        now=lambda: now[0],
    )

    for index in range(MAX_TOUCHES_PER_WINDOW):
        assert _run(gate.check({"_ring_request_id": f"r{index}", "_ring_created_at": now[0]})).passed is True
    limited = _run(gate.check({"_ring_request_id": "overflow", "_ring_created_at": now[0]}))

    assert limited.passed is False
    assert limited.reason == "rate_limited"
    assert gate.clamp({"taps": 99, "interval_ms": 250}) == {"taps": 10, "interval_ms": 1000, "alert_type": 5}
    assert gate.clamp({"taps": 99, "interval_ms": 99999}) == {"taps": 4, "interval_ms": 5000, "alert_type": 5}
