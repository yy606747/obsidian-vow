import asyncio

from app.events import EvidenceLedger
from routes import events


def test_evidence_summary_route_exposes_read_only_lifecycle(monkeypatch):
    ledger = EvidenceLedger(now=lambda: 1000.0, max_records=50)
    ledger.record(kind="sensing.sensor", source="android.sensing", observed_at=990.0)
    monkeypatch.setattr(events, "evidence_ledger", ledger)

    payload = asyncio.run(events.get_evidence_summary(max_age_sec=60.0))

    assert payload["count"] == 1
    assert payload["source_counts"] == {"android.sensing": 1}
    assert payload["lifecycle"] == {
        "storage": "process_memory",
        "max_records": 50,
        "durable": False,
        "retention": "bounded_realtime_window",
        "persistence_decision": "disabled_until_need_is_proven",
    }
