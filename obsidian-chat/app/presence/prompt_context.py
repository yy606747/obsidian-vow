"""Stable Core-facing context for the companion's chosen desktop forms."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .sprites import SpriteLibrary, sprite_library


async def presence_identity_head(
    *, library: SpriteLibrary = sprite_library
) -> dict[str, Any]:
    """Read only the durable fields needed by the stable identity block."""

    baseline = await library.human_baseline()
    count = await library.non_seed_count()
    return {
        "baseline": (
            {
                "created_at": float(baseline["created_at"]),
                "prompt": str(baseline.get("prompt") or "").strip(),
                "description": str(baseline.get("description") or "").strip(),
            }
            if baseline is not None
            else None
        ),
        "non_seed_count": max(0, int(count)),
        "timezone_name": str(
            getattr(library, "timezone_name", "America/Los_Angeles")
        ),
    }


def build_presence_identity_block(
    head: Mapping[str, Any],
    *,
    user_name: str,
    ai_name: str,
    time_formatter: Callable[[float, str], str] | None = None,
) -> str:
    """Render identity continuity without turning an empty library into work."""

    owner_name = str(user_name or "").strip()
    companion_name = str(ai_name or "").strip()
    if not owner_name:
        raise ValueError("user_name is required for presence identity context")
    if not companion_name:
        raise ValueError("ai_name is required for presence identity context")
    baseline = head.get("baseline")
    if not isinstance(baseline, Mapping):
        return (
            f"[关于{companion_name}的桌面形象]\n"
            f"{companion_name}还没有为自己留下形象。这个事实不构成任务，"
            f"也不需要向{owner_name}提出画一个。"
        )
    prompt = " ".join(str(baseline.get("prompt") or "").split())[:500]
    description = " ".join(
        str(baseline.get("description") or "").split()
    )[:2000]
    if not prompt or not description:
        raise ValueError("human baseline prompt and description are required")
    created_at = float(baseline.get("created_at"))
    timezone_name = str(head.get("timezone_name") or "America/Los_Angeles")
    format_time = time_formatter or _format_created_at
    other_count = max(0, int(head.get("non_seed_count") or 0) - 1)
    return (
        f"[关于{companion_name}曾选择的人形]\n"
        f"{format_time(created_at, timezone_name)}，{companion_name}第一次选择以人的样子出现。\n"
        f"外观基准：{prompt}\n"
        f"当时的自述：{description}\n"
        f"除此之外，{companion_name}还留下过 {other_count} 个其他形象。\n"
        f"这只是{companion_name}需要以人形出现时的连续性基准，不要求{companion_name}继续选择人形，"
        f"也不需要主动向{owner_name}谈论这段信息。"
    )


def _format_created_at(timestamp: float, timezone_name: str) -> str:
    try:
        zone = ZoneInfo(str(timezone_name or ""))
    except (ZoneInfoNotFoundError, ValueError):
        zone = ZoneInfo("America/Los_Angeles")
    return datetime.fromtimestamp(float(timestamp), zone).strftime("%Y-%m-%d %H:%M")


__all__ = ["build_presence_identity_block", "presence_identity_head"]
