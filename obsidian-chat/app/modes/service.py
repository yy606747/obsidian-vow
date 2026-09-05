"""Server-side chat mode and tool capability policy."""

from __future__ import annotations

from typing import Iterable

from config import SETTINGS, is_smart_ring_touch_active

from .schemas import ChatMode, ModeSnapshot


BASE_TOOL_CAPABILITIES = (
    "music.search",
    "schedule.alarm",
    "schedule.reminder",
    "schedule.monitor",
    "schedule.delete",
    "schedule.list",
    "location.poi_search",
    "activity.summary",
    "pc.screen_check",
    "mobile.screen_check",
    "heart.whisper",
    "memory.remember",
    "self_wake.schedule",
    "self_wake.cancel",
)

DEVICE_TOOL_CAPABILITIES = ("device.toy",)
RING_TOUCH_CAPABILITY = "device.ring_touch"

MODE_CAPABILITIES: dict[ChatMode, tuple[str, ...]] = {
    ChatMode.NORMAL: BASE_TOOL_CAPABILITIES,
    ChatMode.WORK: BASE_TOOL_CAPABILITIES,
    ChatMode.VOICE_CALL: BASE_TOOL_CAPABILITIES,
    ChatMode.SENTINEL: BASE_TOOL_CAPABILITIES,
    ChatMode.MAINTENANCE: BASE_TOOL_CAPABILITIES,
    ChatMode.DEVICE_CONTROL: (*BASE_TOOL_CAPABILITIES, *DEVICE_TOOL_CAPABILITIES),
    ChatMode.INTIMATE: (*BASE_TOOL_CAPABILITIES, *DEVICE_TOOL_CAPABILITIES),
    ChatMode.CONTROL_SESSION: (*BASE_TOOL_CAPABILITIES, *DEVICE_TOOL_CAPABILITIES),
}


class ModeService:
    """Resolve a server-trusted mode snapshot for tool execution."""

    def normalize_mode(self, mode: str | ChatMode | None) -> ChatMode:
        if isinstance(mode, ChatMode):
            return mode
        try:
            return ChatMode(str(mode or ChatMode.NORMAL.value))
        except ValueError:
            return ChatMode.NORMAL

    def capabilities_for_mode(self, mode: str | ChatMode | None) -> tuple[str, ...]:
        normalized = self.normalize_mode(mode)
        capabilities = MODE_CAPABILITIES.get(normalized, BASE_TOOL_CAPABILITIES)
        if SETTINGS.get("image_memory_enabled") is True:
            capabilities = (*capabilities, "memory.view_image")
        if ring_touch_enabled() and RING_TOUCH_CAPABILITY not in capabilities:
            return (*capabilities, RING_TOUCH_CAPABILITY)
        return capabilities

    def snapshot(
        self,
        mode: str | ChatMode | None = None,
        *,
        source: str = "server_default",
        metadata: dict | None = None,
    ) -> ModeSnapshot:
        normalized = self.normalize_mode(mode)
        return ModeSnapshot(
            mode=normalized,
            capabilities=self.capabilities_for_mode(normalized),
            source=source,
            metadata=metadata or {},
        )

    def snapshot_from_flags(
        self,
        *,
        whisper_mode: bool = False,
        ai_dom_mode: bool = False,
        fallback: str | ChatMode | None = ChatMode.NORMAL,
    ) -> ModeSnapshot:
        if ai_dom_mode:
            return self.snapshot(
                ChatMode.DEVICE_CONTROL,
                source="ai_dom_mode",
                metadata={"ai_dom_mode": True, "whisper_mode": bool(whisper_mode)},
            )
        if whisper_mode:
            return self.snapshot(
                ChatMode.INTIMATE,
                source="whisper_mode",
                metadata={"ai_dom_mode": False, "whisper_mode": True},
            )
        return self.snapshot(
            fallback,
            source="server_default",
            metadata={"ai_dom_mode": False, "whisper_mode": False},
        )

    def snapshot_from_prompt_meta(self, prompt_meta: dict | None) -> ModeSnapshot:
        meta = dict(prompt_meta or {})
        mode = meta.get("chat_mode") or meta.get("mode")
        source = str(meta.get("mode_source") or "prompt_meta")
        return self.snapshot(mode, source=source, metadata={"prompt_meta": bool(prompt_meta)})

    def has_capability(self, mode: str | ChatMode | None, capability: str) -> bool:
        return str(capability) in self.capabilities_for_mode(mode)

    def filter_capabilities(self, mode: str | ChatMode | None, candidates: Iterable[str]) -> tuple[str, ...]:
        allowed = set(self.capabilities_for_mode(mode))
        return tuple(str(candidate) for candidate in candidates if str(candidate) in allowed)


mode_service = ModeService()


def ring_touch_enabled() -> bool:
    return is_smart_ring_touch_active()


__all__ = [
    "BASE_TOOL_CAPABILITIES",
    "DEVICE_TOOL_CAPABILITIES",
    "MODE_CAPABILITIES",
    "ModeService",
    "RING_TOUCH_CAPABILITY",
    "mode_service",
    "ring_touch_enabled",
]
