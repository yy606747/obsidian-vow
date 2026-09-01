import asyncio
import sqlite3
from contextlib import asynccontextmanager

import pytest

from app.control.gateway import ControlCommandGateway
from app.control.service import ControlOwnerMismatch, ControlSessionService, init_control_tables
from app.control.toy_capability import resolve_toy_capability_snapshot
import app.control.service as control_service_module
import app.tide.renderer as tide_renderer_module
from app.control.outcome import init_control_outcome_tables
from app.control.agenda import init_control_agenda_tables
from app.tide.intent import init_tide_tables
from app.devices import BrowserBridgeDeviceDriver, DeviceService
from app.tools.schemas import ToolContext, ToolIntent


class _AsyncCursor:
    def __init__(self, cursor):
        self._cursor = cursor

    async def fetchone(self):
        return self._cursor.fetchone()

    async def fetchall(self):
        return self._cursor.fetchall()


class _AsyncConn:
    def __init__(self, path):
        self._conn = sqlite3.connect(path)
        self._conn.row_factory = sqlite3.Row

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        self._conn.close()
        return False

    @property
    def row_factory(self):
        return self._conn.row_factory

    @row_factory.setter
    def row_factory(self, value):
        self._conn.row_factory = value

    async def execute(self, sql, params=()):
        return _AsyncCursor(self._conn.execute(sql, params))

    async def commit(self):
        self._conn.commit()


async def _init_db(path):
    async with _AsyncConn(path) as db:
        await db.execute("""
            CREATE TABLE memory_events (
                id TEXT PRIMARY KEY,
                source TEXT NOT NULL,
                namespace TEXT NOT NULL DEFAULT 'normal',
                conv_id TEXT,
                role TEXT,
                content TEXT NOT NULL,
                metadata_json TEXT NOT NULL DEFAULT '{}',
                created_at REAL NOT NULL
            )
        """)
        await init_control_tables(db)
        await init_control_outcome_tables(db)
        await init_control_agenda_tables(db)
        await init_tide_tables(db)
        await db.commit()


def _service(tmp_path, *, now_ref, device_service_adapter=None):
    db_path = tmp_path / "control.db"
    asyncio.run(_init_db(db_path))

    @asynccontextmanager
    async def fake_get_db():
        async with _AsyncConn(db_path) as db:
            yield db

    service = ControlSessionService(
        get_db_factory=fake_get_db,
        now=lambda: now_ref[0],
        device_service_adapter=device_service_adapter,
    )
    return service, db_path


class _DeviceReports:
    def __init__(self):
        self.reports = []

    async def report_state(self, device_id, **kwargs):
        self.reports.append({"device_id": device_id, **kwargs})
        return {"ok": True}


class _ControlLedger:
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


def _row(db_path, session_id):
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        return dict(conn.execute("SELECT * FROM control_sessions WHERE session_id=?", (session_id,)).fetchone())
    finally:
        conn.close()


def _events(db_path):
    conn = sqlite3.connect(db_path)
    try:
        return [row[0] for row in conn.execute("SELECT content FROM memory_events ORDER BY created_at")]
    finally:
        conn.close()


def test_control_session_lifecycle_stale_resume_timeout(tmp_path):
    now = [1000.0]
    service, db_path = _service(tmp_path, now_ref=now)

    session = asyncio.run(service.start(
        conv_id="conv1",
        kind="dom",
        owner_client_id="tab1",
        safeword_set=True,
    ))
    assert session.status == "active"
    assert session.control_epoch == 0
    assert session.safeword_set is True

    now[0] = 1010.0
    assert asyncio.run(service.heartbeat(session_id=session.session_id, owner_client_id="tab1")).status == "active"

    now[0] = 1056.0
    stale = asyncio.run(service.get_current(conv_id="conv1"))
    assert stale.status == "stale"

    now[0] = 1060.0
    resumed = asyncio.run(service.heartbeat(session_id=session.session_id, owner_client_id="tab1"))
    assert resumed.status == "active"

    now[0] = 1241.0
    assert asyncio.run(service.get_current(conv_id="conv1")) is None
    ended = _row(db_path, session.session_id)
    assert ended["status"] == "ended"
    assert ended["close_reason"] == "timeout"
    assert "control.session.started" in _events(db_path)
    assert "control.session.stale" in _events(db_path)
    assert "control.session.resumed" in _events(db_path)
    assert "control.session.ended" in _events(db_path)


