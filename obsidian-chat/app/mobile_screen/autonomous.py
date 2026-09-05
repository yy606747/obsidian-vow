"""Autonomous mobile screen-check eligibility.

This is intentionally stricter than reactive chat requests. A spontaneous
mobile screenshot prompt should only be offered when the backend can lock a
single active target without asking the model to guess.
"""

from __future__ import annotations

import time
from typing import Any, Mapping

from activity import read_recent_activity
from app.devices import DeviceStatus, device_service
from app.pc_screen.service import model_supports_vision

from . import mobile_screen_service


AUTONOMOUS_SCREEN_ACTIVE_SEC = 6 * 60
AUTONOMOUS_MOBILE_SCREEN_COOLDOWN_SEC = 3 * 60 * 60

_SCREEN_ON_APPS = {"screen_on", "亮屏"}
_SCREEN_OFF_APPS = {"screen_off", "锁屏"}
_last_autonomous_mobile_screen_at = 0.0


def autonomous_mobile_screen_cooldown_active(now: float | None = None) -> bool:
    reference = time.time() if now is None else float(now)
    return (
        _last_autonomous_mobile_screen_at > 0
        and reference - _last_autonomous_mobile_screen_at < AUTONOMOUS_MOBILE_SCREEN_COOLDOWN_SEC
    )


def record_autonomous_mobile_screen_request(now: float | None = None) -> None:
    global _last_autonomous_mobile_screen_at
    _last_autonomous_mobile_screen_at = time.time() if now is None else float(now)


async def autonomous_mobile_screen_target(
    *,
    model_key: str,
    now: float | None = None,
    ignore_cooldown: bool = False,
) -> dict[str, str] | None:
    """Return the only safe spontaneous mobile target, otherwise None."""
    reference = time.time() if now is None else float(now)
    if not mobile_screen_service.is_enabled():
        return None
    if not model_supports_vision(model_key):
        return None
    if not ignore_cooldown and autonomous_mobile_screen_cooldown_active(reference):
        return None

    driver = device_service.get_driver("android_mobile")
    if driver is None:
        return None

    candidates = []
    for device in await driver.list_devices():
        if device.status is not DeviceStatus.ONLINE:
            continue
        if "screen.capture" not in device.capabilities:
            continue
        if not bool(device.metadata.get("screen_agent_online")):
            continue
        if not mobile_device_screen_active(device.device_id, now=reference):
            continue
        candidates.append(device)

    if len(candidates) != 1:
        return None

    device = candidates[0]
    device_type = str(device.metadata.get("device_type") or "").strip()
    label = _device_label(device.name, device_type)
    return {
        "device_id": device.device_id,
        "device_name": device.name,
        "device_type": device_type,
        "label": label,
    }


def mobile_device_screen_active(device_id: str, *, now: float | None = None) -> bool:
    """Derive current-ish screen activity from per-device activity reports."""
    device_id = str(device_id or "").strip()
    if not device_id:
        return False
    reference = time.time() if now is None else float(now)
    cutoff = reference - AUTONOMOUS_SCREEN_ACTIVE_SEC
    latest: Mapping[str, Any] | None = None

    for entry in read_recent_activity(hours=1):
        if str(entry.get("device_id") or "") != device_id:
            continue
        ts = entry.get("timestamp")
        if not isinstance(ts, (int, float)) or ts > reference:
            continue
        if latest is None or ts > float(latest.get("timestamp") or 0):
            latest = entry

    if latest is None:
        return False

    app = str(latest.get("app") or "").strip()
    ts = float(latest.get("timestamp") or 0)
    if app in _SCREEN_OFF_APPS:
        return False
    if ts < cutoff:
        return False
    return bool(app) or app in _SCREEN_ON_APPS


def _device_label(name: str, device_type: str) -> str:
    if str(name or "").strip():
        return str(name).strip()
    normalized = str(device_type or "").strip().lower()
    if normalized == "tablet":
        return "平板"
    if normalized == "phone":
        return "手机"
    return "这台移动设备"


def _reset_autonomous_state_for_tests() -> None:
    global _last_autonomous_mobile_screen_at
    _last_autonomous_mobile_screen_at = 0.0


__all__ = [
    "AUTONOMOUS_MOBILE_SCREEN_COOLDOWN_SEC",
    "AUTONOMOUS_SCREEN_ACTIVE_SEC",
    "autonomous_mobile_screen_cooldown_active",
    "autonomous_mobile_screen_target",
    "mobile_device_screen_active",
    "record_autonomous_mobile_screen_request",
]
