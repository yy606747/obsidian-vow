"""Persistent one-per-local-night claim state for silent autonomous rounds."""

from __future__ import annotations

import asyncio
import logging
import re
import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import date, datetime, time as datetime_time, timedelta
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import aiosqlite

from database import get_db
from config import load_ai_behavior
from app.self_wake.time_policy import owner_timezone_name


DEFAULT_NIGHT_ROUND_TIMEZONE = "America/Los_Angeles"
NIGHT_BRANCHES = frozenset(
    {"draw", "reflect", "none", "invalid", "provider_failed"}
)
NIGHT_STATUSES = frozenset({"running", "completed", "failed"})
_HHMM_RE = re.compile(r"^(?:[01]\d|2[0-3]):[0-5]\d$")
log = logging.getLogger(__name__)


class NightRoundError(ValueError):
    pass


@dataclass(frozen=True)
class NightWindow:
    night_key: str
    timezone: str
    starts_at: float
    ends_at: float


def validate_hhmm(value: str) -> str:
    normalized = str(value or "").strip()
    if not _HHMM_RE.fullmatch(normalized):
        raise NightRoundError("invalid_night_round_time")
    return normalized


def _minutes(value: str) -> int:
    hour, minute = validate_hhmm(value).split(":", 1)
    return int(hour) * 60 + int(minute)


def _at_local(day: date, value: str, zone: ZoneInfo) -> datetime:
    hour, minute = validate_hhmm(value).split(":", 1)
    return datetime.combine(
        day,
        datetime_time(hour=int(hour), minute=int(minute)),
        tzinfo=zone,
    )


def resolve_night_window(
    reference_time: float,
    *,
    timezone_name: str,
    start: str,
    end: str,
) -> NightWindow | None:
    try:
        zone = ZoneInfo(str(timezone_name or "").strip())
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise NightRoundError("invalid_night_round_timezone") from exc
    start_minute = _minutes(start)
    end_minute = _minutes(end)
    if start_minute == end_minute:
        raise NightRoundError("night_round_window_empty")

    local_now = datetime.fromtimestamp(float(reference_time), zone)
    minute = local_now.hour * 60 + local_now.minute
    local_day = local_now.date()
    if start_minute < end_minute:
        if not start_minute <= minute < end_minute:
            return None
        key_day = local_day
        end_day = local_day
    elif minute >= start_minute:
        key_day = local_day
        end_day = local_day + timedelta(days=1)
    elif minute < end_minute:
        key_day = local_day - timedelta(days=1)
        end_day = local_day
    else:
        return None

    return NightWindow(
        night_key=key_day.isoformat(),
        timezone=str(zone.key),
        starts_at=_at_local(key_day, start, zone).timestamp(),
        ends_at=_at_local(end_day, end, zone).timestamp(),
    )


class NightRoundRepository:
    def __init__(
        self,
        *,
        get_db_factory: Callable = get_db,
        now: Callable[[], float] = time.time,
    ):
        self._get_db = get_db_factory
        self._now = now

    async def claim_current(
        self,
        *,
        timezone_name: str = DEFAULT_NIGHT_ROUND_TIMEZONE,
        start: str = "02:00",
        end: str = "05:00",
        now: float | None = None,
    ) -> dict[str, Any] | None:
        claimed_at = self._now() if now is None else float(now)
        window = resolve_night_window(
            claimed_at,
            timezone_name=timezone_name,
            start=start,
            end=end,
        )
        if window is None:
            return None
        async with self._get_db() as db:
            db.row_factory = aiosqlite.Row
            await db.execute("BEGIN IMMEDIATE")
            try:
                cursor = await db.execute(
                    """
                    INSERT INTO presence_night_rounds(
                        night_key, timezone, status, branch, started_at,
                        finished_at, error, updated_at
                    ) VALUES(?,?,'running',NULL,?,NULL,'',?)
                    ON CONFLICT(night_key) DO NOTHING
                    """,
                    (
                        window.night_key,
                        window.timezone,
                        claimed_at,
                        claimed_at,
                    ),
                )
                inserted = int(cursor.rowcount or 0) == 1
                if inserted:
                    cursor = await db.execute(
                        "SELECT * FROM presence_night_rounds WHERE night_key=?",
                        (window.night_key,),
                    )
                    row = await cursor.fetchone()
                else:
                    row = None
                await db.commit()
            except BaseException:
                await db.rollback()
                raise
        return dict(row) if row is not None else None

    async def finish(
        self,
        night_key: str,
        *,
        status: str,
        branch: str,
        error: str = "",
        now: float | None = None,
    ) -> bool:
        normalized_status = str(status or "").strip().lower()
        normalized_branch = str(branch or "").strip().lower()
        if normalized_status not in {"completed", "failed"}:
            raise NightRoundError("invalid_night_round_finish_status")
        if normalized_branch not in NIGHT_BRANCHES:
            raise NightRoundError("invalid_night_round_branch")
        finished_at = self._now() if now is None else float(now)
        normalized_error = " ".join(str(error or "").split())[:500]
        async with self._get_db() as db:
            cursor = await db.execute(
                """
                UPDATE presence_night_rounds
                SET status=?, branch=?, finished_at=?, error=?, updated_at=?
                WHERE night_key=? AND status='running'
                """,
                (
                    normalized_status,
                    normalized_branch,
                    finished_at,
                    normalized_error,
                    finished_at,
                    str(night_key or "").strip(),
                ),
            )
            await db.commit()
        return int(cursor.rowcount or 0) == 1

    async def fail_running_after_restart(self, *, now: float | None = None) -> int:
        failed_at = self._now() if now is None else float(now)
        async with self._get_db() as db:
            cursor = await db.execute(
                """
                UPDATE presence_night_rounds
                SET status='failed', branch='invalid', finished_at=?,
                    error='server_restart', updated_at=?
                WHERE status='running'
                """,
                (failed_at, failed_at),
            )
            await db.commit()
        return max(0, int(cursor.rowcount or 0))

    async def get(self, night_key: str) -> dict[str, Any] | None:
        async with self._get_db() as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                "SELECT * FROM presence_night_rounds WHERE night_key=?",
                (str(night_key or "").strip(),),
            )
            row = await cursor.fetchone()
        return dict(row) if row is not None else None


