"""Gate and clamp rules for smart-ring touch."""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, Callable, Mapping

from app.devices.schemas import DeviceStatus

MAX_TAPS = 10
MIN_INTERVAL_MS = 1000
MAX_INTERVAL_MS = 5000
DEFAULT_INTERVAL_MS = 2000
DEFAULT_TAPS = 1
TTL_SEC = 60
DEDUP_WINDOW_SEC = 300
RATE_WINDOW_SEC = 600
MAX_TOUCHES_PER_WINDOW = 10
VIBRATION_MS = 500
MAX_TOTAL_MS = 20000


@dataclass(frozen=True)
class GateResult:
    passed: bool
    reason: str = "passed"


class RingTouchGate:
    def __init__(
        self,
        *,
        settings_reader: Callable[[], bool],
        device_status_reader: Callable[[], DeviceStatus | str],
        quiet_hours_reader: Callable[[], bool] | None = None,
        now: Callable[[], float] | None = None,
    ):
        self._settings_reader = settings_reader
        self._device_status_reader = device_status_reader
        self._quiet_hours_reader = quiet_hours_reader or (lambda: False)
        self._now = now or time.time
        self._recent_request_ids: dict[str, float] = {}
        self._recent_touches: list[float] = []

    async def check(self, params: Mapping[str, Any] | None) -> GateResult:
        params = dict(params or {})
        now = self._now()
        self._prune(now)
        if not bool(self._settings_reader()):
            return GateResult(False, "disabled")
        if bool(self._quiet_hours_reader()):
            return GateResult(False, "quiet_hours")
        request_id = str(params.get("_ring_request_id") or "").strip()
        if request_id and request_id in self._recent_request_ids:
            return GateResult(False, "duplicate_request")
        created_at = _to_float(params.get("_ring_created_at"), now)
        if created_at + TTL_SEC < now:
            if request_id:
                self._recent_request_ids[request_id] = now
            return GateResult(False, "skipped_stale")
        if len(self._recent_touches) >= MAX_TOUCHES_PER_WINDOW:
            return GateResult(False, "rate_limited")
        try:
            status = DeviceStatus(self._device_status_reader())
        except ValueError:
            status = DeviceStatus.OFFLINE
        if status is not DeviceStatus.ONLINE:
            return GateResult(False, "device_offline")
        if request_id:
            self._recent_request_ids[request_id] = now
        self._recent_touches.append(now)
        return GateResult(True)

    def clamp(self, haptics: Mapping[str, Any] | None) -> dict[str, int]:
        haptics = dict(haptics or {})
        taps = _clamp(_to_int(haptics.get("taps"), DEFAULT_TAPS), 1, MAX_TAPS)
        interval = _clamp(
            _to_int(haptics.get("interval_ms"), DEFAULT_INTERVAL_MS),
            MIN_INTERVAL_MS,
            MAX_INTERVAL_MS,
        )
        while taps > 1 and _total_duration_ms(taps, interval) > MAX_TOTAL_MS:
            taps -= 1
        return {"taps": taps, "interval_ms": interval, "alert_type": 5}

    def _prune(self, now: float) -> None:
        request_cutoff = now - DEDUP_WINDOW_SEC
        touch_cutoff = now - RATE_WINDOW_SEC
        self._recent_request_ids = {
            key: ts for key, ts in self._recent_request_ids.items() if ts >= request_cutoff
        }
        self._recent_touches = [ts for ts in self._recent_touches if ts >= touch_cutoff]


def _to_float(value: Any, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _to_int(value: Any, default: int) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return default


def _clamp(value: int, low: int, high: int) -> int:
    return max(low, min(high, value))


def _total_duration_ms(taps: int, interval_ms: int) -> int:
    return taps * VIBRATION_MS + max(0, taps - 1) * interval_ms


__all__ = ["GateResult", "RingTouchGate"]
