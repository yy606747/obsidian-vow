"""Mobile screen check request contracts.

Reuses the PC status constants and reject-reason set, extended with the
Android-specific failure modes. The request dataclass mirrors
pc_screen.ScreenCheckRequest plus target-device routing fields.

TODO(dedup): once mobile capture is stable, extract the shared base dataclass
and reject-reason validation into a common app/screen_check module (plan §9.2).
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field

# Shared with PC — same lifecycle states.
from app.pc_screen.schemas import (  # noqa: F401
    NONTERMINAL_STATUSES,
    REJECT_REASONS as PC_REJECT_REASONS,
    STATUS_APPROVED,
    STATUS_COMPLETED,
    STATUS_PENDING,
    STATUS_REJECTED,
    TERMINAL_STATUSES,
)

# Android adds capture-pipeline failures the PC agent never hits.
MOBILE_REJECT_REASONS = PC_REJECT_REASONS | frozenset({
    "permission_denied",   # user/system denied MediaProjection consent
    "projection_failed",   # MediaProjection setup failed
    "capture_failed",      # frame grab/encode failed
    "ambiguous_target",    # multiple devices online and target not specified
})


@dataclass
class MobileScreenCheckRequest:
    request_id: str
    conv_id: str
    msg_id: str
    model_key: str
    reason: str
    target_device_id: str
    target_device_name: str = ""
    target_device_type: str = ""
    platform: str = "android"
    status: str = STATUS_PENDING
    reject_reason: str = ""
    created_at: float = field(default_factory=time.time)
    completed_at: float | None = None
    image_path: str | None = None
    _done_event: asyncio.Event = field(default_factory=asyncio.Event, repr=False, compare=False)

    def to_public(self) -> dict:
        return {
            "request_id": self.request_id,
            "conv_id": self.conv_id,
            "msg_id": self.msg_id,
            "target_device_id": self.target_device_id,
            "target_device_name": self.target_device_name,
            "target_device_type": self.target_device_type,
            "platform": self.platform,
            "reason": self.reason,
            "status": self.status,
            "reject_reason": self.reject_reason,
            "created_at": self.created_at,
            "completed_at": self.completed_at,
        }

    def to_pending(self, ai_name: str = "AI") -> dict:
        """Payload handed to the Android poll loop."""
        return {
            "request_id": self.request_id,
            "target_device_id": self.target_device_id,
            "target_device_name": self.target_device_name,
            "reason": self.reason,
            "ai_name": ai_name,
        }


__all__ = [
    "MOBILE_REJECT_REASONS",
    "MobileScreenCheckRequest",
    "NONTERMINAL_STATUSES",
    "STATUS_APPROVED",
    "STATUS_COMPLETED",
    "STATUS_PENDING",
    "STATUS_REJECTED",
    "TERMINAL_STATUSES",
]
