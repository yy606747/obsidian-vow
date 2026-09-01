"""Read-only evidence snapshots for future Sentinel decisions."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

from app.events import EvidenceLedger, EvidenceRecord, EvidenceSnapshot, evidence_ledger


DEFAULT_SENTINEL_EVIDENCE_MAX_AGE_SEC = 3 * 3600
DEFAULT_SENTINEL_EVIDENCE_LIMIT = 80


@dataclass(frozen=True)
class SentinelEvidenceView:
    """Read-only, explainable evidence view. It never decides or triggers actions."""

    snapshot: EvidenceSnapshot
    summary_lines: tuple[str, ...]

    def to_dict(self) -> dict:
        payload = self.snapshot.to_dict()
        payload["summary_lines"] = list(self.summary_lines)
        payload["policy"] = {
            "read_only": True,
            "decision": None,
            "side_effects": [],
        }
        return payload


class SentinelEvidenceService:
    def __init__(
        self,
        *,
        ledger: EvidenceLedger | None = None,
        default_max_age_sec: float = DEFAULT_SENTINEL_EVIDENCE_MAX_AGE_SEC,
        default_limit: int = DEFAULT_SENTINEL_EVIDENCE_LIMIT,
    ):
        self._ledger = ledger or evidence_ledger
        self._default_max_age_sec = float(default_max_age_sec)
        self._default_limit = int(default_limit)

    def snapshot(
        self,
        *,
        reference_time: float | None = None,
        max_age_sec: float | None = None,
        limit: int | None = None,
        kinds: Iterable[str] | None = None,
        sources: Iterable[str] | None = None,
    ) -> SentinelEvidenceView:
        evidence_snapshot = self._ledger.snapshot(
            max_age_sec=self._default_max_age_sec if max_age_sec is None else max_age_sec,
            reference_time=reference_time,
            include_future=False,
            limit=self._default_limit if limit is None else limit,
            kinds=kinds,
            sources=sources,
        )
        return SentinelEvidenceView(
            snapshot=evidence_snapshot,
            summary_lines=tuple(
                self._summary_line(record, evidence_snapshot.generated_at)
                for record in evidence_snapshot.records
            ),
        )

    def snapshot_payload(self, **kwargs) -> dict:
        return self.snapshot(**kwargs).to_dict()

    def _summary_line(self, record: EvidenceRecord, reference_time: float) -> str:
        freshness = int(record.freshness_at(reference_time))
        confidence = f"{record.confidence:.2f}"
        detail = self._record_detail(record)
        return f"{record.kind} from {record.source}, {freshness}s old, confidence={confidence}: {detail}"

    @staticmethod
    def _record_detail(record: EvidenceRecord) -> str:
        payload = dict(record.payload)
        if record.kind.startswith("sensing."):
            parts = []
            for key in ("motion", "sleep_stage", "heart_rate", "app", "screen_on", "battery_pct"):
                if key in payload:
                    parts.append(f"{key}={payload[key]}")
            return ", ".join(parts) if parts else "sensing update"
        if record.kind == "activity.app":
            device = payload.get("device", "unknown")
            app = payload.get("app", "")
            title = payload.get("title", "")
            return f"{device} active app={app}" + (f", title={title}" if title else "")
        if record.kind == "location.fix":
            parts = []
            for key in ("state", "accuracy", "distance_from_home", "state_changed"):
                if key in payload:
                    parts.append(f"{key}={payload[key]}")
            return ", ".join(parts) if parts else "location update"
        return "evidence update"


sentinel_evidence_service = SentinelEvidenceService()


__all__ = [
    "DEFAULT_SENTINEL_EVIDENCE_LIMIT",
    "DEFAULT_SENTINEL_EVIDENCE_MAX_AGE_SEC",
    "SentinelEvidenceService",
    "SentinelEvidenceView",
    "sentinel_evidence_service",
]
