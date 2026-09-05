"""Legacy text command parser for ToolService migration.

The parser is read-only in the first Batch 4 step: it mirrors the old command
surface as ToolIntent objects while existing postprocess/streaming code keeps
executing the actual side effects.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, Callable, Collection, Iterable, Mapping

from app.chat.commands import (
    ACTIVITY_CHECK_PATTERN,
    HEART_CMD_PATTERN,
    MUSIC_CMD_PATTERN,
    POI_SEARCH_PATTERN,
    PRESENCE_DRAW_PATTERN,
    PRESENCE_SHOW_PATTERN,
    SELF_WAKE_CANCEL_TOKEN,
    SELF_WAKE_PATTERN,
    REMEMBER_CMD_PATTERN,
    VIEW_IMAGE_PATTERN,
    SCREEN_CHECK_PATTERN,
    MOBILE_SCREEN_CHECK_PATTERN,
    TOY_CMD_PATTERN,
)
from camera import CAM_CHECK_CMD
from schedule import (
    ALARM_CMD,
    MONITOR_CMD,
    REMINDER_CMD,
    SCHEDULE_DEL_CMD,
    SCHEDULE_LIST_CMD,
)

from .schemas import SideEffectLevel, ToolIntent, get_tool_definition


ALL_COMMAND_GROUPS = frozenset({
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

TOOL_COMMAND_GROUPS = {
    "music.search": "music",
    "device.toy": "toy",
    "monitor.camera": "cam",
    "activity.summary": "activity",
    "pc.screen_check": "screen",
    "mobile.screen_check": "mobile_screen",
    "location.poi_search": "poi",
    "schedule.alarm": "schedule",
    "schedule.reminder": "schedule",
    "schedule.monitor": "schedule",
    "schedule.delete": "schedule",
    "schedule.list": "schedule",
    "heart.whisper": "heart",
    "memory.remember": "remember",
    "memory.view_image": "view_image",
    "device.ring_touch": "ring",
    "desktop.presence.draw": "presence_draw",
    "desktop.presence.show": "presence_show",
    "self_wake.schedule": "self_wake",
    "self_wake.cancel": "self_wake",
}

STRUCTURED_ACTION_ALIASES = {
    "music": "music.search",
    "music.search": "music.search",
    "toy": "device.toy",
    "device.toy": "device.toy",
    "camera": "monitor.camera",
    "cam": "monitor.camera",
    "monitor.camera": "monitor.camera",
    "activity": "activity.summary",
    "activity.summary": "activity.summary",
    "screen": "pc.screen_check",
    "screen_check": "pc.screen_check",
    "pc.screen_check": "pc.screen_check",
    "mobile_screen": "mobile.screen_check",
    "mobile_screen_check": "mobile.screen_check",
    "mobile.screen_check": "mobile.screen_check",
    "poi": "location.poi_search",
    "location.poi_search": "location.poi_search",
    "alarm": "schedule.alarm",
    "schedule.alarm": "schedule.alarm",
    "reminder": "schedule.reminder",
    "schedule.reminder": "schedule.reminder",
    "monitor": "schedule.monitor",
    "schedule.monitor": "schedule.monitor",
    "schedule.delete": "schedule.delete",
    "schedule.list": "schedule.list",
    "heart": "heart.whisper",
    "heart.whisper": "heart.whisper",
    "remember": "memory.remember",
    "memory.remember": "memory.remember",
    "memory.view_image": "memory.view_image",
    "view_image": "memory.view_image",
    "ring": "device.ring_touch",
    "ring_touch": "device.ring_touch",
    "device.ring_touch": "device.ring_touch",
    "presence_draw": "desktop.presence.draw",
    "desktop.presence.draw": "desktop.presence.draw",
    "presence_show": "desktop.presence.show",
    "desktop.presence.show": "desktop.presence.show",
    "self_wake": "self_wake.schedule",
    "self_wake.schedule": "self_wake.schedule",
    "self_wake_cancel": "self_wake.cancel",
    "self_wake.cancel": "self_wake.cancel",
}


@dataclass(frozen=True)
class _ParsedCommand:
    start: int
    end: int
    tool_name: str
    command_group: str
    raw_text: str
    arguments: dict
    legacy_marker: str


def _normalize_activity_window(raw: str) -> int:
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return 6
    return max(1, min(12, value)) if value > 0 else 6


def _one_line(value: object) -> str:
    return " ".join(str(value or "").split())


_PRESENCE_DRAW_FORMS = {"人形": "human", "非人形": "nonhuman"}


def _presence_draw_arguments(
    form_value: object,
    prompt_value: object,
    description_value: object,
) -> dict[str, Any]:
    raw_form = _one_line(form_value)
    raw_prompt = _one_line(prompt_value)
    raw_description = _one_line(description_value)
    if (
        raw_form not in _PRESENCE_DRAW_FORMS
        or not raw_prompt
        or not raw_description
        or "|" in raw_form
        or "|" in raw_prompt
        or "]" in raw_form
        or "]" in raw_prompt
        or "]" in raw_description
    ):
        return {"parse_error": "parse_failed"}
    return {
        "form": _PRESENCE_DRAW_FORMS[raw_form],
        "prompt": raw_prompt[:500],
        "description": raw_description[:2000],
    }


def _presence_draw_marker_arguments(body: object) -> dict[str, Any]:
    parts = str(body or "").split("|", 2)
    if len(parts) != 3:
        return {"parse_error": "parse_failed"}
    return _presence_draw_arguments(*parts)


def _enabled(enabled_commands: Collection[str] | None) -> set[str]:
    if enabled_commands is None:
        return set(ALL_COMMAND_GROUPS)
    return {str(item) for item in enabled_commands}


def _definition_defaults(tool_name: str) -> tuple[SideEffectLevel, tuple[str, ...], bool]:
    definition = get_tool_definition(tool_name)
    if not definition:
        return SideEffectLevel.READ, ("normal",), False
    return (
        definition.side_effect_level,
        definition.allowed_modes,
        definition.requires_confirmation,
    )


def _intent_id(index: int, tool_name: str) -> str:
    return f"intent_{index:03d}_{tool_name.replace('.', '_')}"


def _build_intent(parsed: _ParsedCommand, index: int) -> ToolIntent:
    side_effect_level, allowed_modes, requires_confirmation = _definition_defaults(parsed.tool_name)
    return ToolIntent(
        id=_intent_id(index, parsed.tool_name),
        tool_name=parsed.tool_name,
        raw_text=parsed.raw_text,
        arguments=parsed.arguments,
        requires_confirmation=requires_confirmation,
        side_effect_level=side_effect_level,
        allowed_modes=allowed_modes,
        metadata={
            "legacy_marker": parsed.legacy_marker,
            "command_group": parsed.command_group,
            "span": [parsed.start, parsed.end],
        },
    )


def _structured_arguments(tool_name: str, action: Mapping[str, Any]) -> dict | None:
    raw_args = action.get("arguments")
    args = dict(raw_args) if isinstance(raw_args, Mapping) else {}

    if tool_name == "memory.view_image":
        message_id = _one_line(args.get("message_id") or action.get("message_id"))
        url = _one_line(args.get("attachment_url") or action.get("attachment_url"))
        if not message_id or not url.startswith("/uploads/"):
            return None
        return {"message_id": message_id, "attachment_url": url}
    if tool_name == "device.toy":
        command = args.get("command") or action.get("command") or action.get("legacy_command") or action.get("value")
        if not _one_line(command):
            return None
        args["command"] = _one_line(command)
    elif tool_name == "device.ring_touch":
        touch = args.get("touch") or action.get("touch") or action.get("text") or action.get("value")
        if not _one_line(touch):
            return None
        args["touch"] = _one_line(touch)
        reason = args.get("reason") or action.get("reason")
        if reason:
            args["reason"] = _one_line(reason)
        haptics = (
            args.get("haptics") if isinstance(args.get("haptics"), Mapping)
            else action.get("haptics") if isinstance(action.get("haptics"), Mapping)
            else {}
        )
        taps = haptics.get("taps") or args.get("taps") or action.get("taps")
        interval = haptics.get("interval_ms") or args.get("interval_ms") or action.get("interval_ms")
        args["haptics"] = {k: v for k, v in {"taps": taps, "interval_ms": interval}.items() if v is not None}
    elif tool_name == "music.search":
        query = args.get("query") or action.get("query") or action.get("song") or action.get("value")
        if query:
            args["query"] = _one_line(query)
    elif tool_name in {"heart.whisper", "memory.remember"}:
        content = args.get("content") or action.get("content") or action.get("text") or action.get("value")
        if content:
            args["content"] = _one_line(content)
    elif tool_name == "location.poi_search":
        category = args.get("category") or action.get("category") or action.get("query") or action.get("value")
        if category:
            args["category"] = _one_line(category)
    elif tool_name == "activity.summary":
        raw = args.get("n") or args.get("raw_window") or action.get("n") or action.get("window") or action.get("value")
        if raw is not None:
            args["raw_window"] = _one_line(raw)
            args["n"] = _normalize_activity_window(str(raw))
    elif tool_name == "pc.screen_check":
        reason = args.get("reason") or action.get("reason") or action.get("value")
        if reason:
            args["reason"] = _one_line(reason)
        if not args.get("reason"):
            return None
    elif tool_name == "mobile.screen_check":
        reason = args.get("reason") or action.get("reason")
        if reason:
            args["reason"] = _one_line(reason)
        target = (args.get("target") or action.get("target")
                  or action.get("device") or action.get("value"))
        if target:
            args["target"] = _one_line(target)
        if not args.get("reason"):
            return None
    elif tool_name == "desktop.presence.draw":
        return _presence_draw_arguments(
            args.get("form") or action.get("form"),
            args.get("prompt") or action.get("prompt"),
            args.get("description")
            or action.get("description")
            or action.get("self_description"),
        )
    elif tool_name == "desktop.presence.show":
        intent_text = (
            args.get("intent_text")
            or action.get("intent_text")
            or action.get("intent")
            or action.get("description")
            or action.get("value")
        )
        if not _one_line(intent_text):
            return None
        args["intent_text"] = _one_line(intent_text)[:1000]
    elif tool_name == "self_wake.schedule":
        wake_at = args.get("wake_at") or action.get("wake_at")
        intent_text = args.get("intent") or action.get("intent") or action.get("value")
        requested = args.get("requested_capabilities")
        if requested is None:
            requested = action.get("requested_capabilities")
        if not _one_line(wake_at) or not _one_line(intent_text):
            return None
        if requested is None:
            normalized_requested = []
        elif isinstance(requested, str):
            normalized_requested = [
                item.strip() for item in requested.split(",") if item.strip()
            ]
        elif isinstance(requested, (list, tuple, set)):
            normalized_requested = [
                str(item).strip() for item in requested if str(item).strip()
            ]
        else:
            return None
        # Return only model-owned arguments. Origin/source/lifecycle fields are
        # always injected later from ToolContext.
        args = {
            "wake_at": _one_line(wake_at),
            "intent": _one_line(intent_text)[:1000],
            "requested_capabilities": normalized_requested,
        }
    elif tool_name == "self_wake.cancel":
        args = {}

    return args


def _mobile_screen_args(raw: str) -> dict:
    """解析 [MOBILE_SCREEN_CHECK:目标|原因]；无 | 时整体视为原因，目标留空。"""
    raw = str(raw or "")
    if "|" in raw:
        target, reason = raw.split("|", 1)
    else:
        target, reason = "", raw
    return {"target": _one_line(target), "reason": _one_line(reason)}


def _self_wake_args(raw: str) -> dict | None:
    """Parse from the right so an intent may itself contain ``|``."""

    body = str(raw or "")
    if "|" not in body:
        return None
    left, capabilities_text = body.rsplit("|", 1)
    if "|" not in left:
        return None
    wake_at, intent_text = left.split("|", 1)
    wake_at = _one_line(wake_at)
    intent_text = _one_line(intent_text)
    if not wake_at or not intent_text:
        return None
    return {
        "wake_at": wake_at,
        "intent": intent_text[:1000],
        "requested_capabilities": [
            item.strip() for item in capabilities_text.split(",") if item.strip()
        ],
    }


def _structured_tool_name(action: Mapping[str, Any]) -> str | None:
    raw = action.get("tool_name") or action.get("tool") or action.get("type") or action.get("action")
    if not raw:
        return None
    return STRUCTURED_ACTION_ALIASES.get(_one_line(raw), _one_line(raw))


def _structured_intent(action: Mapping[str, Any], index: int) -> ToolIntent | None:
    tool_name = _structured_tool_name(action)
    if not tool_name or not get_tool_definition(tool_name):
        return None
    command_group = TOOL_COMMAND_GROUPS.get(tool_name)
    arguments = _structured_arguments(tool_name, action)
    if arguments is None:
        return None
    side_effect_level, allowed_modes, requires_confirmation = _definition_defaults(tool_name)
    return ToolIntent(
        id=_intent_id(index, tool_name),
        tool_name=tool_name,
        raw_text=json.dumps(action, ensure_ascii=False, sort_keys=True),
        arguments=arguments,
        requires_confirmation=requires_confirmation,
        side_effect_level=side_effect_level,
        allowed_modes=allowed_modes,
        source="structured_action",
        metadata={
            "command_group": command_group,
            "schema": "assistant_actions_v1",
            "structured": True,
        },
    )


def _regex_commands(
    text: str,
    *,
    pattern: re.Pattern,
    tool_name: str,
    command_group: str,
    legacy_marker: str,
    argument_builder: Callable[[re.Match], dict],
) -> Iterable[_ParsedCommand]:
    for match in pattern.finditer(text):
        yield _ParsedCommand(
            start=match.start(),
            end=match.end(),
            tool_name=tool_name,
            command_group=command_group,
            raw_text=match.group(0),
            arguments=argument_builder(match),
            legacy_marker=legacy_marker,
        )


def _literal_commands(
    text: str,
    *,
    literal: str,
    tool_name: str,
    command_group: str,
    legacy_marker: str,
    arguments: dict | None = None,
) -> Iterable[_ParsedCommand]:
    if not literal:
        return
    start = 0
    while True:
        found = text.find(literal, start)
        if found < 0:
            break
        end = found + len(literal)
        yield _ParsedCommand(
            start=found,
            end=end,
            tool_name=tool_name,
            command_group=command_group,
            raw_text=literal,
            arguments=dict(arguments or {}),
            legacy_marker=legacy_marker,
        )
        start = end


def parse_tool_intents(
    text: str,
    *,
    enabled_commands: Collection[str] | None = None,
    id_offset: int = 0,
) -> list[ToolIntent]:
    """Parse legacy model-output commands into ToolIntent objects.

    ``enabled_commands`` mirrors ``PostProcessor`` command groups. Passing a
    restricted set makes the read-only parser report only commands that the old
    chain would currently execute.
    """
    raw_text = str(text or "")
    enabled = _enabled(enabled_commands)
    parsed: list[_ParsedCommand] = []

    if "music" in enabled:
        parsed.extend(_regex_commands(
            raw_text,
            pattern=MUSIC_CMD_PATTERN,
            tool_name="music.search",
            command_group="music",
            legacy_marker="MUSIC",
            argument_builder=lambda match: {"query": _one_line(match.group(1))},
        ))
    if "toy" in enabled:
        parsed.extend(_regex_commands(
            raw_text,
            pattern=TOY_CMD_PATTERN,
            tool_name="device.toy",
            command_group="toy",
            legacy_marker="TOY",
            argument_builder=lambda match: {"command": _one_line(match.group(1))},
        ))
    if "cam" in enabled:
        parsed.extend(_literal_commands(
            raw_text,
            literal=CAM_CHECK_CMD,
            tool_name="monitor.camera",
            command_group="cam",
            legacy_marker="CAM_CHECK",
        ))
    if "activity" in enabled:
        parsed.extend(_regex_commands(
            raw_text,
            pattern=ACTIVITY_CHECK_PATTERN,
            tool_name="activity.summary",
            command_group="activity",
            legacy_marker="查看动态",
            argument_builder=lambda match: {
                "raw_window": _one_line(match.group(1)),
                "n": _normalize_activity_window(match.group(1)),
            },
        ))
    if "screen" in enabled:
        parsed.extend(_regex_commands(
            raw_text,
            pattern=SCREEN_CHECK_PATTERN,
            tool_name="pc.screen_check",
            command_group="screen",
            legacy_marker="SCREEN_CHECK",
            argument_builder=lambda match: {"reason": _one_line(match.group(1))},
        ))
    if "mobile_screen" in enabled:
        parsed.extend(_regex_commands(
            raw_text,
            pattern=MOBILE_SCREEN_CHECK_PATTERN,
            tool_name="mobile.screen_check",
            command_group="mobile_screen",
            legacy_marker="MOBILE_SCREEN_CHECK",
            argument_builder=lambda match: _mobile_screen_args(match.group(1)),
        ))
    if "poi" in enabled:
        parsed.extend(_regex_commands(
            raw_text,
            pattern=POI_SEARCH_PATTERN,
            tool_name="location.poi_search",
            command_group="poi",
            legacy_marker="POI_SEARCH",
            argument_builder=lambda match: {"category": _one_line(match.group(1))},
        ))
    if "schedule" in enabled:
        parsed.extend(_regex_commands(
            raw_text,
            pattern=ALARM_CMD,
            tool_name="schedule.alarm",
            command_group="schedule",
            legacy_marker="ALARM",
            argument_builder=lambda match: {
                "raw_datetime": _one_line(match.group(1)),
                "content": _one_line(match.group(2)),
            },
        ))
        parsed.extend(_regex_commands(
            raw_text,
            pattern=REMINDER_CMD,
            tool_name="schedule.reminder",
            command_group="schedule",
            legacy_marker="REMINDER",
            argument_builder=lambda match: {
                "raw_datetime": _one_line(match.group(1)),
                "content": _one_line(match.group(2)),
            },
        ))
        parsed.extend(_regex_commands(
            raw_text,
            pattern=MONITOR_CMD,
            tool_name="schedule.monitor",
            command_group="schedule",
            legacy_marker="Monitor",
            argument_builder=lambda match: {
                "raw_datetime": _one_line(match.group(1)),
                "content": _one_line(match.group(2)),
            },
        ))
        parsed.extend(_regex_commands(
            raw_text,
            pattern=SCHEDULE_DEL_CMD,
            tool_name="schedule.delete",
            command_group="schedule",
            legacy_marker="SCHEDULE_DEL",
            argument_builder=lambda match: {"schedule_id": _one_line(match.group(1))},
        ))
        parsed.extend(_regex_commands(
            raw_text,
            pattern=SCHEDULE_LIST_CMD,
            tool_name="schedule.list",
            command_group="schedule",
            legacy_marker="SCHEDULE_LIST",
            argument_builder=lambda _match: {},
        ))
    if "heart" in enabled:
        parsed.extend(_regex_commands(
            raw_text,
            pattern=HEART_CMD_PATTERN,
            tool_name="heart.whisper",
            command_group="heart",
            legacy_marker="HEART",
            argument_builder=lambda match: {"content": _one_line(match.group(1))},
        ))
    if "view_image" in enabled:
        parsed.extend(_regex_commands(
            raw_text, pattern=VIEW_IMAGE_PATTERN, tool_name="memory.view_image",
            command_group="view_image", legacy_marker="VIEW_IMAGE",
            argument_builder=lambda match: {"message_id": match.group(1), "attachment_url": match.group(2)},
        ))
    if "remember" in enabled:
        parsed.extend(_regex_commands(
            raw_text,
            pattern=REMEMBER_CMD_PATTERN,
            tool_name="memory.remember",
            command_group="remember",
            legacy_marker="REMEMBER",
            argument_builder=lambda match: {"content": _one_line(match.group(1))},
        ))
    if "presence_draw" in enabled:
        parsed.extend(_regex_commands(
            raw_text,
            pattern=PRESENCE_DRAW_PATTERN,
            tool_name="desktop.presence.draw",
            command_group="presence_draw",
            legacy_marker="PRESENCE_DRAW",
            argument_builder=lambda match: _presence_draw_marker_arguments(
                match.group(1)
            ),
        ))
    if "presence_show" in enabled:
        parsed.extend(_regex_commands(
            raw_text,
            pattern=PRESENCE_SHOW_PATTERN,
            tool_name="desktop.presence.show",
            command_group="presence_show",
            legacy_marker="PRESENCE_SHOW",
            argument_builder=lambda match: {
                "intent_text": _one_line(match.group(1))[:1000]
            },
        ))
    if "self_wake" in enabled:
        for match in SELF_WAKE_PATTERN.finditer(raw_text):
            arguments = _self_wake_args(match.group(1))
            if arguments is None:
                continue
            parsed.append(
                _ParsedCommand(
                    start=match.start(),
                    end=match.end(),
                    tool_name="self_wake.schedule",
                    command_group="self_wake",
                    raw_text=match.group(0),
                    arguments=arguments,
                    legacy_marker="SELF_WAKE",
                )
            )
        parsed.extend(_literal_commands(
            raw_text,
            literal=SELF_WAKE_CANCEL_TOKEN,
            tool_name="self_wake.cancel",
            command_group="self_wake",
            legacy_marker="SELF_WAKE_CANCEL",
        ))

    parsed.sort(key=lambda item: (item.start, item.end, item.tool_name))
    return [_build_intent(item, index + id_offset) for index, item in enumerate(parsed, 1)]


def parse_structured_tool_intents(
    actions: Iterable[Mapping[str, Any]] | None,
    *,
    enabled_commands: Collection[str] | None = None,
    id_offset: int = 0,
) -> list[ToolIntent]:
    enabled = _enabled(enabled_commands)
    intents: list[ToolIntent] = []
    for action in actions or ():
        if not isinstance(action, Mapping):
            continue
        tool_name = _structured_tool_name(action)
        command_group = TOOL_COMMAND_GROUPS.get(tool_name or "")
        if command_group not in enabled:
            continue
        intent = _structured_intent(action, id_offset + len(intents) + 1)
        if intent is not None:
            intents.append(intent)
    return intents


def tool_intents_payload(intents: Iterable[ToolIntent]) -> list[dict]:
    return [intent.to_dict() for intent in intents]


__all__ = [
    "ALL_COMMAND_GROUPS",
    "parse_tool_intents",
    "parse_structured_tool_intents",
    "tool_intents_payload",
]
