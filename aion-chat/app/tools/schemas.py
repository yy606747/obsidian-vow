"""Shared ToolService contracts.

These types are intentionally behavior-free. Phase 5 can migrate parsers and
executors behind this contract without changing existing SSE/API shapes first.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Iterable, Mapping


class SideEffectLevel(str, Enum):
    NONE = "none"
    READ = "read"
    WRITE = "write"
    EXTERNAL = "external"
    DEVICE = "device"


class ToolStatus(str, Enum):
    PENDING = "pending"
    SKIPPED = "skipped"
    EXECUTED = "executed"
    FAILED = "failed"


class ToolEventType(str, Enum):
    INTENT_PARSED = "intent_parsed"
    POLICY_SKIPPED = "policy_skipped"
    EXECUTION_STARTED = "execution_started"
    EXECUTION_FINISHED = "execution_finished"
    EXECUTION_FAILED = "execution_failed"


def _as_tuple(values: Iterable[str] | None) -> tuple[str, ...]:
    if values is None:
        return ()
    return tuple(str(value) for value in values if str(value))


def _as_dict(value: Mapping[str, Any] | None) -> dict[str, Any]:
    return dict(value or {})


@dataclass(frozen=True)
class ToolDefinition:
    tool_name: str
    description: str
    side_effect_level: SideEffectLevel = SideEffectLevel.READ
    allowed_modes: tuple[str, ...] = ("normal",)
    requires_confirmation: bool = False
    legacy_markers: tuple[str, ...] = ()
    prompt_orders: tuple[tuple[str, int], ...] = ()
    feedback_timing: str = "none"

    def __post_init__(self):
        object.__setattr__(self, "side_effect_level", SideEffectLevel(self.side_effect_level))
        object.__setattr__(self, "allowed_modes", _as_tuple(self.allowed_modes))
        object.__setattr__(self, "legacy_markers", _as_tuple(self.legacy_markers))
        object.__setattr__(
            self,
            "prompt_orders",
            tuple((str(surface), int(order)) for surface, order in self.prompt_orders),
        )
        if self.feedback_timing not in {"none", "next_turn", "same_turn"}:
            raise ValueError(f"invalid feedback_timing for {self.tool_name}: {self.feedback_timing}")

    def prompt_order(self, surface: str) -> int | None:
        return next(
            (order for candidate, order in self.prompt_orders if candidate == surface),
            None,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "tool_name": self.tool_name,
            "description": self.description,
            "side_effect_level": self.side_effect_level.value,
            "allowed_modes": list(self.allowed_modes),
            "requires_confirmation": self.requires_confirmation,
            "legacy_markers": list(self.legacy_markers),
            "prompt_orders": {surface: order for surface, order in self.prompt_orders},
            "feedback_timing": self.feedback_timing,
        }


@dataclass(frozen=True)
class ToolIntent:
    id: str
    tool_name: str
    raw_text: str
    arguments: Mapping[str, Any] = field(default_factory=dict)
    requires_confirmation: bool = False
    side_effect_level: SideEffectLevel = SideEffectLevel.READ
    allowed_modes: tuple[str, ...] = ("normal",)
    source: str = "model_output"
    confidence: float = 1.0
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self):
        object.__setattr__(self, "arguments", _as_dict(self.arguments))
        object.__setattr__(self, "side_effect_level", SideEffectLevel(self.side_effect_level))
        object.__setattr__(self, "allowed_modes", _as_tuple(self.allowed_modes))
        object.__setattr__(self, "metadata", _as_dict(self.metadata))

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "tool_name": self.tool_name,
            "raw_text": self.raw_text,
            "arguments": dict(self.arguments),
            "requires_confirmation": self.requires_confirmation,
            "side_effect_level": self.side_effect_level.value,
            "allowed_modes": list(self.allowed_modes),
            "source": self.source,
            "confidence": self.confidence,
            "metadata": dict(self.metadata),
        }


@dataclass(frozen=True)
class ToolContext:
    conv_id: str
    msg_id: str | None = None
    request_id: str | None = None
    model_key: str | None = None
    mode: str = "normal"
    capabilities: tuple[str, ...] = ()
    memory_eval_mode: bool = False
    user_id: str | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self):
        object.__setattr__(self, "capabilities", _as_tuple(self.capabilities))
        object.__setattr__(self, "metadata", _as_dict(self.metadata))

    def to_dict(self) -> dict[str, Any]:
        return {
            "conv_id": self.conv_id,
            "msg_id": self.msg_id,
            "request_id": self.request_id,
            "model_key": self.model_key,
            "mode": self.mode,
            "capabilities": list(self.capabilities),
            "memory_eval_mode": self.memory_eval_mode,
            "user_id": self.user_id,
            "metadata": dict(self.metadata),
        }


@dataclass(frozen=True)
class ToolEvent:
    event_type: ToolEventType
    tool_name: str
    intent_id: str | None = None
    message: str = ""
    payload: Mapping[str, Any] = field(default_factory=dict)
    created_at: float | None = None

    def __post_init__(self):
        object.__setattr__(self, "event_type", ToolEventType(self.event_type))
        object.__setattr__(self, "payload", _as_dict(self.payload))

    def to_dict(self) -> dict[str, Any]:
        return {
            "event_type": self.event_type.value,
            "tool_name": self.tool_name,
            "intent_id": self.intent_id,
            "message": self.message,
            "payload": dict(self.payload),
            "created_at": self.created_at,
        }


@dataclass(frozen=True)
class ToolResult:
    tool_name: str
    intent_id: str | None = None
    status: ToolStatus = ToolStatus.PENDING
    result: Mapping[str, Any] | None = None
    error: str | None = None
    events: tuple[ToolEvent, ...] = ()
    user_visible_message: str | None = None
    attachments: tuple[Mapping[str, Any], ...] = ()
    followup_required: bool = False
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self):
        object.__setattr__(self, "status", ToolStatus(self.status))
        object.__setattr__(self, "result", None if self.result is None else _as_dict(self.result))
        object.__setattr__(self, "events", tuple(self.events or ()))
        object.__setattr__(self, "attachments", tuple(_as_dict(item) for item in self.attachments))
        object.__setattr__(self, "metadata", _as_dict(self.metadata))

    @classmethod
    def from_intent(
        cls,
        intent: ToolIntent,
        *,
        status: ToolStatus = ToolStatus.PENDING,
        result: Mapping[str, Any] | None = None,
        error: str | None = None,
        events: Iterable[ToolEvent] = (),
        user_visible_message: str | None = None,
        attachments: Iterable[Mapping[str, Any]] = (),
        followup_required: bool = False,
        metadata: Mapping[str, Any] | None = None,
    ) -> "ToolResult":
        return cls(
            tool_name=intent.tool_name,
            intent_id=intent.id,
            status=status,
            result=result,
            error=error,
            events=tuple(events),
            user_visible_message=user_visible_message,
            attachments=tuple(attachments),
            followup_required=followup_required,
            metadata=metadata or {},
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "tool_name": self.tool_name,
            "intent_id": self.intent_id,
            "status": self.status.value,
            "result": None if self.result is None else dict(self.result),
            "error": self.error,
            "events": [event.to_dict() for event in self.events],
            "user_visible_message": self.user_visible_message,
            "attachments": [dict(item) for item in self.attachments],
            "followup_required": self.followup_required,
            "metadata": dict(self.metadata),
        }


KNOWN_TOOL_DEFINITIONS: dict[str, ToolDefinition] = {
    definition.tool_name: definition
    for definition in (
        ToolDefinition(
            tool_name="music.search",
            description="Search a song and attach playable music cards.",
            side_effect_level=SideEffectLevel.EXTERNAL,
            legacy_markers=("[MUSIC:...]",),
            prompt_orders=(("main_stable", 10), ("schedule", 10)),
            feedback_timing="next_turn",
        ),
        ToolDefinition(
            tool_name="schedule.alarm",
            description="Create an alarm schedule.",
            side_effect_level=SideEffectLevel.WRITE,
            legacy_markers=("[ALARM:datetime|content]",),
            prompt_orders=(("main_stable", 20), ("schedule", 20)),
            feedback_timing="next_turn",
        ),
        ToolDefinition(
            tool_name="schedule.reminder",
            description="Create a reminder schedule.",
            side_effect_level=SideEffectLevel.WRITE,
            legacy_markers=("[REMINDER:date|content]",),
            prompt_orders=(("main_stable", 30), ("schedule", 30)),
            feedback_timing="next_turn",
        ),
        ToolDefinition(
            tool_name="schedule.monitor",
            description="Create a scheduled monitor check.",
            side_effect_level=SideEffectLevel.WRITE,
            legacy_markers=("[Monitor:datetime|content]",),
            prompt_orders=(("main_stable", 40), ("schedule", 40)),
            feedback_timing="next_turn",
        ),
        ToolDefinition(
            tool_name="schedule.delete",
            description="Delete an existing schedule.",
            side_effect_level=SideEffectLevel.WRITE,
            legacy_markers=("[SCHEDULE_DEL:id]",),
            prompt_orders=(("main_stable", 50), ("schedule", 50)),
            feedback_timing="next_turn",
        ),
        ToolDefinition(
            tool_name="schedule.list",
            description="Request schedule listing context.",
            side_effect_level=SideEffectLevel.READ,
            legacy_markers=("[SCHEDULE_LIST]",),
            prompt_orders=(("main_stable", 60),),
            feedback_timing="same_turn",
        ),
        ToolDefinition(
            tool_name="location.poi_search",
            description="Search nearby places from current location.",
            side_effect_level=SideEffectLevel.EXTERNAL,
            legacy_markers=("[POI_SEARCH:category]",),
            prompt_orders=(("main_runtime", 20), ("opportunity", 60), ("self_wake", 60)),
            feedback_timing="same_turn",
        ),
        ToolDefinition(
            tool_name="activity.summary",
            description="Read recent device activity and produce a follow-up reply.",
            side_effect_level=SideEffectLevel.READ,
            legacy_markers=("[查看动态:n]",),
            prompt_orders=(("main_stable", 70),),
            feedback_timing="same_turn",
        ),
        ToolDefinition(
            tool_name="pc.screen_check",
            description="Request a local PC screenshot with user confirmation and produce a follow-up reply.",
            side_effect_level=SideEffectLevel.EXTERNAL,
            requires_confirmation=True,
            legacy_markers=("[SCREEN_CHECK:reason]",),
            prompt_orders=(
                ("main_stable", 80),
                ("opportunity", 10),
                ("sentinel_v2", 30),
                ("self_wake", 20),
            ),
            feedback_timing="same_turn",
        ),
        ToolDefinition(
            tool_name="mobile.screen_check",
            description="Request a phone/tablet screenshot via the Android app (user-confirmed), routed to a target device, then produce a follow-up reply.",
            side_effect_level=SideEffectLevel.EXTERNAL,
            requires_confirmation=True,
            legacy_markers=("[MOBILE_SCREEN_CHECK:target|reason]",),
            prompt_orders=(
                ("main_stable", 90),
                ("opportunity", 20),
                ("sentinel_v2", 40),
                ("self_wake", 30),
            ),
            feedback_timing="same_turn",
        ),
        ToolDefinition(
            tool_name="heart.whisper",
            description="Store a heart whisper attachment for the current assistant message.",
            side_effect_level=SideEffectLevel.WRITE,
            legacy_markers=("[HEART:content]",),
            prompt_orders=(("main_stable", 120), ("opportunity", 50), ("self_wake", 50)),
            feedback_timing="next_turn",
        ),
        ToolDefinition(
            tool_name="memory.view_image",
            description="按来源消息和附件重新查看原图，每轮至多一次补充回复。",
            side_effect_level=SideEffectLevel.READ,
            legacy_markers=("[VIEW_IMAGE:message_id|attachment_url]",),
            prompt_orders=(("main_stable", 105),),
            feedback_timing="same_turn",
        ),
        ToolDefinition(
            tool_name="memory.remember",
            description="Persist an assistant-proposed memory note.",
            side_effect_level=SideEffectLevel.WRITE,
            legacy_markers=("[REMEMBER:content]",),
            prompt_orders=(("main_stable", 110), ("opportunity", 40), ("self_wake", 40)),
            feedback_timing="next_turn",
        ),
        ToolDefinition(
            tool_name="desktop.presence.draw",
            description="Generate a new transparent desktop-presence sprite for the library.",
            side_effect_level=SideEffectLevel.EXTERNAL,
            legacy_markers=(
                "[PRESENCE_DRAW:人形或非人形|视觉规格|形象自述]",
            ),
            prompt_orders=(("opportunity", 70), ("self_wake", 80)),
            feedback_timing="none",
        ),
        ToolDefinition(
            tool_name="desktop.presence.show",
            description="Render and queue one synced desktop-presence appearance.",
            side_effect_level=SideEffectLevel.EXTERNAL,
            legacy_markers=("[PRESENCE_SHOW:natural-language intent]",),
            prompt_orders=(("opportunity", 60), ("self_wake", 70)),
            feedback_timing="none",
        ),
        ToolDefinition(
            tool_name="device.toy",
            description="Emit a device command through the existing bridge.",
            side_effect_level=SideEffectLevel.DEVICE,
            allowed_modes=("intimate", "device_control"),
            legacy_markers=("[TOY:command]",),
            prompt_orders=(
                ("main_runtime", 10),
                ("initiative", 10),
                ("sentinel_legacy", 10),
                ("sentinel_v2", 10),
            ),
            feedback_timing="next_turn",
        ),
        ToolDefinition(
            tool_name="device.ring_touch",
            description="Send a touch request to the smart ring.",
            side_effect_level=SideEffectLevel.DEVICE,
            allowed_modes=("ring_touch_enabled",),
            legacy_markers=(),
            prompt_orders=(
                ("main_stable", 100),
                ("opportunity", 30),
                ("sentinel_v2", 20),
                ("self_wake", 10),
            ),
            feedback_timing="next_turn",
        ),
        ToolDefinition(
            tool_name="self_wake.schedule",
            description="Schedule one relationship-bound autonomous wake, replacing the current pending wake.",
            side_effect_level=SideEffectLevel.WRITE,
            legacy_markers=("[SELF_WAKE:datetime|intent|capabilities]",),
            prompt_orders=(("main_runtime", 30), ("opportunity", 80)),
            feedback_timing="next_turn",
        ),
        ToolDefinition(
            tool_name="self_wake.cancel",
            description="Cancel the current relationship-bound pending autonomous wake.",
            side_effect_level=SideEffectLevel.WRITE,
            legacy_markers=("[SELF_WAKE_CANCEL]",),
            prompt_orders=(("main_runtime", 40), ("opportunity", 90)),
            feedback_timing="next_turn",
        ),
    )
}


def get_tool_definition(tool_name: str) -> ToolDefinition | None:
    return KNOWN_TOOL_DEFINITIONS.get(tool_name)


def tool_definitions_payload() -> list[dict[str, Any]]:
    return [definition.to_dict() for definition in KNOWN_TOOL_DEFINITIONS.values()]


__all__ = [
    "KNOWN_TOOL_DEFINITIONS",
    "SideEffectLevel",
    "ToolContext",
    "ToolDefinition",
    "ToolEvent",
    "ToolEventType",
    "ToolIntent",
    "ToolResult",
    "ToolStatus",
    "get_tool_definition",
    "tool_definitions_payload",
]
