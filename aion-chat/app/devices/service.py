"""DeviceService minimum backend loop for Phase 7."""

from __future__ import annotations

import json
import os
import time
from typing import Any, Awaitable, Callable, Mapping, Protocol

from app.memory_v2.v2_repository import MemoryRepository

from .drivers import AndroidMobileDeviceDriver, BrowserBridgeDeviceDriver, RingDeviceDriver
from .schemas import DeviceCommandResult, DeviceCommandStatus, DeviceState, DeviceStatus


DeviceEventSink = Callable[..., Awaitable[dict]]


class DeviceDriver(Protocol):
    driver_id: str

    async def list_devices(self) -> list[DeviceState]:
        ...

    async def execute_command(
        self,
        device_id: str,
        command: str,
        params: Mapping[str, Any] | None = None,
    ) -> DeviceCommandResult:
        ...


async def _default_event_sink(**kwargs) -> dict:
    return await MemoryRepository().create_event(**kwargs)


async def _default_ws_sender(device_type: str, data: Mapping[str, Any]) -> bool:
    from ws import manager

    return await manager.send_to_device(device_type, dict(data))


async def _default_tool_terminal_sink(**kwargs) -> int:
    from app.tools.ledger import tool_invocation_ledger

    return await tool_invocation_ledger.record_terminal_outcome(**kwargs)


def _smart_ring_touch_enabled() -> bool:
    from config import SETTINGS

    return bool(SETTINGS.get("smart_ring_touch_enabled", SETTINGS.get("ring_touch_enabled", False)))


def _smart_ring_quiet_hours() -> bool:
    from config import is_smart_ring_quiet_hours

    return is_smart_ring_quiet_hours()


def _mock_devices_enabled() -> bool:
    from config import SETTINGS

    return bool(os.environ.get("AION_ENABLE_MOCK_DEVICES") or SETTINGS.get("mock_devices_enabled", False))


def _smart_ring_name_prefix() -> str:
    from config import SETTINGS

    return str(SETTINGS.get("smart_ring_name_prefix") or "AIZO").strip() or "AIZO"


def _smart_ring_keep_connected() -> bool:
    from config import SETTINGS

    return bool(SETTINGS.get("smart_ring_keep_connected", False))


class MockDeviceDriver:
    driver_id = "mock"

    def __init__(self, *, now: Callable[[], float] | None = None):
        self._now = now or time.time
        self._devices = {
            "mock_ring": DeviceState(
                device_id="mock_ring",
                name="Mock Ring",
                kind="wearable",
                status=DeviceStatus.ONLINE,
                driver_id=self.driver_id,
                capabilities=("status.read", "notify.pulse"),
                battery=88,
                last_seen_at=self._now(),
                metadata={"phase": "phase7_mock"},
            )
        }

    async def list_devices(self) -> list[DeviceState]:
        return list(self._devices.values())

    async def execute_command(
        self,
        device_id: str,
        command: str,
        params: Mapping[str, Any] | None = None,
    ) -> DeviceCommandResult:
        device = self._devices.get(device_id)
        if not device:
            return DeviceCommandResult(
                device_id=device_id,
                command=command,
                status=DeviceCommandStatus.FAILED,
                driver_id=self.driver_id,
                message="device_not_found",
            )

        command = str(command or "").strip()
        params = dict(params or {})
        if command == "ping":
            return DeviceCommandResult(
                device_id=device_id,
                command=command,
                status=DeviceCommandStatus.EXECUTED,
                driver_id=self.driver_id,
                message="pong",
                result={"status": device.status.value, "battery": device.battery},
            )
        if command == "pulse":
            return DeviceCommandResult(
                device_id=device_id,
                command=command,
                status=DeviceCommandStatus.EXECUTED,
                driver_id=self.driver_id,
                message="mock_pulse_sent",
                result={"duration_ms": int(params.get("duration_ms") or 300)},
            )
        return DeviceCommandResult(
            device_id=device_id,
            command=command,
            status=DeviceCommandStatus.FAILED,
            driver_id=self.driver_id,
            message="unsupported_command",
            metadata={"supported_commands": ["ping", "pulse"]},
        )


