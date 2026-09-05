from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Callable
from typing import Any

import aiosqlite

from app.background_tasks import create_tracked_task
from database import get_db

from .ledger import ControlLedger
from .schemas import ControlKind, ControlPromptContext, ControlSession
from .legacy_policy import control_legacy_toy_fallback_enabled
STALE_AFTER_SECONDS = 45.0
TIMEOUT_AFTER_SECONDS = 180.0
SAFETY_TOMBSTONE_SECONDS = 20 * 60
EPOCH_INCREMENT_REASONS = frozenset({"panic", "safeword", "device_emergency_stop"})
TIDE_DEFAULT_DEVICE_ID = "muse"
TIDE_DEFAULT_RESOURCE_ID = "toy:muse"
TOY_BRIDGE_DEVICE_ID = "browser_toy_bridge"
TOY_BRIDGE_CAPABILITIES = ("status.read", "notify.pulse", "toy.legacy_command")
FRONTEND_TOY_DRIVER_IDS = frozenset({"sosexy", "cx492b", "sk30", "sk40"})
_tide_switch_lock = asyncio.Lock()

class ControlSessionNotFound(LookupError): pass
class ControlOwnerMismatch(PermissionError): pass
class ControlClaimRejected(PermissionError): pass

async def init_control_tables(db) -> None:
    await db.execute("""
        CREATE TABLE IF NOT EXISTS control_sessions (
            session_id TEXT PRIMARY KEY, conv_id TEXT NOT NULL, kind TEXT NOT NULL, status TEXT NOT NULL,
            owner_client_id TEXT NOT NULL, device_id TEXT, control_resource_id TEXT,
            started_at REAL NOT NULL, last_heartbeat_at REAL NOT NULL,
            last_snapshot_at REAL, ended_at REAL, close_reason TEXT, control_epoch INTEGER NOT NULL DEFAULT 0,
            safeword_set INTEGER NOT NULL DEFAULT 0, frontend_snapshot_json TEXT, metadata_json TEXT NOT NULL DEFAULT '{}'
        )
    """)
    await db.execute("CREATE INDEX IF NOT EXISTS idx_control_sessions_conv_status ON control_sessions(conv_id, status, started_at DESC)")
    try:
        await db.execute("ALTER TABLE control_sessions ADD COLUMN control_resource_id TEXT")
    except aiosqlite.OperationalError:
        pass
    await db.execute("CREATE INDEX IF NOT EXISTS idx_control_sessions_tide_status ON control_sessions(kind, status, started_at DESC)")