class NightRoundScheduler:
    def __init__(
        self,
        *,
        repository: NightRoundRepository | None = None,
        now: Callable[[], float] = time.time,
        config_loader: Callable[[], Mapping[str, Any]] = load_ai_behavior,
        timezone_resolver: Callable[[], str] = owner_timezone_name,
        target_resolver: Callable[[], Awaitable[dict | None]] | None = None,
        turn_runner: Callable[..., Awaitable[dict[str, Any]]] | None = None,
        sprite_state_resolver: Callable[[], Awaitable[Mapping[str, bool]]] | None = None,
    ):
        self.repository = repository or night_round_repository
        self._now = now
        self._config_loader = config_loader
        self._timezone_resolver = timezone_resolver
        self._target_resolver = target_resolver
        self._turn_runner = turn_runner
        self._sprite_state_resolver = sprite_state_resolver

    async def recover_after_restart(self) -> int:
        return await self.repository.fail_running_after_restart(now=self._now())

    async def run_once(self) -> dict[str, Any]:
        config = dict(self._config_loader() or {})
        if not bool(config.get("night_round_enabled", False)):
            return {"status": "gated", "reason": "disabled"}
        now = self._now()
        timezone_name = self._timezone_resolver()
        start = str(config.get("night_round_start") or "02:00")
        end = str(config.get("night_round_end") or "05:00")
        window = resolve_night_window(
            now,
            timezone_name=timezone_name,
            start=start,
            end=end,
        )
        if window is None:
            return {"status": "gated", "reason": "outside_window"}

        if self._sprite_state_resolver is None:
            from app.presence import sprite_library

            has_non_seed = await sprite_library.has_non_seed_sprites()
            has_synced_non_seed = await sprite_library.has_synced_non_seed_sprites(
                device_id="pc"
            )
        else:
            sprite_state = dict(await self._sprite_state_resolver() or {})
            has_non_seed = bool(sprite_state.get("has_non_seed"))
            has_synced_non_seed = bool(sprite_state.get("has_synced_non_seed"))
        if has_non_seed and not has_synced_non_seed:
            return {"status": "gated", "reason": "presence_sprite_pending_sync"}

        if self._target_resolver is None:
            from opportunity import _resolve_target_conv

            target = await _resolve_target_conv()
        else:
            target = await self._target_resolver()
        if not target:
            return {"status": "gated", "reason": "no_conversation"}

        claimed = await self.repository.claim_current(
            timezone_name=timezone_name,
            start=start,
            end=end,
            now=now,
        )
        if claimed is None:
            return {
                "status": "gated",
                "reason": "already_claimed",
                "night_key": window.night_key,
            }

        if self._turn_runner is None:
            from opportunity import run_autonomous_turn

            runner = run_autonomous_turn
        else:
            runner = self._turn_runner
        try:
            result = await runner(kind="night", now=now, target=target)
        except Exception as exc:
            await self.repository.finish(
                window.night_key,
                status="failed",
                branch="provider_failed",
                error=f"night_runner_failed:{type(exc).__name__}:{exc}",
                now=self._now(),
            )
            return {
                "status": "provider_failed",
                "round_kind": "night",
                "round_branch": "provider_failed",
                "night_key": window.night_key,
                "error": str(exc),
            }

        branch = str(result.get("round_branch") or "invalid")
        if branch not in NIGHT_BRANCHES:
            branch = "invalid"
        failed = branch == "provider_failed" or str(result.get("status")) in {
            "provider_failed",
            "action_failed",
        }
        await self.repository.finish(
            window.night_key,
            status="failed" if failed else "completed",
            branch=branch,
            error=str(result.get("error") or ""),
            now=self._now(),
        )
        return {**result, "night_key": window.night_key}

    async def run_loop(self, *, poll_interval_sec: float = 30.0) -> None:
        interval = max(1.0, float(poll_interval_sec))
        while True:
            try:
                await self.run_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("Night round scheduler tick failed")
            await asyncio.sleep(interval)

night_round_repository = NightRoundRepository()
night_round_scheduler = NightRoundScheduler(repository=night_round_repository)


__all__ = [
    "DEFAULT_NIGHT_ROUND_TIMEZONE",
    "NIGHT_BRANCHES",
    "NIGHT_STATUSES",
    "NightRoundError",
    "NightRoundRepository",
    "NightRoundScheduler",
    "NightWindow",
    "night_round_repository",
    "night_round_scheduler",
    "resolve_night_window",
    "validate_hhmm",
]
