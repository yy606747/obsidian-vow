from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Awaitable, Callable, Mapping
from typing import Any

import aiosqlite

from app.background_tasks import create_tracked_task
from database import get_db

from .digest import ControlDigest, ControlDigestCollector, default_digest_collector
from .schemas import ControlSession

AgendaGenerator = Callable[[Mapping[str, Any]], Awaitable[Mapping[str, Any]]]


async def init_control_agenda_tables(db) -> None:
    await db.execute("""
        CREATE TABLE IF NOT EXISTS control_agendas (
            agenda_id TEXT PRIMARY KEY,
            session_id TEXT NOT NULL UNIQUE,
            conv_id TEXT NOT NULL,
            kind TEXT NOT NULL,
            agenda_status TEXT NOT NULL,
            brief TEXT,
            stance TEXT,
            source_refs_json TEXT NOT NULL DEFAULT '[]',
            agenda_json TEXT NOT NULL DEFAULT '{}',
            error TEXT NOT NULL DEFAULT '',
            created_at REAL NOT NULL,
            updated_at REAL NOT NULL
        )
    """)
    await db.execute("CREATE INDEX IF NOT EXISTS idx_control_agendas_session ON control_agendas(session_id, agenda_status)")


class ControlAgendaService:
    def __init__(
        self,
        *,
        get_db_factory: Callable = get_db,
        now: Callable[[], float] = time.time,
        digest_collector: ControlDigestCollector | None = None,
        generator: AgendaGenerator | None = None,
    ):
        self._get_db = get_db_factory
        self._now = now
        self._digests = digest_collector or default_digest_collector(get_db_factory=get_db_factory)
        self._generator = generator

    async def schedule_for_session(self, session_id: str) -> None:
        inserted = await self._ensure_pending(session_id)
        if not inserted:
            return
        create_tracked_task(self._run_job(session_id), name=f"control_agenda:{session_id}")

    async def generate_for_session(self, session_id: str) -> dict[str, Any]:
        session = await self._load_session(session_id)
        if session is None:
            raise LookupError(session_id)
        await self._ensure_pending(session_id)
        digests = await self._digests.collect(conv_id=session.conv_id, kind=session.kind)
        if not digests:
            payload = self._payload(session, status="empty", brief=None, stance=None, source_refs=[], agenda={"reason": "insufficient_facts"})
            await self._write(payload)
            return payload
        try:
            result = await self._generate(session, digests)
            brief = _short_text(result.get("brief"), limit=180)
            stance = _short_text(result.get("stance"), limit=120)
            status = "ready" if brief or stance else "empty"
            payload = self._payload(session, status=status, brief=brief, stance=stance, source_refs=_source_refs(digests), agenda={"digests": [d.__dict__ for d in digests], "raw": dict(result)})
        except Exception as exc:
            payload = self._payload(session, status="failed", brief=None, stance=None, source_refs=_source_refs(digests), agenda={}, error=f"{type(exc).__name__}: {exc}")
        await self._write(payload)
        return payload

    async def get_prompt_agenda(self, session_id: str) -> dict[str, Any] | None:
        async with self._get_db() as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute("SELECT * FROM control_agendas WHERE session_id=?", (session_id,))
            row = await cur.fetchone()
        if not row:
            return None
        data = dict(row)
        data["source_refs"] = _json_list(data.get("source_refs_json"))
        return data

    async def _run_job(self, session_id: str) -> None:
        try:
            await self.generate_for_session(session_id)
        except Exception as exc:
            await self._mark_failed(session_id, f"{type(exc).__name__}: {exc}")

    async def _ensure_pending(self, session_id: str) -> bool:
        session = await self._load_session(session_id)
        if session is None:
            return False
        agenda_id = f"ctrl_ag_{time.time_ns()}"
        now = self._now()
        async with self._get_db() as db:
            cur = await db.execute(
                "INSERT OR IGNORE INTO control_agendas (agenda_id, session_id, conv_id, kind, agenda_status, created_at, updated_at) VALUES (?,?,?,?,?,?,?)",
                (agenda_id, session.session_id, session.conv_id, session.kind, "pending", now, now),
            )
            await db.commit()
        return bool(getattr(cur, "rowcount", 0))

    async def _generate(self, session: ControlSession, digests: list[ControlDigest]) -> Mapping[str, Any]:
        context = {"session": session.to_api_dict(), "digests": [d.__dict__ for d in digests]}
        if self._generator is not None:
            return await self._generator(context)
        facts = [d.text for d in digests if d.source != "chat_recent"] or [digests[0].text]
        return {"brief": f"延续：{facts[0][:120]}", "stance": "稳住节奏，少解释，多观察。"}

    async def _load_session(self, session_id: str) -> ControlSession | None:
        async with self._get_db() as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute("SELECT * FROM control_sessions WHERE session_id=?", (session_id,))
            row = await cur.fetchone()
        return ControlSession.from_row(row) if row else None

    def _payload(self, session: ControlSession, *, status: str, brief: str | None, stance: str | None, source_refs: list[str], agenda: Mapping[str, Any], error: str = "") -> dict[str, Any]:
        return {"agenda_id": f"ctrl_ag_{time.time_ns()}", "session_id": session.session_id, "conv_id": session.conv_id, "kind": session.kind, "agenda_status": status, "brief": brief, "stance": stance, "source_refs": source_refs, "agenda": dict(agenda), "error": error}

    async def _write(self, payload: Mapping[str, Any]) -> None:
        now = self._now()
        async with self._get_db() as db:
            await db.execute(
                "UPDATE control_agendas SET agenda_status=?, brief=?, stance=?, source_refs_json=?, agenda_json=?, error=?, updated_at=? WHERE session_id=?",
                (payload["agenda_status"], payload["brief"], payload["stance"], json.dumps(payload["source_refs"], ensure_ascii=False), json.dumps(payload["agenda"], ensure_ascii=False), payload["error"], now, payload["session_id"]),
            )
            await db.commit()

    async def _mark_failed(self, session_id: str, error: str) -> None:
        async with self._get_db() as db:
            await db.execute("UPDATE control_agendas SET agenda_status='failed', error=?, updated_at=? WHERE session_id=?", (error, self._now(), session_id))
            await db.commit()


def schedule_control_agenda(session_id: str, *, get_db_factory: Callable = get_db, now: Callable[[], float] = time.time) -> None:
    """Schedule agenda generation from code that is already running on the event loop."""
    asyncio.get_running_loop()
    create_tracked_task(
        ControlAgendaService(get_db_factory=get_db_factory, now=now).schedule_for_session(session_id),
        name=f"control_agenda_schedule:{session_id}",
    )


async def get_prompt_agenda(session_id: str, *, get_db_factory: Callable = get_db) -> dict[str, Any] | None:
    return await ControlAgendaService(get_db_factory=get_db_factory).get_prompt_agenda(session_id)


def _source_refs(digests: list[ControlDigest]) -> list[str]:
    refs: list[str] = []
    for digest in digests:
        refs.extend(digest.source_refs)
    return refs[:12]


def _short_text(value: Any, *, limit: int) -> str | None:
    text = str(value or "").strip()
    return text[:limit] if text else None


def _json_list(value: str | None) -> list[str]:
    try:
        parsed = json.loads(value or "[]")
        return [str(item) for item in parsed] if isinstance(parsed, list) else []
    except Exception:
        return []


control_agenda_service = ControlAgendaService()