class DeviceService:
    def __init__(
        self,
        *,
        drivers: list[DeviceDriver] | None = None,
        event_sink: DeviceEventSink | None = None,
    ):
        self._event_sink = event_sink or _default_event_sink
        if drivers is None:
            drivers = [
                BrowserBridgeDeviceDriver(),
                RingDeviceDriver(
                    event_sink=self._event_sink,
                    ws_sender=_default_ws_sender,
                    settings_reader=_smart_ring_touch_enabled,
                    quiet_hours_reader=_smart_ring_quiet_hours,
                    name_prefix_reader=_smart_ring_name_prefix,
                    keep_connected_reader=_smart_ring_keep_connected,
                    terminal_sink=_default_tool_terminal_sink,
                ),
                # Last in the chain: claims only android_* ids, so the fixed-id
                # drivers above get first refusal on report_state.
                AndroidMobileDeviceDriver(),
            ]
            if _mock_devices_enabled():
                drivers.append(MockDeviceDriver())
        self._drivers = list(drivers)

    async def list_devices(self) -> dict:
        devices = []
        for driver in self._drivers:
            devices.extend(await driver.list_devices())
        return {
            "devices": [device.to_dict() for device in devices],
            "count": len(devices),
        }

    async def get_device(self, device_id: str) -> dict:
        for device in (await self.list_devices())["devices"]:
            if device["device_id"] == device_id:
                return {"ok": True, "device": device}
        return {"ok": False, "error": "device_not_found", "device_id": device_id}

    def get_driver(self, driver_id: str) -> DeviceDriver | None:
        for driver in self._drivers:
            if driver.driver_id == driver_id:
                return driver
        return None

    async def execute_command(
        self,
        device_id: str,
        command: str,
        params: Mapping[str, Any] | None = None,
        *,
        request_id: str | None = None,
    ) -> dict:
        result = await self._execute_with_driver(device_id, command, params)
        audit_event_id = await self._record_audit_event(result, params=params, request_id=request_id)
        if audit_event_id:
            result = DeviceCommandResult(
                device_id=result.device_id,
                command=result.command,
                status=result.status,
                driver_id=result.driver_id,
                message=result.message,
                result=result.result,
                audit_event_id=audit_event_id,
                metadata=result.metadata,
            )
        return result.to_dict()

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
    ) -> dict:
        for driver in self._drivers:
            report = getattr(driver, "report_state", None)
            if report is None:
                continue
            state = await report(
                device_id,
                status=status,
                name=name,
                kind=kind,
                capabilities=capabilities,
                battery=battery,
                metadata=metadata,
            )
            if state is not None:
                return {"ok": True, "device": state.to_dict()}
        return {"ok": False, "error": "state_report_not_supported", "device_id": device_id}

    async def _execute_with_driver(
        self,
        device_id: str,
        command: str,
        params: Mapping[str, Any] | None,
    ) -> DeviceCommandResult:
        for driver in self._drivers:
            if any(device.device_id == device_id for device in await driver.list_devices()):
                return await driver.execute_command(device_id, command, params)
        return DeviceCommandResult(
            device_id=device_id,
            command=str(command or "").strip(),
            status=DeviceCommandStatus.FAILED,
            message="device_not_found",
            metadata={"driver_checked": [driver.driver_id for driver in self._drivers]},
        )

    async def _record_audit_event(
        self,
        result: DeviceCommandResult,
        *,
        params: Mapping[str, Any] | None,
        request_id: str | None,
    ) -> str | None:
        metadata = {
            "device_id": result.device_id,
            "driver_id": result.driver_id,
            "command": result.command,
            "params": dict(params or {}),
            "status": result.status.value,
            "ok": result.ok,
            "request_id": request_id,
        }
        try:
            event = await self._event_sink(
                source="device",
                namespace="device",
                role="tool",
                content=f"device command {result.command} -> {result.status.value}",
                metadata_json=json.dumps(metadata, ensure_ascii=False),
            )
        except Exception as exc:
            print(f"[DeviceService] audit event skipped: {exc}")
            return None
        return event.get("id") if isinstance(event, dict) else None


device_service = DeviceService()


__all__ = [
    "DeviceDriver",
    "DeviceEventSink",
    "DeviceService",
    "AndroidMobileDeviceDriver",
    "BrowserBridgeDeviceDriver",
    "MockDeviceDriver",
    "RingDeviceDriver",
    "device_service",
]
