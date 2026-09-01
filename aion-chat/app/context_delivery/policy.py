"""One authority and freshness policy table for context delivery."""

from __future__ import annotations

from dataclasses import dataclass


PHONE_CURRENT_MAX_AGE_SEC = 15 * 60
PC_CURRENT_MAX_AGE_SEC = 5 * 60
LOCATION_CURRENT_MAX_AGE_SEC = 30 * 60
LOCATION_ADDRESS_MAX_AGE_SEC = 20 * 60
RECENT_EVENT_MAX_AGE_SEC = 3 * 60 * 60
SUMMON_EVENT_MAX_AGE_SEC = 24 * 60 * 60

MAX_OBSERVATIONS = 8
MAX_DEVICE_DERIVED = 3
MAX_RECENT_EVENTS = 8
MAX_BASELINE_DEVIATIONS = 3
MAX_AVAILABILITY = 4
MAX_RENDERED_CHARS = 1400


@dataclass(frozen=True)
class ContextKeyPolicy:
    key: str
    section: str
    kinds: tuple[str, ...]
    sources: tuple[str, ...]
    max_age_sec: float
    confidence_field: str | None = None


CONTEXT_KEY_POLICIES = {
    "phone.screen": ContextKeyPolicy(
        "phone.screen", "observations", ("activity.app", "sensing.sensor"),
        ("android.activity", "android.sensing"), PHONE_CURRENT_MAX_AGE_SEC,
    ),
    "phone.motion": ContextKeyPolicy(
        "phone.motion", "device_derived", ("sensing.sensor",),
        ("android.sensing",), PHONE_CURRENT_MAX_AGE_SEC, "motion_confidence",
    ),
    "phone.light_lux": ContextKeyPolicy(
        "phone.light_lux", "observations", ("sensing.sensor",),
        ("android.sensing",), PHONE_CURRENT_MAX_AGE_SEC,
    ),
    "phone.battery": ContextKeyPolicy(
        "phone.battery", "observations", ("sensing.sensor",),
        ("android.sensing",), PHONE_CURRENT_MAX_AGE_SEC,
    ),
    "phone.charging": ContextKeyPolicy(
        "phone.charging", "observations", ("sensing.sensor",),
        ("android.sensing",), PHONE_CURRENT_MAX_AGE_SEC,
    ),
    "phone.wifi": ContextKeyPolicy(
        "phone.wifi", "observations", ("sensing.sensor",),
        ("android.sensing",), PHONE_CURRENT_MAX_AGE_SEC,
    ),
    "mobile.*.foreground_app": ContextKeyPolicy(
        "mobile.*.foreground_app", "observations", ("activity.app",),
        ("android.activity", "legacy.activity"), PHONE_CURRENT_MAX_AGE_SEC,
    ),
    "pc.state": ContextKeyPolicy(
        "pc.state", "observations", ("activity.app",),
        ("pc.activity", "pc.context"), PC_CURRENT_MAX_AGE_SEC,
    ),
    "pc.foreground_app": ContextKeyPolicy(
        "pc.foreground_app", "observations", ("activity.app",),
        ("pc.activity", "pc.context"), PC_CURRENT_MAX_AGE_SEC,
    ),
    "location.place": ContextKeyPolicy(
        "location.place", "observations", ("location.state",),
        ("location.v2",), LOCATION_CURRENT_MAX_AGE_SEC,
    ),
    "location.address": ContextKeyPolicy(
        "location.address", "observations", ("location.state",),
        ("location.v2",), LOCATION_ADDRESS_MAX_AGE_SEC,
    ),
    "phone.unlock": ContextKeyPolicy(
        "phone.unlock", "recent_events", ("sensing.unlock",),
        ("android.sensing",), RECENT_EVENT_MAX_AGE_SEC,
    ),
    "phone.notification": ContextKeyPolicy(
        "phone.notification", "recent_events", ("sensing.notification",),
        ("android.sensing",), RECENT_EVENT_MAX_AGE_SEC,
    ),
    "relationship.summon": ContextKeyPolicy(
        "relationship.summon", "recent_events", ("presence.summon",),
        ("presence.summon",), SUMMON_EVENT_MAX_AGE_SEC,
    ),
}

PROJECTION_KINDS = frozenset(
    kind for policy in CONTEXT_KEY_POLICIES.values() for kind in policy.kinds
)
PROJECTION_SOURCES = frozenset(
    source
    for policy in CONTEXT_KEY_POLICIES.values()
    for source in policy.sources
    if source not in {"pc.context"}
)


__all__ = [
    "CONTEXT_KEY_POLICIES",
    "ContextKeyPolicy",
    "LOCATION_ADDRESS_MAX_AGE_SEC",
    "LOCATION_CURRENT_MAX_AGE_SEC",
    "MAX_AVAILABILITY",
    "MAX_BASELINE_DEVIATIONS",
    "MAX_DEVICE_DERIVED",
    "MAX_OBSERVATIONS",
    "MAX_RECENT_EVENTS",
    "MAX_RENDERED_CHARS",
    "PC_CURRENT_MAX_AGE_SEC",
    "PHONE_CURRENT_MAX_AGE_SEC",
    "PROJECTION_KINDS",
    "PROJECTION_SOURCES",
    "RECENT_EVENT_MAX_AGE_SEC",
    "SUMMON_EVENT_MAX_AGE_SEC",
]
