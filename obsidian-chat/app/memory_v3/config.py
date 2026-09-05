"""Configuration contract for the staged Memory V3 rollout."""

from __future__ import annotations

import time
from typing import Any


CONFIG_KEY = "memory_v3"

DEFAULT_MEMORY_V3_CONFIG = {
    "relational_card_generation_enabled": False,
    # A second opt-in prevents deploying a new prompt implementation from
    # silently changing an already-enabled v1 generation lane.
    "relational_card_v2_generation_enabled": False,
    # Set automatically on every combined off -> on transition. Runtime
    # generation is fail-closed while this is zero, so enabling flags can
    # never become an accidental historical backfill.
    "relational_card_generation_cutoff_ts": 0.0,
    "relational_cards_enabled": False,
    "replace_auto_digest": False,
    "card_readout_mode": "current_raw",
    "relational_card_stability_delay_sec": 900.0,
    "relational_card_generation_batch_size": 2,
    "relational_card_generation_attempts": 2,
    "relational_card_failure_retry_delay_sec": 3600.0,
    # Static names/register only.  Do not put dynamic history, personality
    # conclusions, working-model state, or prior cards in this field.
    "relational_card_relationship_register": "",
    "ai_note_lane_enabled": False,
    "ai_note_top_k": 3,
    "ai_note_max_items": 2,
    "pending_recall_enabled": False,
    "pending_full_corpus_enabled": True,
    "pending_candidate_k": 20,
    "pending_candidate_pool_limit": 1000,
    "pending_select_max": 2,
    "pending_retrieval_timeout_sec": 20.0,
    "pending_join_max_wait_sec": 2.0,
    "pending_selector_timeout_sec": 8.0,
    "pending_selector_attempts": 2,
    "timeline_enabled": False,
    "timeline_hours": 72,
    "timeline_max_chars": 600,
    "timeline_generation_min_interval_sec": 900.0,
    "timeline_generation_timeout_sec": 20.0,
    "timeline_generation_attempts": 2,
}

VALID_CARD_READOUT_MODES = ("current_raw", "card_preferred", "raw_full_fallback")


