"""Atomic persistence and state transitions for Self-Wake V1."""

from __future__ import annotations

import json
import time
from collections.abc import Collection
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import aiosqlite

from database import get_db

from .time_policy import (
    WAKE_EXPIRY_SECONDS,
    owner_day_bounds,
    owner_timezone_name,
    validate_wake_timestamp,
)


MAX_PENDING_WAKES_PER_ORIGIN = 1
MAX_SELF_WAKE_ATTEMPTS_PER_DAY = 3
MAX_WAKE_CALLS_PER_DAY = MAX_SELF_WAKE_ATTEMPTS_PER_DAY
MAX_WAKE_ATTEMPTS_PER_ORIGIN = 6
PROMPT_FAILURE_LOOKBACK_SECONDS = 48 * 60 * 60
V1_ORIGIN = "relationship"


class SelfWakeRepositoryError(ValueError):
    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


def _row(value) -> dict[str, Any] | None:
    return dict(value) if value is not None else None


def _intent(value: str) -> str:
    normalized = str(value or "").strip()
    if not (1 <= len(normalized) <= 1000):
        raise SelfWakeRepositoryError("invalid_intent")
    return normalized


def _capabilities(values: Collection[str] | None) -> tuple[str, ...]:
    if values is None:
        return ()
    if isinstance(values, str):
        values = (values,)
    return tuple(sorted({str(item).strip() for item in values if str(item).strip()}))


def _validate_identity(
    *,
    origin: str,
    origin_ref: str,
    source: str,
    conv_id: str,
    source_turn_id: str,
    timezone_name: str,
) -> None:
    if origin != V1_ORIGIN:
        raise SelfWakeRepositoryError("unsupported_origin")
    if not conv_id or origin_ref != conv_id:
        raise SelfWakeRepositoryError("invalid_origin_ref")
    if source not in {"chat", "opportunity"}:
        raise SelfWakeRepositoryError("unsupported_source")
    if not str(source_turn_id or "").strip():
        raise SelfWakeRepositoryError("missing_source_turn_id")
    try:
        ZoneInfo(timezone_name)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise SelfWakeRepositoryError("invalid_owner_timezone") from exc


