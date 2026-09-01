"""Durable one-shot delivery state machine for Desktop Presence V1."""

from __future__ import annotations

import asyncio
import json
import time
import uuid
from collections.abc import Callable, Mapping
from typing import Any

import aiosqlite

from database import get_db

from .schema import TrajectoryValidationError, validate_trajectory
from .sprites import SpriteLibrary, SpriteLibraryError, sprite_library


DEFAULT_DEVICE_ID = "pc"
DEFAULT_START_TTL_SEC = 30.0
ACCEPTED_GRACE_SEC = 30.0
ACK_PLAYBACK_GRACE_MS = 2_000
AGENT_ONLINE_WINDOW_SEC = 90.0
MAX_LONG_POLL_SEC = 30.0
TERMINAL_STATUSES = frozenset({"played", "rejected", "expired", "superseded"})
ACK_STATUSES = frozenset({"accepted", "played", "rejected", "expired"})
LEDGER_OUTCOMES = {
    "played": "succeeded",
    "expired": "failed",
    "rejected": "rejected",
    "superseded": "rejected",
}


class PresenceDeliveryError(RuntimeError):
    """Stable protocol error exposed by the Presence API."""


class PresenceNotFound(PresenceDeliveryError):
    pass


class PresenceInvalidTransition(PresenceDeliveryError):
    pass


class PresenceStaleIntent(PresenceDeliveryError):
    pass


def _device_id(value: str) -> str:
    normalized = str(value or DEFAULT_DEVICE_ID).strip()[:64]
    if not normalized:
        raise PresenceDeliveryError("presence_device_id_required")
    return normalized


def _clean_text(value: Any, limit: int) -> str:
    return " ".join(str(value or "").split())[:limit]


