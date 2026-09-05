from types import SimpleNamespace
import asyncio

from app.events import evidence_ledger
from app.daily_signals import runtime as daily_signal_runtime
from app.daily_signals.store import DailySignalStore
from routes import activity, events, location, sensing, sentinel


def test_phase8_route_smoke_links_legacy_reports_to_sentinel_snapshot(monkeypatch, tmp_path):
    evidence_ledger.clear()
    broadcasts = []

    async def fake_broadcast(payload):
        broadcasts.append(payload)

    async def fake_process_heartbeat(lng, lat, accuracy, is_gcj02, **_kwargs):
        return {
            "state": "outside",
            "old_state": "at_home",
            "state_changed": True,
            "distance_from_home": 800.0,
            "full_api": False,
        }

    monkeypatch.setattr(sensing, "append_sensing_entry", lambda entry: None)
    monkeypatch.setattr(sensing, "cleanup_old_sensing_logs", lambda: None)
    monkeypatch.setattr(sensing, "manager", SimpleNamespace(broadcast=fake_broadcast))
    monkeypatch.setattr(activity, "append_activity_log", lambda entry: None)
    monkeypatch.setattr(activity, "cleanup_old_activity_logs", lambda: None)
    monkeypatch.setattr(activity, "manager", SimpleNamespace(broadcast=fake_broadcast))
    monkeypatch.setattr(location.location_runtime, "load_config", lambda: {"enabled": True})
    monkeypatch.setattr(location.location_runtime, "process_heartbeat", fake_process_heartbeat)
    monkeypatch.setattr(
        daily_signal_runtime,
        "get_default_store",
        lambda: DailySignalStore(tmp_path / "signal_daily.db"),
    )

    assert asyncio.run(sensing.report_sensor(
        sensing.SensorTick(motion="walking", motion_confidence=90, battery_pct=66)
    )) == {"ok": True}
    assert asyncio.run(activity.report_activity(
        activity.ActivityReport(device="phone", app="微信", title="chat")
    )) == {"ok": True}
    assert asyncio.run(location.location_heartbeat(
        location.HeartbeatBody(lng=120.1, lat=30.2, accuracy=25.0)
    ))["state"] == "outside"

    snapshot = asyncio.run(sentinel.get_evidence_snapshot(max_age_sec=60, kind=None, source=None))
    summary = asyncio.run(events.get_evidence_summary(max_age_sec=60))

    assert snapshot["count"] == 3
    assert snapshot["policy"] == {"read_only": True, "decision": None, "side_effects": []}
    assert snapshot["kind_counts"] == {
        "activity.app": 1,
        "location.fix": 1,
        "sensing.sensor": 1,
    }
    assert summary["count"] == 3
    assert summary["lifecycle"]["storage"] == "process_memory"
    assert [item["type"] for item in broadcasts] == ["sensing_tick", "activity_log"]
