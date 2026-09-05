import asyncio

from app.control import ControlCommandGateway, ControlSession
from app.tools.schemas import ToolContext, ToolIntent


def _session(*, status="active", epoch=2, owner="tab1", close_reason=None, device_id="session_device"):
    return ControlSession(
        session_id="ctrl_1",
        conv_id="conv1",
        kind="dom",
        status=status,
        owner_client_id=owner,
        device_id=device_id,
        started_at=1.0,
        last_heartbeat_at=1.0,
        last_snapshot_at=None,
        ended_at=2.0 if status == "ended" else None,
        close_reason=close_reason,
        control_epoch=epoch,
        safeword_set=True,
    )


def _intent(command="4"):
    return ToolIntent(
        id="intent_1",
        tool_name="device.toy",
        raw_text=f"[TOY:{command}]",
        arguments={"command": command},
        side_effect_level="device",
        allowed_modes=("device_control", "intimate"),
    )


def _context(metadata=None):
    return ToolContext(
        conv_id="conv1",
        msg_id="msg_1",
        request_id="req_1",
        model_key="mock-model",
        mode="device_control",
        capabilities=("device.toy",),
        metadata=metadata or {},
    )


class _Sessions:
    def __init__(self, *, current=None, known=()):
        self.current = current
        self.known = {item.session_id: item for item in known}
        if current:
            self.known[current.session_id] = current

    async def get_current(self, *, conv_id):
        assert conv_id == "conv1"
        return self.current

    async def get_session(self, session_id):
        return self.known.get(session_id)

    async def recent_safety_tombstone(self, conv_id):
        assert conv_id == "conv1"
        return None


class _Ledger:
    def __init__(self):
        self.events = []

    async def record(self, event_type, *, conv_id=None, session_id=None, content=None, metadata=None):
        self.events.append({
            "event_type": event_type,
            "conv_id": conv_id,
            "session_id": session_id,
            "content": content,
            "metadata": dict(metadata or {}),
        })
        return f"ledger_{len(self.events)}"


class _Devices:
    def __init__(self, result=None, *, session=None, device_state=None):
        self.result = result or {"ok": True, "audit_event_id": "dev_evt_1", "message": "sent"}
        self.calls = []
        self.session = session
        self.device_state = device_state

    async def get_device(self, device_id):
        if self.device_state is not None:
            return dict(self.device_state)
        session = self.session
        if session is None:
            return {"ok": False, "error": "device_not_found", "device_id": device_id}
        return {
            "ok": True,
            "device": {
                "device_id": device_id,
                "status": "online",
                "capabilities": ["status.read", "notify.pulse", "toy.legacy_command"],
                "metadata": {
                    "control_session_id": session.session_id,
                    "control_kind": session.kind,
                    "control_epoch": session.control_epoch,
                    "owner_client_id": session.owner_client_id,
                },
            },
        }

    async def execute_command(self, device_id, command, params=None, *, request_id=None):
        self.calls.append({
            "device_id": device_id,
            "command": command,
            "params": dict(params or {}),
            "request_id": request_id,
        })
        return dict(self.result)


def _gateway(*, current=None, known=(), device_result=None, device_state=None):
    ledger = _Ledger()
    devices = _Devices(device_result, session=current, device_state=device_state)
    gateway = ControlCommandGateway(
        session_service=_Sessions(current=current, known=known),
        ledger=ledger,
        device_service_adapter=devices,
    )
    return gateway, ledger, devices


def _session_metadata(session):
    return {
        "control_session_id": session.session_id,
        "control_epoch": session.control_epoch,
        "owner_client_id": session.owner_client_id,
    }


def test_gateway_accepts_active_session_and_records_ledger():
    session = _session()
    gateway, ledger, devices = _gateway(current=session)

    result = asyncio.run(gateway.execute_toy_intent(_intent("4"), _context(_session_metadata(session))))

    assert result["ok"] is True
    assert result["command"] == "4"
    assert result["control_session_id"] == "ctrl_1"
    assert result["control_epoch"] == 2
    assert result["owner_client_id"] == "tab1"
    assert devices.calls == [{
        "device_id": "session_device",
        "command": "pulse",
        "params": {
            "legacy_command": "4",
            "source": "chat_tool",
            "conv_id": "conv1",
            "msg_id": "msg_1",
            "tool_intent_id": "intent_1",
            "control_session_id": "ctrl_1",
            "control_epoch": 2,
            "owner_client_id": "tab1",
        },
        "request_id": "req_1",
    }]
    assert [event["event_type"] for event in ledger.events] == [
        "control.action.proposed",
        "control.action.accepted",
    ]
    assert ledger.events[-1]["metadata"]["device_audit_event_id"] == "dev_evt_1"


def test_gateway_normalizes_frontend_toy_driver_session_device_id():
    session = _session(device_id="cx492b")
    gateway, ledger, devices = _gateway(current=session)

    result = asyncio.run(gateway.execute_toy_intent(_intent("4"), _context(_session_metadata(session))))

    assert result["ok"] is True
    assert result["device_id"] == "browser_toy_bridge"
    assert devices.calls[0]["device_id"] == "browser_toy_bridge"
    assert ledger.events[0]["metadata"]["device_id"] == "browser_toy_bridge"


