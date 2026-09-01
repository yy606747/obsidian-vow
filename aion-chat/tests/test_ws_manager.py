import asyncio
import json

from ws import ConnectionManager


class FakeWebSocket:
    def __init__(self):
        self.sent = []

    async def accept(self):
        return None

    async def send_text(self, text):
        self.sent.append(text)


def test_connection_manager_sends_to_registered_device_only():
    manager = ConnectionManager()
    ws = FakeWebSocket()

    async def run():
        await manager.connect(ws)
        assert await manager.send_to_device("smart_ring", {"type": "x"}) is False
        manager.register_device_ws("smart_ring", ws)
        assert await manager.send_to_device("smart_ring", {"type": "ring_touch_request"}) is True
        manager.disconnect(ws)
        assert await manager.send_to_device("smart_ring", {"type": "ring_touch_request"}) is False

    asyncio.run(run())

    assert '"type": "ring_touch_request"' in ws.sent[0]


def test_assistant_message_broadcast_includes_runtime_ai_name_without_mutating_source():
    manager = ConnectionManager(ai_name_loader=lambda: "Aion")
    ws = FakeWebSocket()
    event = {
        "type": "msg_created",
        "data": {"id": "m1", "role": "assistant", "content": "在呢。"},
    }

    async def run():
        await manager.connect(ws)
        await manager.broadcast(event)

    asyncio.run(run())

    assert json.loads(ws.sent[0])["data"]["ai_name"] == "Aion"
    assert "ai_name" not in event["data"]
