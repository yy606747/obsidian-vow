import asyncio
from types import SimpleNamespace

from app.events import EvidenceLedger
from app.legacy_adapters.evidence import (
    record_activity_entry,
    record_location_heartbeat,
    record_sensing_entry,
)
from app.legacy_adapters import location_runtime as location_runtime_module
from routes import activity as activity_routes
from routes import location as location_routes
from routes import sensing as sensing_routes


def test_sensing_adapter_normalizes_android_entry_to_evidence():
    ledger = EvidenceLedger(now=lambda: 130.0)

    record = record_sensing_entry(
        {
            "timestamp": 100.0,
            "date": "2026-05-13",
            "time": "10:00:00",
            "type": "sensor",
            "data": {"motion": "walking", "motion_confidence": 80, "light_lux": None},
        },
        ledger=ledger,
    )

    payload = record.to_dict(reference_time=130.0)
    assert payload["kind"] == "sensing.sensor"
    assert payload["source"] == "android.sensing"
    assert payload["freshness_sec"] == 30.0
    assert payload["confidence"] == 0.8
    assert payload["payload"] == {"motion": "walking", "motion_confidence": 80}
    assert payload["metadata"]["legacy_entry_type"] == "sensor"


def test_activity_adapter_uses_device_specific_sources():
    ledger = EvidenceLedger(now=lambda: 200.0)

    phone = record_activity_entry(
        {"timestamp": 180.0, "device": "phone", "app": "微信", "title": ""},
        ledger=ledger,
    )
    pc = record_activity_entry(
        {
            "timestamp": 190.0,
            "device": "pc",
            "app": "VS Code",
            "title": "main.py",
            "active_state": "active",
            "last_input_age_sec": 12,
        },
        ledger=ledger,
    )

    snapshot = ledger.snapshot(reference_time=200.0).to_dict()
    assert phone.source == "android.activity"
    assert pc.source == "pc.activity"
    assert phone.payload == {"device": "phone", "app": "微信", "title": ""}
    assert pc.payload["active_state"] == "active"
    assert pc.payload["last_input_age_sec"] == 12
    assert snapshot["source_counts"] == {"android.activity": 1, "pc.activity": 1}


def test_activity_adapter_normalizes_screen_markers_without_changing_app_label():
    ledger = EvidenceLedger(now=lambda: 200.0)

    raw_on = record_activity_entry(
        {"timestamp": 180.0, "device": "phone", "app": "screen_on", "title": ""},
        ledger=ledger,
    )
    resolved_off = record_activity_entry(
        {"timestamp": 190.0, "device": "phone", "app": "锁屏", "title": ""},
        ledger=ledger,
    )

    assert raw_on.payload == {
        "device": "phone", "app": "screen_on", "title": "", "screen_state": "on",
    }
    assert resolved_off.payload == {
        "device": "phone", "app": "锁屏", "title": "", "screen_state": "off",
    }


def test_location_adapter_records_fix_with_accuracy_based_confidence():
    ledger = EvidenceLedger(now=lambda: 300.0)

    record = record_location_heartbeat(
        {"lng": 120.1, "lat": 30.2, "accuracy": 150.0, "is_gcj02": False, "force": True},
        {
            "state": "outside",
            "old_state": "at_home",
            "state_changed": True,
            "distance_from_home": 600.0,
        },
        ledger=ledger,
    )

    payload = record.to_dict(reference_time=300.0)
    assert payload["kind"] == "location.fix"
    assert payload["source"] == "android.location"
    assert payload["confidence"] == 0.55
    assert payload["payload"]["state"] == "outside"
    assert payload["payload"]["distance_from_home"] == 600.0
    assert payload["metadata"]["legacy_route"] == "/api/location/heartbeat"
    assert payload["metadata"]["force"] is True