class SelfWakeRepository:
    async def schedule_or_replace(
        self,
        *,
        wake_at: float,
        intent: str,
        requested_capabilities: Collection[str] | None,
        origin: str,
        origin_ref: str,
        source: str,
        conv_id: str,
        source_turn_id: str,
        owner_timezone: str,
        now: float | None = None,
    ) -> dict[str, Any]:
        created_at = time.time() if now is None else float(now)
        normalized_intent = _intent(intent)
        capabilities = _capabilities(requested_capabilities)
        _validate_identity(
            origin=origin,
            origin_ref=origin_ref,
            source=source,
            conv_id=conv_id,
            source_turn_id=source_turn_id,
            timezone_name=owner_timezone,
        )
        wake_timestamp = validate_wake_timestamp(wake_at, now=created_at)
        wake_id = f"wake_{time.time_ns()}"
        expires_at = wake_timestamp + WAKE_EXPIRY_SECONDS

        async with get_db() as db:
            db.row_factory = aiosqlite.Row
            await db.execute("BEGIN IMMEDIATE")
            try:
                # Repeat horizon validation inside the write transaction: the
                # repository, not a prompt or adapter, owns the final guard.
                validate_wake_timestamp(wake_timestamp, now=created_at)
                cursor = await db.execute(
                    "SELECT 1 FROM conversations WHERE id=?",
                    (conv_id,),
                )
                if await cursor.fetchone() is None:
                    raise SelfWakeRepositoryError("origin_not_found")

                cursor = await db.execute(
                    "SELECT id, wake_at, intent FROM self_wakes "
                    "WHERE origin=? AND origin_ref=? AND state='pending'",
                    (origin, origin_ref),
                )
                replaced = _row(await cursor.fetchone())
                if replaced is not None:
                    await db.execute(
                        "UPDATE self_wakes SET state='invalidated', closed_at=?, "
                        "close_reason='replaced' WHERE id=? AND state='pending'",
                        (created_at, replaced["id"]),
                    )
                await db.execute(
                    "INSERT INTO self_wakes ("
                    "id,wake_at,intent,requested_capabilities_json,origin,origin_ref,"
                    "source,conv_id,source_turn_id,owner_timezone,state,created_at,expires_at"
                    ") VALUES (?,?,?,?,?,?,?,?,?,?,'pending',?,?)",
                    (
                        wake_id,
                        wake_timestamp,
                        normalized_intent,
                        json.dumps(capabilities, ensure_ascii=False, separators=(",", ":")),
                        origin,
                        origin_ref,
                        source,
                        conv_id,
                        str(source_turn_id).strip(),
                        owner_timezone,
                        created_at,
                        expires_at,
                    ),
                )
                await db.commit()
            except BaseException:
                await db.rollback()
                raise
        return {
            "ok": True,
            "status": "pending",
            "wake": {
                "id": wake_id,
                "wake_at": wake_timestamp,
                "intent": normalized_intent,
                "requested_capabilities": list(capabilities),
                "expires_at": expires_at,
            },
            "replaced": replaced,
        }

    async def cancel_pending(
        self,
        *,
        origin: str,
        origin_ref: str,
        now: float | None = None,
    ) -> dict[str, Any]:
        if origin != V1_ORIGIN or not str(origin_ref or "").strip():
            raise SelfWakeRepositoryError("unsupported_origin")
        closed_at = time.time() if now is None else float(now)
        async with get_db() as db:
            db.row_factory = aiosqlite.Row
            await db.execute("BEGIN IMMEDIATE")
            try:
                cursor = await db.execute(
                    "SELECT id, wake_at, intent FROM self_wakes "
                    "WHERE origin=? AND origin_ref=? AND state='pending'",
                    (origin, origin_ref),
                )
                cancelled = _row(await cursor.fetchone())
                if cancelled is None:
                    await db.commit()
                    return {
                        "ok": False,
                        "status": "rejected",
                        "reason": "no_pending_wake",
                    }
                await db.execute(
                    "UPDATE self_wakes SET state='invalidated', closed_at=?, "
                    "close_reason='cancelled' WHERE id=? AND state='pending'",
                    (closed_at, cancelled["id"]),
                )
                await db.commit()
            except BaseException:
                await db.rollback()
                raise
        return {"ok": True, "status": "cancelled", "wake": cancelled}

    async def claim_due_batch(
        self,
        *,
        now: float | None = None,
        timezone_name: str | None = None,
        daily_limit: int = MAX_WAKE_CALLS_PER_DAY,
    ) -> list[dict[str, Any]]:
        claimed_at = time.time() if now is None else float(now)
        timezone_name = str(timezone_name or owner_timezone_name())
        day_start, day_end = owner_day_bounds(
            claimed_at,
            timezone_name=timezone_name,
        )
        limit = max(0, int(daily_limit))
        claimed: list[dict[str, Any]] = []
        async with get_db() as db:
            db.row_factory = aiosqlite.Row
            await db.execute("BEGIN IMMEDIATE")
            try:
                await db.execute(
                    "UPDATE self_wakes SET state='expired', closed_at=?, "
                    "close_reason='expired' WHERE state='pending' AND expires_at<=?",
                    (claimed_at, claimed_at),
                )
                cursor = await db.execute(
                    "SELECT COUNT(*) FROM self_wakes WHERE state='consumed' "
                    "AND consumed_at>=? AND consumed_at<?",
                    (day_start, day_end),
                )
                used = int((await cursor.fetchone())[0])
                remaining = max(0, limit - used)
                cursor = await db.execute(
                    "SELECT * FROM self_wakes WHERE state='pending' "
                    "AND wake_at<=? AND expires_at>? ORDER BY wake_at, created_at, id",
                    (claimed_at, claimed_at),
                )
                due = [dict(item) for item in await cursor.fetchall()]
                to_claim = due[:remaining]
                quota_expired = due[remaining:]
                for row in to_claim:
                    cursor = await db.execute(
                        "UPDATE self_wakes SET state='consumed', consumed_at=? "
                        "WHERE id=? AND state='pending'",
                        (claimed_at, row["id"]),
                    )
                    if int(cursor.rowcount or 0) > 0:
                        row["state"] = "consumed"
                        row["consumed_at"] = claimed_at
                        claimed.append(row)
                for row in quota_expired:
                    await db.execute(
                        "UPDATE self_wakes SET state='expired', closed_at=?, "
                        "close_reason='daily_quota_exhausted' "
                        "WHERE id=? AND state='pending'",
                        (claimed_at, row["id"]),
                    )
                await db.commit()
            except BaseException:
                await db.rollback()
                raise
        return claimed

    async def finish_trigger(
        self,
        wake_id: str,
        *,
        outcome: str,
        error: str = "",
        now: float | None = None,
    ) -> bool:
        finished_at = time.time() if now is None else float(now)
        async with get_db() as db:
            cursor = await db.execute(
                "UPDATE self_wakes SET trigger_outcome=?, trigger_error=?, finished_at=? "
                "WHERE id=? AND state='consumed'",
                (str(outcome or "unknown"), str(error or "")[:4000], finished_at, wake_id),
            )
            await db.commit()
            return int(cursor.rowcount or 0) > 0

    async def get(self, wake_id: str) -> dict[str, Any] | None:
        async with get_db() as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute("SELECT * FROM self_wakes WHERE id=?", (wake_id,))
            return _row(await cursor.fetchone())

    async def load_prompt_status(
        self,
        *,
        origin: str,
        origin_ref: str,
        now: float | None = None,
        timezone_name: str | None = None,
        daily_limit: int = MAX_WAKE_CALLS_PER_DAY,
    ) -> dict[str, Any]:
        current = time.time() if now is None else float(now)
        timezone_name = str(timezone_name or owner_timezone_name())
        day_start, day_end = owner_day_bounds(current, timezone_name=timezone_name)
        async with get_db() as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                "SELECT * FROM self_wakes WHERE origin=? AND origin_ref=? "
                "AND state='pending' ORDER BY created_at DESC LIMIT 1",
                (origin, origin_ref),
            )
            pending = _row(await cursor.fetchone())
            if pending is not None:
                try:
                    pending["requested_capabilities"] = json.loads(
                        pending.get("requested_capabilities_json") or "[]"
                    )
                except (TypeError, json.JSONDecodeError):
                    pending["requested_capabilities"] = []
            cursor = await db.execute(
                "SELECT COUNT(*) FROM self_wakes WHERE state='consumed' "
                "AND consumed_at>=? AND consumed_at<?",
                (day_start, day_end),
            )
            used = int((await cursor.fetchone())[0])
            cursor = await db.execute(
                "SELECT * FROM self_wakes WHERE origin=? AND origin_ref=? AND "
                "((state='expired' AND close_reason IN "
                "('daily_quota_exhausted','expired')) OR "
                "(state='consumed' AND trigger_outcome='provider_failed')) AND "
                "COALESCE(finished_at, closed_at, consumed_at, created_at)>=? "
                "ORDER BY COALESCE(finished_at, closed_at, consumed_at, created_at) DESC "
                "LIMIT 1",
                (origin, origin_ref, current - PROMPT_FAILURE_LOOKBACK_SECONDS),
            )
            recent = _row(await cursor.fetchone())
        if recent is not None:
            recent["outcome"] = (
                recent.get("trigger_outcome") or recent.get("close_reason") or "expired"
            )
        return {
            "pending": pending,
            "quota": {
                "limit": max(0, int(daily_limit)),
                "used": used,
                "remaining": max(0, int(daily_limit) - used),
                "day_start": day_start,
                "day_end": day_end,
            },
            "recent_nonexecution": recent,
            "owner_timezone": timezone_name,
        }

    @staticmethod
    async def invalidate_origin_in_tx(
        db,
        *,
        origin: str,
        origin_ref: str,
        now: float | None = None,
    ) -> int:
        closed_at = time.time() if now is None else float(now)
        try:
            cursor = await db.execute(
                "UPDATE self_wakes SET state='invalidated', closed_at=?, "
                "close_reason='origin_terminated' WHERE origin=? AND origin_ref=? "
                "AND state='pending'",
                (closed_at, origin, origin_ref),
            )
        except aiosqlite.OperationalError as exc:
            # Narrow compatibility for isolated route tests/old databases that
            # have not run init_db yet. If the table does not exist, no wake
            # row can be orphaned by this deletion.
            if "no such table: self_wakes" in str(exc).lower():
                return 0
            raise
        return max(0, int(cursor.rowcount or 0))


