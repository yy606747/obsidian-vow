"""Pure handoff contract from Attention snapshots to Sentinel judgment."""

from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
from typing import Any

from .eval import (
    ATTENTION_SNAPSHOT_SCHEMA_VERSION,
    FORBIDDEN_LAYER1_DECISION_FIELDS,
)


LAYER2_HANDOFF_SCHEMA_VERSION = "sentinel_layer2_handoff.v0"
LAYER2_HANDOFF_SOURCE_FIELDS = (
    "compact_text",
    "world_state",
    "hypotheses",
    "attention_targets",
    "suggested_next_check_sec",
)
LAYER2_HANDOFF_ALLOWED_FIELDS = frozenset((
    "schema_version",
    "attention_schema_version",
    *LAYER2_HANDOFF_SOURCE_FIELDS,
))


def build_layer2_handoff(snapshot: Mapping[str, Any]) -> dict[str, Any]:
    """Return the prompt-facing Attention payload for future Sentinel judgment."""
    if not isinstance(snapshot, Mapping):
        raise ValueError("attention snapshot must be an object")

    forbidden = sorted(FORBIDDEN_LAYER1_DECISION_FIELDS.intersection(snapshot.keys()))
    if forbidden:
        raise ValueError(f"attention snapshot contains forbidden decision fields: {forbidden!r}")

    attention_schema_version = snapshot.get("schema_version")
    if attention_schema_version != ATTENTION_SNAPSHOT_SCHEMA_VERSION:
        raise ValueError(
            "attention snapshot schema_version must be "
            f"{ATTENTION_SNAPSHOT_SCHEMA_VERSION!r}"
        )

    missing = sorted(field for field in LAYER2_HANDOFF_SOURCE_FIELDS if field not in snapshot)
    if missing:
        raise ValueError(f"attention snapshot missing handoff fields: {missing!r}")

    _validate_source_fields(snapshot)
    return {
        "schema_version": LAYER2_HANDOFF_SCHEMA_VERSION,
        "attention_schema_version": attention_schema_version,
        "compact_text": snapshot["compact_text"],
        "world_state": deepcopy(snapshot["world_state"]),
        "hypotheses": deepcopy(snapshot["hypotheses"]),
        "attention_targets": deepcopy(snapshot["attention_targets"]),
        "suggested_next_check_sec": snapshot["suggested_next_check_sec"],
    }


def _validate_source_fields(snapshot: Mapping[str, Any]) -> None:
    compact_text = snapshot.get("compact_text")
    if not isinstance(compact_text, str) or not compact_text.strip():
        raise ValueError("attention snapshot compact_text is required")
    if not isinstance(snapshot.get("world_state"), Mapping):
        raise ValueError("attention snapshot world_state must be an object")
    if not isinstance(snapshot.get("hypotheses"), list):
        raise ValueError("attention snapshot hypotheses must be a list")
    if not _is_text_list(snapshot.get("attention_targets")):
        raise ValueError("attention snapshot attention_targets must be a text list")

    suggested_next_check_sec = snapshot.get("suggested_next_check_sec")
    if isinstance(suggested_next_check_sec, bool) or not isinstance(suggested_next_check_sec, int):
        raise ValueError("attention snapshot suggested_next_check_sec must be an integer")


def _is_text_list(value: Any) -> bool:
    return isinstance(value, list) and all(
        isinstance(item, str) and item.strip()
        for item in value
    )


__all__ = [
    "LAYER2_HANDOFF_ALLOWED_FIELDS",
    "LAYER2_HANDOFF_SCHEMA_VERSION",
    "LAYER2_HANDOFF_SOURCE_FIELDS",
    "build_layer2_handoff",
]
