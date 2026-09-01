import asyncio
import json

from app.devices.drivers.ring import RingDeviceDriver


def test_ring_driver_queues_touch_and_records_lifecycle_events():
    events = []
    terminal_events = []
    sent = []
    now = [1000.0]

    async def event_sink(**kwargs):
        events.append(kwargs)
        return {"id": f"mev_{len(events)}", **kwargs}

    async def ws_sender(device_type, payload):
        sent.append((device_type, payload))
        return True

    async def terminal_sink(**kwargs):
        terminal_events.append(kwargs)
        return 1

    driver = RingDeviceDriver(
        event_sink=event_sink,
        ws_sender=ws_sender,
        settings_reader=lambda: True,
        terminal_sink=terminal_sink,
        now=lambda: now[0],
    )

    async def run():
        await driver.report_state("smart_ring", status="online", battery=88)
        queued = await driver.execute_command("smart_ring", "touch", {
            "touch": "急促地连敲三下",
            "reason": "提醒她回来",
            "haptics": {"taps": 3, "interval_ms": 800},
            "_ring_request_id": "req_ring_1",
            "_ring_created_at": now[0],
        })
        await driver.handle_ack({"request_id": "req_ring_1", "status": "executed"})
        return queued

    result = asyncio.run(run())

    assert result.status.value == "queued"
    assert result.ok is False
    assert result.result == {"queued": True, "request_id": "req_ring_1", "taps": 3, "interval_ms": 1000, "alert_type": 5}
    assert sent[0][0] == "smart_ring"
    assert sent[0][1]["type"] == "ring_touch_request"
    assert sent[0][1]["data"]["request_id"] == "req_ring_1"
    assert sent[0][1]["data"]["name_prefix"] == "AIZO"
    assert [event["content"] for event in events] == ["ring_touch.queued", "ring_touch.executed"]
    queued_meta = json.loads(events[0]["metadata_json"])
    assert queued_meta["ai_haptics"] == {"taps": 3, "interval_ms": 800}
    assert queued_meta["clamped_haptics"] == {"taps": 3, "interval_ms": 1000, "alert_type": 5}
    assert terminal_events[0]["correlation_id"] == "req_ring_1"
    assert terminal_events[0]["outcome"] == "succeeded"
    assert terminal_events[0]["event_type"] == "ring_touch.executed"


def test_ring_driver_timeout_reports_terminal_failure():
    now = [1000.0]
    terminal_events = []

    async def event_sink(**kwargs):
        return kwargs

    async def ws_sender(_device_type, _payload):
        return True

    async def terminal_sink(**kwargs):
        terminal_events.append(kwargs)
        return 1

    driver = RingDeviceDriver(
        event_sink=event_sink,
        ws_sender=ws_sender,
        settings_reader=lambda: True,
        terminal_sink=terminal_sink,
        now=lambda: now[0],
        ack_timeout_sec=30.0,
    )

    async def run():
        await driver.report_state("smart_ring", status="online")
        await driver.execute_command("smart_ring", "touch", {
            "touch": "轻轻碰一下",
            "_ring_request_id": "req_ring_timeout",
            "_ring_created_at": now[0],
        })
        now[0] += 31.0
        await driver.sweep_timeouts()

    asyncio.run(run())

    assert terminal_events[0]["correlation_id"] == "req_ring_timeout"
    assert terminal_events[0]["outcome"] == "failed"
    assert terminal_events[0]["event_type"] == "ring_touch.timeout"


