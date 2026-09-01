"""Pure, model-free candidate rules for context-trigger shadow evaluation."""

from __future__ import annotations

import math
import threading
from dataclasses import dataclass, field
from typing import Any, Mapping

from app.events import EvidenceRecord
from app.location_geofence import (
    LOCATION_GEOFENCE_EVENT_TRANSITION,
    LOCATION_GEOFENCE_PAYLOAD_SCHEMA_VERSION,
    normalize_location_geofence_payload,
)


RULE_LOCATION_REGION_TRANSITION = "location_region_transition"
RULE_FIRST_INTERACTION_AFTER_LONG_SILENCE = (
    "first_interaction_after_long_silence"
)

EVALUATION_MATCHED = "matched"
EVALUATION_NOT_MATCHED = "not_matched"
EVALUATION_UNAVAILABLE = "unavailable"
EVALUATION_STATUSES = frozenset({
    EVALUATION_MATCHED,
    EVALUATION_NOT_MATCHED,
    EVALUATION_UNAVAILABLE,
})

LONG_SILENCE_SHADOW_THRESHOLD_SEC = 60 * 60


@dataclass(frozen=True)
class ShadowRuleEvaluation:
    rule: str
    source_event_id: str
    occurred_at: float
    evaluation_status: str
    evaluation_reason: str
    features: Mapping[str, Any] = field(default_factory=dict)
    event_confidence: float = 1.0

    def __post_init__(self) -> None:
        rule = str(self.rule or "").strip()
        source_event_id = str(self.source_event_id or "").strip()
        reason = str(self.evaluation_reason or "").strip()
        if not rule:
            raise ValueError("shadow rule is required")
        if not source_event_id:
            raise ValueError("shadow source_event_id is required")
        if self.evaluation_status not in EVALUATION_STATUSES:
            raise ValueError("shadow evaluation_status is invalid")
        if not reason:
            raise ValueError("shadow evaluation_reason is required")
        occurred_at = _finite_number(self.occurred_at, key="occurred_at")
        confidence = _finite_number(self.event_confidence, key="event_confidence")
        if not 0.0 <= confidence <= 1.0:
            raise ValueError("shadow event_confidence must be between 0 and 1")
        object.__setattr__(self, "rule", rule)
        object.__setattr__(self, "source_event_id", source_event_id)
        object.__setattr__(self, "occurred_at", occurred_at)
        object.__setattr__(self, "evaluation_reason", reason)
        object.__setattr__(self, "features", dict(self.features or {}))
        object.__setattr__(self, "event_confidence", confidence)


