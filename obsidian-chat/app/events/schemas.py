"""Evidence contracts shared by future backend modules."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping


def _as_dict(value: Mapping[str, Any] | None) -> dict[str, Any]:
    return dict(value or {})


def _clean_text(value: str, *, field_name: str) -> str:
    text = str(value or "").strip()
    if not text:
        raise ValueError(f"{field_name} is required")
    return text


def _confidence(value: float | int | None) -> float:
    if value is None:
        return 1.0
    confidence = float(value)
    if confidence < 0.0 or confidence > 1.0:
        raise ValueError("confidence must be between 0 and 1")
    return confidence


@dataclass(frozen=True)
class EvidenceRecord:
    """A normalized real-time fact candidate for sentinel/location/control."""

    id: str
    kind: str
    source: str
    observed_at: float
    received_at: float
    confidence: float = 1.0
    payload: Mapping[str, Any] = field(default_factory=dict)
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self):
        object.__setattr__(self, "id", _clean_text(self.id, field_name="id"))
        object.__setattr__(self, "kind", _clean_text(self.kind, field_name="kind"))
        object.__setattr__(self, "source", _clean_text(self.source, field_name="source"))
        object.__setattr__(self, "observed_at", float(self.observed_at))
        object.__setattr__(self, "received_at", float(self.received_at))
        object.__setattr__(self, "confidence", _confidence(self.confidence))
        object.__setattr__(self, "payload", _as_dict(self.payload))
        object.__setattr__(self, "metadata", _as_dict(self.metadata))

    def freshness_at(self, reference_time: float | None = None) -> float:
        reference = self.received_at if reference_time is None else float(reference_time)
        return max(0.0, reference - self.observed_at)

    def received_delay_sec(self) -> float:
        return max(0.0, self.received_at - self.observed_at)

    def is_future(self, reference_time: float, *, tolerance_sec: float = 0.0) -> bool:
        return self.observed_at > float(reference_time) + float(tolerance_sec)

    def is_stale(self, reference_time: float, *, max_age_sec: float) -> bool:
        return self.freshness_at(reference_time) > float(max_age_sec)

    def to_dict(self, *, reference_time: float | None = None) -> dict[str, Any]:
        reference = self.received_at if reference_time is None else float(reference_time)
        return {
            "id": self.id,
            "kind": self.kind,
            "source": self.source,
            "observed_at": self.observed_at,
            "received_at": self.received_at,
            "freshness_sec": self.freshness_at(reference),
            "received_delay_sec": self.received_delay_sec(),
            "confidence": self.confidence,
            "payload": dict(self.payload),
            "metadata": dict(self.metadata),
            "future": self.is_future(reference),
        }


@dataclass(frozen=True)
class EvidenceSnapshot:
    """A filtered, explainable view of evidence used by a decision module."""

    generated_at: float
    records: tuple[EvidenceRecord, ...] = ()
    max_age_sec: float | None = None

    def __post_init__(self):
        object.__setattr__(self, "generated_at", float(self.generated_at))
        object.__setattr__(self, "records", tuple(self.records or ()))
        if self.max_age_sec is not None:
            object.__setattr__(self, "max_age_sec", float(self.max_age_sec))

    def to_dict(self) -> dict[str, Any]:
        source_counts: dict[str, int] = {}
        kind_counts: dict[str, int] = {}
        for record in self.records:
            source_counts[record.source] = source_counts.get(record.source, 0) + 1
            kind_counts[record.kind] = kind_counts.get(record.kind, 0) + 1
        return {
            "generated_at": self.generated_at,
            "max_age_sec": self.max_age_sec,
            "count": len(self.records),
            "source_counts": source_counts,
            "kind_counts": kind_counts,
            "records": [
                record.to_dict(reference_time=self.generated_at)
                for record in self.records
            ],
        }


__all__ = [
    "EvidenceRecord",
    "EvidenceSnapshot",
]
