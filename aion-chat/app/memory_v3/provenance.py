"""Canonical provenance helpers shared by chunks and derived memories."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable


def canonical_source_messages(messages: Iterable[dict]) -> list[dict]:
    rows = [
        {
            "id": str(message.get("id") or ""),
            "role": str(message.get("role") or ""),
            "created_at": float(message.get("created_at") or 0),
            "content": str(message.get("content") or ""),
        }
        for message in messages
    ]
    rows.sort(key=lambda row: (row["created_at"], row["id"]))
    return rows


def source_hash_for_messages(messages: Iterable[dict]) -> str:
    payload = json.dumps(
        canonical_source_messages(messages),
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()