class ContextTriggerShadowRuleEngine:
    """Process-local interaction memory plus two bounded candidate rules."""

    def __init__(self, *, long_silence_sec: float = LONG_SILENCE_SHADOW_THRESHOLD_SEC):
        threshold = _finite_number(long_silence_sec, key="long_silence_sec")
        if threshold <= 0:
            raise ValueError("long_silence_sec must be positive")
        self._long_silence_sec = threshold
        self._last_interaction_at: dict[str, float] = {}
        self._screen_state: dict[str, str] = {}
        self._lock = threading.RLock()

    def evaluate(self, record: EvidenceRecord) -> ShadowRuleEvaluation | None:
        if not isinstance(record, EvidenceRecord):
            raise ValueError("shadow rule input must be EvidenceRecord")
        with self._lock:
            location = self._evaluate_location(record)
            if location is not None:
                return location
            return self._evaluate_interaction(record)

    def clear_process_state(self) -> None:
        """Testing/restart seam; durable rows intentionally do not seed this state."""

        with self._lock:
            self._last_interaction_at.clear()
            self._screen_state.clear()

    def _evaluate_location(
        self,
        record: EvidenceRecord,
    ) -> ShadowRuleEvaluation | None:
        if record.kind != "location.state":
            return None
        payload = dict(record.payload)
        if payload.get("payload_schema") != LOCATION_GEOFENCE_PAYLOAD_SCHEMA_VERSION:
            return None
        normalized = normalize_location_geofence_payload(payload)
        if normalized["event_type"] != LOCATION_GEOFENCE_EVENT_TRANSITION:
            return None
        features = {
            key: normalized[key]
            for key in (
                "event_type",
                "geofence_direction",
                "boundary_side",
                "distance_m",
                "accuracy_m",
                "configured_enter_m",
                "configured_exit_m",
                "place_id",
                "place_name",
                "place_kind",
            )
            if key in normalized
        }
        features["evidence_id"] = record.id
        return ShadowRuleEvaluation(
            rule=RULE_LOCATION_REGION_TRANSITION,
            source_event_id=_source_event_id(
                record,
                event_name=str(normalized["geofence_direction"]),
            ),
            occurred_at=record.observed_at,
            evaluation_status=EVALUATION_MATCHED,
            evaluation_reason=(
                "geofence_transition_" + str(normalized["geofence_direction"])
            ),
            features=features,
            event_confidence=record.confidence,
        )

    def _evaluate_interaction(
        self,
        record: EvidenceRecord,
    ) -> ShadowRuleEvaluation | None:
        device_key = _device_key(record)
        interaction_kind = ""

        if record.kind == "sensing.unlock":
            interaction_kind = "unlock"
            self._screen_state[device_key] = "on"
        else:
            reported_state = _reported_screen_state(record)
            if reported_state is None:
                return None
            prior_state = self._screen_state.get(device_key)
            self._screen_state[device_key] = reported_state
            if not (prior_state == "off" and reported_state == "on"):
                return None
            interaction_kind = "screen_off_to_on"

        occurred_at = float(record.observed_at)
        previous = self._last_interaction_at.get(device_key)
        features: dict[str, Any] = {
            "interaction_event": interaction_kind,
            "device_key": device_key,
            "evidence_id": record.id,
            "shadow_threshold_sec": self._long_silence_sec,
        }
        if previous is None:
            status = EVALUATION_UNAVAILABLE
            reason = "missing_previous_interaction"
            features["interaction_gap_sec"] = None
            features["missing_reason"] = reason
            self._last_interaction_at[device_key] = occurred_at
        elif occurred_at < previous:
            status = EVALUATION_UNAVAILABLE
            reason = "out_of_order_interaction"
            features["interaction_gap_sec"] = None
            features["previous_interaction_at"] = previous
        else:
            gap = occurred_at - previous
            features["interaction_gap_sec"] = gap
            features["previous_interaction_at"] = previous
            if gap >= self._long_silence_sec:
                status = EVALUATION_MATCHED
                reason = f"gap_at_least_{int(self._long_silence_sec)}"
            else:
                status = EVALUATION_NOT_MATCHED
                reason = f"gap_below_{int(self._long_silence_sec)}"
            self._last_interaction_at[device_key] = occurred_at

        return ShadowRuleEvaluation(
            rule=RULE_FIRST_INTERACTION_AFTER_LONG_SILENCE,
            source_event_id=_source_event_id(record, event_name=interaction_kind),
            occurred_at=occurred_at,
            evaluation_status=status,
            evaluation_reason=reason,
            features=features,
            # sensing.sensor Evidence confidence currently belongs only to the
            # motion field.  It must not leak onto unlock/screen observations.
            event_confidence=1.0,
        )


def _reported_screen_state(record: EvidenceRecord) -> str | None:
    payload = record.payload
    if record.kind == "sensing.sensor" and isinstance(payload.get("screen_on"), bool):
        return "on" if payload["screen_on"] else "off"
    if record.kind == "activity.app":
        value = str(payload.get("screen_state") or "").strip().lower()
        if value in {"on", "off"}:
            return value
    return None


def is_context_trigger_shadow_input(record: EvidenceRecord) -> bool:
    """Cheap source whitelist used before config or persistence is touched."""

    if not isinstance(record, EvidenceRecord):
        return False
    if record.kind in {"location.state", "sensing.unlock"}:
        return True
    return _reported_screen_state(record) is not None


def _device_key(record: EvidenceRecord) -> str:
    device_id = record.metadata.get("device_id") or record.payload.get("device_id")
    text = str(device_id or "").strip()
    if text:
        return text
    if record.source.startswith("android."):
        return "android-phone"
    return record.source


def _source_event_id(record: EvidenceRecord, *, event_name: str) -> str:
    observed_ms = int(round(float(record.observed_at) * 1000.0))
    return ":".join((
        record.source,
        record.kind,
        _device_key(record),
        str(observed_ms),
        str(event_name or "event").strip(),
    ))


def _finite_number(value: Any, *, key: str) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise ValueError(f"shadow {key} must be a number")
    result = float(value)
    if not math.isfinite(result):
        raise ValueError(f"shadow {key} must be finite")
    return result


__all__ = [
    "ContextTriggerShadowRuleEngine",
    "EVALUATION_MATCHED",
    "EVALUATION_NOT_MATCHED",
    "EVALUATION_STATUSES",
    "EVALUATION_UNAVAILABLE",
    "LONG_SILENCE_SHADOW_THRESHOLD_SEC",
    "RULE_FIRST_INTERACTION_AFTER_LONG_SILENCE",
    "RULE_LOCATION_REGION_TRANSITION",
    "ShadowRuleEvaluation",
    "is_context_trigger_shadow_input",
]
