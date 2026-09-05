"""Lifecycle policy for Phase 8.0 evidence."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class EvidenceLifecyclePolicy:
    """Evidence is intentionally real-time only until a durable need is proven."""

    storage: str = "process_memory"
    durable: bool = False
    retention: str = "bounded_realtime_window"
    persistence_decision: str = "disabled_until_need_is_proven"

    def to_dict(self, *, max_records: int) -> dict:
        return {
            "storage": self.storage,
            "max_records": int(max_records),
            "durable": self.durable,
            "retention": self.retention,
            "persistence_decision": self.persistence_decision,
        }


DEFAULT_EVIDENCE_LIFECYCLE_POLICY = EvidenceLifecyclePolicy()


__all__ = [
    "DEFAULT_EVIDENCE_LIFECYCLE_POLICY",
    "EvidenceLifecyclePolicy",
]
