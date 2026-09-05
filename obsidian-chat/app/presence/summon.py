"""Durable summon facts and the single-process summon coordinator."""

from __future__ import annotations

import asyncio
import time
import uuid
from collections.abc import Awaitable, Callable, Mapping
from typing import Any

import aiosqlite

from database import get_db


SUMMON_EVENT_TTL_SEC = 24 * 60 * 60
SUMMON_STATUSES = frozenset(
    {"processing", "processed", "coalesced", "gated", "failed"}
)


class SummonEventError(ValueError):
    pass


def normalize_summon_id(value: str) -> str:
    try:
        parsed = uuid.UUID(str(value or "").strip())
    except (ValueError, TypeError, AttributeError) as exc:
        raise SummonEventError("invalid_summon_id") from exc
    return str(parsed)


class SummonEventRepository:
    def __init__(
        self,
        *,
        get_db_factory: Callable = get_db,
        now: Callable[[], float] = time.time,
    ):
        self._get_db = get_db_factory
        self._now = now

    async def insert(
        self,
        *,
        summon_id: str,
        conv_id: str,
        device_id: str = "pc",
        occurred_at: float | None = None,
        received_at: float | None = None,
    ) -> dict[str, Any]:
        normalized_id = normalize_summon_id(summon_id)
        normalized_conv = str(conv_id or "").strip()
        normalized_device = str(device_id or "").strip()[:64]
        if not normalized_conv:
            raise SummonEventError("missing_conv_id")
        if not normalized_device:
            raise SummonEventError("missing_device_id")
        received = self._now() if received_at is None else float(received_at)
        occurred = received if occurred_at is None else float(occurred_at)
        if occurred > received + 60:
            raise SummonEventError("summon_occurred_at_in_future")

        async with self._get_db() as db:
            db.row_factory = aiosqlite.Row
            await db.execute("BEGIN IMMEDIATE")
            try:
                await self._cleanup_in_tx(db, now=received)
                cursor = await db.execute(
                    """
                    INSERT INTO summon_events(
                        summon_id, conv_id, device_id, occurred_at, received_at,
                        status, coalesced_into, failure_reason, updated_at
                    ) VALUES(?,?,?,?,?,'processing',NULL,'',?)
                    ON CONFLICT(summon_id) DO NOTHING
                    """,
                    (
                        normalized_id,
                        normalized_conv,
                        normalized_device,
                        occurred,
                        received,
                        received,
                    ),
                )
                inserted = int(cursor.rowcount or 0) == 1
                cursor = await db.execute(
                    "SELECT * FROM summon_events WHERE summon_id=?",
                    (normalized_id,),
                )
                row = await cursor.fetchone()
                await db.commit()
            except BaseException:
                await db.rollback()
                raise
        if row is None:
            raise RuntimeError("summon_event_insert_lost")
        return {"inserted": inserted, "event": dict(row)}

    async def get(self, summon_id: str) -> dict[str, Any] | None:
        normalized_id = normalize_summon_id(summon_id)
        async with self._get_db() as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                "SELECT * FROM summon_events WHERE summon_id=?",
                (normalized_id,),
            )
            row = await cursor.fetchone()
        return dict(row) if row is not None else None

    async def mark_status(
        self,
        summon_id: str,
        *,
        status: str,
        coalesced_into: str | None = None,
        failure_reason: str = "",
        updated_at: float | None = None,
    ) -> bool:
        normalized_id = normalize_summon_id(summon_id)
        normalized_status = str(status or "").strip().lower()
        if normalized_status not in SUMMON_STATUSES:
            raise SummonEventError("invalid_summon_status")
        primary_id = (
            normalize_summon_id(coalesced_into)
            if normalized_status == "coalesced" and coalesced_into
            else None
        )
        if normalized_status == "coalesced" and primary_id is None:
            raise SummonEventError("missing_coalesced_into")
        now = self._now() if updated_at is None else float(updated_at)
        reason = " ".join(str(failure_reason or "").split())[:240]
        async with self._get_db() as db:
            cursor = await db.execute(
                """
                UPDATE summon_events
                SET status=?, coalesced_into=?, failure_reason=?, updated_at=?
                WHERE summon_id=?
                """,
                (normalized_status, primary_id, reason, now, normalized_id),
            )
            await db.commit()
        return int(cursor.rowcount or 0) == 1

    async def recent_facts(
        self,
        *,
        conv_id: str,
        now: float | None = None,
        exclude_summon_id: str | None = None,
    ) -> list[dict[str, Any]]:
        normalized_conv = str(conv_id or "").strip()
        if not normalized_conv:
            return []
        reference_time = self._now() if now is None else float(now)
        excluded = (
            normalize_summon_id(exclude_summon_id)
            if exclude_summon_id
            else None
        )
        async with self._get_db() as db:
            db.row_factory = aiosqlite.Row
            table_cursor = await db.execute(
                "SELECT 1 FROM sqlite_master "
                "WHERE type='table' AND name='summon_events'"
            )
            if await table_cursor.fetchone() is None:
                return []
            await db.execute("BEGIN IMMEDIATE")
            try:
                await self._cleanup_in_tx(db, now=reference_time)
                query = (
                    "SELECT summon_id, conv_id, occurred_at FROM summon_events "
                    "WHERE conv_id=? AND occurred_at>=? AND occurred_at<=?"
                )
                params: list[Any] = [
                    normalized_conv,
                    reference_time - SUMMON_EVENT_TTL_SEC,
                    reference_time,
                ]
                if excluded is not None:
                    query += " AND summon_id<>?"
                    params.append(excluded)
                query += " ORDER BY occurred_at, summon_id"
                cursor = await db.execute(query, params)
                rows = [dict(row) for row in await cursor.fetchall()]
                await db.commit()
            except BaseException:
                rollback = getattr(db, "rollback", None)
                if callable(rollback):
                    await rollback()
                raise
        return rows

    async def cleanup(self, *, now: float | None = None) -> int:
        reference_time = self._now() if now is None else float(now)
        async with self._get_db() as db:
            cursor = await self._cleanup_in_tx(db, now=reference_time)
            await db.commit()
        return max(0, int(cursor.rowcount or 0))

    async def fail_processing_after_restart(
        self,
        *,
        now: float | None = None,
    ) -> int:
        updated_at = self._now() if now is None else float(now)
        async with self._get_db() as db:
            cursor = await db.execute(
                """
                UPDATE summon_events
                SET status='failed', failure_reason='server_restart', updated_at=?
                WHERE status='processing'
                """,
                (updated_at,),
            )
            await db.commit()
        return max(0, int(cursor.rowcount or 0))

    @staticmethod
    async def _cleanup_in_tx(db, *, now: float):
        return await db.execute(
            "DELETE FROM summon_events WHERE occurred_at<?",
            (float(now) - SUMMON_EVENT_TTL_SEC,),
        )


