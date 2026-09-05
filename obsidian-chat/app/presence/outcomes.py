"""One-use, 24-hour Presence outcome inbox for the next natural chat turn."""

from __future__ import annotations

import json
import time
from collections.abc import Callable, Mapping
from typing import Any

import aiosqlite

from database import get_db


OUTCOME_TTL_SEC = 24 * 60 * 60
CLAIM_LEASE_SEC = 10 * 60
MAX_OUTCOMES_PER_TURN = 20
PRESENCE_TERMINALS = frozenset({"played", "rejected", "expired", "superseded"})


class PresenceOutcomeInbox:
    def __init__(
        self,
        *,
        get_db_factory: Callable = get_db,
        now: Callable[[], float] = time.time,
    ):
        self._get_db = get_db_factory
        self._now = now

    async def record_terminal(
        self,
        *,
        event_id: str,
        conv_id: str,
        status: str,
        reason: str = "",
        actual_playback_ms: int | None = None,
        payload: Mapping[str, Any] | None = None,
        created_at: float | None = None,
    ) -> bool:
        event_id = str(event_id or "").strip()
        conv_id = str(conv_id or "").strip()
        status = str(status or "").strip().lower()
        if not event_id or not conv_id or status not in PRESENCE_TERMINALS:
            return False
        now = self._now() if created_at is None else float(created_at)
        outcome_id = f"{event_id}:{status}"
        async with self._get_db() as db:
            await db.execute("BEGIN IMMEDIATE")
            if status != "played":
                # A played ACK is the authoritative correction for an event
                # that the server had already expired or rejected.  A stale
                # cleanup notifier must never put the older result back into
                # the inbox after that correction has been recorded.
                cursor = await db.execute(
                    """
                    SELECT 1 FROM presence_outcomes
                    WHERE event_id=? AND presence_status='played'
                    LIMIT 1
                    """,
                    (event_id,),
                )
                if await cursor.fetchone() is not None:
                    await db.rollback()
                    return False

            # Only the newest terminal fact for an event may remain
            # deliverable.  Keep consumed history intact, but retire any
            # ready/claimed predecessor before inserting the correction.
            await db.execute(
                """
                UPDATE presence_outcomes
                SET state='expired', expires_at=MIN(expires_at, ?),
                    claimed_turn_id=NULL, claimed_at=NULL
                WHERE event_id=? AND outcome_id<>?
                  AND state IN ('ready','claimed')
                """,
                (now, event_id, outcome_id),
            )
            cursor = await db.execute(
                """
                INSERT OR IGNORE INTO presence_outcomes(
                    outcome_id, event_id, conv_id, presence_status, reason,
                    actual_playback_ms, payload_json, state, created_at,
                    expires_at, claimed_turn_id, claimed_at,
                    consumed_by_message_id, consumed_at
                ) VALUES(?,?,?,?,?,?,?,'ready',?,?,NULL,NULL,NULL,NULL)
                """,
                (
                    outcome_id,
                    event_id,
                    conv_id,
                    status,
                    " ".join(str(reason or "").split())[:240],
                    actual_playback_ms,
                    json.dumps(dict(payload or {}), ensure_ascii=False, separators=(",", ":")),
                    now,
                    now + OUTCOME_TTL_SEC,
                ),
            )
            await db.commit()
            return bool(cursor.rowcount)

    async def claim_for_turn(
        self,
        *,
        conv_id: str,
        bound_turn_id: str,
    ) -> dict[str, Any]:
        conv_id = str(conv_id or "").strip()
        bound_turn_id = str(bound_turn_id or "").strip()
        if not conv_id or not bound_turn_id:
            return self._empty(bound_turn_id)
        now = self._now()
        async with self._get_db() as db:
            db.row_factory = aiosqlite.Row
            await db.execute("BEGIN IMMEDIATE")
            await db.execute(
                """
                UPDATE presence_outcomes
                SET state='expired', claimed_turn_id=NULL, claimed_at=NULL
                WHERE state IN ('ready','claimed') AND expires_at<=?
                """,
                (now,),
            )
            await db.execute(
                """
                UPDATE presence_outcomes
                SET state='ready', claimed_turn_id=NULL, claimed_at=NULL
                WHERE state='claimed' AND claimed_at<?
                """,
                (now - CLAIM_LEASE_SEC,),
            )
            cursor = await db.execute(
                """
                SELECT * FROM presence_outcomes
                WHERE conv_id=? AND expires_at>?
                  AND (state='ready' OR (state='claimed' AND claimed_turn_id=?))
                ORDER BY created_at ASC, outcome_id ASC
                LIMIT ?
                """,
                (conv_id, now, bound_turn_id, MAX_OUTCOMES_PER_TURN),
            )
            rows = [dict(row) for row in await cursor.fetchall()]
            ids = [row["outcome_id"] for row in rows]
            if ids:
                placeholders = ",".join("?" for _ in ids)
                await db.execute(
                    f"""
                    UPDATE presence_outcomes
                    SET state='claimed', claimed_turn_id=?, claimed_at=?
                    WHERE outcome_id IN ({placeholders})
                      AND (state='ready' OR claimed_turn_id=?)
                    """,
                    (bound_turn_id, now, *ids, bound_turn_id),
                )
            await db.commit()
        if not rows:
            return self._empty(bound_turn_id)
        return {
            "status": "claimed",
            "bound_turn_id": bound_turn_id,
            "outcome_ids": ids,
            "rows": rows,
            "block": format_presence_outcomes(rows),
        }

    async def consume_claimed_in_tx(
        self,
        db,
        *,
        bound_turn_id: str,
        assistant_message_id: str,
        consumed_at: float | None = None,
    ) -> int:
        now = self._now() if consumed_at is None else float(consumed_at)
        cursor = await db.execute(
            """
            UPDATE presence_outcomes
            SET state='consumed', consumed_by_message_id=?, consumed_at=?
            WHERE state='claimed' AND claimed_turn_id=? AND expires_at>?
            """,
            (
                str(assistant_message_id or ""),
                now,
                str(bound_turn_id or ""),
                now,
            ),
        )
        return max(0, int(cursor.rowcount or 0))

    async def release_claim(self, *, bound_turn_id: str) -> int:
        bound_turn_id = str(bound_turn_id or "").strip()
        if not bound_turn_id:
            return 0
        async with self._get_db() as db:
            cursor = await db.execute(
                """
                UPDATE presence_outcomes
                SET state='ready', claimed_turn_id=NULL, claimed_at=NULL
                WHERE state='claimed' AND claimed_turn_id=?
                """,
                (bound_turn_id,),
            )
            await db.commit()
            return max(0, int(cursor.rowcount or 0))

    async def rows_for_event(self, event_id: str) -> list[dict[str, Any]]:
        async with self._get_db() as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                "SELECT * FROM presence_outcomes WHERE event_id=? ORDER BY created_at",
                (str(event_id or ""),),
            )
            return [dict(row) for row in await cursor.fetchall()]

    @staticmethod
    def _empty(bound_turn_id: str) -> dict[str, Any]:
        return {
            "status": "empty",
            "bound_turn_id": str(bound_turn_id or ""),
            "outcome_ids": [],
            "rows": [],
            "block": "",
        }


