import pytest

from app.events import EvidenceLifecyclePolicy, EvidenceLedger, EvidenceRecord


def test_evidence_record_serializes_source_freshness_and_confidence():
    record = EvidenceRecord(
        id="ev_1",
        kind="sensor.motion",
        source="android.sensing",
        observed_at=100.0,
        received_at=103.0,
        confidence=0.75,
        payload={"motion": "walking"},
        metadata={"device_id": "phone"},
    )

    assert record.to_dict(reference_time=130.0) == {
        "id": "ev_1",
        "kind": "sensor.motion",
        "source": "android.sensing",
        "observed_at": 100.0,
        "received_at": 103.0,
        "freshness_sec": 30.0,
        "received_delay_sec": 3.0,
        "confidence": 0.75,
        "payload": {"motion": "walking"},
        "metadata": {"device_id": "phone"},
        "future": False,
    }


def test_evidence_record_rejects_invalid_contract_values():
    with pytest.raises(ValueError):
        EvidenceRecord(
            id="ev_bad",
            kind="sensor.motion",
            source="android.sensing",
            observed_at=1.0,
            received_at=1.0,
            confidence=1.5,
        )

    with pytest.raises(ValueError):
        EvidenceRecord(
            id="ev_bad",
            kind="",
            source="android.sensing",
            observed_at=1.0,
            received_at=1.0,
        )


def test_evidence_ledger_defaults_missing_timestamp_and_filters_stale_future():
    now = [200.0]
    ledger = EvidenceLedger(now=lambda: now[0], max_records=10)

    current = ledger.record(
        kind="sensor.motion",
        source="android.sensing",
        payload={"motion": "still"},
    )
    ledger.record(
        kind="activity.app",
        source="android.activity",
        observed_at=50.0,
        payload={"app": "Chrome"},
    )
    ledger.record(
        kind="location.fix",
        source="android.location",
        observed_at=260.0,
        payload={"lat": 1.0, "lng": 2.0},
    )

    assert current.observed_at == 200.0
    assert current.received_at == 200.0

    snapshot = ledger.snapshot(max_age_sec=120.0, reference_time=200.0)
    payload = snapshot.to_dict()

    assert payload["count"] == 1
    assert payload["records"][0]["id"] == current.id
    assert payload["records"][0]["freshness_sec"] == 0.0
    assert payload["source_counts"] == {"android.sensing": 1}


def test_evidence_snapshot_can_filter_by_kind_source_and_limit_latest_records():
    ledger = EvidenceLedger(now=lambda: 500.0)
    ledger.record(kind="sensor.motion", source="android.sensing", observed_at=450.0)
    first_location = ledger.record(kind="location.fix", source="android.location", observed_at=460.0)
    second_location = ledger.record(kind="location.fix", source="android.location", observed_at=470.0)
    ledger.record(kind="activity.app", source="pc.activity", observed_at=480.0)

    snapshot = ledger.snapshot(
        kinds=["location.fix"],
        sources=["android.location"],
        max_age_sec=100.0,
        reference_time=500.0,
        limit=1,
    )
    payload = snapshot.to_dict()

    assert payload["count"] == 1
    assert payload["records"][0]["id"] == second_location.id
    assert payload["kind_counts"] == {"location.fix": 1}
    assert payload["source_counts"] == {"android.location": 1}
    assert first_location.id != second_location.id


def test_evidence_ledger_stats_exposes_lifecycle_and_counts():
    ledger = EvidenceLedger(now=lambda: 1000.0, max_records=20)
    ledger.record(kind="sensing.sensor", source="android.sensing", observed_at=980.0)
    ledger.record(kind="location.fix", source="android.location", observed_at=990.0)
    ledger.record(kind="activity.app", source="pc.activity", observed_at=1005.0)

    payload = ledger.stats(max_age_sec=60.0, reference_time=1000.0)

    assert payload["count"] == 2
    assert payload["source_counts"] == {"android.sensing": 1, "android.location": 1}
    assert payload["kind_counts"] == {"sensing.sensor": 1, "location.fix": 1}
    assert payload["oldest_freshness_sec"] == 20.0
    assert payload["newest_freshness_sec"] == 10.0
    assert payload["lifecycle"] == {
        "storage": "process_memory",
        "max_records": 20,
        "durable": False,
        "retention": "bounded_realtime_window",
        "persistence_decision": "disabled_until_need_is_proven",
    }