class ControlSessionService:
    def __init__(
        self, *, get_db_factory: Callable = get_db, now: Callable[[], float] = time.time,
        ledger: ControlLedger | None = None, outcome_service: Any | None = None,
        device_service_adapter: Any | None = None,
    ):
        self._get_db = get_db_factory
        self._now = now
        self._ledger = ledger or ControlLedger(get_db_factory=get_db_factory, now=now)
        self._device_service = device_service_adapter
        if outcome_service is None:
            from .outcome import ControlOutcomeService
            outcome_service = ControlOutcomeService(get_db_factory=get_db_factory, now=now, ledger=self._ledger)
        self._outcomes = outcome_service

    async def start(
        self,
        *,
        conv_id: str,
        kind: ControlKind,
        owner_client_id: str,
        device_id: str | None = None,
        control_resource_id: str | None = None,
        safeword_set: bool = False,
    ) -> ControlSession:
        if kind == "tide":
            return await self._start_tide(
                conv_id=conv_id,
                owner_client_id=owner_client_id,
                device_id=device_id,
                control_resource_id=control_resource_id,
                safeword_set=safeword_set,
            )
        now = self._now()
        session_id = f"ctrl_{time.time_ns()}"
        async with self._get_db() as db:
            db.row_factory = aiosqlite.Row
            epoch = await self._max_epoch(db, conv_id)
            old_sessions = await self._active_sessions(db, conv_id)
            for old in old_sessions:
                await self._mark_ended(db, old, now=now, reason="superseded", epoch=old.control_epoch)
            await db.execute(
                "INSERT INTO control_sessions (session_id, conv_id, kind, status, owner_client_id, device_id, control_resource_id, started_at, last_heartbeat_at, control_epoch, safeword_set, metadata_json) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (session_id, conv_id, kind, "active", owner_client_id, device_id, control_resource_id, now, now, epoch, int(safeword_set), "{}"),
            )
            await db.commit()
        for old in old_sessions:
            if old.kind == "tide":
                from app.tide.renderer import tide_renderer_registry
                await tide_renderer_registry.stop_session(old, emit_stop=True, reason="superseded")
        for old in old_sessions:
            await self._ledger.record("control.session.ended", conv_id=old.conv_id, session_id=old.session_id, metadata={"close_reason": "superseded"})
            await self._complete_outcome(old.session_id, "superseded")
        await self._ledger.record("control.session.started", conv_id=conv_id, session_id=session_id, metadata={"kind": kind, "owner_client_id": owner_client_id})
        session = await self._get_by_id(session_id)
        if session is None:
            raise ControlSessionNotFound(session_id)
        self._schedule_agenda(session.session_id)
        return session

    async def _start_tide(
        self,
        *,
        conv_id: str,
        owner_client_id: str,
        device_id: str | None,
        control_resource_id: str | None,
        safeword_set: bool,
    ) -> ControlSession:
        from app.tide.renderer import tide_renderer_registry

        normalized_device_id = str(device_id or TIDE_DEFAULT_DEVICE_ID).strip() or TIDE_DEFAULT_DEVICE_ID
        normalized_resource_id = str(control_resource_id or TIDE_DEFAULT_RESOURCE_ID).strip() or TIDE_DEFAULT_RESOURCE_ID
        async with _tide_switch_lock:
            now = self._now()
            session_id = f"ctrl_{time.time_ns()}"
            reused: ControlSession | None = None
            old_sessions: list[ControlSession] = []
            async with self._get_db() as db:
                db.row_factory = aiosqlite.Row
                active_tide = await self._active_tide_sessions(db)
                for current in active_tide:
                    if (
                        current.conv_id == conv_id
                        and current.owner_client_id == owner_client_id
                        and current.device_id == normalized_device_id
                        and current.control_resource_id == normalized_resource_id
                    ):
                        reused = current
                        break
                if reused:
                    await db.execute(
                        "UPDATE control_sessions SET status='active', last_heartbeat_at=? WHERE session_id=?",
                        (now, reused.session_id),
                    )
                    await db.commit()
                    session = await self._require_session(reused.session_id)
                    await tide_renderer_registry.activate(session)
                    return session

                old_by_id = {session.session_id: session for session in active_tide}
                for session in await self._active_sessions(db, conv_id):
                    old_by_id[session.session_id] = session
                old_sessions = list(old_by_id.values())
                for old in old_sessions:
                    if old.kind == "tide":
                        await tide_renderer_registry.stop_session(old, emit_stop=True, reason="superseded")
                    await self._mark_ended(db, old, now=now, reason="superseded", epoch=old.control_epoch)
                await db.execute(
                    "INSERT INTO control_sessions (session_id, conv_id, kind, status, owner_client_id, device_id, control_resource_id, started_at, last_heartbeat_at, control_epoch, safeword_set, metadata_json) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                    (session_id, conv_id, "tide", "active", owner_client_id, normalized_device_id, normalized_resource_id, now, now, 0, int(safeword_set), "{}"),
                )
                await db.commit()
            for old in old_sessions:
                await self._ledger.record(
                    "control.session.ended",
                    conv_id=old.conv_id,
                    session_id=old.session_id,
                    metadata={"close_reason": "superseded", "kind": "tide"},
                )
                await self._complete_outcome(old.session_id, "superseded")
            await self._ledger.record(
                "control.session.started",
                conv_id=conv_id,
                session_id=session_id,
                metadata={
                    "kind": "tide",
                    "owner_client_id": owner_client_id,
                    "control_resource_id": normalized_resource_id,
                },
            )
            session = await self._require_session(session_id)
            self._schedule_agenda(session.session_id)
            await tide_renderer_registry.activate(session)
            return session

    async def heartbeat(self, *, session_id: str, owner_client_id: str) -> ControlSession:
        session = await self._require_owner(session_id, owner_client_id)
        if session.status == "ended": return session
        now = self._now()
        async with self._get_db() as db:
            db.row_factory = aiosqlite.Row
            session = await self._refresh_status(db, session)
            if session.status == "ended": return session
            resumed = session.status == "stale"
            await db.execute(
                "UPDATE control_sessions SET status='active', last_heartbeat_at=? WHERE session_id=?",
                (now, session_id),
            )
            await db.commit()
        await self._ledger.record("control.session.heartbeat", conv_id=session.conv_id, session_id=session_id)
        if resumed:
            await self._ledger.record("control.session.resumed", conv_id=session.conv_id, session_id=session_id)
        return await self._require_session(session_id)

    async def snapshot(self, *, session_id: str, owner_client_id: str, frontend_snapshot_json: Any) -> ControlSession:
        session = await self._require_owner(session_id, owner_client_id)
        if session.status == "ended": return session
        snapshot = self._snapshot_text(frontend_snapshot_json)
        snapshot_data = self._snapshot_dict(snapshot)
        now = self._now()
        async with self._get_db() as db:
            db.row_factory = aiosqlite.Row
            session = await self._refresh_status(db, session)
            if session.status == "ended": return session
            resumed = session.status == "stale"
            await db.execute(
                "UPDATE control_sessions SET status='active', last_heartbeat_at=?, last_snapshot_at=?, frontend_snapshot_json=? WHERE session_id=?",
                (now, now, snapshot, session_id),
            )
            await db.commit()
        await self._ledger.record("control.snapshot.updated", conv_id=session.conv_id, session_id=session_id)
        if resumed:
            await self._ledger.record("control.session.resumed", conv_id=session.conv_id, session_id=session_id, metadata={"source": "snapshot"})
        await self._refresh_toy_bridge_from_snapshot(session, snapshot_data)
        return await self._require_session(session_id)

    async def end(self, *, session_id: str, owner_client_id: str, close_reason: str) -> ControlSession:
        session = await self._require_owner(session_id, owner_client_id)
        if session.status == "ended": return session
        now = self._now()
        new_epoch = session.control_epoch + (1 if close_reason in EPOCH_INCREMENT_REASONS else 0)
        async with self._get_db() as db:
            await self._mark_ended(db, session, now=now, reason=close_reason, epoch=new_epoch)
            await db.commit()
        if session.kind == "tide":
            from app.tide.renderer import tide_renderer_registry
            await tide_renderer_registry.stop_session(session, emit_stop=True, reason=close_reason)
        if close_reason == "panic":
            await self._ledger.record("control.panic.triggered", conv_id=session.conv_id, session_id=session_id)
        if close_reason == "safeword":
            await self._ledger.record("control.safeword.triggered", conv_id=session.conv_id, session_id=session_id)
        await self._ledger.record("control.session.ended", conv_id=session.conv_id, session_id=session_id, metadata={"close_reason": close_reason})
        await self._complete_outcome(session_id, close_reason)
        return await self._require_session(session_id)

    async def get_current(self, *, conv_id: str) -> ControlSession | None:
        async with self._get_db() as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute(
                "SELECT * FROM control_sessions WHERE conv_id=? AND status IN ('active','stale') ORDER BY started_at DESC LIMIT 1",
                (conv_id,),
            )
            row = await cur.fetchone()
            if not row:
                return None
            session = await self._refresh_status(db, ControlSession.from_row(row))
        return None if session.status == "ended" else session

    async def get_current_tide(self) -> ControlSession | None:
        async with self._get_db() as db:
            db.row_factory = aiosqlite.Row
            sessions = await self._active_tide_sessions(db)
            current: ControlSession | None = None
            for session in sessions:
                refreshed = await self._refresh_status(db, session)
                if refreshed.status != "ended" and current is None:
                    current = refreshed
            return current

    async def claim_tide_session(self, *, session_id: str, owner_client_id: str) -> ControlSession:
        async with _tide_switch_lock:
            now = self._now()
            async with self._get_db() as db:
                db.row_factory = aiosqlite.Row
                active_tide = []
                for session in await self._active_tide_sessions(db):
                    refreshed = await self._refresh_status(db, session)
                    if refreshed.status != "ended":
                        active_tide.append(refreshed)
                if len(active_tide) != 1 or active_tide[0].session_id != session_id:
                    raise ControlClaimRejected(session_id)
                session = active_tide[0]
                await db.execute(
                    "UPDATE control_sessions SET owner_client_id=?, status='active', last_heartbeat_at=? WHERE session_id=?",
                    (owner_client_id, now, session_id),
                )
                await db.execute(
                    """
                    UPDATE tide_renderer_state
                    SET owner_client_id=?, updated_at=?
                    WHERE control_resource_id=? AND control_session_id=?
                    """,
                    (owner_client_id, now, session.control_resource_id or TIDE_DEFAULT_RESOURCE_ID, session_id),
                )
                await db.commit()

            claimed = await self._require_session(session_id)
            from app.tide.renderer import tide_renderer_registry

            await tide_renderer_registry.rebind_session(claimed)
            await self._ledger.record(
                "control.session.claimed",
                conv_id=claimed.conv_id,
                session_id=claimed.session_id,
                metadata={"kind": "tide", "owner_client_id": owner_client_id},
            )
            return claimed

    async def get_prompt_context(self, conv_id: str, request_body_or_query: Any = None) -> ControlPromptContext:
        session = await self.get_current(conv_id=conv_id)
        if session and session.status == "active":
            snapshot = self._snapshot_dict(session.frontend_snapshot_json)
            from .agenda import get_prompt_agenda
            agenda = await get_prompt_agenda(session.session_id, get_db_factory=self._get_db)
            return self._context_from_data(snapshot, session=session, source="control_session", active=True, agenda=agenda)
        data = self._request_data(request_body_or_query)
        tombstone = await self._recent_safety_tombstone(conv_id)
        if tombstone:
            return self._safety_tombstone_context(tombstone)
        if self._has_legacy_control(data) and control_legacy_toy_fallback_enabled():
            kind = "dom" if self._bool(data.get("ai_dom_mode")) else "whisper"
            return self._context_from_data(data, kind=kind, source="legacy_body", active=True)
        return ControlPromptContext()

    async def get_session(self, session_id: str) -> ControlSession | None:
        return await self._get_by_id(session_id)

    async def recent_safety_tombstone(self, conv_id: str) -> ControlSession | None:
        return await self._recent_safety_tombstone(conv_id)

    async def _refresh_status(self, db, session: ControlSession) -> ControlSession:
        if session.status == "ended":
            return session
        now = self._now()
        elapsed = now - session.last_heartbeat_at
        if elapsed > TIMEOUT_AFTER_SECONDS:
            await self._mark_ended(db, session, now=now, reason="timeout", epoch=session.control_epoch)
            await db.commit()
            if session.kind == "tide":
                from app.tide.renderer import tide_renderer_registry
                await tide_renderer_registry.stop_session(session, emit_stop=True, reason="timeout")
            await self._ledger.record("control.session.ended", conv_id=session.conv_id, session_id=session.session_id, metadata={"close_reason": "timeout"})
            await self._complete_outcome(session.session_id, "timeout")
            return await self._require_session(session.session_id)
        if elapsed > STALE_AFTER_SECONDS and session.status != "stale":
            await db.execute("UPDATE control_sessions SET status='stale' WHERE session_id=?", (session.session_id,))
            await db.commit()
            await self._ledger.record("control.session.stale", conv_id=session.conv_id, session_id=session.session_id)
            return await self._require_session(session.session_id)
        return session

    async def _get_by_id(self, session_id: str) -> ControlSession | None:
        async with self._get_db() as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute("SELECT * FROM control_sessions WHERE session_id=?", (session_id,))
            row = await cur.fetchone()
            return ControlSession.from_row(row) if row else None

    async def _recent_safety_tombstone(self, conv_id: str) -> ControlSession | None:
        cutoff = self._now() - SAFETY_TOMBSTONE_SECONDS
        async with self._get_db() as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute(
                """
                SELECT * FROM control_sessions
                WHERE conv_id=?
                  AND status='ended'
                  AND close_reason IN ('panic','safeword','device_emergency_stop')
                  AND ended_at IS NOT NULL
                  AND ended_at>=?
                ORDER BY ended_at DESC, started_at DESC
                LIMIT 1
                """,
                (conv_id, cutoff),
            )
            row = await cur.fetchone()
            return ControlSession.from_row(row) if row else None

    async def _require_session(self, session_id: str) -> ControlSession:
        session = await self._get_by_id(session_id)
        if session is None: raise ControlSessionNotFound(session_id)
        return session

    async def _require_owner(self, session_id: str, owner_client_id: str) -> ControlSession:
        session = await self._require_session(session_id)
        if session.owner_client_id != owner_client_id: raise ControlOwnerMismatch(session_id)
        return session

    async def _max_epoch(self, db, conv_id: str) -> int:
        cur = await db.execute("SELECT MAX(control_epoch) AS epoch FROM control_sessions WHERE conv_id=?", (conv_id,))
        row = await cur.fetchone()
        return int((row["epoch"] if row else 0) or 0)

    async def _active_sessions(self, db, conv_id: str) -> list[ControlSession]:
        cur = await db.execute(
            "SELECT * FROM control_sessions WHERE conv_id=? AND status IN ('active','stale')",
            (conv_id,),
        )
        return [ControlSession.from_row(row) for row in await cur.fetchall()]

    async def _active_tide_sessions(self, db) -> list[ControlSession]:
        cur = await db.execute(
            "SELECT * FROM control_sessions WHERE kind='tide' AND status IN ('active','stale') ORDER BY started_at DESC",
        )
        return [ControlSession.from_row(row) for row in await cur.fetchall()]

    async def _mark_ended(self, db, session: ControlSession, *, now: float, reason: str, epoch: int) -> None:
        await db.execute(
            "UPDATE control_sessions SET status='ended', ended_at=?, close_reason=?, control_epoch=? WHERE session_id=?",
            (now, reason, epoch, session.session_id),
        )

    async def _complete_outcome(self, session_id: str, close_reason: str) -> None:
        async def _run():
            try:
                await self._outcomes.generate_for_session(session_id)
            except Exception as exc:
                print(f"[ControlOutcome] skipped: {type(exc).__name__}: {exc}")
        if close_reason == "normal":
            create_tracked_task(_run(), name=f"control_outcome:{session_id}")
        else:
            await _run()

    def _schedule_agenda(self, session_id: str) -> None:
        from .agenda import schedule_control_agenda
        schedule_control_agenda(session_id, get_db_factory=self._get_db, now=self._now)

    def _snapshot_text(self, value: Any) -> str | None:
        if value is None: return None
        if isinstance(value, str): return value
        return json.dumps(value, ensure_ascii=False)

    def _snapshot_dict(self, value: str | None) -> dict:
        if not value: return {}
        try:
            parsed = json.loads(value)
            return parsed if isinstance(parsed, dict) else {}
        except Exception:
            return {}

    async def _refresh_toy_bridge_from_snapshot(self, session: ControlSession, snapshot: dict) -> None:
        if session.kind not in {"dom", "whisper"}:
            return
        service = self._device_service
        if service is None:
            from app.devices import device_service
            service = device_service
        device_id = self._frontend_toy_device_id(session)
        try:
            await service.report_state(
                device_id,
                status="online" if snapshot.get("toy_connected") is True else "offline",
                name="Browser Toy Bridge",
                kind="toy_bridge",
                capabilities=TOY_BRIDGE_CAPABILITIES,
                metadata={
                    "source_event": "control_snapshot",
                    "control_session_id": session.session_id,
                    "control_kind": session.kind,
                    "control_epoch": session.control_epoch,
                    "owner_client_id": session.owner_client_id,
                },
            )
        except Exception as exc:
            print(f"[ControlSession] toy bridge snapshot refresh skipped: {type(exc).__name__}: {exc}")

    def _frontend_toy_device_id(self, session: ControlSession) -> str:
        value = str(session.device_id or "").strip()
        if not value or value in FRONTEND_TOY_DRIVER_IDS:
            return TOY_BRIDGE_DEVICE_ID
        return value

    def _request_data(self, value: Any) -> dict:
        if value is None: return {}
        if isinstance(value, dict): return dict(value)
        if hasattr(value, "model_dump"): return value.model_dump()
        if hasattr(value, "dict"): return value.dict()
        return dict(getattr(value, "__dict__", {}) or {})

    def _context_from_data(self, data: dict, *, source: str, active: bool, session: ControlSession | None = None, kind: str | None = None, agenda: dict | None = None) -> ControlPromptContext:
        return ControlPromptContext(
            session_id=session.session_id if session else None,
            kind=session.kind if session else kind,
            active=active,
            source=source,
            owner_client_id=session.owner_client_id if session else None,
            control_epoch=session.control_epoch if session else None,
            control_resource_id=session.control_resource_id if session else None,
            safeword_set=bool(session.safeword_set) if session else bool(data.get("safeword")),
            dom_history=self._list_value(data.get("dom_history")),
            cnc_enabled=self._bool(data.get("cnc_enabled")),
            cnc_weakness=self._list_value(data.get("cnc_weakness"), separator="|"),
            resist_hits=self._int(data.get("resist_hits")),
            short_streak=self._int(data.get("short_streak")),
            reply_delay_ms=self._int(data.get("reply_delay_ms")),
            compliance_streak=self._int(data.get("compliance_streak")),
            session_elapsed=self._int(data.get("session_elapsed")),
            scene_name=str(data.get("scene_name") or "").strip() or None,
            scene_elapsed=self._int(data.get("scene_elapsed")),
            since_last_punish=None if data.get("since_last_punish") in (None, "") else self._int(data.get("since_last_punish")),
            ratchet_valley=self._int(data.get("ratchet_valley")),
            debt=self._float(data.get("debt")),
            stubborn_streak=self._int(data.get("stubborn_streak")),
            hidden_agenda_brief=agenda.get("brief") if agenda and agenda.get("agenda_status") == "ready" else None,
            hidden_agenda_stance=agenda.get("stance") if agenda and agenda.get("agenda_status") == "ready" else None,
            hidden_agenda_status=agenda.get("agenda_status") if agenda else "none",
            hidden_agenda_source_refs=list(agenda.get("source_refs") or []) if agenda else [],
        )

    def _safety_tombstone_context(self, session: ControlSession) -> ControlPromptContext:
        return ControlPromptContext(
            session_id=session.session_id,
            kind=session.kind,
            active=False,
            source="safety_tombstone",
            owner_client_id=session.owner_client_id,
            control_epoch=session.control_epoch,
            safeword_set=session.safeword_set,
            aftercare_active=True,
            safety_close_reason=session.close_reason,
            safety_closed_at=session.ended_at,
        )

    def _has_legacy_control(self, data: dict) -> bool:
        return self._bool(data.get("ai_dom_mode")) or self._bool(data.get("whisper_mode"))

    def _bool(self, value: Any) -> bool:
        if isinstance(value, str):
            return value.lower() in {"1", "true", "yes", "on"}
        return bool(value)

    def _int(self, value: Any) -> int:
        try:
            return int(value or 0)
        except (TypeError, ValueError):
            return 0

    def _float(self, value: Any) -> float:
        try:
            return float(value or 0)
        except (TypeError, ValueError):
            return 0.0

    def _list_value(self, value: Any, *, separator: str = ",") -> list[str]:
        if value is None:
            return []
        if isinstance(value, str):
            return [item.strip() for item in value.split(separator) if item.strip()]
        if isinstance(value, list | tuple):
            return [str(item).strip() for item in value if str(item).strip()]
        return []


control_session_service = ControlSessionService()
