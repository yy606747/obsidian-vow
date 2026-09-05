"""Shared worldbook persona prompt construction."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any


_LEGACY_USER_PLACEHOLDERS = frozenset({"用户", "你", "user"})
_LEGACY_AI_PLACEHOLDERS = frozenset({"ai", "assistant"})


def resolve_worldbook_names(worldbook: Mapping[str, Any]) -> tuple[str, str]:
    """Return the configured relationship names without product-role fallbacks."""

    user_name = str(worldbook.get("user_name") or "").strip()
    ai_name = str(worldbook.get("ai_name") or "").strip()
    if not user_name or user_name.casefold() in _LEGACY_USER_PLACEHOLDERS:
        user_name = "她"
    if not ai_name or ai_name.casefold() in _LEGACY_AI_PLACEHOLDERS:
        ai_name = "我"
    return user_name, ai_name


def load_worldbook_names() -> tuple[str, str]:
    from config import load_worldbook

    return resolve_worldbook_names(load_worldbook())


def build_worldbook_prefix(worldbook: Mapping[str, Any]) -> list[dict[str, str]]:
    user_name, ai_name = resolve_worldbook_names(worldbook)
    prefix: list[dict[str, str]] = []
    if worldbook.get("ai_persona"):
        prefix.append({
            "role": "user",
            "content": f"[关于你自己：{ai_name}]\n{worldbook['ai_persona']}",
        })
        prefix.append({"role": "assistant", "content": f"（嗯，我知道自己是{ai_name}。）"})
    if worldbook.get("user_persona"):
        prefix.append({
            "role": "user",
            "content": f"[关于{user_name}]\n{worldbook['user_persona']}",
        })
        prefix.append({"role": "assistant", "content": f"（嗯，这是我所了解的{user_name}。）"})
    return prefix


__all__ = ["build_worldbook_prefix", "load_worldbook_names", "resolve_worldbook_names"]
