from __future__ import annotations

import json
import time
from uuid import uuid4

import aiosqlite


def encode(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def decode(row):
    if row is None:
        return None
    result = dict(row)
    for key in ("entry_history", "payload", "evidence", "result"):
        if result.get(key) is not None:
            result[key] = json.loads(result[key])
    return result


class TogetherRepository:
    def __init__(self, db_factory=None, *, now=time.time):
        if db_factory is None:
            from database import get_db
            db_factory = get_db
        self.db, self.now = db_factory, now

    async def read(self, sql, params=(), *, many=False):
        async with self.db() as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(sql, params)
            return [decode(row) for row in await cursor.fetchall()] if many else decode(await cursor.fetchone())

    async def execute(self, sql, params=()):
        async with self.db() as db:
            cursor = await db.execute(sql, params)
            await db.commit()
            return cursor.rowcount

    async def session(self, session_id):
        return await self.read("SELECT * FROM together_sessions WHERE id=?", (session_id,))

    async def current(self):
        return await self.read("SELECT * FROM together_sessions WHERE status='active'")

    async def snapshot(self, session_id):
        """One read transaction keeps the event cursor aligned with its data."""
        async with self.db() as db:
            db.row_factory = aiosqlite.Row
            await db.execute("BEGIN")
            row = decode(await (await db.execute("SELECT * FROM together_sessions WHERE id=?", (session_id,))).fetchone())
            messages, captures = [], []
            if row and row["status"] == "active":
                messages = [decode(m) for m in await (await db.execute("SELECT * FROM together_messages WHERE session_id=? ORDER BY seq", (session_id,))).fetchall()]
                captures = [decode(c) for c in await (await db.execute("SELECT * FROM together_captures WHERE session_id=? AND status IN ('requested','ready','attached')", (session_id,))).fetchall()]
            cursor = await (await db.execute("SELECT MAX(seq) FROM together_events WHERE session_id=?", (session_id,))).fetchone()
        return row, messages, captures, cursor[0] or 0

    async def by_request(self, request_id):
        return await self.read("SELECT * FROM together_sessions WHERE request_id=?", (request_id,))

    async def _emit(self, db, session_id, event, payload):
        await db.execute("INSERT INTO together_events(session_id,type,payload,created_at) VALUES (?,?,?,?)",
                         (session_id, event, encode(payload), self.now()))
        await db.execute("""DELETE FROM together_events WHERE session_id=? AND seq NOT IN
            (SELECT seq FROM together_events WHERE session_id=? ORDER BY seq DESC LIMIT 500)""", (session_id, session_id))

    async def emit(self, session_id, event, payload):
        async with self.db() as db:
            await self._emit(db, session_id, event, payload)
            await db.commit()

    async def events(self, session_id, after=0):
        async with self.db() as db:
            db.row_factory = aiosqlite.Row
            bounds = await (await db.execute("SELECT MIN(seq),MAX(seq) FROM together_events WHERE session_id=?", (session_id,))).fetchone()
            rows = await (await db.execute("SELECT * FROM together_events WHERE session_id=? AND seq>? ORDER BY seq LIMIT 200", (session_id, after))).fetchall()
        return {"events": [decode(row) for row in rows], "cursor": int(rows[-1]["seq"] if rows else (bounds[1] or after)),
                "resync": bool(after and bounds[0] and after < bounds[0]-1)}

    async def create(self, *, request_id, conv_id, model_key, device_id, source, entry_history, wait_for_device=False):
        session_id, now = "together_" + uuid4().hex, self.now()
        async with self.db() as db:
            await db.execute("""INSERT INTO together_sessions
                (id,request_id,conv_id,model_key,device_id,source,entry_history,started_at,heartbeat_at,accounted_at)
                VALUES (?,?,?,?,?,?,?,?,?,?)""",
                (session_id, request_id, conv_id, model_key, device_id, source, encode(entry_history), now, 0 if wait_for_device else now, now))
            await self._emit(db, session_id, "started", {"session_id": session_id})
            from .coexist import sync_holds_in_tx
            await sync_holds_in_tx(db, now=now, enabled=True)
            await db.commit()
        return await self.session(session_id)

    async def update(self, session_id, **values):
        allowed = {"model_key", "source", "source_version", "capture_state", "input_busy", "generation",
                   "heartbeat_at", "wake_at", "wake_intent", "wake_image", "opening_pending"}
        if not values or set(values)-allowed:
            raise ValueError("together_invalid_session_update")
        columns = ",".join(key + "=?" for key in values)
        await self.execute(f"UPDATE together_sessions SET {columns} WHERE id=? AND status='active'", (*values.values(), session_id))

    async def heartbeat(self, session_id, *, lease_seconds, input_busy, capture_state, enabled=True):
        now = self.now()
        async with self.db() as db:
            await db.execute("""UPDATE together_sessions SET
                active_seconds=active_seconds+MAX(0,MIN(?,heartbeat_at+?)-accounted_at),
                heartbeat_at=?,accounted_at=?,recovery_heartbeat_at=NULL,input_busy=?,capture_state=?
                WHERE id=? AND status='active'""",
                (now, lease_seconds, now, now, int(input_busy), capture_state, session_id))
            from .coexist import sync_holds_in_tx
            await sync_holds_in_tx(db, now=now, enabled=enabled, lease_seconds=lease_seconds)
            await db.commit()

    async def expire_connection(self, session_id, *, lease_seconds):
        now = self.now()
        async with self.db() as db:
            changed = await db.execute("""UPDATE together_sessions SET
                active_seconds=active_seconds+MAX(0,MIN(?,heartbeat_at+?)-accounted_at),
                accounted_at=?,capture_state='unavailable',input_busy=0
                WHERE id=? AND status='active' AND heartbeat_at+?<=? AND accounted_at<heartbeat_at+?""",
                (now, lease_seconds, now, session_id, lease_seconds, now, lease_seconds))
            if changed.rowcount:
                await db.execute("UPDATE together_captures SET status='failed',error='connection_lost' WHERE session_id=? AND purpose!='manual' AND status='requested'", (session_id,))
                await self._emit(db, session_id, "connection_lost", {})
            await db.commit()

    async def messages(self, session_id, *, before_seq=None, limit=200):
        extra, params = (" AND seq<=?", (before_seq,)) if before_seq is not None else ("", ())
        rows = await self.read(f"""SELECT * FROM together_messages WHERE session_id=?{extra}
            ORDER BY seq DESC LIMIT ?""", (session_id, *params, limit), many=True)
        return list(reversed(rows))

    async def context_messages(self, turn, *, limit):
        """Order processed exchanges by completion, then append the current input.

        Inputs can arrive while the previous reply is still being generated, so
        message insertion order is not conversation-turn order. Group each old
        input with its reply before applying the context limit. Queued inputs do
        not occupy the window or leak into an earlier response.
        """
        current = await self.message(turn["message_id"]) if turn["message_id"] else None
        rows = await self.read("""SELECT m.* FROM together_messages m
            WHERE m.session_id=? AND m.status IN ('complete','failed','interrupted')
                AND (? IS NULL OR m.id!=?)
            ORDER BY COALESCE((SELECT MAX(done.seq) FROM together_messages done
                WHERE done.session_id=m.session_id AND done.turn_id=m.turn_id),m.seq) DESC,
                m.seq DESC LIMIT ?""",
            (turn["session_id"], turn["message_id"], turn["message_id"], max(0, limit-bool(current))), many=True)
        return list(reversed(rows)) + ([current] if current else [])

    async def message(self, message_id):
        return await self.read("SELECT * FROM together_messages WHERE id=?", (message_id,))

    async def add_message(self, session_id, *, client_id, content, capture_id=None):
        async with self.db() as db:
            db.row_factory = aiosqlite.Row
            await db.execute("BEGIN IMMEDIATE")
            old = await (await db.execute("SELECT * FROM together_messages WHERE session_id=? AND client_id=?", (session_id, client_id))).fetchone()
            if old:
                if old["content"] != content or old["capture_id"] != capture_id:
                    raise ValueError("together_message_id_reused")
                return decode(old), False
            row = await (await db.execute("SELECT status FROM together_sessions WHERE id=?", (session_id,))).fetchone()
            if not row or row[0] != "active":
                raise ValueError("together_session_ended")
            count = await (await db.execute("SELECT COUNT(*) FROM together_messages WHERE session_id=? AND status IN ('queued','running')", (session_id,))).fetchone()
            if count[0] >= 50:
                raise ValueError("together_message_queue_full")
            if capture_id:
                claimed = await db.execute("""UPDATE together_captures SET status='attached'
                    WHERE id=? AND session_id=? AND purpose='manual' AND status='ready' AND expires_at>?""",
                    (capture_id, session_id, self.now()))
                if claimed.rowcount != 1:
                    raise ValueError("together_draft_unavailable")
            message_id = "tm_" + uuid4().hex
            await db.execute("""INSERT INTO together_messages(id,session_id,client_id,role,content,status,capture_id,created_at)
                VALUES (?,?,?,'user',?,'queued',?,?)""", (message_id, session_id, client_id, content, capture_id, self.now()))
            await db.execute("UPDATE together_sessions SET wake_at=NULL,wake_intent='',wake_image=0 WHERE id=?", (session_id,))
            await self._emit(db, session_id, "message_accepted", {"message_id": message_id, "client_id": client_id})
            await db.commit()
        return await self.message(message_id), True

    async def claim_turn(self, session_id, *, lease_seconds):
        async with self.db() as db:
            db.row_factory = aiosqlite.Row
            await db.execute("BEGIN IMMEDIATE")
            row = decode(await (await db.execute("SELECT * FROM together_sessions WHERE id=?", (session_id,))).fetchone())
            if not row or row["status"] != "active" or row["heartbeat_at"]+lease_seconds <= self.now():
                return None
            running = await (await db.execute("SELECT 1 FROM together_turns WHERE session_id=? AND status='running'", (session_id,))).fetchone()
            if running:
                return None
            message = decode(await (await db.execute("""SELECT * FROM together_messages
                WHERE session_id=? AND status='queued' ORDER BY seq LIMIT 1""", (session_id,))).fetchone())
            if message:
                kind = "message"
            elif row["opening_pending"]:
                kind = "opening"
            elif row["wake_at"] is not None and row["wake_at"] <= self.now() and not row["input_busy"]:
                kind = "awake"
            else:
                return None
            turn = {"id": "tt_"+uuid4().hex, "session_id": session_id, "kind": kind,
                    "generation": row["generation"], "message_id": message["id"] if message else None,
                    "status": "running", "created_at": self.now()}
            await db.execute("""INSERT INTO together_turns(id,session_id,kind,generation,message_id,status,created_at)
                VALUES (:id,:session_id,:kind,:generation,:message_id,:status,:created_at)""", turn)
            if message:
                await db.execute("UPDATE together_messages SET status='running',turn_id=? WHERE id=?", (turn["id"], message["id"]))
            await db.execute("UPDATE together_sessions SET opening_pending=0,wake_at=NULL,wake_intent='',wake_image=0 WHERE id=?", (session_id,))
            await self._emit(db, session_id, "turn_started", {"turn_id": turn["id"], "kind": kind})
            await db.commit()
        return {**turn, "session": row, "message": message}

    async def turn_current(self, turn):
        row = await self.read("""SELECT t.id FROM together_turns t JOIN together_sessions s ON s.id=t.session_id
            WHERE t.id=? AND t.status='running' AND s.status='active' AND s.generation=t.generation""", (turn["id"],))
        return row is not None

    async def finish_turn(self, turn, *, output, evidence, identity, config):
        async with self.db() as db:
            db.row_factory = aiosqlite.Row
            await db.execute("BEGIN IMMEDIATE")
            row = await (await db.execute("""SELECT s.* FROM together_sessions s JOIN together_turns t ON t.session_id=s.id
                WHERE t.id=? AND t.status='running' AND s.status='active' AND s.generation=t.generation""", (turn["id"],))).fetchone()
            if not row:
                return False
            sid = row["id"]
            if output["visible"]:
                await db.execute("""INSERT INTO together_messages(id,session_id,role,content,status,created_at,turn_id)
                    VALUES (?,?,'assistant',?,'complete',?,?)""", ("tm_"+uuid4().hex, sid, output["visible"], self.now(), turn["id"]))
            if turn["message_id"]:
                await db.execute("UPDATE together_messages SET status='complete' WHERE id=?", (turn["message_id"],))
            awake = output.get("awake")
            # A newer owner message has already cancelled the original wake;
            # only the response to the newest queued input can install another.
            pending = await (await db.execute("SELECT 1 FROM together_messages WHERE session_id=? AND status='queued' LIMIT 1", (sid,))).fetchone()
            if awake and not pending:
                await db.execute("UPDATE together_sessions SET wake_at=?,wake_intent=?,wake_image=? WHERE id=?",
                    (self.now()+max(config["minimum_wake_seconds"], awake["after_seconds"]), awake["intent"], int(awake["needs_image"]), sid))
            for kind, items in (("note", output.get("notes", [])), ("understanding", output.get("understandings", []))):
                for index, value in enumerate(items):
                    payload = {"value": value, "conv_id": row["conv_id"], "model_key": row["model_key"], "identity": identity}
                    await db.execute("""INSERT OR IGNORE INTO together_writebacks
                        (id,session_id,turn_id,kind,payload,evidence,created_at) VALUES (?,?,?,?,?,?,?)""",
                        (f"{turn['id']}:{kind}:{index}", sid, turn["id"], kind, encode(payload), encode(evidence), self.now()))
            await db.execute("UPDATE together_turns SET status='complete',finished_at=? WHERE id=?", (self.now(), turn["id"]))
            await self._emit(db, sid, "turn_finished", {"turn_id": turn["id"], "visible": output["visible"]})
            await self._trim(db, sid, config["retained_messages"])
            await db.commit()
        return True

    async def _trim(self, db, session_id, limit):
        await db.execute("""DELETE FROM together_messages WHERE session_id=? AND status NOT IN ('queued','running')
            AND seq NOT IN (SELECT seq FROM together_messages WHERE session_id=? ORDER BY seq DESC LIMIT ?)""", (session_id, session_id, limit))
        await db.execute("""DELETE FROM together_turns WHERE session_id=? AND status!='running' AND id NOT IN
            (SELECT id FROM together_turns WHERE session_id=? ORDER BY created_at DESC LIMIT ?)""", (session_id, session_id, limit))

    async def abort_turn(self, turn, *, reason, partial="", retry_message=False):
        async with self.db() as db:
            changed = await db.execute("UPDATE together_turns SET status='interrupted',finished_at=?,error=? WHERE id=? AND status='running'",
                                       (self.now(), reason, turn["id"]))
            if changed.rowcount:
                # Ended sessions never receive late text, including partial text.
                await db.execute("UPDATE together_captures SET status='failed',error=? WHERE session_id=? AND purpose!='manual' AND status='requested'", (reason, turn["session_id"]))
                session = await (await db.execute("SELECT status FROM together_sessions WHERE id=?", (turn["session_id"],))).fetchone()
                if session and session[0] == "active":
                    if partial:
                        await db.execute("""INSERT INTO together_messages(id,session_id,role,content,status,created_at,turn_id)
                            VALUES (?,?,'assistant',?,'interrupted',?,?)""",
                            ("tm_"+uuid4().hex, turn["session_id"], partial, self.now(), turn["id"]))
                    if turn["message_id"]:
                        await db.execute("UPDATE together_messages SET status=? WHERE id=?", ("queued" if retry_message else "failed", turn["message_id"]))
                    await self._emit(db, turn["session_id"], "turn_interrupted", {"turn_id": turn["id"], "error": reason})
                await db.commit()

    async def create_capture(self, session, *, purpose, config):
        capture_id = "tc_"+uuid4().hex
        now = self.now()
        async with self.db() as db:
            existing = await (await db.execute("""SELECT id FROM together_captures
                WHERE session_id=? AND status='requested' AND deadline>? LIMIT 1""", (session["id"], now))).fetchone()
            if existing:
                raise ValueError("together_capture_busy")
            await db.execute("""INSERT INTO together_captures(id,session_id,source,source_version,purpose,requested_at,deadline)
                VALUES (?,?,?,?,?,?,?)""", (capture_id, session["id"], session["source"], session["source_version"], purpose, now, now+config["capture_timeout_seconds"]))
            await self._emit(db, session["id"], "capture_requested", {"capture_id": capture_id, "source": session["source"],
                "source_version": session["source_version"], "purpose": purpose, "deadline": now+config["capture_timeout_seconds"]})
            await db.commit()
        return await self.capture(capture_id)

    async def capture(self, capture_id):
        return await self.read("SELECT * FROM together_captures WHERE id=?", (capture_id,))

    async def captures(self, session_id):
        return await self.read("SELECT * FROM together_captures WHERE session_id=? AND status IN ('requested','ready','attached')", (session_id,), many=True)

    async def receive_capture(self, capture_id, *, filename=None, sha256=None, captured_at=None, error=None, draft_seconds=600):
        async with self.db() as db:
            row = await (await db.execute("SELECT session_id FROM together_captures WHERE id=?", (capture_id,))).fetchone()
            if not row:
                raise LookupError("together_capture_missing")
            changed = await db.execute("""UPDATE together_captures SET status=?,filename=?,sha256=?,captured_at=?,received_at=?,expires_at=?,error=?
                WHERE id=? AND status='requested' AND deadline>=? AND EXISTS
                (SELECT 1 FROM together_sessions s WHERE s.id=together_captures.session_id AND s.status='active'
                 AND s.source_version=together_captures.source_version AND s.capture_state='ready')""",
                ("failed" if error else "ready", filename, sha256, captured_at, self.now(), self.now()+draft_seconds, error, capture_id, self.now()))
            if changed.rowcount:
                await self._emit(db, row[0], "capture_finished", {"capture_id": capture_id, "status": "failed" if error else "ready", "error": error})
            await db.commit()
        return bool(changed.rowcount)

    async def invalidate_captures(self, session_id, reason):
        await self.execute("UPDATE together_captures SET status='failed',error=? WHERE session_id=? AND status='requested'", (reason, session_id))

    async def finish(self, session_id, *, event_content, lease_seconds, ended_at=None):
        now = self.now()
        async with self.db() as db:
            db.row_factory = aiosqlite.Row
            await db.execute("BEGIN IMMEDIATE")
            row = decode(await (await db.execute("SELECT * FROM together_sessions WHERE id=?", (session_id,))).fetchone())
            if not row or row["status"] == "ended":
                return row
            # Restart revokes the live lease, but the last real heartbeat still
            # anchors an offline end receipt and can undo provisional credit.
            heartbeat = row["heartbeat_at"] or row["recovery_heartbeat_at"] or 0
            ended = now if ended_at is None else max(row["started_at"], heartbeat, min(now, float(ended_at)))
            credited_until = min(row["accounted_at"], heartbeat+lease_seconds)
            seconds = max(0, row["active_seconds"] + min(ended, heartbeat+lease_seconds)-credited_until)
            event = event_content(seconds)
            await db.execute("""UPDATE together_sessions SET status='ended',ended_at=?,active_seconds=?,accounted_at=?,
                generation=generation+1,wake_at=NULL,wake_intent='',wake_image=0,opening_pending=0,capture_state='paused' WHERE id=?""",
                (ended, seconds, now, session_id))
            await db.execute("UPDATE together_captures SET status='failed',error='ended' WHERE session_id=? AND status='requested'", (session_id,))
            await db.execute("UPDATE together_turns SET status='interrupted',finished_at=?,error='ended' WHERE session_id=? AND status='running'", (now, session_id))
            # Preserve only actual room utterances, never the borrowed entry
            # history or image files. This bounded buffer is not a memory source.
            from .shared_history import retain_context_in_tx
            await retain_context_in_tx(db, session_id, conv_id=row['conv_id'])
            await db.execute("""INSERT OR IGNORE INTO together_writebacks(id,session_id,kind,payload,evidence,created_at)
                VALUES (?,?,'event',?,'{}',?)""", (session_id+":event", session_id, encode({"content": event,
                    "conv_id": row["conv_id"], "started_at": row["started_at"], "ended_at": ended, "active_seconds": seconds}), now))
            await self._emit(db, session_id, "ended", {"content": event, "active_seconds": seconds})
            from .coexist import sync_holds_in_tx
            await sync_holds_in_tx(db, now=now, enabled=False)
            await db.commit()
        return await self.session(session_id)

    async def cleanup_transcript(self, session_id):
        """Only after the event is durable; independent outbox evidence survives."""
        async with self.db() as db:
            row = await (await db.execute("SELECT status,event_message_id FROM together_sessions WHERE id=?", (session_id,))).fetchone()
            if not row or row[0] != "ended" or not row[1]:
                return False
            for table in ("together_messages", "together_turns", "together_events"):
                await db.execute(f"DELETE FROM {table} WHERE session_id=?", (session_id,))
            await db.execute("UPDATE together_sessions SET entry_history='[]',cleaned_at=? WHERE id=?", (self.now(), session_id))
            await db.commit()
        return True

    async def recover(self, *, lease_seconds=45):
        """No model replay; an active session survives a backend restart offline."""
        async with self.db() as db:
            await db.execute("UPDATE together_messages SET status='failed' WHERE status='running'")
            await db.execute("UPDATE together_turns SET status='interrupted',error='server_restart',finished_at=? WHERE status='running'", (self.now(),))
            await db.execute("""UPDATE together_sessions SET
                active_seconds=active_seconds+MAX(0,MIN(?,COALESCE(NULLIF(heartbeat_at,0),recovery_heartbeat_at,0)+?)-accounted_at),
                accounted_at=?,recovery_heartbeat_at=COALESCE(NULLIF(heartbeat_at,0),recovery_heartbeat_at),
                heartbeat_at=0,capture_state='unavailable',input_busy=0,generation=generation+1 WHERE status='active'""",
                (self.now(), lease_seconds, self.now()))
            await db.execute("UPDATE together_captures SET status='failed',error='server_restart' WHERE status='requested'")
            await db.execute("UPDATE together_writebacks SET status='failed',error='server_restart' WHERE status='running'")
            await db.commit()

    async def usage(self, session_id):
        return await self.read("""SELECT kind,model_key,COUNT(*) AS calls,SUM(input_tokens) AS input_tokens,
            SUM(output_tokens) AS output_tokens,SUM(reasoning_tokens) AS reasoning_tokens,SUM(image_count) AS image_count,
            SUM(elapsed_ms) AS elapsed_ms FROM together_usage WHERE session_id=? GROUP BY kind,model_key""", (session_id,), many=True)

    async def record_usage(self, session_id, *, kind, started_at, elapsed_ms, status, turn_id=None, model_key="", meta=None, image_count=0, error=None):
        meta = meta or {}
        def number(*keys):
            for key in keys:
                value = meta.get(key)
                if type(value) is int and value >= 0:
                    return value
            return None
        await self.execute("""INSERT INTO together_usage(session_id,turn_id,kind,model_key,started_at,elapsed_ms,status,input_tokens,output_tokens,reasoning_tokens,image_count,error)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""", (session_id, turn_id, kind, model_key, started_at, max(0, elapsed_ms), status,
                number("prompt_tokens", "input_tokens"), number("completion_tokens", "output_tokens"), number("reasoning_tokens"), image_count, error))
