"""Pure source adapter contracts for Sentinel Attention."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any


FORBIDDEN_SOURCE_DECISION_FIELDS = frozenset({
    "call_core",
    "core_reason",
    "score",
    "wake_intent",
})


@dataclass(frozen=True)
class EvidenceRecord:
    kind: str
    source: str
    text: str
    tags: frozenset[str] = frozenset()
    payload: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "payload", dict(self.payload or {}))


@dataclass(frozen=True)
class ReplayEvidenceBundle:
    evidence: tuple[EvidenceRecord, ...]
    recent_chat: tuple[str, ...]
    reference_time: str
    context_tags: frozenset[str] = frozenset()


def require_no_decision_fields(subject: str, payload: Mapping[str, Any]) -> None:
    forbidden = sorted(FORBIDDEN_SOURCE_DECISION_FIELDS.intersection(payload.keys()))
    if forbidden:
        raise ValueError(f"{subject} contains forbidden decision fields: {forbidden!r}")


def frozen_tags(tags: Iterable[str]) -> frozenset[str]:
    return frozenset(tag for tag in tags if tag)


__all__ = [
    "EvidenceRecord",
    "FORBIDDEN_SOURCE_DECISION_FIELDS",
    "ReplayEvidenceBundle",
    "frozen_tags",
    "require_no_decision_fields",
]