def _as_bool(value: Any, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in {"1", "true", "yes", "on"}:
            return True
        if lowered in {"0", "false", "no", "off"}:
            return False
    return default if value is None else bool(value)


def _as_int(value: Any, default: int, minimum: int, maximum: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = default
    return max(minimum, min(maximum, parsed))


def _as_float(value: Any, default: float, minimum: float, maximum: float) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        parsed = default
    return max(minimum, min(maximum, parsed))


def _as_bounded_text(value: Any, default: str, maximum: int) -> str:
    text = " ".join(str(default if value is None else value).split())
    return text[:maximum]


def normalize_memory_v3_config(raw: dict | None = None) -> dict:
    raw = raw if isinstance(raw, dict) else {}
    config = dict(DEFAULT_MEMORY_V3_CONFIG)
    for key in (
        "relational_card_generation_enabled",
        "relational_card_v2_generation_enabled",
        "relational_cards_enabled",
        "replace_auto_digest",
        "ai_note_lane_enabled",
        "pending_recall_enabled",
        "pending_full_corpus_enabled",
        "timeline_enabled",
    ):
        config[key] = _as_bool(raw.get(key), config[key])

    mode = str(raw.get("card_readout_mode", config["card_readout_mode"])).strip().lower()
    config["card_readout_mode"] = (
        mode if mode in VALID_CARD_READOUT_MODES else config["card_readout_mode"]
    )
    config["pending_candidate_k"] = _as_int(
        raw.get("pending_candidate_k"), config["pending_candidate_k"], 1, 100
    )
    config["pending_candidate_pool_limit"] = _as_int(
        raw.get("pending_candidate_pool_limit"),
        config["pending_candidate_pool_limit"],
        20,
        5000,
    )
    config["pending_select_max"] = _as_int(
        raw.get("pending_select_max"), config["pending_select_max"], 0, 5
    )
    config["relational_card_stability_delay_sec"] = round(
        _as_float(
            raw.get("relational_card_stability_delay_sec"),
            config["relational_card_stability_delay_sec"],
            0.0,
            86400.0,
        ),
        3,
    )
    config["relational_card_generation_batch_size"] = _as_int(
        raw.get("relational_card_generation_batch_size"),
        config["relational_card_generation_batch_size"],
        1,
        20,
    )
    config["relational_card_generation_cutoff_ts"] = round(
        _as_float(
            raw.get("relational_card_generation_cutoff_ts"),
            config["relational_card_generation_cutoff_ts"],
            0.0,
            4102444800.0,
        ),
        3,
    )
    config["relational_card_generation_attempts"] = _as_int(
        raw.get("relational_card_generation_attempts"),
        config["relational_card_generation_attempts"],
        1,
        3,
    )
    config["relational_card_failure_retry_delay_sec"] = round(
        _as_float(
            raw.get("relational_card_failure_retry_delay_sec"),
            config["relational_card_failure_retry_delay_sec"],
            60.0,
            86400.0,
        ),
        3,
    )
    config["relational_card_relationship_register"] = _as_bounded_text(
        raw.get("relational_card_relationship_register"),
        config["relational_card_relationship_register"],
        400,
    )
    config["ai_note_top_k"] = _as_int(
        raw.get("ai_note_top_k"), config["ai_note_top_k"], 0, 20
    )
    config["ai_note_max_items"] = _as_int(
        raw.get("ai_note_max_items"), config["ai_note_max_items"], 0, 5
    )
    config["pending_retrieval_timeout_sec"] = round(
        _as_float(
            raw.get("pending_retrieval_timeout_sec"),
            config["pending_retrieval_timeout_sec"],
            1.0,
            60.0,
        ),
        3,
    )
    config["pending_join_max_wait_sec"] = round(
        _as_float(
            raw.get("pending_join_max_wait_sec"),
            config["pending_join_max_wait_sec"],
            0.0,
            10.0,
        ),
        3,
    )
    config["pending_selector_timeout_sec"] = round(
        _as_float(
            raw.get("pending_selector_timeout_sec"),
            config["pending_selector_timeout_sec"],
            1.0,
            30.0,
        ),
        3,
    )
    config["pending_selector_attempts"] = _as_int(
        raw.get("pending_selector_attempts"),
        config["pending_selector_attempts"],
        1,
        2,
    )
    config["timeline_hours"] = _as_int(
        raw.get("timeline_hours"), config["timeline_hours"], 24, 168
    )
    config["timeline_max_chars"] = _as_int(
        raw.get("timeline_max_chars"), config["timeline_max_chars"], 300, 2000
    )
    config["timeline_generation_min_interval_sec"] = round(
        _as_float(
            raw.get("timeline_generation_min_interval_sec"),
            config["timeline_generation_min_interval_sec"],
            0.0,
            86400.0,
        ),
        3,
    )
    config["timeline_generation_timeout_sec"] = round(
        _as_float(
            raw.get("timeline_generation_timeout_sec"),
            config["timeline_generation_timeout_sec"],
            1.0,
            60.0,
        ),
        3,
    )
    config["timeline_generation_attempts"] = _as_int(
        raw.get("timeline_generation_attempts"),
        config["timeline_generation_attempts"],
        1,
        2,
    )
    return config


def load_memory_v3_config() -> dict:
    from config import SETTINGS

    return normalize_memory_v3_config(SETTINGS.get(CONFIG_KEY))


def save_memory_v3_config(updates: dict) -> dict:
    from config import SETTINGS, save_settings

    current = SETTINGS.get(CONFIG_KEY)
    before = normalize_memory_v3_config(current)
    merged = dict(current) if isinstance(current, dict) else {}
    merged.update(updates if isinstance(updates, dict) else {})
    normalized = normalize_memory_v3_config(merged)
    was_active = bool(
        before["relational_card_generation_enabled"]
        and before["relational_card_v2_generation_enabled"]
    )
    is_active = bool(
        normalized["relational_card_generation_enabled"]
        and normalized["relational_card_v2_generation_enabled"]
    )
    if not was_active and is_active:
        # Every off -> on transition starts a fresh new-chunk-only window.
        # Historical backfill requires a separate explicit cutoff update while
        # the lane is already active.
        normalized["relational_card_generation_cutoff_ts"] = round(time.time(), 3)
    SETTINGS[CONFIG_KEY] = normalized
    save_settings(SETTINGS)
    return normalized
