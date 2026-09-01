"""Pure configuration defaults for Sentinel Attention snapshots."""

from __future__ import annotations

from collections.abc import Mapping
from types import MappingProxyType
from typing import Any


DEFAULT_ATTENTION_CONFIG = MappingProxyType({
    "chat_cooldown_sec": 600,
    "wake_cooldown_sec": 1500,
    "high_score_wake_cooldown_sec": 600,
    "low_confidence_threshold": 0.4,
    "tone_hint_max_chars": 120,
    "next_check_min_sec": 300,
    "next_check_max_sec": 1800,
    "compact_text_max_chars": 600,
    "enable_pc_activity": False,
    "enable_camera_evidence": False,
})

_INTEGER_CONFIG_FIELDS = frozenset({
    "chat_cooldown_sec",
    "wake_cooldown_sec",
    "high_score_wake_cooldown_sec",
    "tone_hint_max_chars",
    "next_check_min_sec",
    "next_check_max_sec",
    "compact_text_max_chars",
})
_FLOAT_CONFIG_FIELDS = frozenset({
    "low_confidence_threshold",
})
_BOOLEAN_CONFIG_FIELDS = frozenset({
    "enable_pc_activity",
    "enable_camera_evidence",
})


def resolve_attention_config(overrides: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """Return default Attention config with validated explicit overrides."""
    if overrides is None:
        overrides = {}
    if not isinstance(overrides, Mapping):
        raise ValueError("attention_config must be an object")

    config = dict(DEFAULT_ATTENTION_CONFIG)
    for key, value in overrides.items():
        if key not in DEFAULT_ATTENTION_CONFIG:
            raise ValueError(f"attention_config unknown key {key!r}")
        if key in _INTEGER_CONFIG_FIELDS:
            config[key] = _required_positive_int(key, value)
        elif key in _FLOAT_CONFIG_FIELDS:
            config[key] = _required_unit_number(key, value)
        elif key in _BOOLEAN_CONFIG_FIELDS:
            config[key] = _required_bool(key, value)

    if config["next_check_min_sec"] > config["next_check_max_sec"]:
        raise ValueError("attention_config next_check_min_sec cannot exceed next_check_max_sec")
    return config


def _required_positive_int(key: str, value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"attention_config {key} must be an integer")
    if value <= 0:
        raise ValueError(f"attention_config {key} must be positive")
    return value


def _required_unit_number(key: str, value: Any) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise ValueError(f"attention_config {key} must be a number")
    number = float(value)
    if not 0.0 <= number <= 1.0:
        raise ValueError(f"attention_config {key} must be 0.0-1.0")
    return number


def _required_bool(key: str, value: Any) -> bool:
    if not isinstance(value, bool):
        raise ValueError(f"attention_config {key} must be a boolean")
    return value


__all__ = [
    "DEFAULT_ATTENTION_CONFIG",
    "resolve_attention_config",
]
