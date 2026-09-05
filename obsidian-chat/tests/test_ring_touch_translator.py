import asyncio

import ring_touch_translator as translator
from ring_touch_translator import DEFAULT_HAPTICS, parse_ring_haptics, translate_ring_touch


def test_parse_ring_haptics_reads_json_fields():
    assert parse_ring_haptics('{"taps": 3, "interval_ms": 1500}') == {
        "taps": 3,
        "interval_ms": 1500,
    }


def test_parse_ring_haptics_uses_defaults_for_bad_json_and_missing_fields():
    assert parse_ring_haptics("not json") == DEFAULT_HAPTICS
    assert parse_ring_haptics('{"taps": 2}') == {"taps": 2, "interval_ms": 2000}


def test_translate_ring_touch_calls_dedicated_configurable_slot(monkeypatch):
    calls = []

    async def fake_call_slot_chat(slot_name, messages, **kwargs):
        calls.append((slot_name, messages, kwargs))
        return '{"taps": 2, "interval_ms": 3000}'

    monkeypatch.setattr(translator, "call_slot_chat", fake_call_slot_chat)

    result = asyncio.run(translate_ring_touch("慢慢地点两下，从容一点"))

    assert result == {"taps": 2, "interval_ms": 3000}
    assert calls[0][0] == "ring_touch_translator"
    assert calls[0][2]["expect_json"] is True
    assert calls[0][2]["temperature"] == 0.3
    assert calls[0][2]["max_tokens"] == 128
    assert "次数是硬约束" in calls[0][1][0]["content"]


def test_translate_ring_touch_uses_default_when_slot_fails(monkeypatch):
    async def fake_call_slot_chat(*_args, **_kwargs):
        raise RuntimeError("provider down")

    monkeypatch.setattr(translator, "call_slot_chat", fake_call_slot_chat)

    assert asyncio.run(translate_ring_touch("轻轻碰一下")) == DEFAULT_HAPTICS
