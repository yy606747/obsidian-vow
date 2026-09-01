import asyncio

from app.web_search.service import WebSearchService


class _Repository:
    def __init__(self):
        self.calls = []

    async def consume_bound_in_tx(self, db, **kwargs):
        self.calls.append(("consume", db, kwargs))
        return 2

    async def enqueue_in_tx(self, db, **kwargs):
        self.calls.append(("enqueue", db, kwargs))
        return {"status": "queued", "search_id": "web-new"}

    async def reassign_consumed_in_tx(self, db, **kwargs):
        self.calls.append(("reassign", db, kwargs))
        return 2


def test_send_finalizes_consumption_and_enqueue_on_the_same_transaction(monkeypatch):
    repository = _Repository()
    service = WebSearchService(repository=repository)
    monkeypatch.setattr(service, "enabled", lambda: True)
    transaction = object()

    result = asyncio.run(service.finalize_dialogue_turn_in_tx(
        transaction,
        conv_id="conv",
        bound_turn_id="send:user-1",
        assistant_message_id="assistant-1",
        intent_text="查今天的新消息",
        origin_source="send",
        allow_new_intent=True,
        now=10,
    ))

    assert result == {"consumed": 2, "status": "queued", "search_id": "web-new"}
    assert [call[0] for call in repository.calls] == ["consume", "enqueue"]
    assert all(call[1] is transaction for call in repository.calls)
    assert repository.calls[1][2]["origin_turn_id"] == "assistant-1"


def test_regenerate_does_not_enqueue_a_replayed_intent(monkeypatch):
    repository = _Repository()
    service = WebSearchService(repository=repository)
    monkeypatch.setattr(service, "enabled", lambda: True)

    result = asyncio.run(service.finalize_dialogue_turn_in_tx(
        object(),
        conv_id="conv",
        bound_turn_id="",
        assistant_message_id="assistant-2",
        intent_text="不应重放",
        origin_source="send",
        allow_new_intent=False,
        now=20,
        replay_from_assistant_message_id="assistant-old",
    ))

    assert result == {"consumed": 0, "status": "ignored", "search_id": None}
    assert [call[0] for call in repository.calls] == ["reassign"]
    assert repository.calls[0][2] == {
        "from_assistant_message_id": "assistant-old",
        "to_assistant_message_id": "assistant-2",
    }
