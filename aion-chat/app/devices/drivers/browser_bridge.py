"""Browser/native frontend bridge driver for semi-real device state."""

from __future__ import annotations

import time
from typing import Any, Callable, Mapping

from app.devices.schemas import DeviceCommandResult, DeviceCommandStatus, DeviceState, DeviceStatus


class BrowserBridgeDeviceDriver:
    driver_id = "browser_bridge"
    default_device_id = "browser_toy_bridge"

    def __init__(
        self,
        *,
        device_id: str | None = None,
        now: Callable[[], float] | None = None,
        stale_after_sec: float = 45.0,
    ):
        self._device_id = str(device_id or self.default_device_id)
        self._now = now or time.time
        self._stale_after_sec = stale_after_sec
        self._state = DeviceState(
            device_id=self._device_id,
            name="Browser Toy Bridge",
            kind="toy_bridge",
            status=DeviceStatus.OFFLINE,
            driver_id=self.driver_id,
            capabilities=("status.read", "notify.pulse", "toy.legacy_command"),
            last_seen_at=None,
            metadata={"source": "frontend_bridge", "transport": "unknown"},
        )

    async def list_devices(self) -> list[DeviceState]:
        return [self._current_state()]

    async def report_state(
        self,
        device_id: str,
        *,
        status: str,
        name: str | None = None,
        kind: str | None = None,
        capabilities: tuple[str, ...] | list[str] | None = None,
        battery: int | None = None,
        metadata: Mapping[str, Any] | None = None,
    ) -> DeviceState | None:
        if device_id != self._device_id:
            return None
        self._state = DeviceState(
            device_id=self._device_id,
            name=name or self._state.name,
            kind=kind or self._state.kind,
            status=DeviceStatus(status),
            driver_id=self.driver_id,
            capabilities=tuple(capabilities or self._state.capabilities),
            battery=battery,
            last_seen_at=self._now(),
            metadata={
                **dict(self._state.metadata),
                **dict(metadata or {}),
                "source": "frontend_bridge",
            },
        )
        return self._state

    async def execute_command(
        self,
        device_id: str,
        command: str,
        params: Mapping[str, Any] | None = None,
    ) -> DeviceCommandResult:
        if device_id != self._device_id:
            return DeviceCommandResult(
                device_id=device_id,
                command=command,
                status=DeviceCommandStatus.FAILED,
                driver_id=self.driver_id,
                message="device_not_found",
            )

        command = str(command or "").strip()
        state = self._current_state()
        if command == "ping":
            return DeviceCommandResult(
                device_id=device_id,
                command=command,
                status=DeviceCommandStatus.EXECUTED,
                driver_id=self.driver_id,
                message="pong" if state.status is DeviceStatus.ONLINE else "bridge_offline",
                result={
                    "status": state.status.value,
                    "last_seen_at": state.last_seen_at,
                    "metadata": dict(state.metadata),
                },
            )

        if command == "pulse":
            if state.status is not DeviceStatus.ONLINE:
                return DeviceCommandResult(
                    device_id=device_id,
                    command=command,
                    status=DeviceCommandStatus.FAILED,
                    driver_id=self.driver_id,
                    message="bridge_offline",
                    result={"status": state.status.value, "last_seen_at": state.last_seen_at},
                )
            params = dict(params or {})
            return DeviceCommandResult(
                device_id=device_id,
                command=command,
                status=DeviceCommandStatus.EXECUTED,
                driver_id=self.driver_id,
                message="bridge_command_queued",
                result={
                    "queued": True,
                    "target": "frontend_bridge",
                    "legacy_command": params.get("legacy_command"),
                },
            )

        return DeviceCommandResult(
            device_id=device_id,
            command=command,
            status=DeviceCommandStatus.FAILED,
            driver_id=self.driver_id,
            message="unsupported_command",
            metadata={"supported_commands": ["ping", "pulse"]},
        )

    def _current_state(self) -> DeviceState:
        if self._state.status is not DeviceStatus.ONLINE or self._state.last_seen_at is None:
            return self._state
        age = self._now() - self._state.last_seen_at
        if age <= self._stale_after_sec:
            return self._state
        return DeviceState(
            device_id=self._state.device_id,
            name=self._state.name,
            kind=self._state.kind,
            status=DeviceStatus.OFFLINE,
            driver_id=self.driver_id,
            capabilities=self._state.capabilities,
            battery=self._state.battery,
            last_seen_at=self._state.last_seen_at,
            metadata={**dict(self._state.metadata), "stale": True, "age_sec": round(age, 3)},
        )


__all__ = ["BrowserBridgeDeviceDriver"]