def _actual_playback_ms(value: Any, *, duration_ms: int) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool):
        raise PresenceDeliveryError("presence_actual_playback_invalid")
    try:
        normalized = int(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise PresenceDeliveryError("presence_actual_playback_invalid") from exc
    if not 0 <= normalized <= int(duration_ms) + ACK_PLAYBACK_GRACE_MS:
        raise PresenceDeliveryError("presence_actual_playback_invalid")
    return normalized


class PresenceDeliveryService:
    def __init__(
        self,
        *,
        get_db_factory: Callable = get_db,
        sprites: SpriteLibrary = sprite_library,
        now: Callable[[], float] = time.time,
        monotonic: Callable[[], float] = time.monotonic,
        terminal_recorder: Any | None = None,
        outcome_recorder: Any | None = None,
        start_ttl_sec: float = DEFAULT_START_TTL_SEC,
        accepted_grace_sec: float = ACCEPTED_GRACE_SEC,
        agent_online_window_sec: float = AGENT_ONLINE_WINDOW_SEC,
    ):
        self._get_db = get_db_factory
        self.sprites = sprites
        self._now = now
        self._monotonic = monotonic
        self._terminal_recorder = terminal_recorder
        if outcome_recorder is None:
            from .outcomes import PresenceOutcomeInbox

            outcome_recorder = PresenceOutcomeInbox(
                get_db_factory=get_db_factory,
                now=now,
            )
        self._outcome_recorder = outcome_recorder
        self.start_ttl_sec = max(1.0, float(start_ttl_sec))
        self.accepted_grace_sec = max(1.0, float(accepted_grace_sec))
        self.agent_online_window_sec = max(1.0, float(agent_online_window_sec))
        self._wake = asyncio.Event()

    async def reserve_intent(
        self,
        *,
        conv_id: str,
        intent_text: str,
        device_id: str = DEFAULT_DEVICE_ID,
    ) -> dict[str, Any]:
        conv_id = _clean_text(conv_id, 160)
        intent_text = _clean_text(intent_text, 1000)
        device_id = _device_id(device_id)
        if not conv_id:
            raise PresenceDeliveryError("presence_conv_id_required")
        if not intent_text:
            raise PresenceDeliveryError("presence_intent_text_required")
        now = self._now()
        intent_id = f"presence_intent_{uuid.uuid4().hex}"
        async with self._get_db() as db:
            await db.execute("BEGIN IMMEDIATE")
            cursor = await db.execute(
                "SELECT latest_version FROM presence_intent_state WHERE device_id=?",
                (device_id,),
            )
            row = await cursor.fetchone()
            version = (int(row[0]) if row else 0) + 1
            await db.execute(
                """
                INSERT INTO presence_intent_state(device_id, latest_version, updated_at)
                VALUES(?,?,?)
                ON CONFLICT(device_id) DO UPDATE SET
                    latest_version=excluded.latest_version,
                    updated_at=excluded.updated_at
                """,
                (device_id, version, now),
            )
            await db.execute(
                """
                INSERT INTO presence_intents(
                    intent_id, conv_id, device_id, intent_version, intent_text,
                    status, event_id, failure_reason, created_at, updated_at
                ) VALUES(?,?,?,?,?,'rendering',NULL,'',?,?)
                """,
                (intent_id, conv_id, device_id, version, intent_text, now, now),
            )
            await db.commit()
        return {
            "intent_id": intent_id,
            "intent_version": version,
            "conv_id": conv_id,
            "device_id": device_id,
            "intent_text": intent_text,
            "status": "rendering",
        }

    async def reject_intent(self, intent_id: str, reason: str) -> None:
        now = self._now()
        async with self._get_db() as db:
            await db.execute(
                """
                UPDATE presence_intents
                SET status='rejected', failure_reason=?, updated_at=?
                WHERE intent_id=? AND status='rendering'
                """,
                (_clean_text(reason, 240), now, str(intent_id or "")),
            )
            await db.commit()

    async def enqueue_trajectory(
        self,
        *,
        trajectory: Mapping[str, Any],
        conv_id: str | None = None,
        intent_text: str = "test trajectory",
        intent_id: str | None = None,
        intent_version: int | None = None,
        device_id: str = DEFAULT_DEVICE_ID,
        start_ttl_sec: float | None = None,
    ) -> dict[str, Any]:
        normalized = validate_trajectory(trajectory)
        device_id = _device_id(device_id)
        if intent_id is None:
            reserved = await self.reserve_intent(
                conv_id=str(conv_id or ""),
                intent_text=intent_text,
                device_id=device_id,
            )
            intent_id = str(reserved["intent_id"])
            intent_version = int(reserved["intent_version"])
        if intent_version is None:
            raise PresenceDeliveryError("presence_intent_version_required")

        now = self._now()
        ttl = self.start_ttl_sec if start_ttl_sec is None else max(1.0, float(start_ttl_sec))
        event_id = f"presence_event_{uuid.uuid4().hex}"
        terminal_rows: list[dict[str, Any]] = []
        stale = False

        async with self._get_db() as db:
            db.row_factory = aiosqlite.Row
            await db.execute("BEGIN IMMEDIATE")
            terminal_rows.extend(await self._expire_in_tx(db, now, device_id=device_id))
            cursor = await db.execute(
                """
                SELECT i.*, s.sprite_hash, s.base_height_dip,
                       s.active, s.archived, COALESCE(ss.status, 'pending') AS sync_status
                FROM presence_intents AS i
                JOIN presence_sprites AS s ON s.sprite_id=?
                LEFT JOIN presence_sprite_sync AS ss
                  ON ss.sprite_hash=s.sprite_hash AND ss.device_id=i.device_id
                WHERE i.intent_id=? AND i.device_id=? AND i.intent_version=?
                """,
                (
                    normalized["sprite_id"],
                    str(intent_id),
                    device_id,
                    int(intent_version),
                ),
            )
            intent_row = await cursor.fetchone()
            if intent_row is None:
                await db.rollback()
                raise PresenceDeliveryError("presence_intent_or_sprite_not_found")
            # The appearance budget starts when the main intent is created,
            # not when rendering happens to finish.  Otherwise renderer time
            # silently grants a fresh TTL and lets stale gestures appear.
            start_before = float(intent_row["created_at"]) + ttl
            cursor = await db.execute(
                "SELECT latest_version FROM presence_intent_state WHERE device_id=?",
                (device_id,),
            )
            state_row = await cursor.fetchone()
            latest_version = int(state_row[0]) if state_row else 0
            if int(intent_version) != latest_version:
                await db.execute(
                    """
                    UPDATE presence_intents
                    SET status='superseded', failure_reason='stale_intent_version', updated_at=?
                    WHERE intent_id=? AND status='rendering'
                    """,
                    (now, str(intent_id)),
                )
                await db.commit()
                stale = True
            elif str(intent_row["status"]) != "rendering":
                await db.rollback()
                raise PresenceInvalidTransition("presence_intent_not_rendering")
            elif not int(intent_row["active"]) or int(intent_row["archived"]):
                await db.rollback()
                raise PresenceDeliveryError("presence_sprite_inactive")
            elif str(intent_row["sync_status"]) != "synced":
                await db.rollback()
                raise PresenceDeliveryError("presence_sprite_not_synced")
            else:
                cursor = await db.execute(
                    """
                    SELECT * FROM presence_events
                    WHERE device_id=? AND status='queued'
                    """,
                    (device_id,),
                )
                queued_rows = [dict(row) for row in await cursor.fetchall()]
                for row in queued_rows:
                    await db.execute(
                        """
                        UPDATE presence_events
                        SET status='superseded', reason='replaced_by_newer_intent',
                            terminal_at=?, terminal_notified_at=NULL, updated_at=?
                        WHERE event_id=? AND status='queued'
                        """,
                        (now, now, row["event_id"]),
                    )
                    await db.execute(
                        """
                        UPDATE presence_intents
                        SET status='superseded', failure_reason='replaced_by_newer_intent',
                            updated_at=? WHERE intent_id=?
                        """,
                        (now, row["intent_id"]),
                    )
                    row.update(
                        status="superseded",
                        reason="replaced_by_newer_intent",
                        terminal_at=now,
                    )
                    terminal_rows.append(row)
                await db.execute(
                    """
                    INSERT INTO presence_events(
                        event_id, correlation_id, conv_id, device_id,
                        intent_id, intent_version, sprite_id, sprite_hash,
                        trajectory_json, duration_ms, status, created_at,
                        start_before, dispatched_at, accepted_at, terminal_at,
                        reason, actual_playback_ms, terminal_notified_at, updated_at
                    ) VALUES(?,?,?,?,?,?,?,?,?,?,'queued',?,?,NULL,NULL,NULL,'',NULL,NULL,?)
                    """,
                    (
                        event_id,
                        event_id,
                        str(intent_row["conv_id"]),
                        device_id,
                        str(intent_id),
                        int(intent_version),
                        normalized["sprite_id"],
                        str(intent_row["sprite_hash"]),
                        json.dumps(normalized, ensure_ascii=False, separators=(",", ":")),
                        int(normalized["duration_ms"]),
                        now,
                        start_before,
                        now,
                    ),
                )
                await db.execute(
                    """
                    UPDATE presence_intents
                    SET status='rendered', event_id=?, failure_reason='', updated_at=?
                    WHERE intent_id=?
                    """,
                    (event_id, now, str(intent_id)),
                )
                await db.commit()

        await self._notify_terminals(terminal_rows)
        if stale:
            raise PresenceStaleIntent("presence_stale_intent_version")
        self._wake.set()
        return {
            "ok": True,
            "status": "queued",
            "event_id": event_id,
            "correlation_id": event_id,
            "intent_id": str(intent_id),
            "intent_version": int(intent_version),
            "sprite_id": normalized["sprite_id"],
            "start_before": start_before,
        }

    async def poll_pending(
        self,
        *,
        timeout: float = MAX_LONG_POLL_SEC,
        device_id: str = DEFAULT_DEVICE_ID,
    ) -> dict[str, Any] | None:
        device_id = _device_id(device_id)
        timeout = max(0.0, min(MAX_LONG_POLL_SEC, float(timeout)))
        deadline = self._monotonic() + timeout
        while True:
            # Clear before the database read: an enqueue between this point and
            # wait() either becomes visible to the read or leaves the event set.
            # Clearing after the read loses that wake-up and can burn the whole
            # start TTL.
            self._wake.clear()
            payload, terminals = await self._poll_once(device_id)
            await self._notify_terminals(terminals)
            if payload is not None:
                return payload
            remaining = deadline - self._monotonic()
            if remaining <= 0:
                return None
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=remaining)
            except asyncio.TimeoutError:
                return None

    async def _poll_once(
        self, device_id: str
    ) -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
        now = self._now()
        selected: dict[str, Any] | None = None
        terminals: list[dict[str, Any]] = []
        async with self._get_db() as db:
            db.row_factory = aiosqlite.Row
            await db.execute("BEGIN IMMEDIATE")
            await db.execute(
                """
                INSERT INTO presence_agent_state(device_id, last_seen_at, updated_at)
                VALUES(?,?,?)
                ON CONFLICT(device_id) DO UPDATE SET
                    last_seen_at=excluded.last_seen_at, updated_at=excluded.updated_at
                """,
                (device_id, now, now),
            )
            terminals.extend(await self._expire_in_tx(db, now, device_id=device_id))
            cursor = await db.execute(
                """
                SELECT e.*, s.base_height_dip
                FROM presence_events AS e
                JOIN presence_sprites AS s ON s.sprite_hash=e.sprite_hash
                WHERE e.device_id=? AND e.status IN ('accepted','dispatched')
                ORDER BY CASE e.status WHEN 'accepted' THEN 0 ELSE 1 END,
                         e.created_at ASC
                LIMIT 1
                """,
                (device_id,),
            )
            inflight = await cursor.fetchone()
            if inflight is not None and str(inflight["status"]) == "dispatched":
                selected = dict(inflight)
            elif inflight is None:
                cursor = await db.execute(
                    """
                    SELECT e.*, s.base_height_dip
                    FROM presence_events AS e
                    JOIN presence_sprites AS s ON s.sprite_hash=e.sprite_hash
                    WHERE e.device_id=? AND e.status='queued' AND e.start_before>?
                    ORDER BY e.created_at DESC LIMIT 1
                    """,
                    (device_id, now),
                )
                queued = await cursor.fetchone()
                if queued is not None:
                    await db.execute(
                        """
                        UPDATE presence_events
                        SET status='dispatched', dispatched_at=COALESCE(dispatched_at, ?),
                            updated_at=?
                        WHERE event_id=? AND status='queued'
                        """,
                        (now, now, str(queued["event_id"])),
                    )
                    selected = dict(queued)
                    selected["status"] = "dispatched"
                    selected["dispatched_at"] = now
            await db.commit()
        return (self._delivery_payload(selected, now) if selected else None), terminals

    @staticmethod
    def _delivery_payload(row: Mapping[str, Any], server_now: float) -> dict[str, Any]:
        trajectory = validate_trajectory(json.loads(str(row["trajectory_json"])))
        return {
            "event_id": str(row["event_id"]),
            "server_now": server_now,
            "remaining_ttl_ms": max(
                0, int(round((float(row["start_before"]) - server_now) * 1000))
            ),
            "sprite_id": str(row["sprite_id"]),
            "sprite_hash": str(row["sprite_hash"]),
            "base_height_dip": float(row["base_height_dip"]),
            "trajectory": trajectory,
        }

    async def ack(
        self,
        event_id: str,
        *,
        status: str,
        reason: str = "",
        actual_playback_ms: int | None = None,
        device_id: str = DEFAULT_DEVICE_ID,
    ) -> dict[str, Any]:
        event_id = str(event_id or "").strip()
        status = str(status or "").strip().lower()
        device_id = _device_id(device_id)
        if status not in ACK_STATUSES:
            raise PresenceDeliveryError("presence_ack_status_invalid")
        now = self._now()
        reason = _clean_text(reason, 240)
        terminal: dict[str, Any] | None = None

        async with self._get_db() as db:
            db.row_factory = aiosqlite.Row
            await db.execute("BEGIN IMMEDIATE")
            cursor = await db.execute(
                "SELECT * FROM presence_events WHERE event_id=? AND device_id=?",
                (event_id, device_id),
            )
            row = await cursor.fetchone()
            if row is None:
                await db.rollback()
                raise PresenceNotFound("presence_event_not_found")
            try:
                actual_playback_ms = _actual_playback_ms(
                    actual_playback_ms,
                    duration_ms=int(row["duration_ms"]),
                )
            except PresenceDeliveryError:
                await db.rollback()
                raise
            current = str(row["status"])

            if current in TERMINAL_STATUSES:
                if status == "played" and current in {"expired", "rejected"}:
                    await db.execute(
                        """
                        UPDATE presence_events
                        SET status='played', reason=?, actual_playback_ms=?,
                            terminal_at=?, terminal_notified_at=NULL, updated_at=?
                        WHERE event_id=?
                        """,
                        (reason or "late_played_ack", actual_playback_ms, now, now, event_id),
                    )
                    terminal = dict(row)
                    terminal.update(
                        status="played",
                        reason=reason or "late_played_ack",
                        actual_playback_ms=actual_playback_ms,
                        terminal_at=now,
                    )
                    current = "played"
                else:
                    await db.commit()
                    return self._ack_payload(dict(row))
            elif status == "accepted":
                if current == "accepted":
                    await db.commit()
                    return self._ack_payload(dict(row))
                if current != "dispatched":
                    await db.rollback()
                    raise PresenceInvalidTransition(
                        f"presence_ack_transition:{current}->accepted"
                    )
                await db.execute(
                    """
                    UPDATE presence_events
                    SET status='accepted', accepted_at=?, updated_at=?
                    WHERE event_id=? AND status='dispatched'
                    """,
                    (now, now, event_id),
                )
                current = "accepted"
            else:
                # If the accepted response was lost after the PC durably saved
                # it, a later played ACK is authoritative.  Materialize the
                # implicit accepted transition in this same transaction.
                allowed = current in {"dispatched", "accepted"}
                if not allowed:
                    await db.rollback()
                    raise PresenceInvalidTransition(
                        f"presence_ack_transition:{current}->{status}"
                    )
                await db.execute(
                    """
                    UPDATE presence_events
                    SET status=?, reason=?, actual_playback_ms=?, terminal_at=?,
                        accepted_at=CASE
                            WHEN ?='played' THEN COALESCE(accepted_at, ?)
                            ELSE accepted_at
                        END,
                        terminal_notified_at=NULL, updated_at=?
                    WHERE event_id=?
                    """,
                    (
                        status,
                        reason,
                        actual_playback_ms,
                        now,
                        status,
                        now,
                        now,
                        event_id,
                    ),
                )
                terminal = dict(row)
                terminal.update(
                    status=status,
                    reason=reason,
                    actual_playback_ms=actual_playback_ms,
                    terminal_at=now,
                )
                current = status
            await db.commit()

        if terminal is not None:
            if terminal["status"] == "rejected" and terminal.get("reason") == "sprite_missing":
                try:
                    await self.sprites.mark_missing(
                        str(terminal["sprite_hash"]), device_id=device_id
                    )
                except SpriteLibraryError:
                    pass
            await self._notify_terminals([terminal])
            self._wake.set()
        stored = await self.get_event(event_id)
        if stored is None:
            raise PresenceNotFound("presence_event_not_found")
        return self._ack_payload(stored)

    @staticmethod
    def _ack_payload(row: Mapping[str, Any]) -> dict[str, Any]:
        return {
            "ok": True,
            "event_id": str(row["event_id"]),
            "status": str(row["status"]),
            "reason": str(row.get("reason") or ""),
            "actual_playback_ms": row.get("actual_playback_ms"),
        }

    async def get_event(self, event_id: str) -> dict[str, Any] | None:
        async with self._get_db() as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                "SELECT * FROM presence_events WHERE event_id=?",
                (str(event_id or ""),),
            )
            row = await cursor.fetchone()
        return dict(row) if row else None

    async def touch_agent(self, *, device_id: str = DEFAULT_DEVICE_ID) -> None:
        device_id = _device_id(device_id)
        now = self._now()
        async with self._get_db() as db:
            await db.execute(
                """
                INSERT INTO presence_agent_state(device_id, last_seen_at, updated_at)
                VALUES(?,?,?)
                ON CONFLICT(device_id) DO UPDATE SET
                    last_seen_at=excluded.last_seen_at, updated_at=excluded.updated_at
                """,
                (device_id, now, now),
            )
            await db.commit()

    async def agent_online(self, *, device_id: str = DEFAULT_DEVICE_ID) -> bool:
        device_id = _device_id(device_id)
        cutoff = self._now() - self.agent_online_window_sec
        async with self._get_db() as db:
            cursor = await db.execute(
                "SELECT 1 FROM presence_agent_state WHERE device_id=? AND last_seen_at>=?",
                (device_id, cutoff),
            )
            row = await cursor.fetchone()
        return row is not None

    async def ready_for_show(self, *, device_id: str = DEFAULT_DEVICE_ID) -> bool:
        return await self.agent_online(device_id=device_id) and await self.sprites.has_available_sprites(
            device_id=device_id
        )

    async def expire_leases(self) -> int:
        now = self._now()
        async with self._get_db() as db:
            db.row_factory = aiosqlite.Row
            await db.execute("BEGIN IMMEDIATE")
            terminals = await self._expire_in_tx(db, now)
            cursor = await db.execute(
                """
                SELECT * FROM presence_events
                WHERE status IN ('played','rejected','expired','superseded')
                  AND terminal_notified_at IS NULL
                ORDER BY terminal_at ASC
                """
            )
            pending = [dict(row) for row in await cursor.fetchall()]
            await db.commit()
        by_id = {str(row["event_id"]): row for row in [*terminals, *pending]}
        await self._notify_terminals(list(by_id.values()))
        if by_id:
            self._wake.set()
        return len(terminals)

    async def run_lease_cleanup_loop(self, interval_sec: float = 5.0) -> None:
        while True:
            await asyncio.sleep(max(1.0, float(interval_sec)))
            try:
                await self.expire_leases()
            except asyncio.CancelledError:
                raise
            except Exception:
                # Delivery polling also performs cleanup, so a transient cleanup
                # failure must not kill the application lifespan.
                continue

    async def _expire_in_tx(
        self,
        db,
        now: float,
        *,
        device_id: str | None = None,
    ) -> list[dict[str, Any]]:
        device_clause = ""
        if device_id is not None:
            device_clause = " AND device_id=?"
        cursor = await db.execute(
            """
            SELECT * FROM presence_events
            WHERE (
                (status IN ('queued','dispatched') AND start_before<=?)
                OR
                (status='accepted' AND accepted_at IS NOT NULL
                    AND accepted_at + (duration_ms / 1000.0) + ? <= ?)
            )
            """ + device_clause,
            # Query order is queued cutoff, accepted grace, accepted now.
            (now, self.accepted_grace_sec, now, *([device_id] if device_id is not None else [])),
        )
        rows = [dict(row) for row in await cursor.fetchall()]
        terminals: list[dict[str, Any]] = []
        for row in rows:
            reason = (
                "delivery_lease_expired"
                if str(row["status"]) == "accepted"
                else "start_before_elapsed"
            )
            await db.execute(
                """
                UPDATE presence_events
                SET status='expired', reason=?, terminal_at=?,
                    terminal_notified_at=NULL, updated_at=?
                WHERE event_id=? AND status=?
                """,
                (reason, now, now, row["event_id"], row["status"]),
            )
            row.update(status="expired", reason=reason, terminal_at=now)
            terminals.append(row)
        return terminals

    async def _notify_terminals(self, rows: list[dict[str, Any]]) -> None:
        for row in rows:
            status = str(row.get("status") or "")
            outcome = LEDGER_OUTCOMES.get(status)
            if outcome is None:
                continue
            recorder = self._terminal_recorder
            if recorder is None:
                from app.tools.ledger import tool_invocation_ledger

                recorder = tool_invocation_ledger
            result = {
                "event_id": str(row.get("event_id") or ""),
                "presence_status": status,
                "reason": str(row.get("reason") or ""),
                "actual_playback_ms": row.get("actual_playback_ms"),
            }
            try:
                await self._outcome_recorder.record_terminal(
                    event_id=str(row.get("event_id") or ""),
                    conv_id=str(row.get("conv_id") or ""),
                    status=status,
                    reason=str(row.get("reason") or ""),
                    actual_playback_ms=row.get("actual_playback_ms"),
                    payload=result,
                    created_at=float(row.get("terminal_at") or self._now()),
                )
                await recorder.record_terminal_outcome(
                    correlation_id=str(row.get("correlation_id") or row.get("event_id") or ""),
                    outcome=outcome,
                    event_type=f"presence.{status}",
                    error=str(row.get("reason") or "") if status != "played" else "",
                    result=result,
                )
            except Exception:
                # The terminal state itself is authoritative.  Leave
                # terminal_notified_at NULL so the 5s cleanup loop retries the
                # two observation sinks without failing the ACK response.
                continue
            notified_at = self._now()
            async with self._get_db() as db:
                await db.execute(
                    """
                    UPDATE presence_events SET terminal_notified_at=?, updated_at=?
                    WHERE event_id=? AND status=?
                    """,
                    (notified_at, notified_at, row.get("event_id"), status),
                )
                await db.commit()


presence_service = PresenceDeliveryService()


__all__ = [
    "ACCEPTED_GRACE_SEC",
    "ACK_PLAYBACK_GRACE_MS",
    "ACK_STATUSES",
    "DEFAULT_START_TTL_SEC",
    "PresenceDeliveryError",
    "PresenceDeliveryService",
    "PresenceInvalidTransition",
    "PresenceNotFound",
    "PresenceStaleIntent",
    "presence_service",
]
