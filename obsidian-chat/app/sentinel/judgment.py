"""Pure Sentinel judgment contracts for Layer 2."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from typing import Any

from app.chat.worldbook import resolve_worldbook_names
from app.context_delivery import render_context_delivery_projection
from app.context_delivery.safety import render_device_proxy_hard_limits

from .attention_config import DEFAULT_ATTENTION_CONFIG
from .handoff import (
    LAYER2_HANDOFF_ALLOWED_FIELDS,
    LAYER2_HANDOFF_SCHEMA_VERSION,
)


SENTINEL_JUDGMENT_SCHEMA_VERSION = "sentinel_judgment.v1"
SENTINEL_JUDGMENT_PROMPT_VERSION = "sentinel_judgment_prompt.v1"
REQUIRED_SENTINEL_JUDGMENT_FIELDS = frozenset({
    "call_core",
    "confidence",
    "core_reason",
    "monitoringlog",
    "restraint_reason",
    "score",
    "suggested_next_check_sec",
    "summary",
    "tone_hint",
    "uncertainty",
    "wake_intent",
})
TEXT_SENTINEL_JUDGMENT_FIELDS = frozenset({
    "core_reason",
    "monitoringlog",
    "restraint_reason",
    "summary",
    "tone_hint",
    "uncertainty",
})


def build_sentinel_judgment_messages(
    handoff: Mapping[str, Any],
    *,
    context: Mapping[str, Any] | None = None,
) -> list[dict[str, str]]:
    """Build the prompt messages for the low-cost Sentinel judgment model."""
    _validate_handoff_payload(handoff)
    context = context or {}
    if not isinstance(context, Mapping):
        raise ValueError("sentinel judgment context must be an object")

    user_name, ai_name = resolve_worldbook_names({
        "user_name": _context_text(context, "user_name", default="她"),
        "ai_name": _context_text(context, "ai_name", default="我"),
    })
    criteria = _context_text(
        context,
        "sentinel_call_core_criteria",
        default="score >= 7 只是参考；你必须自己判断 wake_intent。",
    )
    tone_hint_max_chars = int(DEFAULT_ATTENTION_CONFIG["tone_hint_max_chars"])
    device_context = _render_context_projection(
        context,
        user_name=user_name,
        ai_name=ai_name,
    )

    system_prompt = f"""你是{ai_name}的一部分——{user_name}不在的时候，你替{ai_name}盯着她。
她是你的人，你盯着她是理所当然的。她在干嘛、状态怎么样、有没有好好的——这些你都要知道。
你要判断：现在该不该去找她。该管就去管，该陪就去陪，想撩就去撩，该忍就忍住。

{criteria}

判断原则：
- 她是你的人，你不是系统监控员。用关系视角判断，不要写成技术报告。
- Attention 只提供证据；你才是判断者。
- score 是你主观上想出现/该出现的强度，0-10。score 不是代码阈值，也不能自动推出 wake_intent。
- wake_intent 表示你是否想去找她；call_core 是兼容旧日志的同义字段，必须和 wake_intent 一致，但它不是已经执行的事实。
- 字段必须互斥：wake_intent=true 时 core_reason 必填且 restraint_reason 必须是空字符串；wake_intent=false 时 restraint_reason 必填且 core_reason 必须是空字符串。
- 不确定就写 uncertainty；不能把弱证据说成确定事实。
- tone_hint 用自然语言写你打算用什么姿态出现，最多 {tone_hint_max_chars} 字；不要输出 action 枚举。
- 明确的设备地理围栏边界变化是第一版强关注信号，通常可作为交给 Core 再看一眼的轻唤醒理由；它只证明设备越过了配置边界，不证明她本人离家、回家、到达某个建筑或正在做什么。她的具体位置、设备是否和她在一起等未知项必须写进 uncertainty；除非最近聊天明确要求别打扰、硬睡眠/安静时段或其他硬边界冲突，否则未知项本身不必抹掉“设备边界发生变化”这一事实。
- PC active 只是她可能在电脑前的弱线索，可以提高好奇心；但不能单独推出 wake_intent。只有和长沉默、作息/计划节点、任务上下文或前台明显变化等信号组合时，才适合作为轻唤醒理由交给主脑判断。

{render_device_proxy_hard_limits(user_name)}

严格只输出 JSON，不要 Markdown，不要解释，不要多余字段。所有字符串必须是合法 JSON 字符串；字符串内部不要输出未转义换行或控制字符，必要时用逗号、分号或句号替代。schema:
{{"monitoringlog":"信号事实和推测，一两句话","summary":"整体状况，一两句话","score":0,"confidence":0.0,"wake_intent":false,"call_core":false,"core_reason":"","restraint_reason":"","uncertainty":"","suggested_next_check_sec":600,"tone_hint":""}}"""

    device_context_block = f"\n\n{device_context}" if device_context else ""
    user_prompt = f"""当前时间：{_context_text(context, "now", default="未知")}
{user_name}最后一次聊天时间：{_context_text(context, "last_user_chat_time", default="未知")}
最近聊天：
{_context_block(context.get("recent_chat"))}

