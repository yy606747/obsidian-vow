from __future__ import annotations

import json
import random
import time
from uuid import uuid4

import aiosqlite


def _decode(row):
    if row is None:
        return None
    value = dict(row)
    for key in ("required_devices", "authorized_devices", "bindings", "evidence"):
        if key in value:
            value[key] = json.loads(value[key])
    return value


class SceneRepository:
    def __init__(self, db_factory=None, *, now=time.time, choose=random.choice):
        if db_factory is None:
            from database import get_db
            db_factory = get_db
        self.db = db_factory
        self.now = now
        self.choose = choose

    async def _one(self, sql, params=()):
        async with self.db() as db:
            db.row_factory = aiosqlite.Row
            return _decode(await (await db.execute(sql, params)).fetchone())

    async def add_card(self, raw_text, required_devices, *, source, conv_id, model_key):
        card_id = "card_" + uuid4().hex
        async with self.db() as db:
            await db.execute("INSERT INTO scene_cards VALUES (?,?,?,?,?,?,?,NULL)",
                             (card_id, raw_text, json.dumps(required_devices), source, conv_id, model_key, self.now()))
            await db.commit()
        return await self.card(card_id)

    async def card(self, card_id):
        return await self._one("SELECT * FROM scene_cards WHERE id=?", (card_id,))

    async def matching(self, authorized):
        async with self.db() as db:
            db.row_factory = aiosqlite.Row
            rows = await (await db.execute("SELECT * FROM scene_cards WHERE used_at IS NULL ORDER BY created_at")).fetchall()
        return [card for row in rows if (card := _decode(row))["required_devices"]
                and set(card["required_devices"]).issubset(authorized)]

    async def by_request(self, request_id):
        return await self._one("SELECT * FROM scene_sessions WHERE request_id=?", (request_id,))

    async def session(self, session_id):
        return await self._one("SELECT * FROM scene_sessions WHERE id=?", (session_id,))

    async def create_session(self, *, request_id, conv_id, model_key, owner_id, devices):
        session_id = "scene_" + uuid4().hex
        async with self.db() as db:
            await db.execute("""INSERT INTO scene_sessions
                (id,request_id,conv_id,model_key,owner_id,authorized_devices,status,created_at)
                VALUES (?,?,?,?,?,?,'preparing',?)""",
                (session_id, request_id, conv_id, model_key, owner_id, json.dumps(devices), self.now()))
            await db.commit()
        return await self.session(session_id)

    async def enter(self, session_id, card, bindings, *, generation):
        """Consume exactly once, only if preparation has not been cancelled."""
        async with self.db() as db:
            await db.execute("BEGIN IMMEDIATE")
            changed = await db.execute("""UPDATE scene_sessions SET card_id=?,bindings=?,status='active',started_at=?
                WHERE id=? AND status='preparing' AND generation=?""",
                (card["id"], json.dumps(bindings), self.now(), session_id, generation))
            if changed.rowcount != 1:
                await db.rollback()
                return False
            consumed = await db.execute("UPDATE scene_cards SET used_at=? WHERE id=? AND used_at IS NULL", (self.now(), card["id"]))
            if consumed.rowcount != 1:
                await db.rollback()
                return False
            await db.commit()
        return True

    async def state(self, session_id, *, status, generation, reason=None, recent=None, reveal=False):
        async with self.db() as db:
            await db.execute("""UPDATE scene_sessions SET status=?,generation=?,reason=?,ended_at=?,
                revealed_at=CASE WHEN ? THEN COALESCE(revealed_at,?) ELSE revealed_at END WHERE id=?""",
                (status, generation, reason, self.now() if status == "ended" else None, reveal, self.now(), session_id))
            if status == "ended" and recent is not None:
                from .experience import TTL_SECONDS
                summary_status = "pending" if recent.get("items") and reason != "server_shutdown" else "skipped"
                await db.execute("""INSERT OR IGNORE INTO scene_recent(session_id,evidence,summary_status,expires_at)
                    VALUES (?,?,?,?)""", (session_id, json.dumps(recent, ensure_ascii=False), summary_status, self.now()+TTL_SECONDS))
            await db.commit()

    async def aftercare(self, session_id):
        async with self.db() as db:
            await db.execute("UPDATE scene_sessions SET phase='aftercare' WHERE id=?", (session_id,))
            await db.commit()

    async def reveal(self, session_id):
        async with self.db() as db:
            await db.execute("""UPDATE scene_sessions SET revealed_at=?,generation=generation+1
                WHERE id=? AND status='ended' AND revealed_at IS NULL""", (self.now(), session_id))
            await db.commit()

    async def recover(self):
        async with self.db() as db:
            result = await db.execute("""UPDATE scene_sessions SET status='ended',generation=generation+1,
                ended_at=?,reason='server_restart' WHERE status!='ended'""", (self.now(),))
            await db.execute("UPDATE scene_feedback SET note_status='failed',error_type='server_restart' WHERE note_status='pending' AND processed_version<version")
            from .experience import TTL_SECONDS
            # A crashed process has no surviving transcript. Retain only the
            # known start/end fact; never reconstruct the preparation card.
            await db.execute("""INSERT OR IGNORE INTO scene_recent(session_id,evidence,summary_status,expires_at)
                SELECT id,'{}','failed',ended_at+? FROM scene_sessions
                WHERE reason='server_restart' AND started_at IS NOT NULL AND ended_at>?""",
                (TTL_SECONDS, self.now()-TTL_SECONDS))
            await db.execute("UPDATE scene_recent SET summary_status='failed' WHERE summary_status='pending'")
            await db.execute("UPDATE scene_memory_writes SET status='pending' WHERE status='running'")
            await db.commit()
            return result.rowcount

    async def recent(self, *, now, limit=3):
        async with self.db() as db:
            db.row_factory = aiosqlite.Row
            rows = await (await db.execute("""SELECT s.id,s.started_at,s.ended_at,s.reason,s.status,
                r.evidence,r.summary,r.summary_status,r.expires_at,f.rating,f.reason AS feedback_reason
                FROM scene_recent r JOIN scene_sessions s ON s.id=r.session_id
                LEFT JOIN scene_feedback f ON f.session_id=s.id
                WHERE s.status='ended' AND s.ended_at<=? AND r.expires_at>?
                ORDER BY s.ended_at DESC,s.id DESC LIMIT ?""", (now, now, limit))).fetchall()
        return [_decode(row) for row in rows]

    async def finish_recent(self, session_id, *, summary="", status="done"):
        async with self.db() as db:
            await db.execute("""UPDATE scene_recent SET summary=?,summary_status=?
                WHERE session_id=? AND summary_status='pending' AND expires_at>?""",
                (summary, status, session_id, self.now()))
            await db.commit()

    async def prune_recent(self):
        from .experience import TTL_SECONDS
        async with self.db() as db:
            await db.execute("DELETE FROM scene_recent WHERE expires_at<=?", (self.now(),))
            # Authored notes live in the memory library after delivery; this is
            # only a bounded delivery buffer, not another transcript archive.
            await db.execute("DELETE FROM scene_memory_writes WHERE created_at<=?", (self.now()-TTL_SECONDS,))
            await db.commit()

    async def queue_memories(self, session_id, turn_id, notes, *, created_at):
        async with self.db() as db:
            await db.executemany("""INSERT OR IGNORE INTO scene_memory_writes
                (id,session_id,content,created_at) VALUES (?,?,?,?)""",
                [(f"scene_remember_{turn_id}_{index}", session_id, note, created_at)
                 for index, note in enumerate(notes)])
            await db.commit()

    async def pending_memories(self):
        from .experience import TTL_SECONDS
        async with self.db() as db:
            db.row_factory = aiosqlite.Row
            rows = await (await db.execute("""SELECT w.*,s.conv_id,s.started_at FROM scene_memory_writes w
                JOIN scene_sessions s ON s.id=w.session_id WHERE w.status='pending' AND w.created_at>?
                ORDER BY w.created_at,w.id""", (self.now()-TTL_SECONDS,))).fetchall()
        return [dict(row) for row in rows]

    async def claim_memory(self, job_id):
        async with self.db() as db:
            result = await db.execute("UPDATE scene_memory_writes SET status='running' WHERE id=? AND status='pending'", (job_id,))
            await db.commit()
            return result.rowcount == 1

    async def finish_memory(self, job_id, *, status, memory_id=None, error_type=None):
        async with self.db() as db:
            await db.execute("""UPDATE scene_memory_writes SET status=?,memory_id=?,error_type=?,
                content=CASE WHEN ?='done' THEN '' ELSE content END WHERE id=?""",
                (status, memory_id, error_type, status, job_id))
            await db.commit()

    async def history(self, limit=50):
        async with self.db() as db:
            db.row_factory = aiosqlite.Row
            rows = await (await db.execute("SELECT * FROM scene_sessions WHERE status='ended' AND card_id IS NOT NULL ORDER BY ended_at DESC LIMIT ?", (limit,))).fetchall()
        return [_decode(row) for row in rows]

    async def notes(self):
        return await self._one("SELECT * FROM scene_notes WHERE id=1")

    async def feedback(self, session_id):
        return await self._one("SELECT * FROM scene_feedback WHERE session_id=?", (session_id,))

    async def save_feedback(self, session_id, *, rating, reason):
        if rating not in (None, "up", "down") or not isinstance(reason, str) or len(reason) > 4000:
            raise ValueError("invalid_scene_feedback")
        reason = reason.strip()
        async with self.db() as db:
            db.row_factory = aiosqlite.Row
            await db.execute("BEGIN IMMEDIATE")
            old = await (await db.execute("SELECT * FROM scene_feedback WHERE session_id=?", (session_id,))).fetchone()
            if old and old["rating"] == rating and old["reason"] == reason:
                await db.rollback()
                return dict(old)
            version = int(old["version"]) + 1 if old else 1
            status = "pending" if rating is not None or reason else "skipped"
            await db.execute("""INSERT INTO scene_feedback
                (session_id,rating,reason,version,note_status,updated_at) VALUES (?,?,?,?,?,?)
                ON CONFLICT(session_id) DO UPDATE SET rating=excluded.rating,reason=excluded.reason,
                version=excluded.version,note_status=excluded.note_status,error_type=NULL,updated_at=excluded.updated_at""",
                (session_id, rating, reason, version, status, self.now()))
            await db.commit()
        return await self.feedback(session_id)

    async def apply_notes(self, session_id, *, feedback_version, notes_version, content):
        async with self.db() as db:
            await db.execute("BEGIN IMMEDIATE")
            feedback = await (await db.execute("SELECT version,processed_version FROM scene_feedback WHERE session_id=?", (session_id,))).fetchone()
            note = await (await db.execute("SELECT version FROM scene_notes WHERE id=1")).fetchone()
            if not feedback or feedback[0] != feedback_version or feedback[1] >= feedback_version or note[0] != notes_version:
                await db.rollback()
                return False
            if content is not None:
                await db.execute("UPDATE scene_notes SET content=?,version=version+1,updated_at=? WHERE id=1", (content, self.now()))
            await db.execute("UPDATE scene_feedback SET processed_version=?,note_status='processed',error_type=NULL WHERE session_id=?", (feedback_version, session_id))
            await db.commit()
            return True

    async def note_failed(self, session_id, version, error_type):
        async with self.db() as db:
            await db.execute("UPDATE scene_feedback SET note_status='failed',error_type=? WHERE session_id=? AND version=? AND processed_version<?", (error_type, session_id, version, version))
            await db.commit()
