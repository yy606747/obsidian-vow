"""Strict data contracts for the prompt-facing context projection."""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Mapping

from app.location_geofence import (
    LOCATION_GEOFENCE_EVENT_CURRENT,
    LOCATION_GEOFENCE_EVENT_TRANSITION,
    normalize_location_geofence_payload,
)


LEGACY_SCHEMA_VERSION = "context_delivery_projection.v1"
SCHEMA_VERSION = "context_delivery_projection.v2"
SUPPORTED_SCHEMA_VERSIONS = frozenset({LEGACY_SCHEMA_VERSION, SCHEMA_VERSION})
_SCALAR_TYPES = (str, int, float, bool)


def _text(value: Any, *, field_name: str) -> str:
    result = str(value or "").strip()
    if not result:
        raise ValueError(f"{field_name} is required")
    return result


def _number(value: Any, *, field_name: str, minimum: float | None = None) -> float:
    if isinstance(value, bool):
        raise ValueError(f"{field_name} must be a finite number")
    result = float(value)
    if not math.isfinite(result):
        raise ValueError(f"{field_name} must be a finite number")
    if minimum is not None and result < minimum:
        raise ValueError(f"{field_name} must be >= {minimum}")
    return result


def _confidence(value: Any) -> float:
    result = _number(value, field_name="confidence")
    if result < 0.0 or result > 1.0:
        raise ValueError("confidence must be between 0 and 1")
    return result


def _scalar(value: Any, *, field_name: str) -> str | int | float | bool:
    if value is None or not isinstance(value, _SCALAR_TYPES):
        raise ValueError(f"{field_name} must be a non-null JSON scalar")
    if isinstance(value, float) and not math.isfinite(value):
        raise ValueError(f"{field_name} must be finite")
    return value


def _strict_fields(data: Mapping[str, Any], allowed: set[str], *, contract: str) -> dict[str, Any]:
    payload = dict(data)
    unknown = sorted(set(payload) - allowed)
    if unknown:
        raise ValueError(f"unknown {contract} fields: {unknown}")
    return payload


def _typed_tuple(values: Any, item_type: type, *, field_name: str) -> tuple:
    result = tuple(values or ())
    if any(not isinstance(item, item_type) for item in result):
        raise ValueError(f"{field_name} must contain only {item_type.__name__}")
    return result


@dataclass(frozen=True)
class CurrentContextItem:
    key: str
    value: str | int | float | bool
    source: str
    observed_at: float
    received_at: float
    freshness_sec: float
    since_at: float | None = None
    confidence: float = 1.0
    payload: Mapping[str, Any] | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "key", _text(self.key, field_name="key"))
        object.__setattr__(self, "value", _scalar(self.value, field_name="value"))
        object.__setattr__(self, "source", _text(self.source, field_name="source"))
        object.__setattr__(self, "observed_at", _number(self.observed_at, field_name="observed_at"))
        object.__setattr__(self, "received_at", _number(self.received_at, field_name="received_at"))
        object.__setattr__(self, "freshness_sec", _number(self.freshness_sec, field_name="freshness_sec", minimum=0.0))
        object.__setattr__(self, "confidence", _confidence(self.confidence))
        normalized_payload = _context_item_payload(self.key, self.payload)
        if normalized_payload is not None:
            if normalized_payload["event_type"] != LOCATION_GEOFENCE_EVENT_CURRENT:
                raise ValueError("current location item requires current geofence payload")
            object.__setattr__(self, "payload", normalized_payload)
        else:
            object.__setattr__(self, "payload", None)
        if self.since_at is not None:
            since = _number(self.since_at, field_name="since_at")
            if since > self.observed_at:
                raise ValueError("since_at cannot be later than observed_at")
            object.__setattr__(self, "since_at", since)

    def to_dict(self, *, include_payload: bool = True) -> dict[str, Any]:
        payload = {
            "key": self.key,
            "value": self.value,
            "source": self.source,
            "observed_at": self.observed_at,
            "received_at": self.received_at,
            "freshness_sec": self.freshness_sec,
            "since_at": self.since_at,
            "confidence": self.confidence,
        }
        if include_payload and self.payload is not None:
            payload["payload"] = dict(self.payload)
        return payload

    @classmethod
    def from_dict(
        cls,
        data: Mapping[str, Any],
        *,
        schema_version: str = SCHEMA_VERSION,
    ) -> "CurrentContextItem":
        allowed = set(cls.__dataclass_fields__)
        if schema_version == LEGACY_SCHEMA_VERSION:
            allowed.remove("payload")
        return cls(**_strict_fields(data, allowed, contract="current item"))


