"""Legacy IO boundary for model-free context-trigger shadow evaluation."""

from __future__ import annotations

import asyncio
import logging
import sqlite3
import time
from dataclasses import replace
from pathlib import Path
from typing import Any, Callable, Mapping

from app.context_delivery import ContextDeliveryProjection
from app.context_delivery.shadow_rules import (
    ContextTriggerShadowRuleEngine,
    EVALUATION_MATCHED,
    ShadowRuleEvaluation,
    is_context_trigger_shadow_input,
)
from app.context_delivery.shadow_store import ContextTriggerShadowStore
from app.events import EvidenceRecord
from app.sentinel import (
    WAKE_BOUNDARY_RESULT_SCHEMA_VERSION,
    evaluate_wake_boundaries,
)
from config import (
    DATA_DIR,
    DB_PATH,
    MONITOR_LOGS_DIR,
    load_ai_behavior,
    load_cam_config,
)
from sentinel_runtime_readers import is_quiet_hours


log = logging.getLogger(__name__)

DEFAULT_CONTEXT_TRIGGER_SHADOW_DB_PATH = DATA_DIR / "context_delivery.db"
OWNER_MESSAGE_OUTCOME_WINDOW_SEC = 30 * 60
LEGACY_SENTINEL_OUTCOME_WINDOW_SEC = 5 * 60
OUTCOME_REFRESH_INTERVAL_SEC = 60.0
BOUNDARY_CONTEXT_LOOKBACK_SEC = 6 * 60 * 60