def test_sensing_route_keeps_response_and_shadow_writes_evidence(monkeypatch):
    calls = []
    broadcasts = []

    async def fake_broadcast(payload):
        broadcasts.append(payload)

    monkeypatch.setattr(sensing_routes, "append_sensing_entry", lambda entry: None)
    monkeypatch.setattr(sensing_routes, "cleanup_old_sensing_logs", lambda: None)
    monkeypatch.setattr(sensing_routes, "record_sensing_entry_safely", lambda entry: calls.append(entry))
    monkeypatch.setattr(sensing_routes, "manager", SimpleNamespace(broadcast=fake_broadcast))

    result = asyncio.run(sensing_routes.report_sensor(
        sensing_routes.SensorTick(timestamp=100.0, motion="still", battery_pct=80)
    ))

    assert result == {"ok": True}
    assert len(calls) == 1
    assert calls[0]["type"] == "sensor"
    assert calls[0]["data"] == {"motion": "still", "battery_pct": 80}
    assert broadcasts[0]["type"] == "sensing_tick"


def test_activity_route_keeps_response_and_shadow_writes_evidence(monkeypatch):
    calls = []
    broadcasts = []

    async def fake_broadcast(payload):
        broadcasts.append(payload)

    monkeypatch.setattr(activity_routes, "append_activity_log", lambda entry: None)
    monkeypatch.setattr(activity_routes, "cleanup_old_activity_logs", lambda: None)
    monkeypatch.setattr(activity_routes, "record_activity_entry_safely", lambda entry: calls.append(entry))
    monkeypatch.setattr(activity_routes, "manager", SimpleNamespace(broadcast=fake_broadcast))

    result = asyncio.run(activity_routes.report_activity(
        activity_routes.ActivityReport(device="phone", app="微信", title="chat", timestamp=100.0)
    ))

    assert result == {"ok": True}
    assert len(calls) == 1
    assert calls[0]["device"] == "phone"
    assert calls[0]["app"] == "微信"
    assert broadcasts[0]["type"] == "activity_log"


def test_activity_route_pc_uses_pc_context_service(monkeypatch):
    calls = []
    broadcasts = []

    async def fake_broadcast(payload):
        broadcasts.append(payload)

    monkeypatch.setattr(activity_routes, "append_activity_log", lambda entry: calls.append(("log", entry)))
    monkeypatch.setattr(activity_routes, "cleanup_old_activity_logs", lambda: None)
    monkeypatch.setattr(
        activity_routes,
        "record_activity_entry_safely",
        lambda entry: calls.append(("evidence", entry)),
    )
    monkeypatch.setattr(activity_routes, "manager", SimpleNamespace(broadcast=fake_broadcast))

    result = asyncio.run(activity_routes.report_activity(
        activity_routes.ActivityReport(
            device="pc",
            app="Code.exe",
            title="ObsidianVow",
            timestamp=100.0,
            active_state="active",
            last_input_age_sec=20,
        )
    ))

    assert result == {"ok": True}
    assert calls[0][0] == "log"
    assert calls[0][1]["device"] == "pc"
    assert calls[0][1]["app"] == "VS Code"
    assert calls[0][1]["title"] == "ObsidianVow"
    assert calls[0][1]["active_state"] == "active"
    assert calls[1][0] == "evidence"
    assert broadcasts[0]["data"] == calls[0][1]


def test_location_route_keeps_response_and_shadow_writes_evidence(monkeypatch):
    calls = []

    async def fake_process_heartbeat(lng, lat, accuracy, is_gcj02, **_kwargs):
        assert (lng, lat, accuracy, is_gcj02) == (120.1, 30.2, 25.0, False)
        return {"state": "outside", "state_changed": False}

    monkeypatch.setattr(location_routes.location_runtime, "load_config", lambda: {"enabled": True})
    monkeypatch.setattr(location_routes.location_runtime, "process_heartbeat", fake_process_heartbeat)
    monkeypatch.setattr(
        location_runtime_module,
        "record_location_heartbeat_safely",
        lambda body, result: calls.append((body, result)),
    )

    result = asyncio.run(location_routes.location_heartbeat(
        location_routes.HeartbeatBody(lng=120.1, lat=30.2, accuracy=25.0)
    ))

    assert result == {"ok": True, "state": "outside", "state_changed": False}
    assert len(calls) == 1
    assert calls[0][0]["lng"] == 120.1
    assert calls[0][1]["state"] == "outside"
