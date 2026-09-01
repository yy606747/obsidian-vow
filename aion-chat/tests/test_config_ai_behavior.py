import json

import config


def test_default_ai_behavior_enables_sentinel_v2_full_primary():
    assert config.DEFAULT_AI_BEHAVIOR["sentinel_v2_provider_enabled"] is True
    assert config.DEFAULT_AI_BEHAVIOR["sentinel_v2_provider_shadow_enabled"] is True
    assert config.DEFAULT_AI_BEHAVIOR["sentinel_v2_full_wake_enabled"] is True
    assert config.DEFAULT_AI_BEHAVIOR["sentinel_v2_full_wake_legacy_fallback_enabled"] is False
    assert config.DEFAULT_AI_BEHAVIOR["control_legacy_toy_fallback_enabled"] is False
    assert config.DEFAULT_AI_BEHAVIOR["sentinel_legacy_toy_fallback_enabled"] is False
    assert config.DEFAULT_AI_BEHAVIOR["tool_result_feedback_enabled"] is True
    assert config.DEFAULT_AI_BEHAVIOR["tool_ledger_snapshot_max_bytes"] == 64 * 1024
    assert config.DEFAULT_AI_BEHAVIOR["tool_ledger_retention_days"] == 90
    assert config.DEFAULT_AI_BEHAVIOR["context_delivery_chat_enabled"] is True
    assert config.DEFAULT_AI_BEHAVIOR["context_delivery_autonomous_enabled"] is False
    assert config.DEFAULT_AI_BEHAVIOR["context_trigger_shadow_enabled"] is False


def test_load_ai_behavior_fills_missing_provider_fields_with_full_primary_defaults(monkeypatch, tmp_path):
    path = tmp_path / "ai_behavior.json"
    path.write_text(json.dumps({
        "sentinel_wake_threshold": 7,
    }), encoding="utf-8")
    monkeypatch.setattr(config, "AI_BEHAVIOR_PATH", path)

    loaded = config.load_ai_behavior()

    assert loaded["sentinel_v2_provider_enabled"] is True
    assert loaded["sentinel_v2_provider_shadow_enabled"] is True
    assert loaded["sentinel_v2_full_wake_enabled"] is True
    assert loaded["sentinel_v2_full_wake_legacy_fallback_enabled"] is False
    assert loaded["control_legacy_toy_fallback_enabled"] is False
    assert loaded["sentinel_legacy_toy_fallback_enabled"] is False
    assert loaded["context_delivery_chat_enabled"] is True
    assert loaded["context_delivery_autonomous_enabled"] is False
    assert loaded["context_trigger_shadow_enabled"] is False


def test_load_ai_behavior_migrates_legacy_provider_shadow_alias(monkeypatch, tmp_path):
    path = tmp_path / "ai_behavior.json"
    path.write_text(json.dumps({
        "sentinel_v2_provider_shadow_enabled": True,
    }), encoding="utf-8")
    monkeypatch.setattr(config, "AI_BEHAVIOR_PATH", path)

    loaded = config.load_ai_behavior()

    assert loaded["sentinel_v2_provider_enabled"] is True
    assert loaded["sentinel_v2_provider_shadow_enabled"] is True


def test_load_ai_behavior_uses_new_provider_field_when_alias_conflicts(monkeypatch, tmp_path):
    path = tmp_path / "ai_behavior.json"
    path.write_text(json.dumps({
        "sentinel_v2_provider_enabled": False,
        "sentinel_v2_provider_shadow_enabled": True,
    }), encoding="utf-8")
    monkeypatch.setattr(config, "AI_BEHAVIOR_PATH", path)

    loaded = config.load_ai_behavior()

    assert loaded["sentinel_v2_provider_enabled"] is False
    assert loaded["sentinel_v2_provider_shadow_enabled"] is False


def test_settings_load_and_save_strip_transient_whisper_state(monkeypatch, tmp_path):
    path = tmp_path / "settings.json"
    path.write_text(json.dumps({
        "whisper_active": True,
        "temperature": 0.6,
        "endpoints": [],
        "model_slots": {},
    }), encoding="utf-8")
    monkeypatch.setattr(config, "SETTINGS_PATH", path)

    loaded = config.load_settings()

    assert "whisper_active" not in loaded
    assert "whisper_active" not in json.loads(path.read_text(encoding="utf-8"))

    config.save_settings({"temperature": 0.7, "whisper_active": True})
    assert json.loads(path.read_text(encoding="utf-8")) == {"temperature": 0.7}