self_wake_repository = SelfWakeRepository()


async def schedule_or_replace(**kwargs) -> dict[str, Any]:
    return await self_wake_repository.schedule_or_replace(**kwargs)


async def cancel_pending(**kwargs) -> dict[str, Any]:
    return await self_wake_repository.cancel_pending(**kwargs)


async def claim_due_batch(**kwargs) -> list[dict[str, Any]]:
    return await self_wake_repository.claim_due_batch(**kwargs)


async def finish_trigger(wake_id: str, **kwargs) -> bool:
    return await self_wake_repository.finish_trigger(wake_id, **kwargs)


async def load_prompt_status(**kwargs) -> dict[str, Any]:
    return await self_wake_repository.load_prompt_status(**kwargs)


async def invalidate_origin_in_tx(db, **kwargs) -> int:
    return await SelfWakeRepository.invalidate_origin_in_tx(db, **kwargs)


__all__ = [
    "MAX_WAKE_ATTEMPTS_PER_ORIGIN",
    "MAX_WAKE_CALLS_PER_DAY",
    "MAX_PENDING_WAKES_PER_ORIGIN",
    "MAX_SELF_WAKE_ATTEMPTS_PER_DAY",
    "SelfWakeRepository",
    "SelfWakeRepositoryError",
    "cancel_pending",
    "claim_due_batch",
    "finish_trigger",
    "invalidate_origin_in_tx",
    "load_prompt_status",
    "schedule_or_replace",
    "self_wake_repository",
]