def format_presence_outcomes(rows: list[Mapping[str, Any]]) -> str:
    if not rows:
        return ""
    labels = {
        "played": "确实在 PC 桌面播放完成",
        "rejected": "PC 拒绝播放",
        "expired": "在开始或交付租约截止前未完成，已过期",
        "superseded": "还在队列里时被更新的出现意图覆盖",
    }
    lines = [
        "【桌面化身异步结果（仅本轮可见）】",
        "这些是先前桌面化身动作的实际终态，不是新的行动要求。只有 played 才表示真的出现过；不要把 dispatched/accepted 当成已经出现。",
    ]
    for row in rows:
        status = str(row.get("presence_status") or "")
        line = f"- {labels.get(status, status)}"
        playback = row.get("actual_playback_ms")
        reason = " ".join(str(row.get("reason") or "").split())[:120]
        if status == "played" and playback is not None:
            line += f"（实际播放约 {int(playback)}ms）"
        elif reason:
            line += f"（原因：{reason}）"
        lines.append(line)
    lines.append("这批结果消费一次即清；不必专门提起，但后续表述必须以这些事实为准。")
    return "\n".join(lines)


presence_outcome_inbox = PresenceOutcomeInbox()


__all__ = [
    "CLAIM_LEASE_SEC",
    "OUTCOME_TTL_SEC",
    "PresenceOutcomeInbox",
    "format_presence_outcomes",
    "presence_outcome_inbox",
]
