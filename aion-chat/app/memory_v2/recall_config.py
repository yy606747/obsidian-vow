"""Memory V2 recall rollout configuration."""

from __future__ import annotations

import hashlib
from typing import Any


CONFIG_KEY = "memory_v2_recall"
VALID_MODES = ("legacy", "shadow", "debug", "canary", "full")

DEFAULT_RECALL_CONFIG = {
    "mode": "full",
    "top_k": 8,
    "candidate_limit": 1000,
    "include_trace": False,
    "canary_ratio": 0.0,
    "prompt_min_score": 0.18,
}

LEGACY_RECALL_CONFIG = {
    **DEFAULT_RECALL_CONFIG,
    "mode": "legacy",
}


def _clamp_int(value: Any, default: int, minimum: int, maximum: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = default
    return max(minimum, min(maximum, parsed))


def _clamp_float(value: Any, default: float, minimum: float, maximum: float) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        parsed = default
    return max(minimum, min(maximum, parsed))


def _as_bool(value: Any, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in {"1", "true", "yes", "on"}:
            return True
        if lowered in {"0", "false", "no", "off"}:
            return False
    if value is None:
        return default
    return bool(value)


def normalize_recall_config(raw: dict | None = None) -> dict:
    raw = raw if isinstance(raw, dict) else {}
    config = dict(DEFAULT_RECALL_CONFIG)
    mode = str(raw.get("mode", config["mode"])).strip().lower()
    config["mode"] = mode if mode in VALID_MODES else DEFAULT_RECALL_CONFIG["mode"]
    config["top_k"] = _clamp_int(raw.get("top_k"), config["top_k"], 1, 20)
    config["candidate_limit"] = _clamp_int(
        raw.get("candidate_limit"),
        config["candidate_limit"],
        1,
        2000,
    )
    config["include_trace"] = _as_bool(raw.get("include_trace"), config["include_trace"])
    config["canary_ratio"] = round(
        _clamp_float(raw.get("canary_ratio"), config["canary_ratio"], 0.0, 1.0),
        4,
    )
    config["prompt_min_score"] = round(
        _clamp_float(raw.get("prompt_min_score"), config["prompt_min_score"], 0.0, 1.0),
        4,
    )
    return config


def recall_runtime(config: dict | None = None) -> dict:
    config = normalize_recall_config(config)
    mode = config["mode"]
    v2_enabled = mode != "legacy"
    include_trace = config["include_trace"] or mode in {"debug", "canary", "full"}
    prompt_block_enabled = mode in {"debug", "canary", "full"}
    prompt_injection_enabled = mode == "full" or (
        mode == "canary" and config["canary_ratio"] > 0
    )
    notes = []
    effective_mode = mode
    if mode == "legacy":
        notes.append("V2 recall is disabled; legacy recall remains the only prompt path.")
    elif mode == "shadow":
        notes.append("V2 recall runs for debug summary only; prompt still uses legacy recall.")
    elif mode == "debug":
        notes.append("V2 recall returns trace and prompt block preview; prompt still uses legacy recall.")
    elif mode == "canary":
        notes.append("V2 prompt block may be injected for deterministic canary turns only.")
    elif mode == "full":
        notes.append("V2 prompt block is injected when the builder returns selected memories.")
    return {
        "mode": mode,
        "effective_mode": effective_mode,
        "v2_enabled": v2_enabled,
        "include_trace": include_trace,
        "prompt_block_enabled": prompt_block_enabled,
        "prompt_injection_enabled": prompt_injection_enabled,
        "top_k": config["top_k"],
        "candidate_limit": config["candidate_limit"],
        "canary_ratio": config["canary_ratio"],
        "prompt_min_score": config["prompt_min_score"],
        "notes": notes,
    }


def prompt_injection_decision(config: dict | None = None, *, seed: str = "") -> dict:
    config = normalize_recall_config(config)
    runtime = recall_runtime(config)
    mode = runtime["mode"]
    if not runtime["v2_enabled"]:
        return {"inject": False, "reason": "v2_disabled", "mode": mode}
    if not runtime["prompt_block_enabled"]:
        return {"inject": False, "reason": "prompt_block_disabled", "mode": mode}
    if mode == "debug":
        return {"inject": False, "reason": "debug_preview_only", "mode": mode}
    if mode == "full":
        return {"inject": True, "reason": "full", "mode": mode}
    if mode == "canary":
        ratio = runtime["canary_ratio"]
        if ratio <= 0:
            return {"inject": False, "reason": "canary_ratio_zero", "mode": mode}
        digest = hashlib.sha1(str(seed or "").encode("utf-8", errors="ignore")).hexdigest()
        bucket = int(digest[:8], 16) / 0xFFFFFFFF
        return {
            "inject": bucket < ratio,
            "reason": "canary_selected" if bucket < ratio else "canary_skipped",
            "mode": mode,
            "canary_ratio": ratio,
            "bucket": round(bucket, 6),
        }
    return {"inject": False, "reason": "unsupported_mode", "mode": mode}


def load_recall_config() -> dict:
    from config import SETTINGS

    return normalize_recall_config(SETTINGS.get(CONFIG_KEY))


def merge_recall_config(current: dict | None, updates: dict | None = None) -> dict:
    """Merge config updates, with one-key legacy rollback resetting rollout knobs."""
    updates = updates if isinstance(updates, dict) else {}
    if updates.get("mode") == "legacy" and set(updates.keys()) == {"mode"}:
        return dict(LEGACY_RECALL_CONFIG)
    merged = dict(normalize_recall_config(current))
    merged.update(updates)
    return normalize_recall_config(merged)


def save_recall_config(updates: dict | None = None) -> dict:
    from config import SETTINGS, save_settings

    current = load_recall_config()
    normalized = merge_recall_config(current, updates)
    SETTINGS[CONFIG_KEY] = normalized
    save_settings(SETTINGS)
    return normalized


def describe_recall_config() -> dict:
    config = load_recall_config()
    return {
        "config": config,
        "runtime": recall_runtime(config),
        "valid_modes": list(VALID_MODES),
    }
