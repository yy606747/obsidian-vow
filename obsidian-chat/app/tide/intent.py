from __future__ import annotations

import time
from collections.abc import Callable

import aiosqlite

from database import get_db


async def init_tide_tables(db) -> None:
    await db.execute("""
        CREATE TABLE IF NOT EXISTS tide_intent_events (
            id TEXT PRIMARY KEY,
            conv_id TEXT NOT NULL,
            msg_id TEXT,
            control_session_id TEXT NOT NULL,
            owner_client_id TEXT NOT NULL,
            control_resource_id TEXT NOT NULL,
            intent_text TEXT NOT NULL,
            created_at REAL NOT NULL
        )
    """)
    await db.execute("""
        CREATE TABLE IF NOT EXISTS tide_renderer_state (
            control_resource_id TEXT PRIMARY KEY,
            control_session_id TEXT,
            conv_id TEXT,
            owner_client_id TEXT,
            intent_event_id TEXT,
            intent_text TEXT,
            intent_version INTEGER NOT NULL DEFAULT 0,
            last_frame_json TEXT NOT NULL DEFAULT '{}',
            updated_at REAL NOT NULL
        )
    """)
    try:
        await db.execute("ALTER TABLE tide_renderer_state ADD COLUMN intent_version INTEGER NOT NULL DEFAULT 0")
    except Exception:
        pass
    await db.execute("CREATE INDEX IF NOT EXISTS idx_tide_intents_session_time ON tide_intent_events(control_session_id, created_at DESC)")
    await db.execute("CREATE INDEX IF NOT EXISTS idx_tide_intents_conv_time ON tide_intent_events(conv_id, created_at DESC)")


class TideIntentService:
    def __init__(self, *, get_db_factory: Callable = get_db, now: Callable[[], float] = time.time):
        self._get_db = get_db_factory
        self._now = now

    async def record_intent(
        self,
        *,
        conv_id: str,
        msg_id: str | None,
        intent_text: str,
        invocation_id: str = "",
        advertised_tools: tuple[str, ...] = (),
    ) -> bool:
        text = " ".join(str(intent_text or "").split())
        if not text:
            return False

        from app.control import control_session_service

        session = await control_session_service.get_current(conv_id=conv_id)
        if not session or session.kind != "tide" or session.status != "active":
            return False
        resource_id = session.control_resource_id or "toy:muse"
        event_id = f"tide_intent_{time.time_ns()}"
        now = self._now()
        async with self._get_db() as db:
            await db.execute(
                """
                INSERT INTO tide_intent_events (
                    id, conv_id, msg_id, control_session_id, owner_client_id,
                    control_resource_id, intent_text, created_at
                ) VALUES (?,?,?,?,?,?,?,?)
                """,
                (
                    event_id,
                    conv_id,
                    msg_id,
                    session.session_id,
                    session.owner_client_id,
                    resource_id,
                    text,
                    now,
                ),
            )
            await db.execute(
                """
                INSERT INTO tide_renderer_state (
                    control_resource_id, control_session_id, conv_id, owner_client_id,
                    intent_event_id, intent_text, intent_version, updated_at
                ) VALUES (?,?,?,?,?,?,1,?)
                ON CONFLICT(control_resource_id) DO UPDATE SET
                    control_session_id=excluded.control_session_id,
                    conv_id=excluded.conv_id,
                    owner_client_id=excluded.owner_client_id,
                    intent_event_id=excluded.intent_event_id,
                    intent_text=excluded.intent_text,
                    intent_version=tide_renderer_state.intent_version + 1,
                    updated_at=excluded.updated_at
                """,
                (
                    resource_id,
                    session.session_id,
                    conv_id,
                    session.owner_client_id,
                    event_id,
                    text,
                    now,
                ),
            )
            await db.commit()

        from app.tools.ledger import tool_invocation_ledger
        from app.tools.schemas import ToolContext

        marker_invocation_id = (
            str(invocation_id or "").strip()
            or tool_invocation_ledger.new_invocation_id("tide_intent")
        )
        marker_context = ToolContext(
            conv_id=conv_id,
            msg_id=msg_id,
            request_id=msg_id or event_id,
            capabilities=("device.toy",),
            metadata={
                "source": "tide_intent",
                "source_chain": "tide",
                "invocation_id": marker_invocation_id,
                "advertised_tools": tuple(advertised_tools),
                "control_session_id": session.session_id,
                "control_resource_id": resource_id,
            },
        )
        await tool_invocation_ledger.record_marker(
            marker_context,
            invocation_id=marker_invocation_id,
            marker_name="TIDE_INTENT",
            raw_text=f"[TIDE_INTENT:{text}[/TIDE_INTENT]",
            normalized={
                "intent_event_id": event_id,
                "intent_text": text,
                "control_session_id": session.session_id,
                "control_resource_id": resource_id,
            },
        )

        from app.tide.renderer import tide_renderer_registry

        await tide_renderer_registry.ensure_running(session)
        tide_renderer_registry.notify_intent(session.session_id)
        return True

    async def bind_active_session(self, session) -> None:
        now = self._now()
        resource_id = session.control_resource_id or "toy:muse"
        async with self._get_db() as db:
            await db.execute(
                """
                INSERT INTO tide_renderer_state (
                    control_resource_id, control_session_id, conv_id, owner_client_id,
                    intent_event_id, intent_text, intent_version, last_frame_json, updated_at
                ) VALUES (?,?,?,?,NULL,NULL,0,'{}',?)
                ON CONFLICT(control_resource_id) DO UPDATE SET
                    control_session_id=excluded.control_session_id,
                    conv_id=excluded.conv_id,
                    owner_client_id=excluded.owner_client_id,
                    intent_event_id=NULL,
                    intent_text=NULL,
                    intent_version=0,
                    last_frame_json='{}',
                    updated_at=excluded.updated_at
                """,
                (resource_id, session.session_id, session.conv_id, session.owner_client_id, now),
            )
            await db.commit()

    async def update_owner(self, *, control_resource_id: str, control_session_id: str, owner_client_id: str) -> None:
        async with self._get_db() as db:
            await db.execute(
                """
                UPDATE tide_renderer_state
                SET owner_client_id=?, updated_at=?
                WHERE control_resource_id=? AND control_session_id=?
                """,
                (owner_client_id, self._now(), control_resource_id, control_session_id),
            )
            await db.commit()

    async def latest_state(self, *, control_resource_id: str) -> dict | None:
        async with self._get_db() as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute(
                "SELECT * FROM tide_renderer_state WHERE control_resource_id=?",
                (control_resource_id,),
            )
            row = await cur.fetchone()
            return dict(row) if row else None

    async def store_frame(self, *, control_resource_id: str, frame_json: str) -> None:
        async with self._get_db() as db:
            await db.execute(
                "UPDATE tide_renderer_state SET last_frame_json=?, updated_at=? WHERE control_resource_id=?",
                (frame_json, self._now(), control_resource_id),
            )
            await db.commit()


tide_intent_service = TideIntentService()
