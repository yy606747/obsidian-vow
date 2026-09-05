import asyncio

import app.chat.side_effects as side_effects


def test_v2_generation_requires_both_rollout_switches():
    assert not side_effects._relational_card_v2_generation_active(
        {"relational_card_generation_enabled": True}
    )
    assert not side_effects._relational_card_v2_generation_active(
        {"relational_card_v2_generation_enabled": True}
    )
    assert side_effects._relational_card_v2_generation_active(
        {
            "relational_card_generation_enabled": True,
            "relational_card_v2_generation_enabled": True,
        }
    )


def test_chunk_update_does_not_start_v2_with_only_legacy_switch(monkeypatch):
    generated = False

    async def fake_chunks(_conv_id):
        return {
            "inserted_chunks": 0,
            "embedding_success": 0,
            "embedding_failed": 0,
        }

    async def forbidden_cards(*_args, **_kwargs):
        nonlocal generated
        generated = True
        raise AssertionError("v2 writer must remain gated")

    monkeypatch.setattr(side_effects.memory_service, "ensure_conversation_chunks", fake_chunks)
    monkeypatch.setattr(
        side_effects.memory_service,
        "generate_stable_relational_cards",
        forbidden_cards,
    )

    asyncio.run(
        side_effects._update_memory_chunks(
            "conv",
            memory_v3_config={"relational_card_generation_enabled": True},
        )
    )

    assert generated is False


def test_card_generation_and_digest_share_one_frozen_config_snapshot(monkeypatch):
    generated = []

    async def fake_chunks(_conv_id):
        return {
            "inserted_chunks": 1,
            "embedding_success": 0,
            "embedding_failed": 0,
        }

    async def fake_cards(*, config_snapshot):
        generated.append(dict(config_snapshot))
        return {"selected": 1, "created": 1, "abstained": 0, "invalid": 0}

    def should_not_open_db():
        raise AssertionError("auto digest must return before reading the database")

    monkeypatch.setattr(side_effects.memory_service, "ensure_conversation_chunks", fake_chunks)
    monkeypatch.setattr(side_effects.memory_service, "generate_stable_relational_cards", fake_cards)
    monkeypatch.setattr(side_effects, "get_db", should_not_open_db)
    snapshot = {
        "relational_card_generation_enabled": True,
        "relational_card_v2_generation_enabled": True,
        "replace_auto_digest": True,
    }

    async def scenario():
        await side_effects._update_memory_chunks(
            "conv",
            reason="test",
            memory_v3_config=snapshot,
        )
        await side_effects._maybe_auto_digest(snapshot)

    asyncio.run(scenario())

    assert len(generated) == 1
    assert generated[0]["relational_card_generation_enabled"] is True
    assert generated[0]["relational_card_v2_generation_enabled"] is True
    assert generated[0]["replace_auto_digest"] is True