class ContextTriggerShadowRuntime:
    def __init__(
        self,
        *,
        store: ContextTriggerShadowStore | None = None,
        rule_engine: ContextTriggerShadowRuleEngine | None = None,
        behavior_loader: Callable[[], Mapping[str, Any]] | None = None,
        projection_reader: Callable[..., ContextDeliveryProjection] | None = None,
        boundary_context_reader: Callable[[float], Mapping[str, Any]] | None = None,
        owner_message_checker: Callable[[float, float], bool] | None = None,
        legacy_wake_checker: Callable[[float, float], bool] | None = None,
        now: Callable[[], float] | None = None,
    ):
        self.store = store or ContextTriggerShadowStore(
            DEFAULT_CONTEXT_TRIGGER_SHADOW_DB_PATH
        )
        self.rule_engine = rule_engine or ContextTriggerShadowRuleEngine()
        self._behavior_loader = behavior_loader or load_ai_behavior
        self._projection_reader = projection_reader
        self._boundary_context_reader = boundary_context_reader or _read_boundary_context
        self._owner_message_checker = owner_message_checker or _owner_message_between
        self._legacy_wake_checker = legacy_wake_checker or _legacy_wake_between
        self._now = now or time.time

    def enabled(self) -> bool:
        behavior = self._behavior_loader()
        if not isinstance(behavior, Mapping):
            raise ValueError("context trigger shadow behavior must be an object")
        return bool(behavior.get("context_trigger_shadow_enabled", False))

    def process_evidence(self, record: EvidenceRecord) -> dict[str, Any] | None:
        """Evaluate a relevant evidence record without calling any provider."""

        if not is_context_trigger_shadow_input(record):
            return None
        if not self.enabled():
            return None
        evaluation = self.rule_engine.evaluate(record)
        if evaluation is None:
            return None
        existing = self.store.get_by_rule_source(
            rule=evaluation.rule,
            source_event_id=evaluation.source_event_id,
        )
        if existing is not None:
            return existing

        projection, evaluation = self._capture_projection(record, evaluation)
        gate = None
        if evaluation.evaluation_status == EVALUATION_MATCHED:
            gate = self._evaluate_boundaries(evaluation)
        return self.store.record_evaluation(
            evaluation,
            projection=projection,
            gate=gate,
        )

    def process_evidence_safely(
        self,
        record: EvidenceRecord,
    ) -> dict[str, Any] | None:
        try:
            return self.process_evidence(record)
        except Exception as exc:
            log.warning(
                "Context trigger shadow skipped for %s: %s",
                getattr(record, "id", "unknown"),
                exc,
            )
            return None

    def refresh_due_outcomes(
        self,
        *,
        reference_time: float | None = None,
        limit: int = 500,
    ) -> int:
        reference = self._now() if reference_time is None else float(reference_time)
        if not self.store.db_path.exists() and not self.enabled():
            return 0
        due = self.store.due_outcomes(
            reference_time=reference,
            owner_window_sec=OWNER_MESSAGE_OUTCOME_WINDOW_SEC,
            limit=limit,
        )
        completed = 0
        for row in due:
            occurred_at = float(row["occurred_at"])
            legacy_woke = self._legacy_wake_checker(
                occurred_at,
                occurred_at + LEGACY_SENTINEL_OUTCOME_WINDOW_SEC,
            )
            owner_messaged = self._owner_message_checker(
                occurred_at,
                occurred_at + OWNER_MESSAGE_OUTCOME_WINDOW_SEC,
            )
            self.store.record_outcome(
                row["id"],
                legacy_sentinel_woke_within_5m=bool(legacy_woke),
                owner_message_within_30m=bool(owner_messaged),
                evaluated_at=reference,
            )
            completed += 1
        return completed

    def list_payload(
        self,
        *,
        include_non_matched: bool = False,
        rule: str | None = None,
        limit: int = 50,
        before_occurred_at: float | None = None,
    ) -> dict[str, Any]:
        statuses = None if include_non_matched else (EVALUATION_MATCHED,)
        return {
            "enabled": self.enabled(),
            "entries": self.store.list_evaluations(
                statuses=statuses,
                rule=rule,
                limit=limit,
                before_occurred_at=before_occurred_at,
            ),
            "stats": self.store.stats(),
        }

    def set_owner_label(self, evaluation_id: str, label: str) -> dict[str, Any]:
        return self.store.set_owner_label(evaluation_id, label)

    def _capture_projection(
        self,
        record: EvidenceRecord,
        evaluation: ShadowRuleEvaluation,
    ) -> tuple[dict[str, Any], ShadowRuleEvaluation]:
        reader = self._projection_reader
        if reader is None:
            from context_delivery_runtime_readers import read_context_delivery_projection

            reader = read_context_delivery_projection
        try:
            projection = reader(reference_time=record.received_at)
            if not isinstance(projection, ContextDeliveryProjection):
                raise ValueError("shadow projection reader must return ContextDeliveryProjection")
            return projection.to_dict(), evaluation
        except Exception as exc:
            features = dict(evaluation.features)
            features["projection_capture_error"] = type(exc).__name__
            fallback = ContextDeliveryProjection(generated_at=record.received_at)
            return fallback.to_dict(), replace(evaluation, features=features)

    def _evaluate_boundaries(
        self,
        evaluation: ShadowRuleEvaluation,
    ) -> dict[str, Any]:
        try:
            context = self._boundary_context_reader(evaluation.occurred_at)
            return evaluate_wake_boundaries(
                context=context,
                event_confidence=evaluation.event_confidence,
            )
        except Exception as exc:
            return {
                "schema_version": WAKE_BOUNDARY_RESULT_SCHEMA_VERSION,
                "runtime_mode": "dry_run",
                "status": "unavailable",
                "wake_allowed": False,
                "blocked_reasons": ["boundary_context_unavailable"],
                "side_effects": [],
                "error_type": type(exc).__name__,
            }


