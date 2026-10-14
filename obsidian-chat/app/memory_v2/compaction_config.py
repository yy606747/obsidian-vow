"""Independent, reversible memory residency/compaction settings."""
from __future__ import annotations

import json
from pathlib import Path


DEFAULTS = {
    "enabled": False,
    "recent_days": 30,
    "cache_mib": 64,
    "daily_batches": 12,
    "batch_items": 24,
    "batch_chars": 12000,
    "run_hour": 3,
}


def config_path() -> Path:
    from config import DATA_DIR
    return Path(DATA_DIR) / "memory_compaction.json"


def load_config() -> dict:
    try:
        raw = json.loads(config_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raw = {}
    if not isinstance(raw, dict):
        raw = {}
    result = dict(DEFAULTS)
    result["enabled"] = raw.get("enabled") is True
    for key, bounds in {
        "recent_days": (1, 3650), "cache_mib": (1, 1024),
        "daily_batches": (0, 100), "batch_items": (2, 48),
        "batch_chars": (2000, 24000), "run_hour": (0, 23),
    }.items():
        try:
            result[key] = max(bounds[0], min(bounds[1], int(raw.get(key, result[key]))))
        except (TypeError, ValueError):
            pass
    return result


def save_config(updates: dict) -> dict:
    path = config_path()
    data = {**load_config(), **updates}
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)
    return load_config()
