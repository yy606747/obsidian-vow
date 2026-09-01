from __future__ import annotations

import json
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol

import aiosqlite

from database import get_db


@dataclass(frozen=True)
class ControlDigest:
    source: str
    text: str
    source_refs: list[str] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)


class ControlDigestSource(Protocol):
    async def collect(self, *, conv_id: str, kind: str) -> list[ControlDigest]:
        ...


class SessionOutcomeSource:
    def __init__(self, *, get_db_factory: Callable = get_db):
        self._get_db = get_db_factory

    async def collect(self, *, conv_id: str, kind: str) -> list[ControlDigest]:
        async with self._get_db() as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute(
                "SELECT outcome_id, summary, source_refs_json FROM control_session_outcomes WHERE conv_id=? AND kind=? AND outcome_status='completed' ORDER BY created_at DESC LIMIT 1",
                (conv_id, kind),
            )
            row = await cur.fetchone()
        if not row or not str(row["summary"] or "").strip():
            return []
        return [ControlDigest("session_outcome", row["summary"].strip(), _json_list(row["source_refs_json"]) or [row["outcome_id"]])]


class ControlNoteSource:
    def __init__(self, *, get_db_factory: Callable = get_db, limit: int = 3):
        self._get_db = get_db_factory
        self._limit = max(1, int(limit or 3))

    async def collect(self, *, conv_id: str, kind: str) -> list[ControlDigest]:
        async with self._get_db() as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute(
                "SELECT id, content, metadata_json FROM memory_events WHERE source='control' AND conv_id=? ORDER BY created_at DESC LIMIT 20",
                (conv_id,),
            )
            rows = await cur.fetchall()
        digests: list[ControlDigest] = []
        for row in rows:
            meta = _json_obj(row["metadata_json"])
            if meta.get("event_type") != "control.note":
                continue
            text = str(row["content"] or "").strip()
            if text:
                digests.append(ControlDigest("control_note", text, meta.get("source_refs") or [row["id"]]))
            if len(digests) >= self._limit:
                break
        return digests


class ChatRecentSource:
    def __init__(self, *, get_db_factory: Callable = get_db, limit: int = 6):
        self._get_db = get_db_factory
        self._limit = max(1, int(limit or 6))

    async def collect(self, *, conv_id: str, kind: str) -> list[ControlDigest]:
        async with self._get_db() as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute(
                "SELECT id, role, content FROM messages WHERE conv_id=? AND role IN ('user','assistant') ORDER BY created_at DESC LIMIT ?",
                (conv_id, self._limit),
            )
            rows = await cur.fetchall()
        text = "\n".join(f"{row['role']}: {str(row['content'] or '').strip()[:120]}" for row in reversed(rows) if str(row["content"] or "").strip())
        refs = [row["id"] for row in rows]
        return [ControlDigest("chat_recent", text, refs)] if text else []


class CallableDigestSource:
    def __init__(self, source: str, reader: Callable[..., Awaitable[Sequence[str] | str | None]] | None):
        self._source = source
        self._reader = reader

    async def collect(self, *, conv_id: str, kind: str) -> list[ControlDigest]:
        if self._reader is None:
            return []
        value = await self._reader(conv_id=conv_id, kind=kind)
        if value is None:
            return []
        items = [value] if isinstance(value, str) else list(value)
        return [ControlDigest(self._source, str(item).strip(), [self._source]) for item in items if str(item).strip()]


class ControlDigestCollector:
    def __init__(self, sources: Sequence[ControlDigestSource]):
        self._sources = list(sources)

    async def collect(self, *, conv_id: str, kind: str) -> list[ControlDigest]:
        digests: list[ControlDigest] = []
        for source in self._sources:
            digests.extend(await source.collect(conv_id=conv_id, kind=kind))
        return digests


def default_digest_collector(*, get_db_factory: Callable = get_db, sentinel_reader=None, schedule_reader=None, location_reader=None) -> ControlDigestCollector:
    return ControlDigestCollector([
        SessionOutcomeSource(get_db_factory=get_db_factory),
        ControlNoteSource(get_db_factory=get_db_factory),
        CallableDigestSource("sentinel_digest", sentinel_reader),
        CallableDigestSource("schedule_digest", schedule_reader),
        CallableDigestSource("location_digest", location_reader),
        ChatRecentSource(get_db_factory=get_db_factory),
    ])


def _json_list(value: str | None) -> list[str]:
    parsed = _json_obj(value, default=[])
    return [str(item) for item in parsed] if isinstance(parsed, list) else []


def _json_obj(value: str | None, *, default=None):
    try:
        return json.loads(value or "")
    except Exception:
        return [] if default == [] else {}