@dataclass(frozen=True)
class RecentContextEvent:
    key: str
    event: str
    to_value: str | int | float | bool
    observed_at: float
    source: str
    confidence: float = 1.0
    from_value: str | int | float | bool | None = None
    occurrence_count: int = 1
    first_observed_at: float | None = None
    payload: Mapping[str, Any] | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "key", _text(self.key, field_name="key"))
        event = _text(self.event, field_name="event")
        if event not in {"transition", "occurred"}:
            raise ValueError("event must be transition or occurred")
        object.__setattr__(self, "event", event)
        object.__setattr__(self, "to_value", _scalar(self.to_value, field_name="to_value"))
        object.__setattr__(self, "observed_at", _number(self.observed_at, field_name="observed_at"))
        object.__setattr__(self, "source", _text(self.source, field_name="source"))
        object.__setattr__(self, "confidence", _confidence(self.confidence))
        normalized_payload = _context_item_payload(self.key, self.payload)
        if normalized_payload is not None:
            if normalized_payload["event_type"] != LOCATION_GEOFENCE_EVENT_TRANSITION:
                raise ValueError("recent location event requires transition geofence payload")
            object.__setattr__(self, "payload", normalized_payload)
        else:
            object.__setattr__(self, "payload", None)
        if isinstance(self.occurrence_count, bool) or not isinstance(
            self.occurrence_count,
            int,
        ):
            raise ValueError("occurrence_count must be a positive integer")
        count = int(self.occurrence_count)
        if count < 1:
            raise ValueError("occurrence_count must be a positive integer")
        object.__setattr__(self, "occurrence_count", count)
        if self.first_observed_at is not None:
            first = _number(self.first_observed_at, field_name="first_observed_at")
            if first > self.observed_at:
                raise ValueError("first_observed_at cannot be later than observed_at")
            object.__setattr__(self, "first_observed_at", first)
        if self.from_value is not None:
            object.__setattr__(self, "from_value", _scalar(self.from_value, field_name="from_value"))
        if event == "transition":
            if self.from_value is None:
                raise ValueError("transition requires from_value")
            if self.from_value == self.to_value:
                raise ValueError("transition values must differ")
            if self.occurrence_count != 1 or self.first_observed_at is not None:
                raise ValueError("transition cannot carry occurrence aggregation")
        elif self.from_value is not None:
            raise ValueError("occurred event cannot have from_value")
        elif self.occurrence_count > 1 and self.first_observed_at is None:
            raise ValueError("aggregated occurred event requires first_observed_at")

    def to_dict(self, *, include_payload: bool = True) -> dict[str, Any]:
        payload = {
            "key": self.key,
            "event": self.event,
            "to_value": self.to_value,
            "observed_at": self.observed_at,
            "source": self.source,
            "confidence": self.confidence,
        }
        if self.from_value is not None:
            payload["from_value"] = self.from_value
        if self.occurrence_count != 1:
            payload["occurrence_count"] = self.occurrence_count
        if self.first_observed_at is not None:
            payload["first_observed_at"] = self.first_observed_at
        if include_payload and self.payload is not None:
            payload["payload"] = dict(self.payload)
        return payload

    @classmethod
    def from_dict(
        cls,
        data: Mapping[str, Any],
        *,
        schema_version: str = SCHEMA_VERSION,
    ) -> "RecentContextEvent":
        allowed = set(cls.__dataclass_fields__)
        if schema_version == LEGACY_SCHEMA_VERSION:
            allowed.remove("payload")
        return cls(**_strict_fields(data, allowed, contract="recent event"))


@dataclass(frozen=True)
class BaselineDeviation:
    key: str
    current_summary: str
    baseline_summary: str
    sample_window: str
    coverage: float
    source: str

    def __post_init__(self) -> None:
        for name in ("key", "current_summary", "baseline_summary", "sample_window", "source"):
            object.__setattr__(self, name, _text(getattr(self, name), field_name=name))
        coverage = _number(self.coverage, field_name="coverage")
        if coverage < 0.0 or coverage > 1.0:
            raise ValueError("coverage must be between 0 and 1")
        object.__setattr__(self, "coverage", coverage)

    def to_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "current_summary": self.current_summary,
            "baseline_summary": self.baseline_summary,
            "sample_window": self.sample_window,
            "coverage": self.coverage,
            "source": self.source,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "BaselineDeviation":
        return cls(**_strict_fields(data, set(cls.__dataclass_fields__), contract="baseline deviation"))


@dataclass(frozen=True)
class AvailabilityItem:
    source: str
    status: str
    reason: str
    last_observed_at: float | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "source", _text(self.source, field_name="source"))
        status = _text(self.status, field_name="status")
        if status not in {"missing", "stale"}:
            raise ValueError("availability status must be missing or stale")
        object.__setattr__(self, "status", status)
        object.__setattr__(self, "reason", _text(self.reason, field_name="reason"))
        if self.last_observed_at is not None:
            object.__setattr__(self, "last_observed_at", _number(self.last_observed_at, field_name="last_observed_at"))

    def to_dict(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "status": self.status,
            "last_observed_at": self.last_observed_at,
            "reason": self.reason,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "AvailabilityItem":
        return cls(**_strict_fields(data, set(cls.__dataclass_fields__), contract="availability item"))


@dataclass(frozen=True)
class SourceStatus:
    """Normalized runtime status supplied by the legacy IO reader."""

    source: str
    enabled: bool
    expected_periodic: bool
    max_age_sec: float
    observed_at: float | None = None
    received_at: float | None = None
    since_at: float | None = None
    values: Mapping[str, str | int | float | bool] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "source", _text(self.source, field_name="source"))
        object.__setattr__(self, "enabled", bool(self.enabled))
        object.__setattr__(self, "max_age_sec", _number(self.max_age_sec, field_name="max_age_sec", minimum=0.0))
        for name in ("observed_at", "received_at", "since_at"):
            value = getattr(self, name)
            if value is not None:
                object.__setattr__(self, name, _number(value, field_name=name))
        values = {str(key): _scalar(value, field_name=f"values.{key}") for key, value in dict(self.values).items() if value is not None}
        object.__setattr__(self, "values", values)
        if (
            self.since_at is not None
            and self.observed_at is not None
            and self.since_at > self.observed_at
        ):
            raise ValueError("since_at cannot be later than observed_at")


