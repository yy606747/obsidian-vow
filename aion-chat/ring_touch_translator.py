"""Translate natural-language smart-ring touch descriptions into haptics."""

from __future__ import annotations

import json
from typing import Any, Mapping

from ai_providers import call_slot_chat


DEFAULT_HAPTICS = {"taps": 1, "interval_ms": 2000}


def parse_ring_haptics(raw: str | Mapping[str, Any] | None) -> dict[str, int]:
    payload: Any = raw
    if isinstance(raw, str):
        text = raw.strip()
        if text.startswith("```"):
            lines = text.splitlines()
            if lines and lines[0].startswith("```"):
                lines = lines[1:]
            if lines and lines[-1].startswith("```"):
                lines = lines[:-1]
            text = "\n".join(lines).strip()
        try:
            payload, _ = json.JSONDecoder().raw_decode(text)
        except (json.JSONDecodeError, ValueError):
            return dict(DEFAULT_HAPTICS)
    if not isinstance(payload, Mapping):
        return dict(DEFAULT_HAPTICS)
    # Bounds are left to RingTouchGate so logs can keep the model's raw haptics.
    return {
        "taps": _to_int(payload.get("taps"), DEFAULT_HAPTICS["taps"]),
        "interval_ms": _to_int(payload.get("interval_ms"), DEFAULT_HAPTICS["interval_ms"]),
    }


async def translate_ring_touch(touch_text: str) -> dict[str, int]:
    """Translate a self-contained touch description into ring haptics."""
    text = " ".join(str(touch_text or "").split())[:120]
    if not text:
        return dict(DEFAULT_HAPTICS)
    messages = [
        {
            "role": "system",
            "content": (
                "你是一个触感翻译器。输入是自然语言的触碰描述，输出是振动参数 JSON。\n\n"
                "【次数是硬约束】只要输入明确出现“一下、两下、三下……”或对应数字，"
                "taps 必须严格等于该次数，绝不能因情绪、力度或动作类型改写次数。"
                "只有未写明次数时，才按语义自由选择。\n\n"
                "参数：\n"
                "- taps: 振动次数，整数，1-10\n"
                "- interval_ms: 每次振动之间的间隔（毫秒），整数，1000-5000\n"
                "  参考：约1000=急促/催促/紧张，约2000=从容/日常/陪伴，约3000以上=缓慢/犹豫/试探\n\n"
                "只输出 JSON，不解释。"
            ),
        },
        {"role": "user", "content": f"输入：{text}"},
    ]
    try:
        raw = await call_slot_chat(
            "ring_touch_translator",
            messages,
            expect_json=True,
            temperature=0.3,
            timeout=10.0,
            max_tokens=128,
        )
    except Exception:
        return dict(DEFAULT_HAPTICS)
    return parse_ring_haptics(raw)


def _to_int(value: Any, fallback: int) -> int:
    if isinstance(value, bool):
        return fallback
    try:
        return int(value)
    except (TypeError, ValueError):
        return fallback
