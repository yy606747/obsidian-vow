"""Turn-scoped capability policy shared by prompt, parsing, and execution."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Collection, Literal

from app.chat.control_syntax import canonicalize_control_markers
from app.tools.parser import TOOL_COMMAND_GROUPS
from app.tools.schemas import KNOWN_TOOL_DEFINITIONS


PromptSource = Literal["send", "regenerate", "opportunity", "initiative", "self_wake"]
AutonomousKind = Literal["idle", "summon", "night"]

MARKER_VISIBLE_REPLY = "visible_reply"
MARKER_WORKING_MODEL_REQUEST = "working_model_request"
MARKER_VOW = "vow"
MARKER_RECALL_INTENT = "recall_intent"
MARKER_WEB_SEARCH_INTENT = "web_search_intent"
MARKER_OPPORTUNITY_NONE = "opportunity_none"
MARKER_OPPORTUNITY_REFLECT = "opportunity_reflect"
MARKER_SELF_WAKE_NONE = "self_wake_none"

OPPORTUNITY_NONE_TOKEN = "[OPPORTUNITY_NONE]"
OPPORTUNITY_REFLECT_TOKEN = "[OPPORTUNITY_REFLECT]"
SELF_WAKE_NONE_TOKEN = "[SELF_WAKE_NONE]"

OPPORTUNITY_TOOL_CAPABILITIES = frozenset(
    {
        "heart.whisper",
        "device.ring_touch",
        "pc.screen_check",
        "mobile.screen_check",
        "memory.remember",
        "location.poi_search",
        "desktop.presence.draw",
        "desktop.presence.show",
        "self_wake.schedule",
        "self_wake.cancel",
    }
)

PRESENCE_TOOL_CAPABILITIES = frozenset(
    {"desktop.presence.draw", "desktop.presence.show"}
)

# ``monitor.camera`` is deliberately absent from the callable-tool registry and
# therefore can never be advertised to the model.  Keep accepting its legacy
# chat marker solely so old/model-emitted text retains the shipped
# ``cam_disabled`` response instead of being silently dropped.
_LEGACY_CHAT_MARKER_CAPABILITIES = frozenset({"monitor.camera"})


@dataclass(frozen=True)
class TurnProfile:
    """The single source of truth for one turn's model-visible capabilities."""

    prompt_source: PromptSource
    allowed_markers: frozenset[str]
    allowed_tool_capabilities: frozenset[str]

    @property
    def enabled_commands(self) -> frozenset[str]:
        return frozenset(
            group
            for tool_name, group in TOOL_COMMAND_GROUPS.items()
            if tool_name in self.allowed_tool_capabilities
        )

    def allows_marker(self, marker: str) -> bool:
        return str(marker) in self.allowed_markers

    def allows_tool(self, tool_name: str) -> bool:
        return str(tool_name) in self.allowed_tool_capabilities


OpportunityControlKind = Literal["ordinary", "none", "reflect", "invalid"]
SelfWakeControlKind = Literal["ordinary", "none", "invalid"]


def classify_opportunity_control_output(
    raw_output: str,
    *,
    profile: TurnProfile,
) -> OpportunityControlKind:
    """Classify reserved opportunity syntax before post-processing or effects."""

    text = canonicalize_control_markers(raw_output).strip()
    if "[OPPORTUNITY_" not in text.upper():
        return "ordinary"
    if text == OPPORTUNITY_NONE_TOKEN and profile.allows_marker(
        MARKER_OPPORTUNITY_NONE
    ):
        return "none"
    if text == OPPORTUNITY_REFLECT_TOKEN and profile.allows_marker(
        MARKER_OPPORTUNITY_REFLECT
    ):
        return "reflect"
    return "invalid"


def chat_turn_profile(prompt_source: str, *, web_search_allowed: bool = False) -> TurnProfile:
    source: PromptSource = "regenerate" if prompt_source == "regenerate" else "send"
    markers = {
        MARKER_VISIBLE_REPLY,
        MARKER_WORKING_MODEL_REQUEST,
        MARKER_VOW,
        MARKER_RECALL_INTENT,
    }
    if source == "send" and web_search_allowed:
        markers.add(MARKER_WEB_SEARCH_INTENT)
    return TurnProfile(
        prompt_source=source,
        allowed_markers=frozenset(markers),
        # Keep ordinary chat behavior unchanged. Its existing mode snapshot is
        # still the runtime policy; this profile only adds the strict source
        # allowlist required by the shared action layer.
        allowed_tool_capabilities=(
            (frozenset(KNOWN_TOOL_DEFINITIONS) - PRESENCE_TOOL_CAPABILITIES)
            | _LEGACY_CHAT_MARKER_CAPABILITIES
        ),
    )


