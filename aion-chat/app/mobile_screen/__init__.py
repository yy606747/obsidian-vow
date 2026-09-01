"""Per-device mobile screen capture state machine (Phase 3).

Kept as a separate facade from app/pc_screen on purpose: the client capture
paths (Windows ImageGrab vs Android MediaProjection) share nothing, so V1 does
not unify them. The backend state machine, however, is largely the same shape —
small helpers (status constants, reject-reason validation, vision gate) are
reused/copied from pc_screen and marked ``TODO: dedup`` rather than forked
silently. See MOBILE_MULTI_DEVICE_SCREEN_V1_PLAN §9.
"""

from .schemas import (
    MOBILE_REJECT_REASONS,
    MobileScreenCheckRequest,
)
from .service import MobileScreenService, mobile_screen_service

__all__ = [
    "MOBILE_REJECT_REASONS",
    "MobileScreenCheckRequest",
    "MobileScreenService",
    "mobile_screen_service",
]
