import asyncio

import pytest
from fastapi import HTTPException

from routes import sentinel


class _FakeShadowRuntime:
    def __init__(self):
        self.refresh_calls = 0
        self.list_calls = []
        self.labels = []

    def refresh_due_outcomes(self):
        self.refresh_calls += 1
        return 2

    def list_payload(self, **kwargs):
        self.list_calls.append(kwargs)
        return {
            "enabled": True,
            "entries": [{"id": "ctx_shadow_1", "evaluation_status": "matched"}],
            "stats": {"total": 1},
        }

    def set_owner_label(self, evaluation_id, label):
        self.labels.append((evaluation_id, label))
        return {"id": evaluation_id, "owner_label": label}


def test_shadow_query_refreshes_outcomes_and_defaults_to_matched(monkeypatch):
    fake = _FakeShadowRuntime()
    monkeypatch.setattr(sentinel, "context_trigger_shadow_runtime", fake)

    result = asyncio.run(sentinel.get_context_trigger_shadow(
        include_non_matched=False,
        rule=None,
        limit=50,
        before_occurred_at=None,
    ))

    assert result["enabled"] is True
    assert fake.refresh_calls == 1
    assert fake.list_calls == [{
        "include_non_matched": False,
        "rule": None,
        "limit": 50,
        "before_occurred_at": None,
    }]


def test_shadow_label_route_is_one_click_owner_annotation(monkeypatch):
    fake = _FakeShadowRuntime()
    monkeypatch.setattr(sentinel, "context_trigger_shadow_runtime", fake)

    result = asyncio.run(sentinel.put_context_trigger_shadow_label(
        "ctx_shadow_1",
        sentinel.ContextTriggerShadowLabelUpdate(label="indifferent"),
    ))

    assert result == {
        "ok": True,
        "entry": {"id": "ctx_shadow_1", "owner_label": "indifferent"},
    }
    assert fake.labels == [("ctx_shadow_1", "indifferent")]


def test_shadow_label_route_maps_missing_row_to_404(monkeypatch):
    fake = _FakeShadowRuntime()
    fake.set_owner_label = lambda *_args: (_ for _ in ()).throw(KeyError("missing"))
    monkeypatch.setattr(sentinel, "context_trigger_shadow_runtime", fake)

    with pytest.raises(HTTPException) as exc:
        asyncio.run(sentinel.put_context_trigger_shadow_label(
            "missing",
            sentinel.ContextTriggerShadowLabelUpdate(label="wrong"),
        ))

    assert exc.value.status_code == 404
