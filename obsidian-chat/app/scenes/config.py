"""Scene-only configuration. Physical transports are explicitly opt-in."""

from __future__ import annotations

from copy import deepcopy
import json
import logging
import math
from pathlib import Path
import re

from .hardware import HARDWARE, compiler_capabilities, current_introduction, validate_capabilities


DEFAULT_DEVICES = [
    {"id": "mock_a", "name": "模拟设备 A", "driver": "mock",
     "description": "模拟设备 A：一个独立输出通道。数值只用于软件联调，不代表真实设备能力。",
     "capabilities": {"channels": {"level": {"min": 0, "max": 100}},
                      "max_step_ms": 60000, "max_cycle_ms": 600000}},
    {"id": "mock_b", "name": "模拟设备 B", "driver": "mock",
     "description": "模拟设备 B：两个可独立变化的输出通道。仅在界面显示，不连接真实设备。",
     "capabilities": {"channels": {"left": {"min": 0, "max": 100}, "right": {"min": 0, "max": 100}},
                      "max_step_ms": 60000, "max_cycle_ms": 600000}},
]


def defaults() -> dict:
    return {"enabled": False, "devices": deepcopy(DEFAULT_DEVICES)}


def validate_config(value: dict) -> dict:
    if not isinstance(value, dict) or not isinstance(value.get("enabled"), bool):
        raise ValueError("invalid_scene_config")
    devices = value.get("devices")
    if not isinstance(devices, list) or len(devices) > 16:
        raise ValueError("invalid_scene_devices")
    seen = set()
    transports = set()
    cleaned = []
    for device in devices:
        if not isinstance(device, dict):
            raise ValueError("invalid_scene_device")
        device_id = device.get("id", "")
        if not isinstance(device_id, str) or not re.fullmatch(r"[a-zA-Z0-9_-]{1,64}", device_id) or device_id in seen:
            raise ValueError("invalid_scene_device_id")
        driver = device.get("driver")
        if driver != "mock" and driver not in HARDWARE:
            raise ValueError("scene_driver_unsupported")
        if driver != "mock" and driver in transports:
            raise ValueError("scene_hardware_transport_already_used")
        name = device.get("name", device_id)
        description = device.get("description", "")
        if not isinstance(name, str) or not isinstance(description, str) or len(description) > 8000:
            raise ValueError("invalid_scene_device_description")
        name, description = current_introduction(driver, name, description)
        caps = device.get("capabilities", {})
        channels = caps.get("channels") if isinstance(caps, dict) else None
        if not isinstance(channels, dict) or not 1 <= len(channels) <= 16:
            raise ValueError("invalid_scene_channels")
        for channel, bounds in channels.items():
            if not re.fullmatch(r"[a-zA-Z0-9_]{1,32}", channel) or not isinstance(bounds, dict):
                raise ValueError("invalid_scene_channel")
            lo, hi = bounds.get("min"), bounds.get("max")
            if any(isinstance(n, bool) or not isinstance(n, (int, float)) or not math.isfinite(n) for n in (lo, hi)) or lo > hi:
                raise ValueError("invalid_scene_range")
        for key, maximum in (("max_step_ms", 600000), ("max_cycle_ms", 3600000)):
            number = caps.get(key)
            if type(number) is not int or not 1 <= number <= maximum:
                raise ValueError("invalid_scene_duration")
        if driver != "mock":
            validate_capabilities(driver, caps)
            transports.add(driver)
        seen.add(device_id)
        cleaned.append({"id": device_id, "name": name[:100], "description": description,
                        "driver": driver, "capabilities": compiler_capabilities(driver, caps)})
    return {"enabled": value["enabled"], "devices": cleaned}


class SceneConfig:
    def __init__(self, path: Path | None = None):
        self.path = path

    def _path(self) -> Path:
        if self.path is not None:
            return self.path
        from config import DATA_DIR
        return DATA_DIR / "scenes.json"

    def load(self) -> dict:
        path = self._path()
        if not path.exists():
            return defaults()
        # Invalid configuration is an error, never a silent re-enable/reset.
        return validate_config(json.loads(path.read_text(encoding="utf-8")))

    def save(self, value: dict) -> dict:
        result = validate_config(value)
        path = self._path()
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(".tmp")
        temporary.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(path)
        return result

    def available(self) -> bool:
        try:
            value = self.load()
        except (ValueError, OSError) as exc:
            # A broken optional feature config must not disable the rest of
            # the night round. Direct configuration reads still report it.
            logging.getLogger(__name__).warning("scene option unavailable: %s", type(exc).__name__)
            return False
        return value["enabled"] and bool(value["devices"])


scene_config = SceneConfig()
