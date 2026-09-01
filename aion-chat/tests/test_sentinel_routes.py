import asyncio

from routes import sentinel


class FakeSentinelEvidenceService:
    def __init__(self):
        self.kwargs = None

    def snapshot_payload(self, **kwargs):
        self.kwargs = kwargs
        return {
            "count": 0,
            "records": [],
            "summary_lines": [],
            "policy": {"read_only": True, "decision": None, "side_effects": []},
        }


def test_sentinel_evidence_snapshot_route_is_read_only_and_passes_filters(monkeypatch):
    service = FakeSentinelEvidenceService()
    monkeypatch.setattr(sentinel, "sentinel_evidence_service", service)

    payload = asyncio.run(sentinel.get_evidence_snapshot(
        max_age_sec=60.0,
        limit=5,
        kind=["location.fix"],
        source=["android.location"],
    ))

    assert payload["policy"] == {"read_only": True, "decision": None, "side_effects": []}
    assert service.kwargs == {
        "max_age_sec": 60.0,
        "limit": 5,
        "kinds": ["location.fix"],
        "sources": ["android.location"],
    }


class FakeSentinelRuntime:
    def status_payload(self):
        return {
            "enabled": True,
            "monitoring": True,
            "next_check_in": 120,
            "thread_alive": True,
        }


def test_sentinel_status_uses_runtime_status_payload(monkeypatch):
    monkeypatch.setattr(sentinel, "sentinel_runtime", FakeSentinelRuntime())

    payload = asyncio.run(sentinel.get_sentinel_status())

    assert payload == {
        "enabled": True,
        "monitoring": True,
        "next_check_in": 120,
        "thread_alive": True,
    }
