"""In-memory evidence ledger for the backend foundation gate."""

from __future__ import annotations

import itertools
import threading
import time
from collections import Counter
from collections.abc import Iterable
from typing import Any, Callable, Mapping

from .lifecycle import DEFAULT_EVIDENCE_LIFECYCLE_POLICY, EvidenceLifecyclePolicy
from .schemas import EvidenceRecord, EvidenceSnapshot


NowProvider = Callable[[], float]
EvidenceIdFactory = Callable[[str, str, float], str]
RetentionStreamKey = tuple[str, str, str]


class EvidenceLedger:
    """Small append-only evidence buffer used before durable event storage exists."""

    def __init__(
        self,
        *,
        now: NowProvider | None = None,
        id_factory: EvidenceIdFactory | None = None,
        max_records: int = 1000,
        min_records_per_stream: int = 9,
        lifecycle_policy: EvidenceLifecyclePolicy = DEFAULT_EVIDENCE_LIFECYCLE_POLICY,
    ):
        self._now = now or time.time
        self._id_factory = id_factory
        self._max_records = max(1, int(max_records))
        self._min_records_per_stream = max(0, int(min_records_per_stream))
        self._lifecycle_policy = lifecycle_policy
        self._counter = itertools.count(1)
        self._records: list[EvidenceRecord] = []
        self._stream_counts: Counter[RetentionStreamKey] = Counter()
        self._evicted_total = 0
        self._evicted_by_stream: Counter[RetentionStreamKey] = Counter()
        self._retention_floor_fallbacks = 0
        self._lock = threading.RLock()

    def record(
        self,
        *,
        kind: str,
        source: str,
        payload: Mapping[str, Any] | None = None,
        observed_at: float | None = None,
        received_at: float | None = None,
        confidence: float | int | None = 1.0,
        metadata: Mapping[str, Any] | None = None,
        evidence_id: str | None = None,
    ) -> EvidenceRecord:
        received = self._now() if received_at is None else float(received_at)
        observed = received if observed_at is None else float(observed_at)
        record = EvidenceRecord(
            id=evidence_id or self._new_id(kind, source, received),
            kind=kind,
            source=source,
            observed_at=observed,
            received_at=received,
            confidence=confidence,
            payload=payload or {},
            metadata=metadata or {},
        )
        with self._lock:
            self._records.append(record)
            self._stream_counts[self._retention_stream(record)] += 1
            if len(self._records) > self._max_records:
                self._evict_one_locked()
        return record

    def recent(
        self,
        *,
        max_age_sec: float | None = None,
        kinds: Iterable[str] | None = None,
        sources: Iterable[str] | None = None,
        reference_time: float | None = None,
        include_future: bool = False,
        future_tolerance_sec: float = 0.0,
        limit: int | None = None,
    ) -> list[EvidenceRecord]:
        reference = self._now() if reference_time is None else float(reference_time)
        kind_set = {str(kind) for kind in kinds} if kinds is not None else None
        source_set = {str(source) for source in sources} if sources is not None else None

        records = []
        with self._lock:
            source_records = list(self._records)
        for record in source_records:
            if kind_set is not None and record.kind not in kind_set:
                continue
            if source_set is not None and record.source not in source_set:
                continue
            if not include_future and record.is_future(
                reference,
                tolerance_sec=future_tolerance_sec,
            ):
                continue
            if max_age_sec is not None and record.is_stale(reference, max_age_sec=max_age_sec):
                continue
            records.append(record)

        records.sort(key=lambda item: (item.observed_at, item.received_at, item.id))
        if limit is not None:
            records = records[-max(0, int(limit)):]
        return records

    def snapshot(
        self,
        *,
        max_age_sec: float | None = None,
        kinds: Iterable[str] | None = None,
        sources: Iterable[str] | None = None,
        reference_time: float | None = None,
        include_future: bool = False,
        future_tolerance_sec: float = 0.0,
        limit: int | None = None,
    ) -> EvidenceSnapshot:
        reference = self._now() if reference_time is None else float(reference_time)
        return EvidenceSnapshot(
            generated_at=reference,
            max_age_sec=max_age_sec,
            records=tuple(
                self.recent(
                    max_age_sec=max_age_sec,
                    kinds=kinds,
                    sources=sources,
                    reference_time=reference,
                    include_future=include_future,
                    future_tolerance_sec=future_tolerance_sec,
                    limit=limit,
                )
            ),
        )

    def stats(
        self,
        *,
        max_age_sec: float | None = None,
        reference_time: float | None = None,
    ) -> dict:
        reference = self._now() if reference_time is None else float(reference_time)
        records = self.recent(
            max_age_sec=max_age_sec,
            reference_time=reference,
            include_future=False,
        )
        source_counts: dict[str, int] = {}
        kind_counts: dict[str, int] = {}
        freshness_values = []
        for record in records:
            source_counts[record.source] = source_counts.get(record.source, 0) + 1
            kind_counts[record.kind] = kind_counts.get(record.kind, 0) + 1
            freshness_values.append(record.freshness_at(reference))
        with self._lock:
            retained_by_stream = {
                self._stream_label(key): count
                for key, count in sorted(self._stream_counts.items())
            }
            evicted_by_stream = {
                self._stream_label(key): count
                for key, count in sorted(self._evicted_by_stream.items())
            }
            evicted_total = self._evicted_total
            retention_floor_fallbacks = self._retention_floor_fallbacks
        return {
            "generated_at": reference,
            "max_age_sec": max_age_sec,
            "count": len(records),
            "source_counts": source_counts,
            "kind_counts": kind_counts,
            "oldest_freshness_sec": max(freshness_values) if freshness_values else None,
            "newest_freshness_sec": min(freshness_values) if freshness_values else None,
            "lifecycle": self._lifecycle_policy.to_dict(max_records=self._max_records),
            "retention": {
                "min_records_per_stream": self._min_records_per_stream,
                "retained_by_stream": retained_by_stream,
                "evicted_total": evicted_total,
                "evicted_by_stream": evicted_by_stream,
                "retention_floor_fallbacks": retention_floor_fallbacks,
            },
        }

    def prune(
        self,
        *,
        max_age_sec: float,
        reference_time: float | None = None,
    ) -> dict:
        reference = self._now() if reference_time is None else float(reference_time)
        with self._lock:
            before = len(self._records)
            self._records = [
                record for record in self._records
                if not record.is_stale(reference, max_age_sec=max_age_sec)
                and not record.is_future(reference)
            ]
            self._rebuild_stream_counts_locked()
            after = len(self._records)
        return {
            "generated_at": reference,
            "max_age_sec": float(max_age_sec),
            "before": before,
            "after": after,
            "deleted": before - after,
        }

    def clear(self):
        with self._lock:
            self._records.clear()
            self._stream_counts.clear()
            self._evicted_total = 0
            self._evicted_by_stream.clear()
            self._retention_floor_fallbacks = 0

    def _evict_one_locked(self) -> None:
        eviction_index = None
        for index, record in enumerate(self._records):
            stream = self._retention_stream(record)
            if self._stream_counts[stream] > self._min_records_per_stream:
                eviction_index = index
                break
        if eviction_index is None:
            eviction_index = 0
            self._retention_floor_fallbacks += 1
        evicted = self._records.pop(eviction_index)
        stream = self._retention_stream(evicted)
        self._stream_counts[stream] -= 1
        if self._stream_counts[stream] <= 0:
            del self._stream_counts[stream]
        self._evicted_total += 1
        self._evicted_by_stream[stream] += 1

    def _rebuild_stream_counts_locked(self) -> None:
        self._stream_counts = Counter(
            self._retention_stream(record) for record in self._records
        )

    @staticmethod
    def _retention_stream(record: EvidenceRecord) -> RetentionStreamKey:
        device_id = record.metadata.get("device_id")
        if not device_id:
            device_id = record.payload.get("device_id")
        return (record.kind, record.source, str(device_id or "").strip())

    @staticmethod
    def _stream_label(stream: RetentionStreamKey) -> str:
        kind, source, device_id = stream
        return f"{kind}|{source}|device_id={device_id or '-'}"

    def _new_id(self, kind: str, source: str, received_at: float) -> str:
        if self._id_factory:
            return self._id_factory(kind, source, received_at)
        safe_kind = "".join(ch if ch.isalnum() else "_" for ch in str(kind).strip()) or "event"
        safe_source = "".join(ch if ch.isalnum() else "_" for ch in str(source).strip()) or "source"
        return f"ev_{safe_kind}_{safe_source}_{int(received_at * 1000)}_{next(self._counter)}"


evidence_ledger = EvidenceLedger()


__all__ = [
    "EvidenceIdFactory",
    "EvidenceLifecyclePolicy",
    "EvidenceLedger",
    "NowProvider",
    "RetentionStreamKey",
    "evidence_ledger",
]