def classify_self_wake_control_output(
    raw_output: str,
    *,
    profile: TurnProfile,
) -> SelfWakeControlKind:
    """The reserved NONE marker is valid only as the entire output."""

    text = canonicalize_control_markers(raw_output).strip()
    if SELF_WAKE_NONE_TOKEN not in text.upper():
        return "ordinary"
    if text == SELF_WAKE_NONE_TOKEN and profile.allows_marker(MARKER_SELF_WAKE_NONE):
        return "none"
    return "invalid"


def initiative_turn_profile(*, toy_enabled: bool) -> TurnProfile:
    tools = {"memory.remember"}
    if toy_enabled:
        tools.add("device.toy")
    return TurnProfile(
        prompt_source="initiative",
        allowed_markers=frozenset({MARKER_VISIBLE_REPLY}),
        allowed_tool_capabilities=frozenset(tools),
    )


def opportunity_turn_profile(
    *,
    runtime_capabilities: Collection[str],
    reflection_allowed: bool,
    web_search_allowed: bool = False,
    kind: AutonomousKind = "idle",
    presence_bootstrap_required: bool = False,
) -> TurnProfile:
    if kind not in {"idle", "summon", "night"}:
        raise ValueError("invalid_autonomous_kind")
    if presence_bootstrap_required and kind != "night":
        raise ValueError("presence_bootstrap_requires_night")
    markers = set()
    if not presence_bootstrap_required:
        markers.add(MARKER_OPPORTUNITY_NONE)
    if kind == "idle":
        markers.add(MARKER_VISIBLE_REPLY)
    if (
        reflection_allowed
        and kind in {"idle", "night"}
        and not presence_bootstrap_required
    ):
        markers.add(MARKER_OPPORTUNITY_REFLECT)
    if web_search_allowed and kind == "idle":
        markers.add(MARKER_WEB_SEARCH_INTENT)
    requested = {str(item) for item in runtime_capabilities}
    if kind == "summon":
        requested &= {"desktop.presence.show"}
    elif kind == "night":
        requested &= {"desktop.presence.draw"}
    return TurnProfile(
        prompt_source="opportunity",
        allowed_markers=frozenset(markers),
        allowed_tool_capabilities=frozenset(
            item for item in requested if item in OPPORTUNITY_TOOL_CAPABILITIES
        ),
    )


def self_wake_turn_profile(
    effective_capabilities: Collection[str],
) -> TurnProfile:
    from app.self_wake import SELF_WAKE_SURFACE_CAPABILITIES

    return TurnProfile(
        prompt_source="self_wake",
        allowed_markers=frozenset({MARKER_VISIBLE_REPLY, MARKER_SELF_WAKE_NONE}),
        allowed_tool_capabilities=frozenset(
            str(item)
            for item in effective_capabilities
            if str(item) in SELF_WAKE_SURFACE_CAPABILITIES
        ),
    )


__all__ = [
    "MARKER_OPPORTUNITY_NONE",
    "MARKER_OPPORTUNITY_REFLECT",
    "MARKER_RECALL_INTENT",
    "MARKER_SELF_WAKE_NONE",
    "MARKER_WEB_SEARCH_INTENT",
    "MARKER_VISIBLE_REPLY",
    "MARKER_VOW",
    "MARKER_WORKING_MODEL_REQUEST",
    "OPPORTUNITY_NONE_TOKEN",
    "OPPORTUNITY_REFLECT_TOKEN",
    "OPPORTUNITY_TOOL_CAPABILITIES",
    "AutonomousKind",
    "PRESENCE_TOOL_CAPABILITIES",
    "PromptSource",
    "SELF_WAKE_NONE_TOKEN",
    "TurnProfile",
    "chat_turn_profile",
    "classify_opportunity_control_output",
    "classify_self_wake_control_output",
    "initiative_turn_profile",
    "opportunity_turn_profile",
    "self_wake_turn_profile",
]
