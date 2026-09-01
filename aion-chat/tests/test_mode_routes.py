import asyncio

from routes import modes


def test_list_modes_exposes_capability_catalog():
    payload = asyncio.run(modes.list_modes())

    assert payload["default_mode"] == "normal"
    by_mode = {item["mode"]: item for item in payload["modes"]}
    assert "normal" in by_mode
    assert "device_control" in by_mode
    assert "music.search" in by_mode["normal"]["capabilities"]
    assert "device.toy" not in by_mode["normal"]["capabilities"]
    assert "device.toy" in by_mode["device_control"]["capabilities"]


def test_resolve_mode_uses_query_mode_and_existing_chat_flags():
    normal = asyncio.run(modes.resolve_mode())
    intimate = asyncio.run(modes.resolve_mode(whisper_mode=True))
    device = asyncio.run(modes.resolve_mode(ai_dom_mode=True, whisper_mode=True))

    assert normal["mode"] == "normal"
    assert normal["source"] == "query"
    assert "device.toy" not in normal["capabilities"]
    assert intimate["mode"] == "intimate"
    assert intimate["source"] == "whisper_mode"
    assert "device.toy" in intimate["capabilities"]
    assert device["mode"] == "device_control"
    assert device["source"] == "ai_dom_mode"
    assert "device.toy" in device["capabilities"]