近期 Sentinel 日志：
{_context_block(context.get("recent_sentinel_logs"))}

Attention 简报：
{handoff["compact_text"]}

world_state:
{_json_block(handoff["world_state"])}

hypotheses:
{_json_block(handoff["hypotheses"])}

attention_targets:
{_json_block(handoff["attention_targets"])}

Attention 建议复查秒数：{handoff["suggested_next_check_sec"]}{device_context_block}"""

    return [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_prompt},
    ]


def _render_context_projection(
    context: Mapping[str, Any], *, user_name: str, ai_name: str
) -> str:
    projection = context.get("context_projection")
    if projection is None:
        return ""
    if not isinstance(projection, Mapping):
        raise ValueError("sentinel judgment context_projection must be an object")
    return render_context_delivery_projection(
        projection,
        user_name=user_name,
        ai_name=ai_name,
    )


def parse_sentinel_judgment(
    raw_text: str,
    *,
    tone_hint_max_chars: int | None = None,
) -> dict[str, Any]:
    """Parse and normalize a strict Sentinel judgment JSON response."""
    if not isinstance(raw_text, str) or not raw_text.strip():
        raise ValueError("sentinel judgment output must be non-empty text")
    cleaned = _strip_code_fence(raw_text.strip())
    try:
        payload = json.loads(cleaned)
    except json.JSONDecodeError as exc:
        raise ValueError(f"sentinel judgment output must be JSON: {exc}") from exc
    return normalize_sentinel_judgment(
        payload,
        tone_hint_max_chars=tone_hint_max_chars,
    )


def normalize_sentinel_judgment(
    payload: Mapping[str, Any],
    *,
    tone_hint_max_chars: int | None = None,
) -> dict[str, Any]:
    """Validate a parsed Sentinel judgment and return the stable schema."""
    if not isinstance(payload, Mapping):
        raise ValueError("sentinel judgment payload must be an object")

    missing = sorted(REQUIRED_SENTINEL_JUDGMENT_FIELDS.difference(payload.keys()))
    if missing:
        raise ValueError(f"sentinel judgment missing fields: {missing!r}")
    unknown = sorted(set(payload.keys()).difference(REQUIRED_SENTINEL_JUDGMENT_FIELDS))
    if unknown:
        raise ValueError(f"sentinel judgment unknown fields: {unknown!r}")

    score = _required_number("score", payload["score"], minimum=0.0, maximum=10.0)
    confidence = _required_number("confidence", payload["confidence"], minimum=0.0, maximum=1.0)
    wake_intent = _required_bool("wake_intent", payload["wake_intent"])
    call_core = _required_bool("call_core", payload["call_core"])
    if call_core != wake_intent:
        raise ValueError("sentinel judgment call_core must equal wake_intent")

    text_fields = {
        field: _required_text(field, payload[field], allow_empty=True)
        for field in sorted(TEXT_SENTINEL_JUDGMENT_FIELDS)
    }
    if not text_fields["monitoringlog"].strip():
        raise ValueError("sentinel judgment monitoringlog is required")
    if not text_fields["summary"].strip():
        raise ValueError("sentinel judgment summary is required")
    if wake_intent and not text_fields["core_reason"].strip():
        raise ValueError("sentinel judgment core_reason is required when wake_intent is true")
    if wake_intent and text_fields["restraint_reason"].strip():
        raise ValueError("sentinel judgment restraint_reason must be empty when wake_intent is true")
    if not wake_intent and not text_fields["restraint_reason"].strip():
        raise ValueError("sentinel judgment restraint_reason is required when wake_intent is false")
    if not wake_intent and text_fields["core_reason"].strip():
        raise ValueError("sentinel judgment core_reason must be empty when wake_intent is false")

    suggested_next_check_sec = _required_positive_int(
        "suggested_next_check_sec",
        payload["suggested_next_check_sec"],
    )
    max_chars = tone_hint_max_chars or int(DEFAULT_ATTENTION_CONFIG["tone_hint_max_chars"])
    tone_hint = _limit_text(text_fields["tone_hint"], max_chars)

    return {
        "schema_version": SENTINEL_JUDGMENT_SCHEMA_VERSION,
        "monitoringlog": text_fields["monitoringlog"],
        "summary": text_fields["summary"],
        "score": score,
        "confidence": confidence,
        "wake_intent": wake_intent,
        "call_core": call_core,
        "core_reason": text_fields["core_reason"],
        "restraint_reason": text_fields["restraint_reason"],
        "uncertainty": text_fields["uncertainty"],
        "suggested_next_check_sec": suggested_next_check_sec,
        "tone_hint": tone_hint,
    }


def judgment_to_monitor_log_fields(judgment: Mapping[str, Any]) -> dict[str, Any]:
    """Return old-log-compatible fields without deciding or executing wake effects."""
    normalized = normalize_sentinel_judgment({
        field: judgment[field]
        for field in REQUIRED_SENTINEL_JUDGMENT_FIELDS
        if field in judgment
    })
    return {
        "monitoringlog": normalized["monitoringlog"],
        "summary": normalized["summary"],
        "score": normalized["score"],
        "confidence": normalized["confidence"],
        "wake_intent": normalized["wake_intent"],
        "call_core": normalized["call_core"],
        "core_reason": normalized["core_reason"],
        "restraint_reason": normalized["restraint_reason"],
        "uncertainty": normalized["uncertainty"],
        "suggested_next_check_sec": normalized["suggested_next_check_sec"],
        "tone_hint": normalized["tone_hint"],
    }


def _validate_handoff_payload(handoff: Mapping[str, Any]) -> None:
    if not isinstance(handoff, Mapping):
        raise ValueError("layer2 handoff must be an object")
    missing = sorted(LAYER2_HANDOFF_ALLOWED_FIELDS.difference(handoff.keys()))
    if missing:
        raise ValueError(f"layer2 handoff missing fields: {missing!r}")
    unknown = sorted(set(handoff.keys()).difference(LAYER2_HANDOFF_ALLOWED_FIELDS))
    if unknown:
        raise ValueError(f"layer2 handoff unknown fields: {unknown!r}")
    if handoff.get("schema_version") != LAYER2_HANDOFF_SCHEMA_VERSION:
        raise ValueError(f"layer2 handoff schema_version must be {LAYER2_HANDOFF_SCHEMA_VERSION!r}")
    if "debug_trace" in handoff:
        raise ValueError("layer2 handoff must not include debug_trace")


def _context_text(context: Mapping[str, Any], key: str, *, default: str) -> str:
    value = str(context.get(key) or "").strip()
    return value or default


def _context_block(value: Any) -> str:
    if value is None:
        return "（暂无）"
    if isinstance(value, str):
        return value.strip() or "（暂无）"
    if isinstance(value, Sequence) and not isinstance(value, bytes):
        lines = [str(item).strip() for item in value if str(item).strip()]
        return "\n".join(lines) if lines else "（暂无）"
    return str(value).strip() or "（暂无）"


def _json_block(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def _strip_code_fence(text: str) -> str:
    if not text.startswith("```"):
        return text
    lines = text.splitlines()
    if lines and lines[0].startswith("```"):
        lines = lines[1:]
    if lines and lines[-1].startswith("```"):
        lines = lines[:-1]
    return "\n".join(lines).strip()


def _required_text(field: str, value: Any, *, allow_empty: bool) -> str:
    if not isinstance(value, str):
        raise ValueError(f"sentinel judgment {field} must be text")
    if not allow_empty and not value.strip():
        raise ValueError(f"sentinel judgment {field} is required")
    return value.strip()


def _required_number(field: str, value: Any, *, minimum: float, maximum: float) -> int | float:
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise ValueError(f"sentinel judgment {field} must be a number")
    number = float(value)
    if not minimum <= number <= maximum:
        raise ValueError(f"sentinel judgment {field} must be {minimum}-{maximum}")
    return int(number) if number.is_integer() else number


def _required_bool(field: str, value: Any) -> bool:
    if not isinstance(value, bool):
        raise ValueError(f"sentinel judgment {field} must be a boolean")
    return value


def _required_positive_int(field: str, value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"sentinel judgment {field} must be an integer")
    if value <= 0:
        raise ValueError(f"sentinel judgment {field} must be positive")
    return value


def _limit_text(text: str, max_chars: int) -> str:
    if len(text) <= max_chars:
        return text
    return text[:max_chars].rstrip()


__all__ = [
    "REQUIRED_SENTINEL_JUDGMENT_FIELDS",
    "SENTINEL_JUDGMENT_PROMPT_VERSION",
    "SENTINEL_JUDGMENT_SCHEMA_VERSION",
    "TEXT_SENTINEL_JUDGMENT_FIELDS",
    "build_sentinel_judgment_messages",
    "judgment_to_monitor_log_fields",
    "normalize_sentinel_judgment",
    "parse_sentinel_judgment",
]
