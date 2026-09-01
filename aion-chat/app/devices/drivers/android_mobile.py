"""Android mobile device driver: dynamic multi-device registry.

Unlike the single-device browser/ring drivers, one Android driver owns many
phones/tablets at once. Each Android app reports itself via
``POST /api/devices/{device_id}/state`` (handled by ``DeviceService.report_state``)
and the driver keeps a ``device_id -> DeviceState`` table.

Ownership rule: this driver claims any ``device_id`` with the ``android_`` prefix
(the identity scheme guarantees ``android_<short_random>``). That keeps it from
swallowing the fixed-id ring/browser devices even when registered last.
"""

from __future__ import annotations

import time
from typing import Any, Callable, Mapping

from app.devices.schemas import DeviceCommandResult, DeviceCommandStatus, DeviceState, DeviceStatus


DEVICE_ID_PREFIX = "android_"
# Ordinary online: heartbeat/activity/state report seen within this window.
MOBILE_STALE_AFTER_SEC = 120.0
# Screen-capture online: poll loop hit within this window. Must stay separate
# from ordinary online — an activity heartbeat does not mean the screen poll
# loop is running. Mirrors pc_screen.SCREEN_AGENT_ONLINE_SEC.
SCREEN_AGENT_ONLINE_SEC = 90.0


def _derive_kind(device_type: str | None, fallback: str | None) -> str:
    dt = str(device_type or "").strip().lower()
    if dt == "phone":
        return "android_phone"
    if dt == "tablet":
        return "android_tablet"
    return fallback or "android_device"


class AndroidMobileDeviceDriver:
    driver_id = "android_mobile"

    def __init__(
        self,
        *,
        now: Callable[[], float] | None = None,
        stale_after_sec: float = MOBILE_STALE_AFTER_SEC,
        screen_online_sec: float = SCREEN_AGENT_ONLINE_SEC,
    ):
        self._now = now or time.time
        self._stale_after_sec = stale_after_sec
        self._screen_online_sec = screen_online_sec
        self._devices: dict[str, DeviceState] = {}

    def owns(self, device_id: str) -> bool:
        return str(device_id or "").startswith(DEVICE_ID_PREFIX)

    async def list_devices(self) -> list[DeviceState]:
        return [self._project_state(state) for state in self._devices.values()]

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
        if not self.owns(device_id):
            return None
        prev = self._devices.get(device_id)
        prev_meta = dict(prev.metadata) if prev else {}
        new_meta = {**prev_meta, **dict(metadata or {})}
        device_type = new_meta.get("device_type")
        state = DeviceState(
            device_id=device_id,
            name=name or (prev.name if prev else None) or "Android 设备",
            kind=kind or _derive_kind(device_type, prev.kind if prev else None),
            status=DeviceStatus(status),
            driver_id=self.driver_id,
            capabilities=tuple(capabilities or (prev.capabilities if prev else ())),
            battery=battery if battery is not None else (prev.battery if prev else None),
            last_seen_at=self._now(),
            metadata=new_meta,
        )
        self._devices[device_id] = state
        return self._project_state(state)

    def mark_screen_poll(self, device_id: str, now: float | None = None) -> bool:
        """Record that the device's screen-capture poll loop is alive.

        Called by the mobile_screen service when the device long-polls for
        pending screenshot requests. Returns False if the device is unknown.
        """
        state = self._devices.get(device_id)
        if state is None:
            return False
        meta = {**dict(state.metadata), "screen_poll_at": self._now() if now is None else float(now)}
        self._devices[device_id] = DeviceState(
            device_id=state.device_id,
            name=state.name,
            kind=state.kind,
            status=state.status,
            driver_id=self.driver_id,
            capabilities=state.capabilities,
            battery=state.battery,
            last_seen_at=state.last_seen_at,
            metadata=meta,
        )
        return True

    def screen_agent_online(self, device_id: str, now: float | None = None) -> bool:
        state = self._devices.get(device_id)
        if state is None:
            return False
        return self._screen_online(state, self._now() if now is None else float(now))

    async def execute_command(
        self,
        device_id: str,
        command: str,
        params: Mapping[str, Any] | None = None,
    ) -> DeviceCommandResult:
        command = str(command or "").strip()
        state = self._devices.get(device_id)
        if state is None:
            return DeviceCommandResult(
                device_id=device_id,
                command=command,
                status=DeviceCommandStatus.FAILED,
                driver_id=self.driver_id,
                message="device_not_found",
            )
        projected = self._project_state(state)
        if command == "ping":
            return DeviceCommandResult(
                device_id=device_id,
                command=command,
                status=DeviceCommandStatus.EXECUTED,
                driver_id=self.driver_id,
                message="pong" if projected.status is DeviceStatus.ONLINE else "device_offline",
                result={
                    "status": projected.status.value,
                    "last_seen_at": projected.last_seen_at,
                    "screen_agent_online": projected.metadata.get("screen_agent_online"),
                },
            )
        # Screenshots go through the mobile_screen service in V1, not a device
        # command. screen_check command lands here only after Phase 6 unification.
        return DeviceCommandResult(
            device_id=device_id,
            command=command,
            status=DeviceCommandStatus.FAILED,
            driver_id=self.driver_id,
            message="unsupported_command",
            metadata={"supported_commands": ["ping"]},
        )

    def _online(self, state: DeviceState, now: float) -> bool:
        if state.status is not DeviceStatus.ONLINE or state.last_seen_at is None:
            return False
        return now - state.last_seen_at <= self._stale_after_sec

    def _screen_online(self, state: DeviceState, now: float) -> bool:
        poll_at = state.metadata.get("screen_poll_at")
        if not isinstance(poll_at, (int, float)):
            return False
        return now - float(poll_at) <= self._screen_online_sec

    def _project_state(self, state: DeviceState) -> DeviceState:
        """Apply staleness and derive screen_agent_online for output."""
        now = self._now()
        online = self._online(state, now)
        screen_online = self._screen_online(state, now)
        meta = {**dict(state.metadata), "screen_agent_online": screen_online}
        status = DeviceStatus.ONLINE if online else DeviceStatus.OFFLINE
        if not online and state.status is DeviceStatus.ONLINE and state.last_seen_at is not None:
            meta["stale"] = True
            meta["age_sec"] = round(now - state.last_seen_at, 3)
        return DeviceState(
            device_id=state.device_id,
            name=state.name,
            kind=state.kind,
            status=status,
            driver_id=self.driver_id,
            capabilities=state.capabilities,
            battery=state.battery,
            last_seen_at=state.last_seen_at,
            metadata=meta,
        )


__all__ = ["AndroidMobileDeviceDriver", "DEVICE_ID_PREFIX", "MOBILE_STALE_AFTER_SEC", "SCREEN_AGENT_ONLINE_SEC"]
