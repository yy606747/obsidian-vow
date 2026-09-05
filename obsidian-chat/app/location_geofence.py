"""Strict structured contract for the configured home-geofence signal."""

from __future__ import annotations

import math
from collections.abc import Mapping
from typing import Any


LOCATION_GEOFENCE_PAYLOAD_SCHEMA_VERSION = "location_geofence.v1"
LOCATION_GEOFENCE_EVENT_CURRENT = "current"
LOCATION_GEOFENCE_EVENT_TRANSITION = "transition"

_BOUNDARY_SIDES = frozenset({"inside", "outside"})
_DIRECTION_TARGETS = {
    "inside_to_outside": "outside",
    "outside_to_inside": "inside",
}
_ALLOWED_FIELDS = frozenset({
    "accuracy_m",
    "boundary_side",
    "configured_enter_m",
    "configured_exit_m",
    "distance_m",
    "event_type",
    "geofence_direction",
    "last_fix_at",
    "payload_schema",
    "place_id",
    "place_kind",
    "place_name",
    "state_updated_at",
})


def normalize_location_geofence_payload(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Validate and normalize one ``location_geofence.v1`` payload.

    A declared schema is never repaired from prompt text. Callers must either
    provide the complete structure or reject the record.
    """

    if not isinstance(payload, Mapping):
        raise ValueError("location geofence payload must be an object")
    value = dict(payload)
    unknown = sorted(set(value).difference(_ALLOWED_FIELDS))
    if unknown:
        raise ValueError(f"unknown location geofence payload fields: {unknown!r}")
    if value.get("payload_schema") != LOCATION_GEOFENCE_PAYLOAD_SCHEMA_VERSION:
        raise ValueError(
            "location geofence payload_schema must be "
            f"{LOCATION_GEOFENCE_PAYLOAD_SCHEMA_VERSION!r}"
        )

    event_type = _required_choice(
        value.get("event_type"),
        field_name="event_type",
        choices={LOCATION_GEOFENCE_EVENT_CURRENT, LOCATION_GEOFENCE_EVENT_TRANSITION},
    )
    boundary_side = _required_choice(
        value.get("boundary_side"),
        field_name="boundary_side",
        choices=_BOUNDARY_SIDES,
    )
    distance_m = _required_number(value.get("distance_m"), field_name="distance_m", minimum=0.0)
    accuracy_m = _required_number(value.get("accuracy_m"), field_name="accuracy_m", minimum=0.0)
    configured_enter_m = _required_number(
        value.get("configured_enter_m"),
        field_name="configured_enter_m",
        minimum=0.0,
    )
    configured_exit_m = _required_number(
        value.get("configured_exit_m"),
        field_name="configured_exit_m",
        minimum=0.0,
    )
    if configured_exit_m <= configured_enter_m:
        raise ValueError("configured_exit_m must be greater than configured_enter_m")

    normalized: dict[str, Any] = {
        "payload_schema": LOCATION_GEOFENCE_PAYLOAD_SCHEMA_VERSION,
        "event_type": event_type,
        "boundary_side": boundary_side,
        "distance_m": distance_m,
        "accuracy_m": accuracy_m,
        "configured_enter_m": configured_enter_m,
        "configured_exit_m": configured_exit_m,
    }

    direction = value.get("geofence_direction")
    if event_type == LOCATION_GEOFENCE_EVENT_TRANSITION:
        direction = _required_choice(
            direction,
            field_name="geofence_direction",
            choices=set(_DIRECTION_TARGETS),
        )
        if _DIRECTION_TARGETS[direction] != boundary_side:
            raise ValueError("geofence_direction target must match boundary_side")
        normalized["geofence_direction"] = direction
    elif direction is not None:
        raise ValueError("current geofence payload cannot carry geofence_direction")

    for field_name in ("place_id", "place_name", "place_kind"):
        text = _optional_text(value.get(field_name), field_name=field_name)
        if text is not None:
            normalized[field_name] = text
    for field_name in ("last_fix_at", "state_updated_at"):
        if value.get(field_name) is not None:
            normalized[field_name] = _required_number(
                value.get(field_name),
                field_name=field_name,
                minimum=0.0,
            )
    return normalized


def as_current_location_geofence_payload(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Return the current-state view of a valid transition/current payload."""

    value = normalize_location_geofence_payload(payload)
    value["event_type"] = LOCATION_GEOFENCE_EVENT_CURRENT
    value.pop("geofence_direction", None)
    return value


def location_geofence_tags(payload: Mapping[str, Any]) -> frozenset[str]:
    """Derive Sentinel tags solely from the structured contract."""

    value = normalize_location_geofence_payload(payload)
    tags: set[str] = set()
    if value["event_type"] == LOCATION_GEOFENCE_EVENT_TRANSITION:
        if value["geofence_direction"] == "inside_to_outside":
            tags.add("left_home_transition")
        else:
            tags.add("return_home_transition")
    elif value["boundary_side"] == "outside":
        tags.update({"outside_continuous", "no_location_change"})
    else:
        tags.add("at_home")
    return frozenset(tags)


def _required_choice(value: Any, *, field_name: str, choices: set[str] | frozenset[str]) -> str:
    text = str(value or "").strip()
    if text not in choices:
        raise ValueError(f"{field_name} must be one of {sorted(choices)!r}")
    return text


def _required_number(value: Any, *, field_name: str, minimum: float) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise ValueError(f"{field_name} must be a finite number")
    number = float(value)
    if not math.isfinite(number):
        raise ValueError(f"{field_name} must be a finite number")
    if number < minimum:
        raise ValueError(f"{field_name} must be >= {minimum}")
    return number


def _optional_text(value: Any, *, field_name: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError(f"{field_name} must be text")
    text = value.strip()
    return text or None


__all__ = [
    "LOCATION_GEOFENCE_EVENT_CURRENT",
    "LOCATION_GEOFENCE_EVENT_TRANSITION",
    "LOCATION_GEOFENCE_PAYLOAD_SCHEMA_VERSION",
    "as_current_location_geofence_payload",
    "location_geofence_tags",
    "normalize_location_geofence_payload",
]
