"""Shared, read-only projection of device and environment evidence."""

from .contracts import (
    AvailabilityItem,
    BaselineDeviation,
    ContextDeliveryProjection,
    CurrentContextItem,
    LEGACY_SCHEMA_VERSION,
    RecentContextEvent,
    SCHEMA_VERSION,
    SourceStatus,
    SUPPORTED_SCHEMA_VERSIONS,
)
from .policy import CONTEXT_KEY_POLICIES, ContextKeyPolicy
from .projection import (
    BaselineDeviationProvider,
    NullBaselineDeviationProvider,
    build_context_delivery_projection,
)
from .renderer import render_context_delivery_projection
from .safety import (
    DEVICE_PROXY_HARD_LIMITS,
    TRIGGER_CONTEXT_HARD_LIMIT,
    render_device_proxy_hard_limits,
)


__all__ = [
    "AvailabilityItem",
    "BaselineDeviation",
    "BaselineDeviationProvider",
    "CONTEXT_KEY_POLICIES",
    "ContextDeliveryProjection",
    "ContextKeyPolicy",
    "CurrentContextItem",
    "DEVICE_PROXY_HARD_LIMITS",
    "LEGACY_SCHEMA_VERSION",
    "NullBaselineDeviationProvider",
    "RecentContextEvent",
    "SCHEMA_VERSION",
    "SourceStatus",
    "SUPPORTED_SCHEMA_VERSIONS",
    "TRIGGER_CONTEXT_HARD_LIMIT",
    "build_context_delivery_projection",
    "render_device_proxy_hard_limits",
    "render_context_delivery_projection",
]
