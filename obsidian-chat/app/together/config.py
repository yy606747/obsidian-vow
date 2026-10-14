from __future__ import annotations

import json
from pathlib import Path


DEFAULTS = {
    "enabled": False,
    "heartbeat_seconds": 10,
    "lease_seconds": 45,
    "capture_timeout_seconds": 10,
    "draft_seconds": 600,
    "minimum_wake_seconds": 30,
    "context_messages": 40,
    "context_characters": 16000,
    "retained_messages": 200,
    "image_max_edge": 1280,
    "image_max_bytes": 2 * 1024 * 1024,
    "jpeg_quality": 75,
    "model_timeout_seconds": 240,
    "recording_seconds": 60,
}


class TogetherConfig:
    def __init__(self, path: Path | None = None):
        self.path = path

    def _path(self):
        if self.path is not None:
            return self.path
        from config import DATA_DIR
        return DATA_DIR / "together.json"

    def load(self):
        path = self._path()
        return self.validate(json.loads(path.read_text()) if path.exists() else {})

    @staticmethod
    def validate(value):
        if not isinstance(value, dict) or set(value) - set(DEFAULTS):
            raise ValueError("together_invalid_config")
        result = {**DEFAULTS, **value}
        if type(result["enabled"]) is not bool:
            raise ValueError("together_invalid_enabled")
        for key, default in DEFAULTS.items():
            if key == "enabled":
                continue
            if type(result[key]) is not int or not 1 <= result[key] <= max(default * 4, 100):
                raise ValueError("together_invalid_" + key)
        if (result["lease_seconds"] <= result["heartbeat_seconds"]
                or result["retained_messages"] < result["context_messages"]
                or result["jpeg_quality"] > 100):
            raise ValueError("together_inconsistent_config")
        return result

    def save(self, value):
        result = self.validate(value)
        path = self._path()
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(".tmp")
        temporary.write_text(json.dumps(result, ensure_ascii=False, indent=2))
        temporary.replace(path)
        return result


together_config = TogetherConfig()