def test_control_session_snapshot_resumes_stale_owner_session(tmp_path):
    now = [1000.0]
    devices = _DeviceReports()
    service, db_path = _service(tmp_path, now_ref=now, device_service_adapter=devices)

    session = asyncio.run(service.start(
        conv_id="conv_snapshot_resume",
        kind="dom",
        owner_client_id="tab1",
        safeword_set=True,
    ))

    now[0] = 1050.0
    updated = asyncio.run(service.snapshot(
        session_id=session.session_id,
        owner_client_id="tab1",
        frontend_snapshot_json={"dom_history": ["HOLD:2:1"], "debt": 1, "toy_connected": True},
    ))

    assert updated.status == "active"
    assert updated.last_heartbeat_at == 1050.0
    assert updated.last_snapshot_at == 1050.0
    row = _row(db_path, session.session_id)
    assert row["status"] == "active"
    assert row["last_heartbeat_at"] == 1050.0

    ctx = asyncio.run(service.get_prompt_context("conv_snapshot_resume", {"ai_dom_mode": True}))
    assert ctx.source == "control_session"
    assert ctx.active is True
    assert ctx.session_id == session.session_id
    assert ctx.dom_history == ["HOLD:2:1"]
    assert devices.reports == [{
        "device_id": "browser_toy_bridge",
        "status": "online",
        "name": "Browser Toy Bridge",
        "kind": "toy_bridge",
        "capabilities": ("status.read", "notify.pulse", "toy.legacy_command"),
        "metadata": {
            "source_event": "control_snapshot",
            "control_session_id": session.session_id,
            "control_kind": "dom",
            "control_epoch": 0,
            "owner_client_id": "tab1",
        },
    }]
    asyncio.run(service.snapshot(
        session_id=session.session_id,
        owner_client_id="tab1",
        frontend_snapshot_json={"toy_connected": False},
    ))
    assert devices.reports[-1]["status"] == "offline"
    assert devices.reports[-1]["metadata"]["control_session_id"] == session.session_id
    assert "control.session.stale" in _events(db_path)
    assert "control.session.resumed" in _events(db_path)


def test_control_snapshot_keeps_second_toy_command_executable_after_bridge_stales(tmp_path):
    now = [1000.0]

    async def event_sink(**kwargs):
        return {"id": "dev_evt", **kwargs}

    devices = DeviceService(
        drivers=[BrowserBridgeDeviceDriver(now=lambda: now[0], stale_after_sec=10.0)],
        event_sink=event_sink,
    )
    service, _db_path = _service(tmp_path, now_ref=now, device_service_adapter=devices)
    gateway = ControlCommandGateway(
        session_service=service,
        ledger=_ControlLedger(),
        device_service_adapter=devices,
    )

    session = asyncio.run(service.start(
        conv_id="conv_second_toy",
        kind="dom",
        owner_client_id="tab1",
        device_id="browser_toy_bridge",
        safeword_set=True,
    ))
    asyncio.run(service.snapshot(
        session_id=session.session_id,
        owner_client_id="tab1",
        frontend_snapshot_json={"toy_connected": True},
    ))

    def execute(command):
        return asyncio.run(gateway.execute_toy_intent(
            ToolIntent(
                id=f"toy_{command}",
                tool_name="device.toy",
                raw_text=f"[TOY:{command}]",
                arguments={"command": command},
                side_effect_level="device",
                allowed_modes=("device_control",),
            ),
            ToolContext(
                conv_id="conv_second_toy",
                msg_id=f"msg_{command}",
                request_id=f"req_{command}",
                model_key="mock-model",
                mode="device_control",
                capabilities=("device.toy",),
                metadata={
                    "control_session_id": session.session_id,
                    "control_epoch": session.control_epoch,
                    "owner_client_id": "tab1",
                    "control_context_source": "control_session",
                    "control_kind": "dom",
                },
            ),
        ))

    first = execute("HOLD:2:1")
    assert first["ok"] is True
    assert first["command"] == "HOLD:2:1"

    now[0] = 1012.0
    stale_device = asyncio.run(devices.get_device("browser_toy_bridge"))
    assert stale_device["device"]["status"] == "offline"

    asyncio.run(service.snapshot(
        session_id=session.session_id,
        owner_client_id="tab1",
        frontend_snapshot_json={
            "toy_connected": True,
            "dom_history": ["HOLD:2:1"],
        },
    ))
    second = execute("SPIKE:4:2")

    assert second["ok"] is True
    assert second["command"] == "SPIKE:4:2"
    assert second["message"] == "bridge_command_queued"


