import asyncio

import pytest
from fastapi import HTTPException

from config import DEFAULT_AI_BEHAVIOR
from routes import settings


def test_ai_behavior_route_persists_reflection_and_opportunity_intervals(monkeypatch):
    saved = []
    monkeypatch.setattr(
        settings,
        "load_ai_behavior",
        lambda: {
            "opportunity_enabled": False,
            "working_model_reflection_enabled": False,
            "opportunity_intervals_min": [21, 34, 55, 89],
        },
    )
    monkeypatch.setattr(settings, "save_ai_behavior", lambda value: saved.append(dict(value)))

    result = asyncio.run(
        settings.put_ai_behavior(
            settings.AIBehaviorUpdate(
                opportunity_enabled=True,
                working_model_reflection_enabled=True,
                opportunity_intervals_min=[13, 21, 34],
            )
        )
    )

    assert result == {"ok": True}
    assert saved == [
        {
            "opportunity_enabled": True,
            "working_model_reflection_enabled": True,
            "opportunity_intervals_min": [13, 21, 34],
        }
    ]


def test_ai_behavior_route_can_disable_tool_result_feedback(monkeypatch):
    saved = []
    monkeypatch.setattr(
        settings,
        "load_ai_behavior",
        lambda: {"tool_result_feedback_enabled": True},
    )
    monkeypatch.setattr(settings, "save_ai_behavior", lambda value: saved.append(dict(value)))

    result = asyncio.run(settings.put_ai_behavior(
        settings.AIBehaviorUpdate(tool_result_feedback_enabled=False)
    ))

    assert result == {"ok": True}
    assert saved == [{"tool_result_feedback_enabled": False}]


def test_ai_behavior_route_persists_web_search_switch(monkeypatch):
    saved = []
    monkeypatch.setattr(
        settings,
        "load_ai_behavior",
        lambda: {"web_search_enabled": False},
    )
    monkeypatch.setattr(settings, "save_ai_behavior", lambda value: saved.append(dict(value)))

    result = asyncio.run(settings.put_ai_behavior(
        settings.AIBehaviorUpdate(web_search_enabled=True)
    ))

    assert result == {"ok": True}
    assert saved == [{"web_search_enabled": True}]


def test_ai_behavior_route_can_disable_context_delivery_chat(monkeypatch):
    saved = []
    monkeypatch.setattr(
        settings,
        "load_ai_behavior",
        lambda: {"context_delivery_chat_enabled": True},
    )
    monkeypatch.setattr(settings, "save_ai_behavior", lambda value: saved.append(dict(value)))

    result = asyncio.run(settings.put_ai_behavior(
        settings.AIBehaviorUpdate(context_delivery_chat_enabled=False)
    ))

    assert result == {"ok": True}
    assert saved == [{"context_delivery_chat_enabled": False}]


def test_ai_behavior_route_can_enable_context_delivery_autonomous(monkeypatch):
    saved = []
    monkeypatch.setattr(
        settings,
        "load_ai_behavior",
        lambda: {"context_delivery_autonomous_enabled": False},
    )
    monkeypatch.setattr(
        settings,
        "save_ai_behavior",
        lambda value: saved.append(dict(value)),
    )

    result = asyncio.run(settings.put_ai_behavior(
        settings.AIBehaviorUpdate(context_delivery_autonomous_enabled=True)
    ))

    assert result == {"ok": True}
    assert saved == [{"context_delivery_autonomous_enabled": True}]


def test_ai_behavior_route_can_enable_context_trigger_shadow(monkeypatch):
    saved = []
    monkeypatch.setattr(
        settings,
        "load_ai_behavior",
        lambda: {"context_trigger_shadow_enabled": False},
    )
    monkeypatch.setattr(
        settings,
        "save_ai_behavior",
        lambda value: saved.append(dict(value)),
    )

    result = asyncio.run(settings.put_ai_behavior(
        settings.AIBehaviorUpdate(context_trigger_shadow_enabled=True)
    ))

    assert result == {"ok": True}
    assert saved == [{"context_trigger_shadow_enabled": True}]


def test_ai_behavior_route_persists_tool_ledger_retention_settings(monkeypatch):
    saved = []
    monkeypatch.setattr(settings, "load_ai_behavior", lambda: {})
    monkeypatch.setattr(
        settings,
        "save_ai_behavior",
        lambda value: saved.append(dict(value)),
    )

    result = asyncio.run(settings.put_ai_behavior(settings.AIBehaviorUpdate(
        tool_ledger_snapshot_max_bytes=128 * 1024,
        tool_ledger_retention_days=45,
    )))

    assert result == {"ok": True}
    assert saved == [{
        "tool_ledger_snapshot_max_bytes": 128 * 1024,
        "tool_ledger_retention_days": 45,
    }]


def test_presence_summon_and_night_round_defaults_are_off():
    assert DEFAULT_AI_BEHAVIOR["presence_summon_enabled"] is False
    assert DEFAULT_AI_BEHAVIOR["presence_long_duration_enabled"] is False
    assert DEFAULT_AI_BEHAVIOR["night_round_enabled"] is False
    assert DEFAULT_AI_BEHAVIOR["night_round_start"] == "02:00"
    assert DEFAULT_AI_BEHAVIOR["night_round_end"] == "05:00"


def test_ai_behavior_route_persists_presence_switches_and_night_window(monkeypatch):
    saved = []
    monkeypatch.setattr(
        settings,
        "load_ai_behavior",
        lambda: {
            "presence_summon_enabled": False,
            "presence_long_duration_enabled": False,
            "night_round_enabled": False,
            "night_round_start": "02:00",
            "night_round_end": "05:00",
        },
    )
    monkeypatch.setattr(settings, "save_ai_behavior", lambda value: saved.append(dict(value)))

    result = asyncio.run(settings.put_ai_behavior(settings.AIBehaviorUpdate(
        presence_summon_enabled=True,
        presence_long_duration_enabled=True,
        night_round_enabled=True,
        night_round_start="23:30",
        night_round_end="03:15",
    )))

    assert result == {"ok": True}
    assert saved == [{
        "presence_summon_enabled": True,
        "presence_long_duration_enabled": True,
        "night_round_enabled": True,
        "night_round_start": "23:30",
        "night_round_end": "03:15",
    }]


def test_ai_behavior_route_rejects_invalid_or_empty_night_window(monkeypatch):
    monkeypatch.setattr(
        settings,
        "load_ai_behavior",
        lambda: {"night_round_start": "02:00", "night_round_end": "05:00"},
    )
    monkeypatch.setattr(settings, "save_ai_behavior", lambda _value: None)

    with pytest.raises(HTTPException) as invalid:
        asyncio.run(settings.put_ai_behavior(
            settings.AIBehaviorUpdate(night_round_start="2am")
        ))
    assert invalid.value.status_code == 422

    with pytest.raises(HTTPException) as empty:
        asyncio.run(settings.put_ai_behavior(settings.AIBehaviorUpdate(
            night_round_start="05:00",
            night_round_end="05:00",
        )))
    assert empty.value.status_code == 422
