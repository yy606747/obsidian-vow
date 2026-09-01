"""Phase 2: device identity must survive the report path and the Evidence
shadow write so phone vs tablet stays distinguishable downstream."""

from app.events import EvidenceLedger
from app.legacy_adapters.evidence import record_activity_entry


def test_evidence_preserves_device_identity_for_mobile():
    ledger = EvidenceLedger()
    record = record_activity_entry(
        {
            "device": "phone",  # legacy coarse during migration
            "device_id": "android_tab1",
            "device_name": "华为平板",
            "device_type": "tablet",
            "platform": "android",
            "app": "网易云音乐",
            "title": "",
            "timestamp": 1000.0,
        },
        ledger=ledger,
    )

    assert record.source == "android.activity"
    assert record.payload["device_id"] == "android_tab1"
    assert record.payload["device_name"] == "华为平板"
    assert record.payload["device_type"] == "tablet"
    assert record.payload["platform"] == "android"
    # queryable in metadata too
    assert record.metadata["device_id"] == "android_tab1"
    assert record.metadata["device_type"] == "tablet"


def test_evidence_legacy_entry_without_identity_still_works():
    ledger = EvidenceLedger()
    record = record_activity_entry(
        {"device": "phone", "app": "微信", "title": "", "timestamp": 1000.0},
        ledger=ledger,
    )
    assert record.source == "android.activity"
    assert "device_id" not in record.payload  # _payload drops None/empty
    assert record.payload["device"] == "phone"


def test_pc_entry_unaffected_by_identity_fields():
    ledger = EvidenceLedger()
    record = record_activity_entry(
        {"device": "pc", "app": "VSCode", "title": "", "timestamp": 1000.0,
         "active_state": "active", "last_input_age_sec": 3},
        ledger=ledger,
    )
    assert record.source == "pc.activity"
    assert record.payload["active_state"] == "active"
    assert "device_id" not in record.payload


def test_report_endpoint_persists_identity_fields(monkeypatch):
    import routes.activity as act

    captured = {}

    def fake_append(entry):
        captured["entry"] = entry

    broadcasts = []

    class FakeManager:
        async def broadcast(self, msg):
            broadcasts.append(msg)

    monkeypatch.setattr(act, "append_activity_log", fake_append)
    monkeypatch.setattr(act, "record_activity_entry_safely", lambda e: None)
    monkeypatch.setattr(act, "cleanup_old_activity_logs", lambda: None)
    monkeypatch.setattr(act, "resolve_app_name", lambda app, title: app)
    monkeypatch.setattr(act, "manager", FakeManager())

    import asyncio

    report = act.ActivityReport(
        device="phone",
        device_id="android_tab1",
        device_name="华为平板",
        device_type="tablet",
        platform="android",
        app="网易云音乐",
        title="",
        timestamp=1000.0,
    )
    result = asyncio.run(act.report_activity(report))

    assert result["ok"] is True
    entry = captured["entry"]
    assert entry["device"] == "phone"
    assert entry["device_id"] == "android_tab1"
    assert entry["device_name"] == "华为平板"
    assert entry["device_type"] == "tablet"
    assert entry["platform"] == "android"
    # broadcast carries the same enriched entry
    assert broadcasts[0]["data"]["device_id"] == "android_tab1"