def test_toy_capability_is_false_for_disconnected_whisper_and_after_timeout(tmp_path):
    now = [1000.0]
    devices = DeviceService(
        drivers=[BrowserBridgeDeviceDriver(now=lambda: now[0])],
    )
    service, _db_path = _service(tmp_path, now_ref=now, device_service_adapter=devices)
    session = asyncio.run(service.start(
        conv_id="conv_whisper_offline",
        kind="whisper",
        owner_client_id="tab1",
        device_id="browser_toy_bridge",
    ))
    asyncio.run(service.snapshot(
        session_id=session.session_id,
        owner_client_id="tab1",
        frontend_snapshot_json={"toy_connected": False},
    ))

    disconnected = asyncio.run(resolve_toy_capability_snapshot(
        conv_id="conv_whisper_offline",
        session_service=service,
        device_service_adapter=devices,
    ))
    assert disconnected.allowed is False
    assert disconnected.reason == "device_offline"

    now[0] = 1181.0
    timed_out = asyncio.run(resolve_toy_capability_snapshot(
        conv_id="conv_whisper_offline",
        session_service=service,
        device_service_adapter=devices,
    ))
    assert timed_out.allowed is False
    assert timed_out.reason == "no_active_session"


def test_control_session_owner_supersede_and_panic_epoch(tmp_path):
    now = [2000.0]
    service, db_path = _service(tmp_path, now_ref=now)

    first = asyncio.run(service.start(conv_id="conv2", kind="whisper", owner_client_id="tab1"))
    with pytest.raises(ControlOwnerMismatch):
        asyncio.run(service.snapshot(
            session_id=first.session_id,
            owner_client_id="tab2",
            frontend_snapshot_json={"scene": "x"},
        ))

    now[0] = 2001.0
    second = asyncio.run(service.start(conv_id="conv2", kind="dom", owner_client_id="tab2"))
    old = _row(db_path, first.session_id)
    assert old["status"] == "ended"
    assert old["close_reason"] == "superseded"
    assert second.owner_client_id == "tab2"

    ended = asyncio.run(service.end(
        session_id=second.session_id,
        owner_client_id="tab2",
        close_reason="panic",
    ))
    assert ended.status == "ended"
    assert ended.control_epoch == 1

    now[0] = 2002.0
    restarted = asyncio.run(service.start(conv_id="conv2", kind="dom", owner_client_id="tab3"))
    assert restarted.control_epoch == 1
    assert "control.panic.triggered" in _events(db_path)


def test_tide_start_globally_serializes_sessions_and_stops_old_runner(tmp_path, monkeypatch):
    now = [2500.0]
    service, db_path = _service(tmp_path, now_ref=now)
    calls = []

    class FakeTideRegistry:
        async def activate(self, session):
            calls.append(("activate", session.conv_id, session.session_id))

        async def stop_session(self, session, *, emit_stop, reason):
            calls.append(("stop", session.conv_id, session.session_id, emit_stop, reason))

    monkeypatch.setattr(tide_renderer_module, "tide_renderer_registry", FakeTideRegistry())

    first = asyncio.run(service.start(
        conv_id="conv_tide_a",
        kind="tide",
        owner_client_id="tab_a",
        device_id="muse",
        control_resource_id="toy:muse",
    ))
    now[0] = 2501.0
    second = asyncio.run(service.start(
        conv_id="conv_tide_b",
        kind="tide",
        owner_client_id="tab_b",
        device_id="muse",
        control_resource_id="toy:muse",
    ))

    old = _row(db_path, first.session_id)
    new = _row(db_path, second.session_id)
    assert old["status"] == "ended"
    assert old["close_reason"] == "superseded"
    assert new["status"] == "active"
    assert new["control_resource_id"] == "toy:muse"
    assert asyncio.run(service.get_current(conv_id="conv_tide_a")) is None
    assert asyncio.run(service.get_current(conv_id="conv_tide_b")).session_id == second.session_id
    assert calls == [
        ("activate", "conv_tide_a", first.session_id),
        ("stop", "conv_tide_a", first.session_id, True, "superseded"),
        ("activate", "conv_tide_b", second.session_id),
    ]