@dataclass(frozen=True)
class ContextDeliveryProjection:
    generated_at: float
    observations: tuple[CurrentContextItem, ...] = ()
    device_derived: tuple[CurrentContextItem, ...] = ()
    recent_events: tuple[RecentContextEvent, ...] = ()
    baseline_deviations: tuple[BaselineDeviation, ...] = ()
    availability: tuple[AvailabilityItem, ...] = ()
    metrics: Mapping[str, Any] = field(default_factory=dict)
    schema_version: str = SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.schema_version not in SUPPORTED_SCHEMA_VERSIONS:
            raise ValueError(f"schema_version must be one of {sorted(SUPPORTED_SCHEMA_VERSIONS)!r}")
        object.__setattr__(self, "generated_at", _number(self.generated_at, field_name="generated_at"))
        object.__setattr__(self, "observations", _typed_tuple(self.observations, CurrentContextItem, field_name="observations"))
        object.__setattr__(self, "device_derived", _typed_tuple(self.device_derived, CurrentContextItem, field_name="device_derived"))
        object.__setattr__(self, "recent_events", _typed_tuple(self.recent_events, RecentContextEvent, field_name="recent_events"))
        object.__setattr__(self, "baseline_deviations", _typed_tuple(self.baseline_deviations, BaselineDeviation, field_name="baseline_deviations"))
        object.__setattr__(self, "availability", _typed_tuple(self.availability, AvailabilityItem, field_name="availability"))
        object.__setattr__(self, "metrics", dict(self.metrics or {}))
        if self.schema_version == LEGACY_SCHEMA_VERSION and any(
            item.payload is not None
            for item in (*self.observations, *self.device_derived, *self.recent_events)
        ):
            raise ValueError("context_delivery_projection.v1 cannot carry item payload")

    @property
    def is_empty(self) -> bool:
        return not any((self.observations, self.device_derived, self.recent_events, self.baseline_deviations, self.availability))

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "generated_at": self.generated_at,
            "observations": [item.to_dict(include_payload=self.schema_version == SCHEMA_VERSION) for item in self.observations],
            "device_derived": [item.to_dict(include_payload=self.schema_version == SCHEMA_VERSION) for item in self.device_derived],
            "recent_events": [item.to_dict(include_payload=self.schema_version == SCHEMA_VERSION) for item in self.recent_events],
            "baseline_deviations": [item.to_dict() for item in self.baseline_deviations],
            "availability": [item.to_dict() for item in self.availability],
            "metrics": dict(self.metrics),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "ContextDeliveryProjection":
        payload = _strict_fields(data, set(cls.__dataclass_fields__), contract="projection")
        schema_version = payload.get("schema_version", SCHEMA_VERSION)
        if schema_version not in SUPPORTED_SCHEMA_VERSIONS:
            raise ValueError(f"schema_version must be one of {sorted(SUPPORTED_SCHEMA_VERSIONS)!r}")
        return cls(
            schema_version=schema_version,
            generated_at=payload["generated_at"],
            observations=tuple(CurrentContextItem.from_dict(item, schema_version=schema_version) for item in payload.get("observations", ())),
            device_derived=tuple(CurrentContextItem.from_dict(item, schema_version=schema_version) for item in payload.get("device_derived", ())),
            recent_events=tuple(RecentContextEvent.from_dict(item, schema_version=schema_version) for item in payload.get("recent_events", ())),
            baseline_deviations=tuple(BaselineDeviation.from_dict(item) for item in payload.get("baseline_deviations", ())),
            availability=tuple(AvailabilityItem.from_dict(item) for item in payload.get("availability", ())),
            metrics=payload.get("metrics", {}),
        )


def _context_item_payload(key: str, value: Mapping[str, Any] | None) -> dict[str, Any] | None:
    if value is None:
        return None
    if key != "location.place":
        raise ValueError("structured context item payload is only supported for location.place")
    return normalize_location_geofence_payload(value)


__all__ = [
    "AvailabilityItem",
    "BaselineDeviation",
    "ContextDeliveryProjection",
    "CurrentContextItem",
    "LEGACY_SCHEMA_VERSION",
    "RecentContextEvent",
    "SCHEMA_VERSION",
    "SUPPORTED_SCHEMA_VERSIONS",
    "SourceStatus",
]
