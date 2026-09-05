import asyncio

from app.devices import AndroidMobileDeviceDriver, DeviceService


async def _noop_sink(**kwargs):
    # execute_command audits via the event sink; without a no-op here it falls
    # back to the real MemoryRepository and hangs the test.
    return {"id": "test_event"}


def test_driver_registers_and_distinguishes_two_android_devices():
    now = [1000.0]
    driver = AndroidMobileDeviceDriver(now=lambda: now[0])
    service = DeviceService(drivers=[driver], event_sink=_noop_sink)

    asyncio.run(service.report_state(
        "android_phone1",
        status="online",
        name="我的手机",
        capabilities=["activity.report", "screen.capture"],
        metadata={"platform": "android", "device_type": "phone"},
    ))
    asyncio.run(service.report_state(
        "android_tablet1",
        status="online",
        name="华为平板",
        capabilities=["activity.report", "screen.capture"],
        metadata={"platform": "android", "device_type": "tablet"},
    ))

    payload = asyncio.run(service.list_devices())
    by_id = {d["device_id"]: d for d in payload["devices"]}

    assert payload["count"] == 2
    assert by_id["android_phone1"]["name"] == "我的手机"
    assert by_id["android_phone1"]["kind"] == "android_phone"
    assert by_id["android_tablet1"]["name"] == "华为平板"
    assert by_id["android_tablet1"]["kind"] == "android_tablet"
    # both reported online and fresh
    assert by_id["android_phone1"]["status"] == "online"
    assert by_id["android_tablet1"]["status"] == "online"


def test_driver_marks_device_offline_when_stale():
    now = [1000.0]
    driver = AndroidMobileDeviceDriver(now=lambda: now[0], stale_after_sec=120.0)
    service = DeviceService(drivers=[driver], event_sink=_noop_sink)

    asyncio.run(service.report_state(
        "android_phone1", status="online", name="手机",
        metadata={"platform": "android", "device_type": "phone"},
    ))
    now[0] = 1000.0 + 121.0  # past stale window

    device = asyncio.run(service.get_device("android_phone1"))["device"]
    assert device["status"] == "offline"
    assert device["metadata"]["stale"] is True
    assert device["metadata"]["age_sec"] == 121.0


def test_screen_agent_online_is_separate_from_ordinary_online():
    now = [1000.0]
    driver = AndroidMobileDeviceDriver(now=lambda: now[0])
    service = DeviceService(drivers=[driver], event_sink=_noop_sink)

    asyncio.run(service.report_state(
        "android_tablet1", status="online", name="华为平板",
        metadata={"platform": "android", "device_type": "tablet"},
    ))

    # Ordinary online, but screen poll never happened → screen agent offline.
    device = asyncio.run(service.get_device("android_tablet1"))["device"]
    assert device["status"] == "online"
    assert device["metadata"]["screen_agent_online"] is False
    assert driver.screen_agent_online("android_tablet1") is False

    # After a screen poll, screen agent reads online.
    assert driver.mark_screen_poll("android_tablet1") is True
    assert driver.screen_agent_online("android_tablet1") is True
    device = asyncio.run(service.get_device("android_tablet1"))["device"]
    assert device["metadata"]["screen_agent_online"] is True

    # Screen poll ages out independently of the 120s ordinary-online window.
    now[0] = 1000.0 + 91.0
    assert driver.screen_agent_online("android_tablet1") is False


def test_driver_only_owns_android_prefixed_ids():
    driver = AndroidMobileDeviceDriver()
    assert driver.owns("android_abc123") is True
    assert driver.owns("smart_ring") is False
    assert driver.owns("browser_toy_bridge") is False

    # report_state for a non-android id returns None (declines ownership),
    # letting fixed-id drivers claim it in the DeviceService chain.
    result = asyncio.run(driver.report_state("smart_ring", status="online"))
    assert result is None


def test_ping_reports_offline_when_stale_but_device_known():
    now = [1000.0]
    driver = AndroidMobileDeviceDriver(now=lambda: now[0], stale_after_sec=120.0)
    service = DeviceService(drivers=[driver], event_sink=_noop_sink)

    asyncio.run(service.report_state(
        "android_phone1", status="online", name="手机",
        metadata={"platform": "android", "device_type": "phone"},
    ))
    now[0] = 1000.0 + 200.0

    result = asyncio.run(service.execute_command("android_phone1", "ping"))
    assert result["ok"] is True  # command executed
    assert result["message"] == "device_offline"
    assert result["result"]["status"] == "offline"


def test_default_catalog_includes_android_mobile_driver():
    service = DeviceService(event_sink=_noop_sink)
    assert service.get_driver("android_mobile") is not None
    # android device id is claimed by android_mobile, not ring/browser
    asyncio.run(service.report_state(
        "android_phone1", status="online", name="手机",
        metadata={"platform": "android", "device_type": "phone"},
    ))
    payload = asyncio.run(service.list_devices())
    by_id = {d["device_id"]: d for d in payload["devices"]}
    assert by_id["android_phone1"]["driver_id"] == "android_mobile"
