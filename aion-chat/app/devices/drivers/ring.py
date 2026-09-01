"""Smart ring device driver."""

from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, Mapping

from app.devices.gates.ring_touch import TTL_SEC, RingTouchGate
from app.devices.schemas import DeviceCommandResult, DeviceCommandStatus, DeviceState, DeviceStatus


class RingDeviceDriver:
    driver_id = "smart_ring"

    def __init__(
        self,
        *,
        event_sink: Callable[..., Awaitable[dict]],
        ws_sender: Callable[[str, Mapping[str, Any]], Awaitable[bool]],
        settings_reader: Callable[[], bool],
        quiet_hours_reader: Callable[[], bool] | None = None,
        name_prefix_reader: Callable[[], str] | None = None,
        keep_connected_reader: Callable[[], bool] | None = None,
        terminal_sink: Callable[..., Awaitable[Any]] | None = None,
        now: Callable[[], float] | None = None,
        ack_timeout_sec: float = 30.0,
    ):
        self._event_sink = event_sink
        self._ws_sender = ws_sender
        self._name_prefix_reader = name_prefix_reader or (lambda: "AIZO")
        self._keep_connected_reader = keep_connected_reader or (lambda: False)
        self._terminal_sink = terminal_sink
        self._now = now or time.time
        self._ack_timeout_sec = ack_timeout_sec
        self._seq = 0
        self._pending: dict[str, dict[str, Any]] = {}
        self._state = DeviceState(
            device_id="smart_ring",
            name="AIZO Ring",
            kind="wearable",
            status=DeviceStatus.OFFLINE,
            driver_id=self.driver_id,
            capabilities=("ring.touch", "ring.status"),
            metadata={"device_type": self.driver_id, "source": "android_bridge"},
        )
        self._touch_gate = RingTouchGate(
            settings_reader=settings_reader,
            device_status_reader=lambda: self._state.status,
            quiet_hours_reader=quiet_hours_reader,
            now=self._now,
        )

    async def list_devices(self) -> list[DeviceState]:
        return [self._state]

    async def report_state(
        self,
        device_id,
        *,
        status,
        name=None,
        kind=None,
        capabilities=None,
        battery=None,
        metadata=None,
    ) -> DeviceState | None:
        if device_id != self._state.device_id:
            return None
        meta = {**dict(self._state.metadata), **dict(metadata or {}), "source": "android_bridge"}
        self._state = DeviceState(
            device_id=device_id,
            name=name or self._state.name,
            kind=kind or self._state.kind,
            status=DeviceStatus(status),
            driver_id=self.driver_id,
            capabilities=tuple(capabilities or self._state.capabilities),
            battery=battery,
            last_seen_at=self._now(),
            metadata=meta,
        )
        return self._state

    async def execute_command(
        self,
        device_id: str,
        command: str,
        params: Mapping[str, Any] | None = None,
    ) -> DeviceCommandResult:
        await self.sweep_timeouts()
        command = str(command or "").strip()
        if device_id != self._state.device_id:
            return self._result(device_id, command, DeviceCommandStatus.FAILED, "device_not_found")
        if command == "ping":
            return self._ping(device_id, command)
        if command == "connect":
            return await self._execute_connect(device_id, command, dict(params or {}))
        if command == "keepalive":
            return await self._execute_keepalive(device_id, command, dict(params or {}))
        if command != "touch":
            return self._result(device_id, command, DeviceCommandStatus.FAILED, "unsupported_command")
        return await self._execute_touch(device_id, dict(params or {}))

    async def handle_ack(self, data: Mapping[str, Any] | None) -> None:
        data = dict(data or {})
        request_id = str(data.get("request_id") or "").strip()
        pending = self._pending.pop(request_id, None)
        if not pending:
            return
        status = str(data.get("status") or "failed").strip()
        event = "ring_touch.skipped_stale" if status == "skipped_stale" else f"ring_touch.{status}"
        if event not in {"ring_touch.executed", "ring_touch.skipped_stale"}:
            event = "ring_touch.failed"
        await self._write_touch_event(event, request_id=request_id, ble_result=status, **pending)

    async def sweep_timeouts(self) -> None:
        now = self._now()
        expired = [
            rid for rid, item in self._pending.items()
            if now - item["queued_at"] > self._ack_timeout_sec
        ]
        for request_id in expired:
            pending = self._pending.pop(request_id)
            await self._write_touch_event(
                "ring_touch.timeout",
                request_id=request_id,
                ble_result="timeout",
                **pending,
            )

    def _ping(self, device_id: str, command: str) -> DeviceCommandResult:
        return self._result(
            device_id,
            command,
            DeviceCommandStatus.EXECUTED,
            "pong",
            {
                "status": self._state.status.value,
                "battery": self._state.battery,
                "last_seen_at": self._state.last_seen_at,
            },
        )

    async def _execute_connect(
        self,
        device_id: str,
        command: str,
        params: dict[str, Any],
    ) -> DeviceCommandResult:
        request_id = self._request_id(params)
        payload = {"type": "ring_connect_request", "data": self._base_payload(params, request_id)}
        if not await self._ws_sender(self.driver_id, payload):
            return self._result(device_id, command, DeviceCommandStatus.FAILED, "phone_ws_unavailable")
        return self._result(
            device_id,
            command,
            DeviceCommandStatus.QUEUED,
            "ring_connect_queued",
            {"queued": True, "request_id": request_id},
        )

    async def _execute_keepalive(
        self,
        device_id: str,
        command: str,
        params: dict[str, Any],
    ) -> DeviceCommandResult:
        request_id = self._request_id(params)
        payload = {"type": "ring_keepalive_request", "data": self._base_payload(params, request_id)}
        if not await self._ws_sender(self.driver_id, payload):
            return self._result(device_id, command, DeviceCommandStatus.FAILED, "phone_ws_unavailable")
        return self._result(
            device_id,
            command,
            DeviceCommandStatus.QUEUED,
            "ring_keepalive_queued",
            {"queued": True, "request_id": request_id},
        )

    async def _execute_touch(self, device_id: str, params: dict[str, Any]) -> DeviceCommandResult:
        request_id = self._request_id(params)
        params.setdefault("_ring_request_id", request_id)
        params.setdefault("_ring_created_at", self._now())
        haptics = params.get("haptics") if isinstance(params.get("haptics"), Mapping) else {}
        gate = await self._touch_gate.check(params)
        if not gate.passed:
            event = "ring_touch.skipped_stale" if gate.reason == "skipped_stale" else "ring_touch.failed"
            await self._write_touch_event(
                event,
                request_id=request_id,
                params=params,
                clamped_haptics={},
                gate_result=gate.reason,
                ble_result="not_sent",
                queued_at=self._now(),
            )
            return self._result(
                device_id,
                "touch",
                DeviceCommandStatus.SKIPPED,
                gate.reason,
                metadata={"request_id": request_id},
            )

        clamped = self._touch_gate.clamp(haptics)
        payload_data = {
            **self._base_payload(params, request_id),
            "touch": str(params.get("touch") or ""),
            **clamped,
            "expires_at": _iso_ts(float(params["_ring_created_at"]) + TTL_SEC),
        }
        if not await self._ws_sender(self.driver_id, {"type": "ring_touch_request", "data": payload_data}):
            await self._write_touch_event(
                "ring_touch.failed",
                request_id=request_id,
                params=params,
                clamped_haptics=clamped,
                gate_result="passed",
                ble_result="ws_unavailable",
                queued_at=self._now(),
            )
            return self._result(device_id, "touch", DeviceCommandStatus.FAILED, "phone_ws_unavailable")

        pending = {
            "params": params,
            "clamped_haptics": clamped,
            "gate_result": "passed",
            "queued_at": self._now(),
        }
        self._pending[request_id] = pending
        await self._write_touch_event("ring_touch.queued", request_id=request_id, ble_result="queued", **pending)
        return self._result(
            device_id,
            "touch",
            DeviceCommandStatus.QUEUED,
            "ring_touch_queued",
            {"queued": True, "request_id": request_id, **clamped},
            metadata={"request_id": request_id, "gate_result": "passed"},
        )

    def _base_payload(self, params: Mapping[str, Any], request_id: str) -> dict[str, str]:
        prefix = str(params.get("name_prefix") or self._name_prefix_reader() or "AIZO").strip() or "AIZO"
        keep_connected = bool(params.get("keep_connected") or self._keep_connected_reader())
        return {"request_id": request_id, "name_prefix": prefix, "keep_connected": keep_connected}

    def _request_id(self, params: Mapping[str, Any]) -> str:
        value = str(params.get("_ring_request_id") or "").strip()
        if value:
            return value
        self._seq += 1
        return f"ring_{int(self._now() * 1000)}_{self._seq}"

    async def _write_touch_event(
        self,
        event,
        *,
        request_id,
        params,
        clamped_haptics,
        gate_result,
        ble_result,
        queued_at,
    ) -> None:
        metadata = {
            "event": event,
            "request_id": request_id,
            "wake_id": params.get("_ring_wake_id"),
            "device_id": self._state.device_id,
            "touch": params.get("touch"),
            "reason": params.get("reason"),
            "ai_haptics": dict(params.get("haptics") or {}),
            "clamped_haptics": dict(clamped_haptics or {}),
            "gate_result": gate_result,
            "ble_result": ble_result,
            "created_at": params.get("_ring_created_at"),
            "queued_at": queued_at,
        }
        try:
            await self._event_sink(
                source="device",
                namespace="ring_touch",
                role="tool",
                content=event,
                metadata_json=json.dumps(metadata, ensure_ascii=False),
            )
        except Exception as exc:
            print(f"[RingDeviceDriver] touch event skipped: {exc}")
        if self._terminal_sink is not None and event != "ring_touch.queued":
            outcome = {
                "ring_touch.executed": "succeeded",
                "ring_touch.skipped_stale": "rejected",
                "ring_touch.failed": "failed",
                "ring_touch.timeout": "failed",
            }.get(str(event), "unknown")
            try:
                await self._terminal_sink(
                    correlation_id=request_id,
                    outcome=outcome,
                    event_type=event,
                    error="" if outcome == "succeeded" else str(ble_result or event),
                    result=metadata,
                )
            except Exception as exc:
                print(f"[RingDeviceDriver] terminal ledger update skipped: {exc}")

    def _result(self, device_id, command, status, message, result=None, metadata=None) -> DeviceCommandResult:
        return DeviceCommandResult(
            device_id=device_id,
            command=command,
            status=status,
            driver_id=self.driver_id,
            message=message,
            result=result,
            metadata=metadata or {},
        )


def _iso_ts(timestamp: float) -> str:
    return datetime.fromtimestamp(timestamp, timezone.utc).isoformat()