def test_ring_driver_fails_when_phone_ws_unavailable():
    events = []

    async def event_sink(**kwargs):
        events.append(kwargs)
        return {"id": f"mev_{len(events)}", **kwargs}

    async def ws_sender(_device_type, _payload):
        return False

    driver = RingDeviceDriver(
        event_sink=event_sink,
        ws_sender=ws_sender,
        settings_reader=lambda: True,
        now=lambda: 2000.0,
    )

    async def run():
        await driver.report_state("smart_ring", status="online")
        return await driver.execute_command("smart_ring", "touch", {
            "touch": "轻轻点一下",
            "_ring_request_id": "req_ring_ws",
            "_ring_created_at": 2000.0,
        })

    result = asyncio.run(run())

    assert result.status.value == "failed"
    assert result.message == "phone_ws_unavailable"
    assert json.loads(events[0]["metadata_json"])["ble_result"] == "ws_unavailable"


def test_ring_driver_connect_command_targets_phone_bridge():
    sent = []

    async def event_sink(**kwargs):
        return {"id": "mev", **kwargs}

    async def ws_sender(device_type, payload):
        sent.append((device_type, payload))
        return True

    driver = RingDeviceDriver(
        event_sink=event_sink,
        ws_sender=ws_sender,
        settings_reader=lambda: False,
        now=lambda: 3000.0,
    )

    result = asyncio.run(driver.execute_command("smart_ring", "connect", {"_ring_request_id": "req_connect"}))

    assert result.status.value == "queued"
    assert result.message == "ring_connect_queued"
    assert sent == [(
        "smart_ring",
        {
            "type": "ring_connect_request",
            "data": {"request_id": "req_connect", "name_prefix": "AIZO", "keep_connected": False},
        },
    )]


def test_ring_driver_uses_configured_name_prefix_for_connect_and_touch():
    sent = []

    async def event_sink(**kwargs):
        return {"id": "mev", **kwargs}

    async def ws_sender(device_type, payload):
        sent.append((device_type, payload))
        return True

    driver = RingDeviceDriver(
        event_sink=event_sink,
        ws_sender=ws_sender,
        settings_reader=lambda: True,
        name_prefix_reader=lambda: "TEST",
        now=lambda: 4000.0,
    )

    async def run():
        await driver.execute_command("smart_ring", "connect", {"_ring_request_id": "req_connect_prefix"})
        await driver.report_state("smart_ring", status="online")
        return await driver.execute_command("smart_ring", "touch", {
            "touch": "轻轻点一下",
            "_ring_request_id": "req_touch_prefix",
            "_ring_created_at": 4000.0,
        })

    result = asyncio.run(run())

    assert result.status.value == "queued"
    assert sent[0][1]["data"]["name_prefix"] == "TEST"
    assert sent[1][1]["data"]["name_prefix"] == "TEST"


def test_ring_driver_sends_keep_connected_preference():
    sent = []

    async def event_sink(**kwargs):
        return {"id": "mev", **kwargs}

    async def ws_sender(device_type, payload):
        sent.append((device_type, payload))
        return True

    driver = RingDeviceDriver(
        event_sink=event_sink,
        ws_sender=ws_sender,
        settings_reader=lambda: False,
        keep_connected_reader=lambda: True,
        now=lambda: 5000.0,
    )

    result = asyncio.run(driver.execute_command("smart_ring", "connect", {"_ring_request_id": "req_keep"}))

    assert result.status.value == "queued"
    assert sent[0][1]["data"]["keep_connected"] is True


def test_ring_driver_keepalive_command_targets_phone_bridge():
    sent = []

    async def event_sink(**kwargs):
        return {"id": "mev", **kwargs}

    async def ws_sender(device_type, payload):
        sent.append((device_type, payload))
        return True

    driver = RingDeviceDriver(
        event_sink=event_sink,
        ws_sender=ws_sender,
        settings_reader=lambda: False,
        keep_connected_reader=lambda: True,
        now=lambda: 6000.0,
    )

    result = asyncio.run(driver.execute_command("smart_ring", "keepalive", {"_ring_request_id": "req_keepalive"}))

    assert result.status.value == "queued"
    assert sent == [(
        "smart_ring",
        {
            "type": "ring_keepalive_request",
            "data": {"request_id": "req_keepalive", "name_prefix": "AIZO", "keep_connected": True},
        },
    )]
