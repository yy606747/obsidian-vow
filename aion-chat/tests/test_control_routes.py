import asyncio

import pytest
from fastapi import HTTPException

from app.control import ControlSession
from routes import control


class FakeControlSessionService:
    def __init__(self):
        self.start_kwargs = None
        self.session = ControlSession(
            session_id="ctrl_1",
            conv_id="conv1",
            kind="dom",
            status="active",
            owner_client_id="tab1",
            device_id="browser_toy_bridge",
            started_at=1.0,
            last_heartbeat_at=1.0,
            last_snapshot_at=None,
            ended_at=None,
            close_reason=None,
            control_epoch=0,
            safeword_set=True,
            frontend_snapshot_json='{"debt": 1, "schema_version": "control_snapshot_v1"}',
        )

    async def start(self, **kwargs):
        assert kwargs["conv_id"] == "conv1"
        assert kwargs["owner_client_id"] == "tab1"
        self.start_kwargs = kwargs
        return self.session

    async def heartbeat(self, **kwargs):
        return self.session

    async def snapshot(self, **kwargs):
        return self.session

    async def end(self, **kwargs):
        return ControlSession(**{**self.session.__dict__, "status": "ended", "close_reason": kwargs["close_reason"]})

    async def get_current(self, **kwargs):
        return self.session if kwargs["conv_id"] == "conv1" else None

    async def get_current_tide(self):
        return ControlSession(**{**self.session.__dict__, "kind": "tide", "device_id": "muse", "control_resource_id": "toy:muse"})

    async def claim_tide_session(self, **kwargs):
        return ControlSession(**{
            **self.session.__dict__,
            "kind": "tide",
            "device_id": "muse",
            "control_resource_id": "toy:muse",
            "owner_client_id": kwargs["owner_client_id"],
        })


def test_control_routes_expose_session_crud(monkeypatch):
    service = FakeControlSessionService()
    monkeypatch.setattr(control, "control_session_service", service)

    started = asyncio.run(control.start_control_session(control.ControlSessionStartBody(
        conv_id="conv1",
        kind="dom",
        owner_client_id="tab1",
        device_id="browser_toy_bridge",
        safeword_set=True,
    )))
    heartbeat = asyncio.run(control.heartbeat_control_session("ctrl_1", control.ControlSessionOwnerBody(owner_client_id="tab1")))
    snapshot = asyncio.run(control.snapshot_control_session("ctrl_1", control.ControlSessionSnapshotBody(owner_client_id="tab1", frontend_snapshot_json={"debt": 1})))
    ended = asyncio.run(control.end_control_session("ctrl_1", control.ControlSessionEndBody(owner_client_id="tab1", close_reason="normal")))
    current = asyncio.run(control.get_current_control_session("conv1"))
    tide_current = asyncio.run(control.get_current_tide_control_session())
    claimed = asyncio.run(control.claim_tide_control_session("ctrl_1", control.ControlSessionOwnerBody(owner_client_id="tab2")))

    assert started["session_id"] == "ctrl_1"
    assert heartbeat["status"] == "active"
    assert snapshot["owner_client_id"] == "tab1"
    assert snapshot["frontend_snapshot"]["debt"] == 1
    assert ended["status"] == "ended"
    assert ended["close_reason"] == "normal"
    assert current["control_epoch"] == 0
    assert tide_current["kind"] == "tide"
    assert claimed["owner_client_id"] == "tab2"
    assert claimed["control_resource_id"] == "toy:muse"
    assert service.start_kwargs["device_id"] == "browser_toy_bridge"
    assert asyncio.run(control.get_current_control_session("missing")) is None


def test_control_start_normalizes_frontend_toy_driver_ids(monkeypatch):
    service = FakeControlSessionService()
    monkeypatch.setattr(control, "control_session_service", service)

    asyncio.run(control.start_control_session(control.ControlSessionStartBody(
        conv_id="conv1",
        kind="dom",
        owner_client_id="tab1",
        device_id="cx492b",
        safeword_set=True,
    )))

    assert service.start_kwargs["device_id"] == "browser_toy_bridge"


def test_control_route_error_mapping():
    with pytest.raises(HTTPException) as missing:
        control._raise_session_error(control.ControlSessionNotFound("ctrl_missing"))
    with pytest.raises(HTTPException) as owner:
        control._raise_session_error(control.ControlOwnerMismatch("ctrl_1"))

    assert missing.value.status_code == 404
    assert missing.value.detail == "session_not_found"
    assert owner.value.status_code == 403
    assert owner.value.detail == "owner_mismatch"
