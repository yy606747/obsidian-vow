import asyncio

from app.devices import DeviceService
from app.devices.drivers.ring import RingDeviceDriver
from app.tools.schemas import ToolContext, ToolIntent, ToolStatus
from app.tools.service import ToolService


def test_ring_tool_intent_executes_through_device_service_to_ws():
    events = []
    sent = []

    async def event_sink(**kwargs):
        events.append(kwargs)
        return {"id": f"mev_{len(events)}", **kwargs}

    async def ws_sender(device_type, payload):
        sent.append((device_type, payload))
        return True

    driver = RingDeviceDriver(
        event_sink=event_sink,
        ws_sender=ws_sender,
        settings_reader=lambda: True,
        now=lambda: 3000.0,
    )
    service = DeviceService(drivers=[driver], event_sink=event_sink)

    async def ring_adapter(intent, context):
        request_id = f"{context.request_id}:{intent.id}"
        params = dict(intent.arguments)
        params["_ring_request_id"] = request_id
        params["_ring_created_at"] = 3000.0
        return await service.execute_command("smart_ring", "touch", params, request_id=request_id)

    intent = ToolIntent(
        id="intent_ring_001",
        tool_name="device.ring_touch",
        raw_text="{}",
        arguments={"touch": "轻轻点两下", "haptics": {"taps": 2, "interval_ms": 2000}},
        side_effect_level="device",
        allowed_modes=("ring_touch_enabled",),
    )
    context = ToolContext(
        conv_id="conv",
        msg_id="msg",
        request_id="msg",
        mode="normal",
        capabilities=("device.ring_touch",),
    )

    async def run():
        await driver.report_state("smart_ring", status="online")
        return await ToolService().execute_async([intent], context=context, adapters={"device.ring_touch": ring_adapter})

    results = asyncio.run(run())

    assert results[0].status is ToolStatus.EXECUTED
    assert results[0].result["status"] == "queued"
    assert sent[0][0] == "smart_ring"
    assert sent[0][1]["data"]["taps"] == 2
    assert [event["namespace"] for event in events] == ["ring_touch", "device"]
