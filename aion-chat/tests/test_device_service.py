import asyncio
import json

from app.devices import BrowserBridgeDeviceDriver, DeviceService, MockDeviceDriver


def test_device_service_lists_mock_device_from_driver():
    service = DeviceService(drivers=[MockDeviceDriver(now=lambda: 123.0)], event_sink=None)

    payload = asyncio.run(service.list_devices())

    assert payload["count"] == 1
    device = payload["devices"][0]
    assert device["device_id"] == "mock_ring"
    assert device["status"] == "online"
    assert device["driver_id"] == "mock"
    assert "status.read" in device["capabilities"]
    assert device["last_seen_at"] == 123.0


def test_browser_bridge_driver_reports_online_state_and_executes_pulse():
    now = [100.0]
    driver = BrowserBridgeDeviceDriver(now=lambda: now[0], stale_after_sec=30.0)

    async def event_sink(**kwargs):
        return {"id": "mev_bridge_1", **kwargs}

    service = DeviceService(drivers=[driver], event_sink=event_sink)

    reported = asyncio.run(service.report_state(
        "browser_toy_bridge",
        status="online",
        name="SOSEXY",
        capabilities=["status.read", "notify.pulse", "toy.legacy_command"],
        metadata={"transport": "web_bluetooth"},
    ))
    result = asyncio.run(service.execute_command(
        "browser_toy_bridge",
        "pulse",
        {"legacy_command": "2"},
        request_id="msg_bridge_1",
    ))

    assert reported["ok"] is True
    assert reported["device"]["status"] == "online"
    assert reported["device"]["last_seen_at"] == 100.0
    assert result["ok"] is True
    assert result["driver_id"] == "browser_bridge"
    assert result["message"] == "bridge_command_queued"
    assert result["result"] == {
        "queued": True,
        "target": "frontend_bridge",
        "legacy_command": "2",
    }


def test_browser_bridge_driver_blocks_pulse_when_stale_or_offline():
    now = [100.0]
    driver = BrowserBridgeDeviceDriver(now=lambda: now[0], stale_after_sec=10.0)

    async def event_sink(**kwargs):
        return {"id": "mev_bridge_stale", **kwargs}

    service = DeviceService(drivers=[driver], event_sink=event_sink)

    asyncio.run(service.report_state("browser_toy_bridge", status="online"))
    now[0] = 120.5
    state = asyncio.run(service.get_device("browser_toy_bridge"))
    result = asyncio.run(service.execute_command("browser_toy_bridge", "pulse", {"legacy_command": "3"}))

    assert state["device"]["status"] == "offline"
    assert state["device"]["metadata"]["stale"] is True
    assert result["ok"] is False
    assert result["message"] == "bridge_offline"


def test_device_service_default_catalog_includes_browser_bridge_and_smart_ring():
    service = DeviceService(event_sink=None)

    payload = asyncio.run(service.list_devices())

    ids = {device["device_id"] for device in payload["devices"]}
    assert {"browser_toy_bridge", "smart_ring"}.issubset(ids)
    assert "mock_ring" not in ids


def test_device_service_executes_mock_command_and_writes_event_audit():
    events = []

    async def event_sink(**kwargs):
        events.append(kwargs)
        return {"id": "mev_device_1", **kwargs}

    service = DeviceService(
        drivers=[MockDeviceDriver(now=lambda: 123.0)],
        event_sink=event_sink,
    )

    result = asyncio.run(service.execute_command(
        "mock_ring",
        "ping",
        {"source": "test"},
        request_id="req_device_1",
    ))

    assert result["ok"] is True
    assert result["status"] == "executed"
    assert result["message"] == "pong"
    assert result["audit_event_id"] == "mev_device_1"
    assert result["result"] == {"status": "online", "battery": 88}
    assert len(events) == 1
    assert events[0]["source"] == "device"
    assert events[0]["namespace"] == "device"
    assert events[0]["role"] == "tool"
    assert "memory_items" not in json.dumps(events[0], ensure_ascii=False)
    metadata = json.loads(events[0]["metadata_json"])
    assert metadata["device_id"] == "mock_ring"
    assert metadata["command"] == "ping"
    assert metadata["status"] == "executed"
    assert metadata["request_id"] == "req_device_1"


def test_device_service_unknown_device_fails_and_is_audited():
    events = []

    async def event_sink(**kwargs):
        events.append(kwargs)
        return {"id": "mev_device_missing", **kwargs}

    service = DeviceService(
        drivers=[MockDeviceDriver(now=lambda: 123.0)],
        event_sink=event_sink,
    )

    result = asyncio.run(service.execute_command("missing_device", "ping"))

    assert result["ok"] is False
    assert result["status"] == "failed"
    assert result["message"] == "device_not_found"
    assert result["audit_event_id"] == "mev_device_missing"
    metadata = json.loads(events[0]["metadata_json"])
    assert metadata["device_id"] == "missing_device"
    assert metadata["status"] == "failed"


def test_device_service_get_device_returns_not_found_payload():
    service = DeviceService(drivers=[MockDeviceDriver(now=lambda: 123.0)], event_sink=None)

    payload = asyncio.run(service.get_device("missing_device"))

    assert payload == {
        "ok": False,
        "error": "device_not_found",
        "device_id": "missing_device",
    }
