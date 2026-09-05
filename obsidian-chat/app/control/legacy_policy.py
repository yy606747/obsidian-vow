from __future__ import annotations

import os
from typing import Any


def control_legacy_toy_fallback_enabled() -> bool:
    return _flag("control_legacy_toy_fallback_enabled", "OBSIDIAN_CONTROL_LEGACY_TOY_FALLBACK")


def sentinel_legacy_toy_fallback_enabled() -> bool:
    return _flag("sentinel_legacy_toy_fallback_enabled", "OBSIDIAN_SENTINEL_LEGACY_TOY_FALLBACK")


def _flag(name: str, env_name: str) -> bool:
    env_value = os.environ.get(env_name)
    if env_value is not None:
        return _as_bool(env_value)
    values: list[Any] = []
    try:
        from config import SETTINGS

        values.append(SETTINGS.get(name))
    except Exception:
        pass
    try:
        from config import load_ai_behavior

        values.append(load_ai_behavior().get(name))
    except Exception:
        pass
    for value in values:
        if value is not None:
            return _as_bool(value)
    return False


def _as_bool(value: Any) -> bool:
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on", "enabled"}
    return bool(value)
