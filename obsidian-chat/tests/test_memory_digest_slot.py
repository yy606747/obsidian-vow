import asyncio

import pytest

import config
import memory


def test_settings_migration_adds_memory_digest_slot():
    data = {
        "endpoints": [{"id": "sf", "name": "sf", "type": "openai"}],
        "slots": {
            "sentinel": {"endpoint": "sf", "model": "custom-sentinel"},
            "asr": {"endpoint": "sf", "model": "custom-asr", "path": "/audio/transcriptions"},
        },
        "user_models": {},
    }

    changed = config._ensure_endpoints_and_slots(data)

    assert changed is True
    assert data["slots"]["sentinel"]["model"] == "custom-sentinel"
    assert data["slots"]["memory_digest"] == {
        "endpoint": "sf",
        "model": config.DEFAULT_MEMORY_DIGEST_MODEL,
    }
    assert data["slots"]["relational_card_generation"] == {
        "endpoint": "sf",
        "model": config.DEFAULT_RELATIONAL_CARD_GENERATION_MODEL,
    }


def test_settings_migration_preserves_custom_relational_card_slot():
    data = {
        "endpoints": [
            {
                "id": "custom",
                "name": "custom",
                "type": "openai",
                "base_url": "https://api.siliconflow.cn/v1",
            }
        ],
        "slots": {
            "sentinel": {"endpoint": "custom", "model": "sentinel"},
            "harness_tool": {"endpoint": "custom", "model": "harness"},
            "ring_touch_translator": {
                "endpoint": "custom",
                "model": "harness",
            },
            "memory_digest": {"endpoint": "custom", "model": "digest"},
            "working_model_gate": {"endpoint": "custom", "model": "gate"},
            "relational_card_generation": {
                "endpoint": "custom",
                "model": "custom-card-model",
            },
            "presence_renderer": {"endpoint": "custom", "model": "renderer"},
            "presence_image": {"endpoint": "custom", "model": "image"},
            "vision_summary": {"endpoint": "", "model": "glm-4.6v-flash", "enabled": False},
            "asr": {"endpoint": "custom", "model": "asr"},
        },
        "presence_image_slot_migration_v1": True,
        "user_models": {},
        "screen_capture_enabled": False,
        "mobile_screen_capture_enabled": False,
        "smart_ring_touch_enabled": False,
        "smart_ring_name_prefix": "AIZO",
        "smart_ring_keep_connected": False,
        "smart_ring_quiet_hours_enabled": False,
        "smart_ring_quiet_hours_start": "00:00",
        "smart_ring_quiet_hours_end": "08:00",
        "mock_devices_enabled": False,
    }

    changed = config._ensure_endpoints_and_slots(data)

    assert changed is False
    assert data["slots"]["relational_card_generation"] == {
        "endpoint": "custom",
        "model": "custom-card-model",
    }


def test_call_flash_lite_uses_memory_digest_slot(monkeypatch):
    calls = []

    async def fake_call_slot_chat(slot_name, *, messages, **kwargs):
        calls.append({"slot_name": slot_name, "messages": messages, "kwargs": kwargs})
        return '{"summary":"ok","keywords":["k"],"importance":0.4,"unresolved":false}'

    monkeypatch.setattr(memory, "call_slot_chat", fake_call_slot_chat)

    result = asyncio.run(memory._call_flash_lite("prompt text", scope="memory:test_digest"))

    assert result["summary"] == "ok"
    assert calls[0]["slot_name"] == "memory_digest"
    assert calls[0]["messages"] == [{"role": "user", "content": "prompt text"}]
    assert calls[0]["kwargs"]["scope"] == "memory:test_digest"
    assert calls[0]["kwargs"]["timeout"] == 60.0
