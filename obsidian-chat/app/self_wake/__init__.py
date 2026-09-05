"""Self-Wake V1 contracts.

The package deliberately has no eager imports so database initialization and
the shared tool registry can import individual modules without cycles.
"""

SELF_WAKE_ENTRY_TOOLS = frozenset({"self_wake.schedule", "self_wake.cancel"})
SELF_WAKE_SURFACE_CAPABILITIES = frozenset(
    {
        "device.ring_touch",
        "pc.screen_check",
        "mobile.screen_check",
        "memory.remember",
        "heart.whisper",
        "location.poi_search",
        "desktop.presence.show",
        "desktop.presence.draw",
    }
)

__all__ = ["SELF_WAKE_ENTRY_TOOLS", "SELF_WAKE_SURFACE_CAPABILITIES"]
