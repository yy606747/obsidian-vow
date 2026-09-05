"""Post-generation cleanup and side-effect planning for chat replies."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Awaitable, Callable, Collection

from camera import CAM_CHECK_CMD
from schedule import (
    ALARM_CMD,
    MONITOR_CMD,
    REMINDER_CMD,
    SCHEDULE_DEL_CMD,
    SCHEDULE_LIST_CMD,
)

from app.tools.parser import parse_structured_tool_intents, parse_tool_intents
from app.tools.ledger import ToolInvocationLedger, tool_invocation_ledger
from app.tools.schemas import ToolContext, ToolIntent, ToolResult
from app.tools.service import tool_service
from app.vows.service import VowExtract, extract_vow_marker, find_vow_markers, strip_vow_markers
from app.memory_v3.recall_intent import extract_recall_intent
from app.web_search.intent import extract_web_search_intent
from app.working_model.request_tag import (
    WorkingModelRequestCandidate,
    extract_working_model_request,
)

from .control_syntax import canonicalize_control_markers, strip_control_markers
from .commands import (
    ACTIVITY_CHECK_PATTERN,
    HEART_CMD_PATTERN,
    MUSIC_CMD_PATTERN,
    POI_SEARCH_PATTERN,
    REMEMBER_CMD_PATTERN,
    PRESENCE_DRAW_PATTERN,
    PRESENCE_SHOW_PATTERN,
    SELF_WAKE_CANCEL_PATTERN,
    SELF_WAKE_PATTERN,
    RING_TOUCH_PATTERN,
    SCREEN_CHECK_PATTERN,
    MOBILE_SCREEN_CHECK_PATTERN,
    TOY_CMD_PATTERN,
    UPDATE_MODEL_CMD_PATTERN,
    UNFINISHED_UPDATE_MODEL_CMD_PATTERN,
    WORKING_MODEL_REQUEST_OPEN,
    WORKING_MODEL_REQUEST_PATTERN,
    _strip_eval_side_effect_commands,
    _strip_retry,
)


ALL_COMMANDS = frozenset({
    "music",
    "toy",
    "cam",
    "activity",
    "screen",
    "mobile_screen",
    "poi",
    "schedule",
    "heart",
    "remember",
    "view_image",
    "ring",
    "presence_draw",
    "presence_show",
    "self_wake",
})


def strip_retry_marker(text: str) -> str:
    return _strip_retry(text)


_PRIVATE_BLOCK_PATTERNS = (
    re.compile(r"<meta\b[^>]*>.*?</meta>", re.IGNORECASE | re.DOTALL),
    re.compile(r"<(think|thinking|thought|analysis|reasoning)\b[^>]*>.*?</\1>", re.IGNORECASE | re.DOTALL),
    re.compile(r"```(?:think|thinking|thought|analysis|reasoning)\b[\s\S]*?```", re.IGNORECASE),
)

_UNFINISHED_PRIVATE_BLOCK_PATTERN = re.compile(
    r"<(?:meta|think|thinking|thought|analysis|reasoning)\b[^>]*>[\s\S]*$",
    re.IGNORECASE,
)
TIDE_INTENT_PATTERN = re.compile(r"\[TIDE_INTENT:([\s\S]*?)\[/TIDE_INTENT\]", re.IGNORECASE)
UNFINISHED_TIDE_INTENT_PATTERN = re.compile(r"\[TIDE_INTENT:[\s\S]*$", re.IGNORECASE)


def strip_meta_tags(text: str) -> str:
    cleaned = str(text or "")
    for pattern in _PRIVATE_BLOCK_PATTERNS:
        cleaned = pattern.sub("", cleaned)
    candidate = _UNFINISHED_PRIVATE_BLOCK_PATTERN.sub("", cleaned)
    if candidate.strip():
        cleaned = candidate
    return cleaned.strip()


_VOW_MASK_TEMPLATE = "\x00VOWMASK{}\x00"


def _private_state_marker_spans(text: str) -> list[tuple[int, int]]:
    """Locate blocks whose payload must be inert to the VOW parser."""

    spans = [
        *(match.span() for match in WORKING_MODEL_REQUEST_PATTERN.finditer(text)),
        *(match.span() for match in UPDATE_MODEL_CMD_PATTERN.finditer(text)),
    ]
    for marker in (WORKING_MODEL_REQUEST_OPEN, "[UPDATE_MODEL:"):
        start = 0
        while True:
            index = text.find(marker, start)
            if index < 0:
                break
            if not any(left <= index < right for left, right in spans):
                # An unmatched private marker owns the remaining tail.
                spans.append((index, len(text)))
            start = index + len(marker)
    return sorted(spans)


def _shield_vows_inside_private_state(text: str) -> str:
    """Keep quoted VOW syntax as data while preserving the request payload."""

    spans = _private_state_marker_spans(text)
    if not spans or "[VOW:" not in text:
        return text
    chars = list(text)
    start = 0
    while True:
        index = text.find("[VOW:", start)
        if index < 0:
            break
        if any(left <= index < right for left, right in spans):
            # Same length, so all precomputed spans remain valid. The request
            # parser still sees valid JSON, but the vow parser sees plain data.
            chars[index:index + 5] = list("[V0W:")
        start = index + 5
    return "".join(chars)


def _marker_is_nested_in_vow(text: str, marker_index: int) -> bool:
    if marker_index < 0:
        return False
    spans, unclosed_start = find_vow_markers(text)
    if any(left <= marker_index < right for left, right, _payload in spans):
        return True
    return unclosed_start is not None and marker_index >= unclosed_start


def _strip_meta_tags_outside_vow(text: str) -> str:
    """剥私有块时给完整 [VOW:...] 标记套占位壳（§4.1 惰性文本）。

    标记内部的 `<meta` 必须原样活到净化器手里被黑名单拒绝，不能在这里
    被洗白后变成合法候选；整体躺在私有块里的标记则随块一起消亡
    （占位符同块被删，恢复不回来），不构成候选。
    """
    raw = str(text or "")
    markers, _unclosed = find_vow_markers(raw)
    if not markers:
        return strip_meta_tags(raw)
    parts: list[str] = []
    slots: list[str] = []
    pos = 0
    for start, end, _inner in markers:
        parts.append(raw[pos:start])
        parts.append(_VOW_MASK_TEMPLATE.format(len(slots)))
        slots.append(raw[start:end])
        pos = end
    parts.append(raw[pos:])
    cleaned = strip_meta_tags("".join(parts))
    for index, original in enumerate(slots):
        cleaned = cleaned.replace(_VOW_MASK_TEMPLATE.format(index), original)
    return cleaned


def _drop_vow_carrying_actions(actions: list[dict]) -> list[dict]:
    """Private state markers inside actions are inert and drop the action."""
    kept: list[dict] = []
    for action in actions:
        try:
            serialized = json.dumps(action, ensure_ascii=False)
        except (TypeError, ValueError):
            serialized = repr(action)
        if any(
            marker in serialized
            for marker in (
                "[VOW:",
                "[UPDATE_MODEL:",
                "[WORKING_MODEL_REQUEST]",
                "[/WORKING_MODEL_REQUEST]",
            )
        ):
            continue
        kept.append(action)
    return kept


def extract_tide_intent(text: str) -> tuple[str, str]:
    raw = str(text or "")
    matches = [item.strip() for item in TIDE_INTENT_PATTERN.findall(raw) if item.strip()]
    cleaned = TIDE_INTENT_PATTERN.sub("", raw)
    cleaned = UNFINISHED_TIDE_INTENT_PATTERN.sub("", cleaned).strip()
    return cleaned, (matches[-1] if matches else "")


def _normalize_activity_window(raw: str) -> int:
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return 6
    return max(1, min(12, value)) if value > 0 else 6


def _strip_schedule_commands(text: str) -> str:
    for pattern in (ALARM_CMD, REMINDER_CMD, MONITOR_CMD, SCHEDULE_DEL_CMD, SCHEDULE_LIST_CMD):
        text = pattern.sub("", text)
    return text.strip()


def _maybe_json_object(text: str) -> dict | None:
    raw = str(text or "").strip()
    if raw.startswith("```"):
        lines = raw.splitlines()
        if lines and lines[0].strip().lower() in {"```", "```json", "```aion"} and lines[-1].strip() == "```":
            raw = "\n".join(lines[1:-1]).strip()
    if not raw.startswith("{"):
        return None
    try:
        value, _ = json.JSONDecoder().raw_decode(raw)
    except (json.JSONDecodeError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def looks_like_structured_reply(text: str) -> bool:
    raw = str(text or "").lstrip().lower()
    return raw.startswith("{") or raw.startswith("```json") or raw.startswith("```aion")


def _extract_structured_reply(text: str) -> tuple[str, list[dict], bool]:
    """返回 (内容, actions, ok)。ok=False 表示原文不是合法结构化回复
    （非 JSON / JSON 损坏 / 缺 assistant_text）——此时原文整段返回，
    调用方不得把其中的 [VOW:] 当作候选（§4.1：VOW 只从 assistant_text 提取）。"""
    payload = _maybe_json_object(text)
    if (
        not payload
        or not isinstance(payload.get("actions"), list)
        or "assistant_text" not in payload
    ):
        return text, [], False
    content = payload.get("assistant_text")
    actions = [item for item in payload["actions"] if isinstance(item, dict)]
    return str(content or ""), actions, True


_RING_TOUCH_CLAIM_PATTERNS = (
    re.compile(
        r"(?:我|给你|轻轻|悄悄|现在|这就|刚刚|已经|顺手|忍不住|隔空|用戒指)"
        r"[\s\S]{0,16}(?:触碰|轻碰|碰(?!巧)|轻敲|敲|点|戳|震|振)"
        r"[\s\S]{0,16}(?:你|你的戒指|戒指)"
    ),
    re.compile(
        r"(?:戒指)[\s\S]{0,16}(?:震|振|轻敲|敲|点|戳|触碰|碰(?!巧))"
        r"[\s\S]{0,16}(?:一下|[0-9一二两三四五六七八九十]+下)?"
    ),
)
_RING_TOUCH_NEGATED = re.compile(r"(?:不|没|没有|别|不要|不能|无法|不会)[\s\S]{0,8}(?:碰|触碰|敲|点|戳|震|振)")
_RING_ACTION_ALIASES = frozenset({"ring", "ring_touch", "device.ring_touch"})


def _ring_touch_claim_snippet(text: str) -> str:
    raw = str(text or "")
    for pattern in _RING_TOUCH_CLAIM_PATTERNS:
        match = pattern.search(raw)
        if not match:
            continue
        snippet = match.group(0).strip(" \t\r\n，。！？,.!?")
        if not snippet or "碰巧" in snippet:
            continue
        if _RING_TOUCH_NEGATED.search(snippet):
            continue
        return snippet[:120]
    return ""


def _ring_marker_descriptions(text: str, *, enabled: set[str]) -> tuple[str, list[str]]:
    if "ring" not in enabled:
        return text, []
    descriptions = [
        item.strip()[:120]
        for item in RING_TOUCH_PATTERN.findall(text)
        if item.strip()
    ]
    if not descriptions:
        return text, []
    return RING_TOUCH_PATTERN.sub("", text).strip(), descriptions[:1]


def _is_structured_ring_action(action: dict) -> bool:
    raw = action.get("tool_name") or action.get("tool") or action.get("type") or action.get("action")
    return str(raw or "").strip().lower() in _RING_ACTION_ALIASES


def _extract_ring_touch_text(action: dict) -> str:
    args = action.get("arguments") if isinstance(action.get("arguments"), dict) else {}
    touch = (
        args.get("touch")
        or action.get("touch")
        or action.get("text")
        or action.get("value")
        or ""
    )
    return " ".join(str(touch or "").split())[:120]


def _intercept_structured_ring_actions(
    actions: list[dict],
    *,
    enabled: set[str],
    existing_descriptions: list[str],
) -> tuple[list[dict], list[str]]:
    if "ring" not in enabled:
        return actions, existing_descriptions
    remaining: list[dict] = []
    descriptions = list(existing_descriptions[:1])
    for action in actions:
        if not _is_structured_ring_action(action):
            remaining.append(action)
            continue
        if descriptions:
            continue
        touch = _extract_ring_touch_text(action)
        if touch:
            descriptions = [touch]
    return remaining, descriptions


def _infer_ring_touch_descriptions(text: str, *, enabled: set[str], existing: list[str]) -> list[str]:
    if "ring" not in enabled or existing:
        return []
    snippet = _ring_touch_claim_snippet(text)
    if not snippet:
        return []
    return [snippet]


def _commands_from_intents(intents: list[ToolIntent], tool_name: str, argument: str) -> list[str]:
    values: list[str] = []
    for intent in intents:
        if intent.tool_name != tool_name:
            continue
        value = str(intent.arguments.get(argument) or "").strip()
        if value:
            values.append(value)
    return values


@dataclass
class PostProcessResult:
    content: str
    tool_intents: list[ToolIntent] = field(default_factory=list)
    tool_results: list[ToolResult] = field(default_factory=list)
    music_cards: list[dict] = field(default_factory=list)
    toy_commands: list[str] = field(default_factory=list)
    cam_triggered: bool = False
    activity_n: int = 0
    screen_check_reasons: list[str] = field(default_factory=list)
    poi_categories: list[str] = field(default_factory=list)
    heart_whispers: list[str] = field(default_factory=list)
    remember_notes: list[str] = field(default_factory=list)
    working_model_request: WorkingModelRequestCandidate | None = None
    working_model_request_reject_reason: str | None = None
    # CP2 compatibility surface only. Old UPDATE_MODEL markers are stripped
    # but never returned for execution.
    working_model_update: str = ""
    ring_touch_descriptions: list[str] = field(default_factory=list)
    tide_intent: str = ""
    recall_intent: str = ""
    web_search_intent: str = ""
    vow: VowExtract = field(default_factory=lambda: VowExtract(found=False))

    @property
    def music_attachments(self) -> list[dict]:
        return [
            {
                "type": "music",
                "name": song["name"],
                "artist": song["artist"],
                "id": song["id"],
            }
            for song in self.music_cards
        ]

    @property
    def tool_intent_payloads(self) -> list[dict]:
        return [intent.to_dict() for intent in self.tool_intents]

    @property
    def tool_result_payloads(self) -> list[dict]:
        return [result.to_dict() for result in self.tool_results]


class PostProcessor:
    def __init__(
        self,
        *,
        schedule_processor: Callable[[str, str | None], Awaitable[str]] | None = None,
        ledger: ToolInvocationLedger | None = None,
    ):
        self.schedule_processor = schedule_processor
        self.ledger = ledger if ledger is not None else tool_invocation_ledger

    async def process(
        self,
        full_text: str,
        *,
        conv_id: str,
        memory_eval_mode: bool = False,
        enabled_commands: Collection[str] | None = None,
        tool_context: ToolContext | None = None,
    ) -> PostProcessResult:
        raw_for_candidates = canonicalize_control_markers(
            strip_retry_marker(full_text)
        )
        text = raw_for_candidates
        # 剥私有块时保护 VOW 标记内部：<meta 不被洗白，私有块里的标记随块消亡
        text = _strip_meta_tags_outside_vow(text)

        enabled = ALL_COMMANDS if enabled_commands is None else set(enabled_commands)
        # §4.1 硬约束顺序：先解析 JSON 外壳（不执行 actions）→ 隔离两个
        # 私有状态通道 → 之后才轮到工具解析。VOW / working-model 只从合法
        # assistant_text 提取，永不从 actions 参数中提取；两种标记互为惰性
        # 数据，不能借一条私有通道触发另一条。
        structured_like = looks_like_structured_reply(text)
        text, structured_actions, structured_ok = _extract_structured_reply(text)
        candidate_text = text if structured_ok else ("" if structured_like else raw_for_candidates)
        structured_actions = _drop_vow_carrying_actions(structured_actions)
        working_model_candidate = None
        working_model_reject_reason = None
        if not structured_ok and looks_like_structured_reply(text):
            # 异常结构化外壳（JSON 损坏 / actions-only / 缺 assistant_text）：
            # 没有合法的 assistant_text 来源 → 只剥除，绝不从中提取状态申请。
            text = strip_vow_markers(text)
            vow_extract = VowExtract(found=False)
            text, _discarded = extract_working_model_request(text)
        else:
            assistant_text = text
            _, working_model_extract = extract_working_model_request(
                assistant_text
            )
            working_model_candidate = working_model_extract.candidate
            working_model_reject_reason = working_model_extract.reject_reason
            request_open_index = assistant_text.find(WORKING_MODEL_REQUEST_OPEN)
            if (
                working_model_candidate is not None
                and _marker_is_nested_in_vow(assistant_text, request_open_index)
            ):
                working_model_candidate = None
                working_model_reject_reason = "nested_in_vow"

            # Quote-like VOW syntax inside request/source data is shielded from
            # the VOW parser. The original request was parsed above, so its
            # source text remains byte-for-byte semantically intact.
            text, vow_extract = extract_vow_marker(
                _shield_vows_inside_private_state(assistant_text)
            )
            text, _discarded = extract_working_model_request(text)

        # The legacy full-document marker is hidden but never executed. It was
        # included in the VOW shield above, so VOW syntax inside its payload is
        # also inert while UPDATE_MODEL nested inside a VOW remains rejectable.
        text = UPDATE_MODEL_CMD_PATTERN.sub("", text).strip()
        text = UNFINISHED_UPDATE_MODEL_CMD_PATTERN.sub("", text).strip()
        text, tide_intent = extract_tide_intent(text)
        text, recall_intent = extract_recall_intent(text)
        text, web_search_intent = extract_web_search_intent(text)
        text, ring_touch_descriptions = _ring_marker_descriptions(text, enabled=enabled)
        structured_actions, ring_touch_descriptions = _intercept_structured_ring_actions(
            structured_actions,
            enabled=enabled,
            existing_descriptions=ring_touch_descriptions,
        )
        structured_intents = parse_structured_tool_intents(structured_actions, enabled_commands=enabled)
        legacy_intents = parse_tool_intents(text, enabled_commands=enabled, id_offset=len(structured_intents))
        tool_intents = [*structured_intents, *legacy_intents]
        ring_touch_descriptions.extend(
            _infer_ring_touch_descriptions(text, enabled=enabled, existing=ring_touch_descriptions)
        )
        tool_plan = tool_service.plan(
            tool_intents,
            context=(
                tool_context
                if tool_context is not None
                else ToolContext(
                    conv_id=conv_id,
                    mode="normal",
                    memory_eval_mode=memory_eval_mode,
                )
            ),
        )
        tool_results = list(tool_plan.results)
        await self.ledger.record_postprocess(
            tool_plan.context,
            raw_output=candidate_text,
            intents=tool_intents,
            plan_results=tool_results,
            ring_touch_descriptions=ring_touch_descriptions,
            enabled_commands=enabled,
        )

        if memory_eval_mode:
            # eval：VOW 照常剥除（上面已移除），禁止写入——不携带候选（§4.1）。
            return PostProcessResult(
                content=strip_meta_tags(
                    strip_control_markers(_strip_eval_side_effect_commands(text))
                ),
                tool_intents=tool_intents,
                tool_results=tool_results,
                ring_touch_descriptions=[],
                tide_intent=tide_intent,
                recall_intent="",
                web_search_intent="",
            )

        if "music" in enabled and MUSIC_CMD_PATTERN.search(text):
            text = MUSIC_CMD_PATTERN.sub("", text).strip()

        toy_commands = _commands_from_intents(structured_intents, "device.toy", "command")
        legacy_toy_commands = TOY_CMD_PATTERN.findall(text) if "toy" in enabled else []
        toy_commands.extend(legacy_toy_commands)
        if "toy" in enabled and legacy_toy_commands:
            text = TOY_CMD_PATTERN.sub("", text).strip()

        cam_triggered = "cam" in enabled and CAM_CHECK_CMD in text
        if cam_triggered:
            text = text.replace(CAM_CHECK_CMD, "").strip()

        activity_n = 0
        activity_match = ACTIVITY_CHECK_PATTERN.search(text) if "activity" in enabled else None
        if activity_match:
            activity_n = _normalize_activity_window(activity_match.group(1))
            text = ACTIVITY_CHECK_PATTERN.sub("", text).strip()

        screen_check_reasons = _commands_from_intents(structured_intents, "pc.screen_check", "reason")
        screen_check_reasons.extend([
            item.strip()
            for item in (SCREEN_CHECK_PATTERN.findall(text) if "screen" in enabled else [])
            if item.strip()
        ])
        if screen_check_reasons:
            text = SCREEN_CHECK_PATTERN.sub("", text).strip()

        # 移动端截图意图由 parse_tool_intents 经 tool_intents 携带；这里只需把
        # 可见文本里的标记清掉，避免 [MOBILE_SCREEN_CHECK:...] 出现在回复里。
        if "mobile_screen" in enabled and MOBILE_SCREEN_CHECK_PATTERN.search(text):
            text = MOBILE_SCREEN_CHECK_PATTERN.sub("", text).strip()

        poi_categories = POI_SEARCH_PATTERN.findall(text) if "poi" in enabled else []
        if poi_categories:
            text = POI_SEARCH_PATTERN.sub("", text).strip()

        if "schedule" in enabled:
            text = _strip_schedule_commands(text)

        heart_whispers = _commands_from_intents(structured_intents, "heart.whisper", "content")
        heart_whispers.extend([
            item.strip()
            for item in (HEART_CMD_PATTERN.findall(text) if "heart" in enabled else [])
            if item.strip()
        ])
        if heart_whispers:
            text = HEART_CMD_PATTERN.sub("", text).strip()

        remember_notes = _commands_from_intents(structured_intents, "memory.remember", "content")
        remember_notes.extend([
            item.strip()
            for item in (REMEMBER_CMD_PATTERN.findall(text) if "remember" in enabled else [])
            if item.strip()
        ])
        if remember_notes:
            text = REMEMBER_CMD_PATTERN.sub("", text).strip()

        if "presence_draw" in enabled and PRESENCE_DRAW_PATTERN.search(text):
            text = PRESENCE_DRAW_PATTERN.sub("", text).strip()
        if "presence_show" in enabled and PRESENCE_SHOW_PATTERN.search(text):
            text = PRESENCE_SHOW_PATTERN.sub("", text).strip()
        if "self_wake" in enabled and SELF_WAKE_PATTERN.search(text):
            text = SELF_WAKE_PATTERN.sub("", text).strip()
        if "self_wake" in enabled and SELF_WAKE_CANCEL_PATTERN.search(text):
            text = SELF_WAKE_CANCEL_PATTERN.sub("", text).strip()

        return PostProcessResult(
            # Parsing remains profile-scoped, but no recognized private/tool
            # marker is ever user-visible merely because that profile disabled
            # its parser group.  Reuse the existing central sanitizer instead
            # of maintaining source-specific deny lists.
            content=strip_meta_tags(
                strip_control_markers(_strip_eval_side_effect_commands(text))
            ),
            tool_intents=tool_intents,
            tool_results=tool_results,
            toy_commands=toy_commands,
            cam_triggered=cam_triggered,
            activity_n=activity_n,
            screen_check_reasons=screen_check_reasons,
            poi_categories=poi_categories,
            heart_whispers=heart_whispers,
            remember_notes=remember_notes,
            working_model_request=working_model_candidate,
            working_model_request_reject_reason=working_model_reject_reason,
            ring_touch_descriptions=ring_touch_descriptions[:1],
            tide_intent=tide_intent,
            recall_intent=recall_intent,
            web_search_intent=web_search_intent,
            vow=vow_extract,
        )
