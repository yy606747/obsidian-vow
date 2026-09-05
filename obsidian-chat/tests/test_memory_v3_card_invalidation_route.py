import asyncio

import pytest
from fastapi import HTTPException

from routes import memories


def test_memory_v3_config_route_accepts_static_relationship_register(monkeypatch):
    captured = []

    def fake_update(updates: dict):
        captured.append(updates)
        return updates

    monkeypatch.setattr(memories.memory_service, "update_memory_v3_config", fake_update)
    body = memories.MemoryV3ConfigUpdate(
        relational_card_relationship_register="daddy、dom 是双方既有的日常关系语域。"
    )
    result = asyncio.run(memories.update_memory_v3_config(body))

    assert result == {
        "relational_card_relationship_register": "daddy、dom 是双方既有的日常关系语域。"
    }
    assert captured == [result]


def test_invalidate_relational_card_route_delegates_to_owner_correction(monkeypatch):
    calls = []

    async def fake_invalidate(card_id: str, *, reason: str):
        calls.append((card_id, reason))
        return {
            "id": card_id,
            "status": "invalid",
            "invalidated": True,
        }

    monkeypatch.setattr(
        memories.memory_service,
        "invalidate_relational_card",
        fake_invalidate,
    )
    result = asyncio.run(
        memories.invalidate_relational_card(
            "relcard-1",
            memories.RelationalCardInvalidateRequest(reason="这张卡把原意讲反了"),
        )
    )

    assert calls == [("relcard-1", "这张卡把原意讲反了")]
    assert result == {
        "id": "relcard-1",
        "status": "invalid",
        "invalidated": True,
    }


@pytest.mark.parametrize(
    ("error", "expected_status"),
    [(KeyError("missing"), 404), (ValueError("not active"), 409)],
)
def test_invalidate_relational_card_route_maps_repository_errors(
    monkeypatch,
    error,
    expected_status,
):
    async def fake_invalidate(_card_id: str, *, reason: str):
        del reason
        raise error

    monkeypatch.setattr(
        memories.memory_service,
        "invalidate_relational_card",
        fake_invalidate,
    )

    with pytest.raises(HTTPException) as exc:
        asyncio.run(
            memories.invalidate_relational_card(
                "relcard-1",
                memories.RelationalCardInvalidateRequest(),
            )
        )

    assert exc.value.status_code == expected_status