summon_event_repository = SummonEventRepository()


async def resolve_summon_target(
    *,
    get_db_factory: Callable = get_db,
) -> dict[str, Any] | None:
    """Resolve the latest conversation that has an owner-authored turn."""

    from config import DEFAULT_MODEL

    async with get_db_factory() as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT c.id, c.model FROM conversations AS c "
            "WHERE EXISTS ("
            "SELECT 1 FROM messages AS m "
            "WHERE m.conv_id=c.id AND m.role='user'"
            ") ORDER BY c.updated_at DESC LIMIT 1"
        )
        conversation = await cursor.fetchone()
        if conversation is None:
            return None
        cursor = await db.execute(
            "SELECT created_at FROM messages "
            "WHERE conv_id=? AND role='user' "
            "ORDER BY created_at DESC LIMIT 1",
            (conversation["id"],),
        )
        last_user = await cursor.fetchone()
    return {
        "conv_id": str(conversation["id"]),
        "model_key": str(conversation["model"] or DEFAULT_MODEL),
        "last_user_ts": float(last_user[0]) if last_user is not None else 0.0,
    }


class SummonCoordinator:
    """Coalesce overlapping summons while preserving every durable click."""

    def __init__(
        self,
        *,
        repository: SummonEventRepository = summon_event_repository,
        readiness: Callable[..., Awaitable[Mapping[str, Any]]] | None = None,
        runner: Callable[..., Awaitable[Mapping[str, Any]]] | None = None,
        now: Callable[[], float] = time.time,
    ):
        self.repository = repository
        self._readiness = readiness
        self._runner = runner
        self._now = now
        self._state_lock = asyncio.Lock()
        self._primary_summon_id: str | None = None

    async def process(
        self,
        *,
        summon_id: str,
        target: Mapping[str, Any],
        device_id: str = "pc",
    ) -> dict[str, Any]:
        normalized_id = normalize_summon_id(summon_id)
        async with self._state_lock:
            primary_id = self._primary_summon_id
            if primary_id is not None:
                await self.repository.mark_status(
                    normalized_id,
                    status="coalesced",
                    coalesced_into=primary_id,
                )
                return {
                    "status": "coalesced",
                    "summon_id": normalized_id,
                    "coalesced_into": primary_id,
                }
            self._primary_summon_id = normalized_id

        try:
            readiness = await self._check_readiness(device_id=device_id)
            if not bool(readiness.get("ready")):
                reason = str(readiness.get("reason") or "presence_not_ready")
                await self.repository.mark_status(
                    normalized_id,
                    status="gated",
                    failure_reason=reason,
                )
                return {
                    "status": "gated",
                    "summon_id": normalized_id,
                    "reason": reason,
                }
            result = await self._run_turn(
                kind="summon",
                now=self._now(),
                target={
                    **dict(target),
                    "exclude_summon_id": normalized_id,
                },
            )
            await self.repository.mark_status(normalized_id, status="processed")
            return {
                "status": "processed",
                "summon_id": normalized_id,
                "branch": str(result.get("round_branch") or "invalid"),
            }
        except BaseException as exc:
            if isinstance(exc, asyncio.CancelledError):
                reason = "summon_task_cancelled"
            else:
                reason = f"summon_failed:{type(exc).__name__}"
            await self.repository.mark_status(
                normalized_id,
                status="failed",
                failure_reason=reason,
            )
            if isinstance(exc, asyncio.CancelledError):
                raise
            return {
                "status": "failed",
                "summon_id": normalized_id,
                "reason": reason,
            }
        finally:
            async with self._state_lock:
                if self._primary_summon_id == normalized_id:
                    self._primary_summon_id = None

    async def recover_after_restart(self) -> int:
        return await self.repository.fail_processing_after_restart(now=self._now())

    async def _check_readiness(self, *, device_id: str) -> Mapping[str, Any]:
        if self._readiness is None:
            from .renderer import presence_show_readiness

            return await presence_show_readiness(device_id=device_id)
        return await self._readiness(device_id=device_id)

    async def _run_turn(self, **kwargs: Any) -> Mapping[str, Any]:
        if self._runner is None:
            from opportunity import run_autonomous_turn

            return await run_autonomous_turn(**kwargs)
        return await self._runner(**kwargs)


summon_coordinator = SummonCoordinator()


__all__ = [
    "SUMMON_EVENT_TTL_SEC",
    "SUMMON_STATUSES",
    "SummonEventError",
    "SummonEventRepository",
    "SummonCoordinator",
    "normalize_summon_id",
    "resolve_summon_target",
    "summon_coordinator",
    "summon_event_repository",
]
