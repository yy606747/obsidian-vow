from __future__ import annotations

import logging
import time

import aiosqlite

from database import get_db

log = logging.getLogger("schedule")


async def add_schedule(stype: str, trigger_at: str, content: str) -> str | None:
    trigger_at = trigger_at.replace("T", " ")
    async with get_db() as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT id FROM schedules WHERE status='active' AND type=? AND trigger_at=? AND content=?",
            (stype, trigger_at, content),
        )
        if await cur.fetchone():
            log.info("schedule deduplicated: %s %s %s", stype, trigger_at, content)
            return None
        sid = f"sch_{time.time_ns()}"
        await db.execute(
            "INSERT INTO schedules (id, type, trigger_at, content, created_at, status) VALUES (?,?,?,?,?,?)",
            (sid, stype, trigger_at, content, time.time(), "active"),
        )
        await db.commit()
        return sid


async def delete_schedule(sid: str) -> None:
    async with get_db() as db:
        await db.execute("UPDATE schedules SET status='cancelled' WHERE id=?", (sid,))
        await db.commit()


async def get_schedule(sid: str) -> dict | None:
    async with get_db() as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT id, type, trigger_at, content, status FROM schedules WHERE id=?",
            (sid,),
        )
        row = await cur.fetchone()
        return dict(row) if row else None


async def list_active() -> list[dict]:
    async with get_db() as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT id, type, trigger_at, content FROM schedules WHERE status='active' ORDER BY trigger_at",
        )
        return [dict(r) for r in await cur.fetchall()]


async def list_due(now_iso: str) -> list[dict]:
    async with get_db() as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT * FROM schedules WHERE status='active' AND type IN ('alarm','monitor') "
            "AND trigger_at <= ? ORDER BY trigger_at, CASE type WHEN 'alarm' THEN 0 ELSE 1 END",
            (now_iso,),
        )
        return [dict(r) for r in await cur.fetchall()]


async def mark_triggered(sid: str) -> None:
    async with get_db() as db:
        await db.execute("UPDATE schedules SET status='triggered' WHERE id=?", (sid,))
        await db.commit()


async def mark_missed(ids: list[str]) -> None:
    if not ids:
        return
    placeholders = ",".join("?" * len(ids))
    async with get_db() as db:
        await db.execute(f"UPDATE schedules SET status='missed' WHERE id IN ({placeholders})", ids)
        await db.commit()


async def list_missed_candidates(cutoff: str) -> list[dict]:
    async with get_db() as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT id, type, trigger_at, content FROM schedules "
            "WHERE status='active' AND trigger_at < ? ORDER BY trigger_at",
            (cutoff,),
        )
        return [dict(r) for r in await cur.fetchall()]


def build_schedule_prompt(schedules: list[dict]) -> str:
    if not schedules:
        return "暂无日程"
    type_map = {"alarm": ("🔔", "闹铃"), "reminder": ("📋", "日程"), "monitor": ("👁", "查岗")}
    lines = []
    for schedule in schedules:
        icon, label = type_map.get(schedule["type"], ("📋", "日程"))
        lines.append(
            f"- {icon} {label} #{schedule['id']}: "
            f"{schedule['trigger_at'].replace('T', ' ')} — {schedule['content']}"
        )
    return "\n".join(lines)
