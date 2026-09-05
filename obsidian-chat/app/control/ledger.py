from __future__ import annotations

import json
import time
from collections.abc import Callable, Mapping
from typing import Any

from database import get_db


class ControlLedger:
    def __init__(self, *, get_db_factory: Callable = get_db, now: Callable[[], float] = time.time):
        self._get_db = get_db_factory
        self._now = now

    async def record(
        self,
        event_type: str,
        *,
        conv_id: str | None = None,
        session_id: str | None = None,
        content: str | None = None,
        metadata: Mapping[str, Any] | None = None,
    ) -> str:
        created_at = self._now()
        event_id = f"ctl_evt_{time.time_ns()}"
        payload = dict(metadata or {})
        if session_id:
            payload["session_id"] = session_id
        async with self._get_db() as db:
            await db.execute(
                "INSERT INTO memory_events (id, source, namespace, conv_id, role, content, metadata_json, created_at) VALUES (?,?,?,?,?,?,?,?)",
                (
                    event_id,
                    "control",
                    "control",
                    conv_id,
                    None,
                    content or event_type,
                    json.dumps({"event_type": event_type, **payload}, ensure_ascii=False),
                    created_at,
                ),
            )
            await db.commit()
        return event_id