def test_gateway_preserves_sentinel_audit_source():
    session = _session()
    gateway, ledger, devices = _gateway(current=session)
    metadata = {
        **_session_metadata(session),
        "source": "sentinel",
        "delivery_path": "core_wake",
        "wake_id": "wake_1",
    }

    result = asyncio.run(gateway.execute_toy_intent(_intent("4"), _context(metadata)))

    assert result["ok"] is True
    assert devices.calls[0]["params"]["source"] == "sentinel"
    assert ledger.events[0]["metadata"]["source"] == "sentinel"
    assert ledger.events[0]["metadata"]["delivery_path"] == "core_wake"
    assert ledger.events[0]["metadata"]["wake_id"] == "wake_1"
    assert ledger.events[0]["metadata"]["request_id"] == "req_1"
    assert ledger.events[-1]["metadata"]["source"] == "sentinel"


def test_gateway_rejects_without_active_session():
    gateway, ledger, devices = _gateway()

    result = asyncio.run(gateway.execute_toy_intent(_intent("4"), _context()))

    assert result["ok"] is False
    assert result["command"] == ""
    assert result["message"] == "no_active_session"
    assert devices.calls == []
    assert ledger.events[-1]["event_type"] == "control.action.rejected"
    assert ledger.events[-1]["session_id"] is None
    assert ledger.events[-1]["metadata"]["reason"] == "no_active_session"


def test_gateway_rejects_legacy_body_without_session():
    gateway, ledger, devices = _gateway()

    result = asyncio.run(gateway.execute_toy_intent(
        _intent("4"),
        _context({"control_context_source": "legacy_body"}),
    ))

    assert result["ok"] is False
    assert result["message"] == "no_active_session"
    assert devices.calls == []
    assert [event["event_type"] for event in ledger.events] == ["control.action.rejected"]


def test_gateway_rejects_stale_session():
    session = _session(status="stale")
    gateway, ledger, devices = _gateway(current=session)

    result = asyncio.run(gateway.execute_toy_intent(_intent("4"), _context(_session_metadata(session))))

    assert result["ok"] is False
    assert result["message"] == "session_stale"
    assert result["control_session_id"] == "ctrl_1"
    assert devices.calls == []
    assert ledger.events[-1]["metadata"]["reason"] == "session_stale"


def test_gateway_rejects_active_session_without_control_metadata():
    session = _session()
    gateway, ledger, devices = _gateway(current=session)

    result = asyncio.run(gateway.execute_toy_intent(_intent("4"), _context()))

    assert result["ok"] is False
    assert result["message"] == "missing_control_metadata"
    assert result["control_session_id"] == "ctrl_1"
    assert devices.calls == []
    assert ledger.events[-1]["metadata"]["reason"] == "missing_control_metadata"


def test_gateway_rejects_ended_panic_session():
    session = _session(status="ended", epoch=3, close_reason="panic")
    gateway, ledger, devices = _gateway(known=(session,))
    metadata = {"control_session_id": session.session_id, "control_epoch": 2, "owner_client_id": "tab1"}

    result = asyncio.run(gateway.execute_toy_intent(_intent("STOP"), _context(metadata)))

    assert result["ok"] is False
    assert result["message"] == "session_ended"
    assert result["control_epoch"] == 3
    assert devices.calls == []
    assert ledger.events[-1]["session_id"] == "ctrl_1"


def test_gateway_rejects_epoch_mismatch():
    session = _session(epoch=3)
    gateway, ledger, devices = _gateway(current=session)
    metadata = {**_session_metadata(session), "control_epoch": 2}

    result = asyncio.run(gateway.execute_toy_intent(_intent("4"), _context(metadata)))

    assert result["ok"] is False
    assert result["message"] == "epoch_mismatch"
    assert devices.calls == []
    assert ledger.events[-1]["metadata"]["reason"] == "epoch_mismatch"


def test_gateway_rejects_owner_mismatch():
    session = _session(owner="tab1")
    gateway, ledger, devices = _gateway(current=session)
    metadata = {**_session_metadata(session), "owner_client_id": "tab2"}

    result = asyncio.run(gateway.execute_toy_intent(_intent("4"), _context(metadata)))

    assert result["ok"] is False
    assert result["message"] == "owner_mismatch"
    assert devices.calls == []
    assert ledger.events[-1]["metadata"]["reason"] == "owner_mismatch"


def test_gateway_rejects_device_failure_without_executable_command():
    session = _session()
    gateway, ledger, devices = _gateway(
        current=session,
        device_result={"ok": False, "audit_event_id": "dev_evt_fail", "message": "device_offline"},
    )

    result = asyncio.run(gateway.execute_toy_intent(_intent("4"), _context(_session_metadata(session))))

    assert result["ok"] is False
    assert result["command"] == ""
    assert result["legacy_command"] == "4"
    assert result["message"] == "device_offline"
    assert len(devices.calls) == 1
    assert ledger.events[-1]["event_type"] == "control.action.rejected"
    assert ledger.events[-1]["metadata"]["device_audit_event_id"] == "dev_evt_fail"


def test_gateway_rejects_live_session_when_bound_device_is_offline():
    session = _session()
    gateway, ledger, devices = _gateway(
        current=session,
        device_state={
            "ok": True,
            "device": {
                "device_id": "session_device",
                "status": "offline",
                "capabilities": ["toy.legacy_command"],
                "metadata": {
                    "control_session_id": session.session_id,
                    "control_kind": session.kind,
                    "control_epoch": session.control_epoch,
                    "owner_client_id": session.owner_client_id,
                },
            },
        },
    )

    result = asyncio.run(gateway.execute_toy_intent(_intent("4"), _context(_session_metadata(session))))

    assert result["ok"] is False
    assert result["message"] == "device_offline"
    assert devices.calls == []
    assert ledger.events[-1]["metadata"]["reason"] == "device_offline"
