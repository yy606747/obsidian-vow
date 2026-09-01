import pytest

from app.sentinel import DEFAULT_ATTENTION_CONFIG, build_attention_snapshot
from app.sentinel.attention_config import resolve_attention_config


def test_attention_config_uses_fixed_defaults():
    config = resolve_attention_config()

    assert config == dict(DEFAULT_ATTENTION_CONFIG)
    assert config["next_check_min_sec"] == 300
    assert config["next_check_max_sec"] == 1800
    assert config["compact_text_max_chars"] == 600
    assert config["enable_pc_activity"] is False
    assert config["enable_camera_evidence"] is False


def test_attention_config_accepts_explicit_overrides():
    config = resolve_attention_config({
        "next_check_min_sec": 600,
        "next_check_max_sec": 900,
        "compact_text_max_chars": 80,
        "enable_pc_activity": True,
    })

    assert config["next_check_min_sec"] == 600
    assert config["next_check_max_sec"] == 900
    assert config["compact_text_max_chars"] == 80
    assert config["enable_pc_activity"] is True
    assert config["enable_camera_evidence"] is False


def test_attention_config_rejects_unknown_or_invalid_values():
    with pytest.raises(ValueError, match="unknown key 'new_rule'"):
        resolve_attention_config({"new_rule": 1})

    with pytest.raises(ValueError, match="next_check_min_sec must be an integer"):
        resolve_attention_config({"next_check_min_sec": True})

    with pytest.raises(ValueError, match="low_confidence_threshold must be 0.0-1.0"):
        resolve_attention_config({"low_confidence_threshold": 1.5})

    with pytest.raises(ValueError, match="cannot exceed"):
        resolve_attention_config({
            "next_check_min_sec": 1200,
            "next_check_max_sec": 600,
        })


def test_attention_builder_applies_config_without_changing_default_contract():
    snapshot = build_attention_snapshot({
        "reference_time": "2026-05-14T19:10:00+08:00",
        "attention_config": {
            "next_check_min_sec": 600,
            "next_check_max_sec": 900,
            "compact_text_max_chars": 42,
        },
        "raw_signals": [
            {"kind": "location.fix", "source": "android.location", "text": "状态从 at_home 变为 outside，距离家约820米"},
            {"kind": "location.fix", "source": "android.location", "text": "GPS 精度 24m"},
        ],
        "recent_chat": [],
    })

    assert snapshot["suggested_next_check_sec"] == 600
    assert len(snapshot["compact_text"]) <= 42
    assert snapshot["compact_text"].endswith("...")
    assert snapshot["debug_trace"]["raw_signal_count"] == 2
