from __future__ import annotations

import asyncio
import hashlib
import json

from app.provider_privacy import private_provider_call


async def _memory_writer(content, kind, **kwargs):
    from app.memory_v2 import memory_service
    from ws import manager
    result = await memory_service.create_memory(content, kind, **kwargs)
    if not result.get("deduplicated"):
        await manager.broadcast({"type": "memory_added", "data": result})
    return result


async def _cognition_writer(job, *, db_factory):
    from app.working_model.runtime import (capture_together_working_model_pipeline_input,
        run_working_model_pipeline, working_model_v2_write_enabled)
    from app.working_model.writer import build_writer_identity_snapshot
    if not working_model_v2_write_enabled():
        raise ValueError("together_cognition_write_disabled")
    payload = job["payload"]
    identity = build_writer_identity_snapshot(payload["identity"])
    value = capture_together_working_model_pipeline_input(conv_id=payload["conv_id"], together_source_id=job["id"],
        statement=payload["value"]["statement"], source=payload["value"]["source"], model_key=payload["model_key"],
        identity_snapshot=identity, together_attempt=job["attempts"])
    result = await run_working_model_pipeline(value, db_factory=db_factory)
    if result["request"]["status"] in {"processing", "failed"}:
        raise ValueError("together_cognition_" + (result["request"].get("failure_code") or "unfinished"))
    return {"request_id": result["request"]["id"], "status": result["request"]["status"]}


class Writebacks:
    def __init__(self, repository, *, memory_writer=_memory_writer, cognition_writer=_cognition_writer, broadcast=None):
        self.repo = repository
        self.memory_writer, self.cognition_writer = memory_writer, cognition_writer
        self.broadcast = broadcast
        self.lock = asyncio.Lock()

    async def drain(self):
        async with self.lock:
            jobs = await self.repo.read("""SELECT * FROM together_writebacks WHERE status='pending'
                ORDER BY CASE kind WHEN 'event' THEN 0 ELSE 1 END,created_at LIMIT 16""", many=True)
            for job in jobs:
                claimed = await self.repo.execute("UPDATE together_writebacks SET status='running' WHERE id=? AND status='pending'", (job["id"],))
                if not claimed:
                    continue
                try:
                    with private_provider_call("together:writeback"):
                        result = await self._write(job)
                    await self.repo.execute("UPDATE together_writebacks SET status='done',result=?,finished_at=?,error=NULL WHERE id=?",
                                             (json.dumps(result), self.repo.now(), job["id"]))
                except asyncio.CancelledError:
                    await self.repo.execute("UPDATE together_writebacks SET status='failed',error='interrupted' WHERE id=?", (job["id"],))
                    raise
                except Exception as exc:
                    # Do not retain provider prose or auto-loop paid retries.
                    error = str(exc)
                    if not error.startswith("together_") or not all(c.isalnum() or c == "_" for c in error):
                        error = type(exc).__name__
                    await self.repo.execute("UPDATE together_writebacks SET status='failed',error=? WHERE id=?", (error, job["id"]))
                finally:
                    # All optional evidence was copied into the outbox before
                    # these writes. A failed optional write needs no full transcript.
                    await self.repo.cleanup_transcript(job["session_id"])

    async def _write(self, job):
        data = job["payload"]
        if job["kind"] == "event":
            message_id = "together_event_"+hashlib.sha256(job["session_id"].encode()).hexdigest()
            async with self.repo.db() as db:
                await db.execute("BEGIN IMMEDIATE")
                await db.execute("""INSERT OR IGNORE INTO messages(id,conv_id,role,content,created_at)
                    VALUES (?,?,'system',?,?)""", (message_id, data["conv_id"], data["content"], data["ended_at"]))
                await db.execute("UPDATE conversations SET updated_at=MAX(updated_at,?) WHERE id=?", (data["ended_at"], data["conv_id"]))
                await db.execute("UPDATE together_sessions SET event_message_id=? WHERE id=?", (message_id, job["session_id"]))
                await db.commit()
            if self.broadcast:
                await self.broadcast({"type": "msg_created", "data": {"id": message_id, "conv_id": data["conv_id"],
                    "role": "system", "content": data["content"], "created_at": data["ended_at"]}})
            # The text event is already durable even if embedding/indexing fails.
            result = await self.memory_writer(data["content"], "event", source_conv=data["conv_id"],
                source_start_ts=data["started_at"], source_end_ts=data["ended_at"], idempotency_key=job["id"])
            return {"message_id": message_id, "memory_id": result["id"]}
        if job["kind"] == "note":
            result = await self.memory_writer(data["value"], "ai_note", source_conv=data["conv_id"],
                importance=.6, idempotency_key=job["id"])
            return {"memory_id": result["id"], "deduplicated": bool(result.get("deduplicated"))}
        if job["kind"] == "understanding":
            return await self.cognition_writer(job, db_factory=self.repo.db)
        raise ValueError("together_unknown_writeback")

    async def retry(self, job_id):
        job = await self.repo.read("SELECT * FROM together_writebacks WHERE id=?", (job_id,))
        if not job or job["status"] != "failed":
            raise ValueError("together_writeback_not_failed")
        # A terminal cognition failure may get a new attempt only on explicit
        # retry. First recover an already-applied attempt after an interrupted
        # outbox acknowledgement, avoiding a second writer call.
        increment = 0
        if job["kind"] == "understanding":
            from app.working_model.runtime import capture_together_working_model_pipeline_input, stable_working_model_request_id
            from app.working_model.writer import build_writer_identity_snapshot
            data = job["payload"]
            value = capture_together_working_model_pipeline_input(conv_id=data["conv_id"], together_source_id=job_id,
                statement=data["value"]["statement"], source=data["value"]["source"], model_key=data["model_key"],
                identity_snapshot=build_writer_identity_snapshot(data["identity"]), together_attempt=job["attempts"])
            request = await self.repo.read("SELECT status FROM working_model_requests WHERE id=?", (stable_working_model_request_id(value),))
            increment = int(bool(request and request["status"] == "failed"))
        await self.repo.execute("UPDATE together_writebacks SET status='pending',error=NULL,attempts=attempts+? WHERE id=? AND status='failed'", (increment, job_id))