def test_tide_global_current_and_owner_claim_rebinds_runner(tmp_path, monkeypatch):
    now = [2550.0]
    service, db_path = _service(tmp_path, now_ref=now)
    calls = []

    class FakeTideRegistry:
        async def activate(self, session):
            calls.append(("activate", session.owner_client_id, session.session_id))

        async def rebind_session(self, session):
            calls.append(("rebind", session.owner_client_id, session.session_id))

    monkeypatch.setattr(tide_renderer_module, "tide_renderer_registry", FakeTideRegistry())

    session = asyncio.run(service.start(
        conv_id="conv_tide_claim",
        kind="tide",
        owner_client_id="old_owner",
        device_id="muse",
        control_resource_id="toy:muse",
    ))
    current = asyncio.run(service.get_current_tide())
    claimed = asyncio.run(service.claim_tide_session(
        session_id=session.session_id,
        owner_client_id="new_owner",
    ))

    assert current.session_id == session.session_id
    assert claimed.owner_client_id == "new_owner"
    row = _row(db_path, session.session_id)
    assert row["owner_client_id"] == "new_owner"
    assert row["status"] == "active"
    assert calls == [
        ("activate", "old_owner", session.session_id),
        ("rebind", "new_owner", session.session_id),
    ]


def test_non_tide_start_stops_superseded_tide_runner(tmp_path, monkeypatch):
    now = [2600.0]
    service, db_path = _service(tmp_path, now_ref=now)
    calls = []

    class FakeTideRegistry:
        async def activate(self, session):
            calls.append(("activate", session.conv_id, session.session_id))

        async def stop_session(self, session, *, emit_stop, reason):
            calls.append(("stop", session.conv_id, session.session_id, emit_stop, reason))

    monkeypatch.setattr(tide_renderer_module, "tide_renderer_registry", FakeTideRegistry())

    tide = asyncio.run(service.start(
        conv_id="conv_tide_same",
        kind="tide",
        owner_client_id="tab_tide",
        device_id="muse",
        control_resource_id="toy:muse",
    ))
    now[0] = 2601.0
    dom = asyncio.run(service.start(
        conv_id="conv_tide_same",
        kind="dom",
        owner_client_id="tab_dom",
    ))

    old = _row(db_path, tide.session_id)
    assert old["status"] == "ended"
    assert old["close_reason"] == "superseded"
    assert dom.kind == "dom"
    assert calls == [
        ("activate", "conv_tide_same", tide.session_id),
        ("stop", "conv_tide_same", tide.session_id, True, "superseded"),
    ]


def test_tide_start_supersedes_existing_non_tide_session_same_conv(tmp_path, monkeypatch):
    now = [2700.0]
    service, db_path = _service(tmp_path, now_ref=now)
    calls = []

    class FakeTideRegistry:
        async def activate(self, session):
            calls.append(("activate", session.conv_id, session.session_id))

        async def stop_session(self, session, *, emit_stop, reason):
            calls.append(("stop", session.conv_id, session.session_id, emit_stop, reason))

    monkeypatch.setattr(tide_renderer_module, "tide_renderer_registry", FakeTideRegistry())

    dom = asyncio.run(service.start(
        conv_id="conv_switch_to_tide",
        kind="dom",
        owner_client_id="tab_dom",
    ))
    now[0] = 2701.0
    tide = asyncio.run(service.start(
        conv_id="conv_switch_to_tide",
        kind="tide",
        owner_client_id="tab_tide",
        device_id="muse",
        control_resource_id="toy:muse",
    ))

    old = _row(db_path, dom.session_id)
    assert old["status"] == "ended"
    assert old["close_reason"] == "superseded"
    assert tide.kind == "tide"
    assert asyncio.run(service.get_current(conv_id="conv_switch_to_tide")).session_id == tide.session_id
    assert calls == [("activate", "conv_switch_to_tide", tide.session_id)]