def test_evidence_ledger_prune_removes_stale_and_future_records():
    ledger = EvidenceLedger(now=lambda: 1000.0)
    kept = ledger.record(kind="location.fix", source="android.location", observed_at=990.0)
    ledger.record(kind="sensing.sensor", source="android.sensing", observed_at=100.0)
    ledger.record(kind="activity.app", source="pc.activity", observed_at=1100.0)

    result = ledger.prune(max_age_sec=60.0, reference_time=1000.0)
    remaining = ledger.snapshot(reference_time=1000.0).to_dict()

    assert result["deleted"] == 2
    assert remaining["count"] == 1
    assert remaining["records"][0]["id"] == kept.id


def test_evidence_lifecycle_policy_is_explicitly_non_durable_by_default():
    policy = EvidenceLifecyclePolicy()

    assert policy.to_dict(max_records=10) == {
        "storage": "process_memory",
        "max_records": 10,
        "durable": False,
        "retention": "bounded_realtime_window",
        "persistence_decision": "disabled_until_need_is_proven",
    }


def test_evidence_ledger_retains_nine_per_stream_through_notification_flood():
    ledger = EvidenceLedger(now=lambda: 5000.0, max_records=1000, min_records_per_stream=9)
    critical_streams = (
        ("sensing.sensor", "android.sensing", "phone-sensing"),
        ("location.state", "location.v2", ""),
        ("activity.app", "pc.activity", ""),
        ("activity.app", "android.activity", "owner-phone"),
    )
    critical_ids = set()
    for kind, source, device_id in critical_streams:
        for index in range(9):
            record = ledger.record(
                kind=kind,
                source=source,
                observed_at=100 + index,
                payload={"device_id": device_id} if device_id else {},
                metadata={"device_id": device_id} if device_id else {},
            )
            critical_ids.add(record.id)

    for index in range(1500):
        ledger.record(
            kind="sensing.notification",
            source="android.sensing",
            observed_at=1000 + index,
            payload={"app": "群聊"},
        )

    snapshot = ledger.snapshot(reference_time=5000).to_dict()
    retained_ids = {record["id"] for record in snapshot["records"]}
    stats = ledger.stats(reference_time=5000)

    assert snapshot["count"] == 1000
    assert critical_ids <= retained_ids
    assert sum(stats["retention"]["retained_by_stream"].values()) == 1000
    assert stats["retention"]["min_records_per_stream"] == 9
    assert stats["retention"]["evicted_total"] == 536
    assert stats["retention"]["retention_floor_fallbacks"] == 0


def test_evidence_ledger_floor_fallback_preserves_hard_global_limit():
    ledger = EvidenceLedger(now=lambda: 100.0, max_records=2, min_records_per_stream=9)
    first = ledger.record(kind="a", source="one")
    ledger.record(kind="b", source="two")
    ledger.record(kind="c", source="three")

    snapshot = ledger.snapshot(reference_time=100).to_dict()
    stats = ledger.stats(reference_time=100)["retention"]

    assert snapshot["count"] == 2
    assert first.id not in {record["id"] for record in snapshot["records"]}
    assert stats["retention_floor_fallbacks"] == 1
    assert stats["evicted_total"] == 1


def test_prune_rebuilds_stream_counts_and_clear_resets_retention_diagnostics():
    ledger = EvidenceLedger(now=lambda: 1000.0, max_records=3, min_records_per_stream=1)
    ledger.record(kind="old", source="one", observed_at=1)
    ledger.record(kind="new", source="two", observed_at=999)
    ledger.record(kind="noise", source="three", observed_at=999)
    ledger.record(kind="noise", source="three", observed_at=1000)

    ledger.prune(max_age_sec=10, reference_time=1000)
    retention = ledger.stats(reference_time=1000)["retention"]

    assert sum(retention["retained_by_stream"].values()) == 2
    assert not any(label.startswith("old|") for label in retention["retained_by_stream"])

    ledger.clear()
    retention = ledger.stats(reference_time=1000)["retention"]
    assert retention["retained_by_stream"] == {}
    assert retention["evicted_total"] == 0
    assert retention["evicted_by_stream"] == {}
    assert retention["retention_floor_fallbacks"] == 0
