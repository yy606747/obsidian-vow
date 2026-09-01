from app.events import EvidenceLedger
from app.sentinel import SentinelEvidenceService


def test_sentinel_evidence_snapshot_is_read_only_and_explainable():
    ledger = EvidenceLedger(now=lambda: 1000.0)
    ledger.record(
        kind="sensing.sensor",
        source="android.sensing",
        observed_at=940.0,
        confidence=0.8,
        payload={"motion": "walking", "battery_pct": 70},
    )
    ledger.record(
        kind="activity.app",
        source="pc.activity",
        observed_at=930.0,
        payload={"device": "pc", "app": "Code.exe", "title": "service.py"},
    )

    service = SentinelEvidenceService(ledger=ledger, default_max_age_sec=300, default_limit=10)
    payload = service.snapshot_payload(reference_time=1000.0)

    assert payload["count"] == 2
    assert payload["policy"] == {"read_only": True, "decision": None, "side_effects": []}
    assert payload["source_counts"] == {"pc.activity": 1, "android.sensing": 1}
    assert any("sensing.sensor from android.sensing" in line for line in payload["summary_lines"])
    assert any("activity.app from pc.activity" in line for line in payload["summary_lines"])


def test_sentinel_evidence_snapshot_filters_future_and_stale_records():
    ledger = EvidenceLedger(now=lambda: 1000.0)
    current = ledger.record(
        kind="location.fix",
        source="android.location",
        observed_at=980.0,
        payload={"state": "outside", "accuracy": 25.0},
    )
    ledger.record(kind="sensing.sensor", source="android.sensing", observed_at=500.0)
    ledger.record(kind="activity.app", source="pc.activity", observed_at=1200.0)

    service = SentinelEvidenceService(ledger=ledger, default_max_age_sec=60, default_limit=10)
    payload = service.snapshot_payload(reference_time=1000.0)

    assert payload["count"] == 1
    assert payload["records"][0]["id"] == current.id
    assert payload["records"][0]["future"] is False
    assert payload["summary_lines"] == [
        "location.fix from android.location, 20s old, confidence=1.00: state=outside, accuracy=25.0"
    ]


def test_sentinel_evidence_snapshot_supports_kind_and_source_filters():
    ledger = EvidenceLedger(now=lambda: 1000.0)
    ledger.record(kind="sensing.sensor", source="android.sensing", observed_at=990.0)
    location = ledger.record(kind="location.fix", source="android.location", observed_at=995.0)
    ledger.record(kind="activity.app", source="pc.activity", observed_at=998.0)

    service = SentinelEvidenceService(ledger=ledger)
    payload = service.snapshot_payload(
        reference_time=1000.0,
        kinds=["location.fix"],
        sources=["android.location"],
    )

    assert payload["count"] == 1
    assert payload["records"][0]["id"] == location.id
    assert payload["kind_counts"] == {"location.fix": 1}
    assert payload["source_counts"] == {"android.location": 1}