def _read_boundary_context(reference_time: float) -> dict[str, Any]:
    reference = float(reference_time)
    context: dict[str, Any] = {
        "quiet_hours_active": is_quiet_hours(
            load_cam_config(),
            reference_time=reference,
        ),
        "clear_sleep": False,
        "urgent_risk": False,
        "device_effect_requested": False,
        "device_effect_allowed": False,
    }
    last_user_ts = _last_user_message_at_or_before(reference)
    if last_user_ts > 0:
        context["last_user_message_age_sec"] = max(0.0, reference - last_user_ts)
    last_wake_ts = _last_legacy_wake_at_or_before(reference)
    if last_wake_ts > 0:
        context["last_wake_age_sec"] = max(0.0, reference - last_wake_ts)
    return context


def _last_user_message_at_or_before(reference_time: float) -> float:
    with sqlite3.connect(str(DB_PATH)) as conn:
        row = conn.execute(
            "SELECT created_at FROM messages "
            "WHERE role='user' AND created_at <= ? "
            "ORDER BY created_at DESC LIMIT 1",
            (reference_time,),
        ).fetchone()
    return float(row[0]) if row else 0.0


def _owner_message_between(start_at: float, end_at: float) -> bool:
    with sqlite3.connect(str(DB_PATH)) as conn:
        row = conn.execute(
            "SELECT 1 FROM messages "
            "WHERE role='user' AND created_at > ? AND created_at <= ? LIMIT 1",
            (start_at, end_at),
        ).fetchone()
    return row is not None


def _last_legacy_wake_at_or_before(reference_time: float) -> float:
    entries = _monitor_entries_since(reference_time - BOUNDARY_CONTEXT_LOOKBACK_SEC)
    for entry in reversed(entries):
        timestamp = float(entry.get("timestamp") or 0.0)
        if timestamp <= reference_time and entry.get("call_core") is True:
            return timestamp
    return 0.0


def _legacy_wake_between(start_at: float, end_at: float) -> bool:
    return any(
        start_at < float(entry.get("timestamp") or 0.0) <= end_at
        and entry.get("call_core") is True
        for entry in _monitor_entries_since(start_at)
    )


def _monitor_entries_since(since_at: float) -> list[dict[str, Any]]:
    import datetime as dt
    import json

    since_date = dt.date.fromtimestamp(since_at)
    entries: list[dict[str, Any]] = []
    for path in sorted(Path(MONITOR_LOGS_DIR).glob("*.jsonl")):
        try:
            if dt.date.fromisoformat(path.stem) < since_date:
                continue
        except ValueError:
            continue
        for line in path.read_text(encoding="utf-8").splitlines():
            try:
                entry = json.loads(line)
            except (TypeError, ValueError):
                continue
            timestamp = entry.get("timestamp")
            if isinstance(timestamp, int | float) and not isinstance(timestamp, bool):
                if float(timestamp) >= since_at:
                    entries.append(entry)
    entries.sort(key=lambda item: float(item.get("timestamp") or 0.0))
    return entries


async def run_context_trigger_outcome_loop(
    *,
    interval_sec: float = OUTCOME_REFRESH_INTERVAL_SEC,
    runtime: ContextTriggerShadowRuntime | None = None,
) -> None:
    target = runtime or context_trigger_shadow_runtime
    while True:
        try:
            await asyncio.to_thread(target.refresh_due_outcomes)
        except Exception as exc:
            log.warning("Context trigger outcome refresh skipped: %s", exc)
        await asyncio.sleep(max(1.0, float(interval_sec)))


context_trigger_shadow_runtime = ContextTriggerShadowRuntime()


__all__ = [
    "BOUNDARY_CONTEXT_LOOKBACK_SEC",
    "ContextTriggerShadowRuntime",
    "DEFAULT_CONTEXT_TRIGGER_SHADOW_DB_PATH",
    "LEGACY_SENTINEL_OUTCOME_WINDOW_SEC",
    "OUTCOME_REFRESH_INTERVAL_SEC",
    "OWNER_MESSAGE_OUTCOME_WINDOW_SEC",
    "context_trigger_shadow_runtime",
    "run_context_trigger_outcome_loop",
]
