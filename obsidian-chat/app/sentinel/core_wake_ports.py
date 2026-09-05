"""Ports for Core wake execution side effects.

These protocols describe the boundary Wake Orchestrator will use when it is
allowed to execute. Tests use fake in-memory ports; runtime adapters are a later
step and should stay outside the pure Sentinel package.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any, Protocol


class CoreWakePorts(Protocol):
    """Async side-effect boundary required by Core wake execution.

    Prompt context loaders such as ``load_vow_prompt_context``,
    ``load_working_model_prompt_context`` and ``load_timeline_prompt_context``
    are optional capabilities discovered by the orchestrator. They stay out of
    ``CORE_WAKE_PORT_METHODS`` so pure test ports can intentionally omit
    production-only context. Timeline usage recording is optional for the same
    reason.
    """

    async def broadcast_monitor_alert(self, content: str) -> None:
        """Broadcast that Sentinel would wake Core."""

    async def insert_system_wake_notice(
        self,
        *,
        conv_id: str,
        content: str,
        created_at: float,
    ) -> Mapping[str, Any]:
        """Persist the system wake notice and return the stored message."""

    async def stream_core(
        self,
        *,
        messages: Sequence[Mapping[str, str]],
        model_key: str,
        temperature: Any = None,
    ) -> str:
        """Call Core and return the complete assistant content."""

    async def insert_assistant_message(
        self,
        *,
        conv_id: str,
        content: str,
        created_at: float,
    ) -> Mapping[str, Any]:
        """Persist Core's assistant message and return the stored message."""

    async def update_conversation(self, *, conv_id: str, updated_at: float) -> None:
        """Update conversation metadata after a successful Core wake."""

    async def broadcast_msg_created(self, message: Mapping[str, Any]) -> None:
        """Broadcast a stored chat message."""

    async def broadcast_toy_command(
        self,
        *,
        commands: Sequence[str],
        msg_id: str,
        conv_id: str | None = None,
        toy_capability_allowed: bool = False,
        control_session_id: str | None = None,
        control_epoch: int | None = None,
        owner_client_id: str | None = None,
        control_device_id: str | None = None,
        request_id: str | None = None,
        wake_id: str | None = None,
    ) -> Mapping[str, Any] | None:
        """Broadcast toy commands stripped from Core assistant content."""

    async def request_screen_check(
        self,
        *,
        conv_id: str,
        msg_id: str,
        model_key: str,
        reason: str,
        request_id: str | None = None,
        wake_id: str | None = None,
    ) -> Mapping[str, Any] | None:
        """Request a PC screenshot through the existing confirmation-gated service."""

    async def request_mobile_screen_check(
        self,
        *,
        conv_id: str,
        msg_id: str,
        model_key: str,
        target: str,
        reason: str,
        request_id: str | None = None,
        wake_id: str | None = None,
    ) -> Mapping[str, Any] | None:
        """Request a phone/tablet screenshot through the Android confirmation flow."""

    async def execute_ring_touch(
        self,
        *,
        touch_descriptions: Sequence[str],
        conv_id: str,
        msg_id: str,
        model_key: str,
        request_id: str | None = None,
        wake_id: str | None = None,
    ) -> Mapping[str, Any]:
        """Execute translated smart-ring touches through the device service."""

    async def sleep(self, seconds: float) -> None:
        """Sleep between user-visible alert and Core call, or between retries."""

    async def write_monitor_log(self, entry: Mapping[str, Any]) -> None:
        """Write a monitor log entry for the Core wake result."""

    async def prepare_web_search_turn(
        self,
        *,
        conv_id: str,
        bound_turn_id: str,
    ) -> Mapping[str, Any]:
        """Optionally bind ready web-search material for this Core turn."""

    async def finalize_web_search_turn(
        self,
        *,
        conv_id: str,
        bound_turn_id: str,
        assistant_message_id: str,
        intent_text: str,
    ) -> Mapping[str, Any]:
        """Consume bound material and enqueue a new Core-owned intent."""

    def now(self) -> float:
        """Return current timestamp for deterministic message ids in tests."""


CORE_WAKE_PORT_METHODS = (
    "broadcast_monitor_alert",
    "broadcast_msg_created",
    "request_screen_check",
    "request_mobile_screen_check",
    "broadcast_toy_command",
    "insert_assistant_message",
    "insert_system_wake_notice",
    "now",
    "sleep",
    "stream_core",
    "update_conversation",
    "write_monitor_log",
)


def validate_core_wake_ports(ports: Any) -> CoreWakePorts:
    """Fail loud if a ports object is missing any required side-effect method."""
    if ports is None:
        raise ValueError("core wake orchestrator ports are required for test execution")
    missing = [name for name in CORE_WAKE_PORT_METHODS if not callable(getattr(ports, name, None))]
    if missing:
        raise ValueError(f"core wake orchestrator ports missing methods: {missing!r}")
    return ports


def stored_message_to_dict(message: Mapping[str, Any], *, label: str) -> dict[str, Any]:
    """Validate the minimal stored-message shape needed for broadcasts/traces."""
    if not isinstance(message, Mapping):
        raise ValueError(f"core wake {label} message must be an object")
    required = {"content", "conv_id", "created_at", "id", "role"}
    missing = sorted(required.difference(message.keys()))
    if missing:
        raise ValueError(f"core wake {label} message missing fields: {missing!r}")
    result = dict(message)
    for key in ("content", "conv_id", "id", "role"):
        if not isinstance(result[key], str) or not result[key].strip():
            raise ValueError(f"core wake {label} message {key} must be non-empty text")
        result[key] = result[key].strip()
    if isinstance(result["created_at"], bool) or not isinstance(result["created_at"], int | float):
        raise ValueError(f"core wake {label} message created_at must be a number")
    result["attachments"] = list(result.get("attachments") or [])
    return result


__all__ = [
    "CORE_WAKE_PORT_METHODS",
    "CoreWakePorts",
    "stored_message_to_dict",
    "validate_core_wake_ports",
]
