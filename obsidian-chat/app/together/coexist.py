"""Suspend initiative before claiming/charging it; preserve pending lifetimes."""

import json
import time

import aiosqlite

from .config import together_config


def _config():
    try:
        return together_config.load()
    except (ValueError, OSError):
        return {"enabled": False, "lease_seconds": 45}


async def active_in_tx(db, *, now=None, enabled=None, lease_seconds=None):
    config = _config()
    if enabled is None:
        enabled = config["enabled"]
    if not enabled:
        return False
    now = time.time() if now is None else now
    try:
        row = await (await db.execute("SELECT 1 FROM together_sessions WHERE status='active' AND heartbeat_at+?>? LIMIT 1",
                                      (lease_seconds or config["lease_seconds"], now))).fetchone()
    except aiosqlite.OperationalError as exc:
        if "no such table: together_sessions" in str(exc):
            return False
        raise
    return row is not None


async def is_active(*, now=None, db_factory=None):
    if not _config()["enabled"]:
        return False
    from database import get_db
    async with (db_factory or get_db)() as db:
        return await active_in_tx(db, now=now)


async def sync_holds_in_tx(db, *, now=None, enabled=None, lease_seconds=None):
    now = time.time() if now is None else now
    active = await active_in_tx(db, now=now, enabled=enabled, lease_seconds=lease_seconds)
    tables = {row[0] for row in await (await db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name IN ('together_holds','self_wakes','look_at_me_requests')")).fetchall()}
    if "together_holds" not in tables:
        return active
    if active:
        if "self_wakes" in tables:
            rows = await (await db.execute("SELECT id,expires_at FROM self_wakes WHERE state='pending' AND expires_at>?", (now,))).fetchall()
            for row in rows:
                await db.execute("INSERT OR IGNORE INTO together_holds VALUES ('self_wake',?,?,?)", (row[0], now, json.dumps({"expires_at": row[1]})))
        if "look_at_me_requests" in tables:
            rows = await (await db.execute("SELECT id,expires_at,choice_deadline FROM look_at_me_requests WHERE status IN ('queued','offered') AND COALESCE(choice_deadline,expires_at)>?", (now,))).fetchall()
            for row in rows:
                await db.execute("INSERT OR IGNORE INTO together_holds VALUES ('invitation',?,?,?)", (row[0], now,
                    json.dumps({"expires_at": row[1], "choice_deadline": row[2]})))
    else:
        rows = await (await db.execute("SELECT kind,item_id,started_at,deadlines FROM together_holds")).fetchall()
        for kind, item_id, started, deadlines in rows:
            previous = json.loads(deadlines)
            elapsed = max(0, now-started)
            if kind == "self_wake" and "self_wakes" in tables:
                await db.execute("UPDATE self_wakes SET expires_at=? WHERE id=? AND state='pending'", (previous["expires_at"]+elapsed, item_id))
            elif kind == "invitation" and "look_at_me_requests" in tables:
                choice = previous["choice_deadline"]
                await db.execute("UPDATE look_at_me_requests SET expires_at=?,choice_deadline=? WHERE id=? AND status IN ('queued','offered')",
                    (previous["expires_at"]+elapsed, choice+elapsed if choice is not None else None, item_id))
            await db.execute("DELETE FROM together_holds WHERE kind=? AND item_id=?", (kind, item_id))
    return active


async def sync_holds(*, now=None, db_factory=None, enabled=None, lease_seconds=None):
    from database import get_db
    async with (db_factory or get_db)() as db:
        await db.execute("BEGIN IMMEDIATE")
        active = await sync_holds_in_tx(db, now=now, enabled=enabled, lease_seconds=lease_seconds)
        await db.commit()
    return active
