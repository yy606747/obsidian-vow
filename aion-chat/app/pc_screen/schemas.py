"""PC screen check request contracts."""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field


STATUS_PENDING = "pending"
STATUS_APPROVED = "approved"
STATUS_COMPLETED = "completed"
STATUS_REJECTED = "rejected"

NONTERMINAL_STATUSES = frozenset({STATUS_PENDING, STATUS_APPROVED})
TERMINAL_STATUSES = frozenset({STATUS_COMPLETED, STATUS_REJECTED})

REJECT_REASONS = frozenset({
    "denied",
    "confirm_timeout",
    "offline",
    "locked",
    "rate_limited",
    "duplicate_pending",
    "disabled",
    "model_no_vision",
    "hard_blocked",
    "upload_failed",
    "request_expired",
})


@dataclass
class ScreenCheckRequest:
    request_id: str
    conv_id: str
    msg_id: str
    model_key: str
    reason: str
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
            "reason": self.reason,
            "status": self.status,
            "reject_reason": self.reject_reason,
            "created_at": self.created_at,
            "completed_at": self.completed_at,
        }
