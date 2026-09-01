import asyncio

from routes import devices


class FakeDeviceService:
    async def list_devices(self):
        return {
            "count": 1,
            "devices": [{"device_id": "mock_ring", "status": "online"}],
        }

    async def get_device(self, device_id):
        return {"ok": True, "device": {"device_id": device_id, "status": "online"}}

    async def execute_command(self, device_id, command, params=None, *, request_id=None):
        return {
            "ok": True,
            "device_id": device_id,
            "command": command,
            "status": "executed",
            "result": params or {},
            "audit_event_id": request_id,
        }

    async def report_state(self, device_id, *, status, name=None, kind=None, capabilities=None, battery=None, metadata=None):
        return {
            "ok": True,
            "device": {
                "device_id": device_id,
                "status": status,
                "name": name,
                "kind": kind,
                "capabilities": capabilities or [],
                "battery": battery,
                "metadata": metadata or {},
            },
        }


def test_device_routes_expose_status_catalog(monkeypatch):
    monkeypatch.setattr(devices, "device_service", FakeDeviceService())

    payload = asyncio.run(devices.list_devices())
    one = asyncio.run(devices.get_device("mock_ring"))

    assert payload["devices"][0]["device_id"] == "mock_ring"
    assert one["device"]["status"] == "online"


def test_device_routes_execute_command(monkeypatch):
    monkeypatch.setattr(devices, "device_service", FakeDeviceService())

    body = devices.DeviceCommandBody(
        command="ping",
        params={"source": "route_test"},
        request_id="req_route",
    )
    result = asyncio.run(devices.execute_device_command("mock_ring", body))

    assert result["ok"] is True
    assert result["device_id"] == "mock_ring"
    assert result["command"] == "ping"
    assert result["result"] == {"source": "route_test"}
    assert result["audit_event_id"] == "req_route"


def test_device_routes_report_state(monkeypatch):
    monkeypatch.setattr(devices, "device_service", FakeDeviceService())

    body = devices.DeviceStateReportBody(
        status="online",
        name="SOSEXY",
        kind="toy_bridge",
        capabilities=["notify.pulse"],
        metadata={"transport": "web_bluetooth"},
    )
    result = asyncio.run(devices.report_device_state("browser_toy_bridge", body))

    assert result["ok"] is True
    assert result["device"]["device_id"] == "browser_toy_bridge"
    assert result["device"]["status"] == "online"
    assert result["device"]["capabilities"] == ["notify.pulse"]
