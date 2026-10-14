"""Optional authored memories and temporary handoff summaries, outside playback."""
from __future__ import annotations

import asyncio
import logging

from app.provider_privacy import private_provider_call
from .prompts import recent_messages

logger = logging.getLogger(__name__)


async def write_memory(content, **kwargs):
    from app.memory_v2 import memory_service
    from ws import manager
    result = await memory_service.create_memory(content, "ai_note", **kwargs)
    if not result.get("deduplicated"):
        try:
            async with asyncio.timeout(2):
                await manager.broadcast({"type": "memory_added", "data": result})
        except Exception:
            logger.warning("Scene memory saved; notification unavailable")
    return result


class SceneMemory:
    def __init__(self, repository, models, *, writer=write_memory):
        self.repo, self.models, self.writer = repository, models, writer
        self.lock = asyncio.Lock()

    async def remember(self, session_id, turn_id, notes, *, created_at):
        await self.repo.queue_memories(session_id, turn_id, notes, created_at=created_at)
        await self.drain()

    async def drain(self):
        async with self.lock:
            for job in await self.repo.pending_memories():
                if not await self.repo.claim_memory(job["id"]):
                    continue
                try:
                    with private_provider_call("scene:remember"):
                        async with asyncio.timeout(60):
                            result = await self.writer(job["content"], source_conv=job["conv_id"],
                                source_start_ts=job["started_at"], source_end_ts=job["created_at"],
                                importance=0.6, idempotency_key=job["id"])
                    await self.repo.finish_memory(job["id"], status="done", memory_id=result["id"])
                except asyncio.CancelledError:
                    # Recovery may retry this accepted note with the same key.
                    await self.repo.finish_memory(job["id"], status="pending")
                    raise
                except Exception as exc:
                    await self.repo.finish_memory(job["id"], status="failed", error_type=type(exc).__name__)
                    logger.warning("Scene memory write failed: %s", type(exc).__name__)

    async def summarize(self, row, evidence, identity):
        try:
            async with asyncio.timeout(60):
                text = (await self.models.once(row["model_key"], recent_messages(
                    user_name=identity.user_name, ai_name=identity.ai_name, ai_persona=identity.ai_persona,
                    evidence=evidence, started_at=row["started_at"], ended_at=row["ended_at"],
                    reason=row["reason"]), scope="recent")).strip()
            # This one-shot only condenses temporary evidence; it has no tools
            # and must never interpret a returned remember/device command.
            if not text or len(text) > 2000 or "<" in text or "[REMEMBER:" in text:
                raise ValueError("scene_recent_invalid")
            await self.repo.finish_recent(row["id"], summary=text)
        except asyncio.CancelledError:
            await self.repo.finish_recent(row["id"], status="failed")
            raise
        except Exception as exc:
            await self.repo.finish_recent(row["id"], status="failed")
            logger.warning("Scene handoff uses factual fallback: %s", type(exc).__name__)