def test_control_prompt_context_uses_session_snapshot_then_blocks_legacy_by_default(tmp_path):
    now = [3000.0]
    service, _db_path = _service(tmp_path, now_ref=now)
    session = asyncio.run(service.start(conv_id="conv3", kind="dom", owner_client_id="tab1"))
    asyncio.run(service.snapshot(
        session_id=session.session_id,
        owner_client_id="tab1",
        frontend_snapshot_json={
            "dom_history": ["HOLD:5:3"],
            "cnc_enabled": True,
            "cnc_weakness": ["嘴硬"],
            "debt": "2.5",
            "scene_name": "edge",
        },
    ))

    ctx = asyncio.run(service.get_prompt_context("conv3", {"ai_dom_mode": False}))
    assert ctx.source == "control_session"
    assert ctx.session_id == session.session_id
    assert ctx.kind == "dom"
    assert ctx.owner_client_id == "tab1"
    assert ctx.control_epoch == 0
    assert ctx.active is True
    assert ctx.dom_history == ["HOLD:5:3"]
    assert ctx.cnc_enabled is True
    assert ctx.cnc_weakness == ["嘴硬"]
    assert ctx.debt == 2.5
    assert ctx.scene_name == "edge"

    legacy = asyncio.run(service.get_prompt_context("conv4", {
        "ai_dom_mode": True,
        "dom_history": "EDGE,HOLD:4",
        "cnc_weakness": "怕被看穿|嘴硬",
        "debt": "bad",
    }))
    assert legacy.source == "none"
    assert legacy.active is False

    none = asyncio.run(service.get_prompt_context("conv5", {"content": "hi"}))
    assert none.source == "none"
    assert none.active is False


def test_control_prompt_context_uses_configured_legacy_fallback(tmp_path, monkeypatch):
    monkeypatch.setattr(control_service_module, "control_legacy_toy_fallback_enabled", lambda: True)
    service, _db_path = _service(tmp_path, now_ref=[3000.0])

    legacy = asyncio.run(service.get_prompt_context("conv4", {
        "ai_dom_mode": True,
        "dom_history": "EDGE,HOLD:4",
        "cnc_weakness": "怕被看穿|嘴硬",
        "debt": "bad",
    }))

    assert legacy.source == "legacy_body"
    assert legacy.kind == "dom"
    assert legacy.dom_history == ["EDGE", "HOLD:4"]
    assert legacy.cnc_weakness == ["怕被看穿", "嘴硬"]
    assert legacy.debt == 0.0


def test_safety_tombstone_suppresses_legacy_control_for_twenty_minutes(tmp_path, monkeypatch):
    monkeypatch.setattr(control_service_module, "control_legacy_toy_fallback_enabled", lambda: True)
    now = [4000.0]
    service, _db_path = _service(tmp_path, now_ref=now)
    session = asyncio.run(service.start(
        conv_id="conv6",
        kind="dom",
        owner_client_id="tab1",
        safeword_set=True,
    ))

    now[0] = 4010.0
    ended = asyncio.run(service.end(
        session_id=session.session_id,
        owner_client_id="tab1",
        close_reason="safeword",
    ))
    assert ended.control_epoch == 1

    now[0] = 4020.0
    tombstone = asyncio.run(service.get_prompt_context("conv6", {
        "ai_dom_mode": True,
        "dom_history": "EDGE,HOLD:4",
    }))
    assert tombstone.source == "safety_tombstone"
    assert tombstone.session_id == session.session_id
    assert tombstone.kind == "dom"
    assert tombstone.active is False
    assert tombstone.aftercare_active is True
    assert tombstone.safety_close_reason == "safeword"
    assert tombstone.control_epoch == 1

    now[0] = 4010.0 + (20 * 60) + 1
    legacy = asyncio.run(service.get_prompt_context("conv6", {
        "ai_dom_mode": True,
        "dom_history": "EDGE,HOLD:4",
    }))
    assert legacy.source == "legacy_body"
    assert legacy.active is True
    assert legacy.dom_history == ["EDGE", "HOLD:4"]
