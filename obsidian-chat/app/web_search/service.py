"""Background web search and dialogue-turn inbox interface."""

from __future__ import annotations

import asyncio
import time

from app.background_tasks import create_tracked_task
from config import load_ai_behavior
from database import get_db

from .organizer import organize_search_results
from .prompt import render_ready_results
from .provider import TavilySearchProvider, tavily_api_key
from .repository import WebSearchRepository


class WebSearchService:
    def __init__(self, repository=None, provider=None, organizer=None):
        self.repository = repository or WebSearchRepository()
        self.provider = provider or TavilySearchProvider()
        self.organizer = organizer or organize_search_results
        self._tasks: dict[str, asyncio.Task] = {}

    def enabled(self) -> bool:
        return bool(load_ai_behavior().get("web_search_enabled", False) and tavily_api_key())

    def start_background(self, search_id: str | None) -> None:
        if not search_id or search_id in self._tasks:
            return
        task = create_tracked_task(self._run(search_id), name=f"web_search:{search_id}")
        self._tasks[search_id] = task
        task.add_done_callback(lambda _task: self._tasks.pop(search_id, None))

    async def _run(self, search_id: str) -> None:
        row = await self.repository.get(search_id)
        if not row or row.get("status") != "queued":
            return
        if float(row.get("expires_at") or 0) <= time.time():
            await self.repository.mark_failed(search_id, reason="worker_deadline")
            return
        try:
            response = await self.provider.search(str(row.get("intent_text") or ""))
            searched_at = time.time()
            results = list(response.get("results") or [])
            if results:
                organized = await self.organizer(
                    str(row.get("intent_text") or ""),
                    searched_at,
                    results,
                )
            else:
                organized = {
                    "digest": "这次没有找到足够可靠的资料。",
                    "claims": [],
                    "uncertainties": ["搜索没有返回可用结果"],
                    "sources": [],
                    "searched_at": searched_at,
                }
            if response.get("request_id"):
                organized["provider_request_id"] = response["request_id"]
            await self.repository.mark_ready(search_id, result=organized, now=time.time())
        except Exception as exc:
            await self.repository.mark_failed(
                search_id,
                reason=f"{type(exc).__name__}:{exc}",
            )

    async def resume_queued(self) -> int:
        if not self.enabled():
            return 0
        ids = await self.repository.recoverable_queued(now=time.time())
        for search_id in ids:
            self.start_background(search_id)
        return len(ids)

    async def capacity_snapshot(self, conv_id: str) -> dict:
        return await self.repository.capacity_snapshot(conv_id)

    async def prepare_dialogue_turn(
        self,
        *,
        conv_id: str,
        bound_turn_id: str,
        user_name: str | None = None,
    ) -> dict:
        if not self.enabled():
            return {"status": "disabled", "rows": [], "block": ""}
        rows = await self.repository.claim_ready(
            conv_id=conv_id,
            bound_turn_id=bound_turn_id,
            now=time.time(),
        )
        return {
            "status": "bound" if rows else "none",
            "bound_turn_id": bound_turn_id,
            "ids": [str(row["id"]) for row in rows],
            "rows": rows,
            "block": render_ready_results(rows, user_name=user_name),
        }

    async def replay_for_assistant(
        self,
        assistant_message_id: str,
        *,
        user_name: str | None = None,
    ) -> dict:
        rows = await self.repository.consumed_for_assistant(assistant_message_id)
        return {
            "status": "replay" if rows else "none",
            "assistant_message_id": assistant_message_id,
            "rows": rows,
            "block": render_ready_results(rows, user_name=user_name),
        }

    async def finalize_dialogue_turn_in_tx(
        self,
        db,
        *,
        conv_id: str,
        bound_turn_id: str,
        assistant_message_id: str,
        intent_text: str,
        origin_source: str,
        allow_new_intent: bool,
        now: float,
        replay_from_assistant_message_id: str = "",
    ) -> dict:
        if replay_from_assistant_message_id:
            await self.repository.reassign_consumed_in_tx(
                db,
                from_assistant_message_id=replay_from_assistant_message_id,
                to_assistant_message_id=assistant_message_id,
            )
        consumed = 0
        if bound_turn_id:
            consumed = await self.repository.consume_bound_in_tx(
                db,
                bound_turn_id=bound_turn_id,
                assistant_message_id=assistant_message_id,
                now=now,
            )
        queued = {"status": "ignored", "search_id": None}
        if allow_new_intent and self.enabled() and str(intent_text or "").strip():
            queued = await self.repository.enqueue_in_tx(
                db,
                conv_id=conv_id,
                origin_source=origin_source,
                origin_turn_id=assistant_message_id,
                intent_text=str(intent_text).strip(),
                now=now,
            )
        return {"consumed": consumed, **queued}

    async def finalize_independent(self, **kwargs) -> dict:
        async with get_db() as db:
            await db.execute("BEGIN IMMEDIATE")
            try:
                result = await self.finalize_dialogue_turn_in_tx(db, **kwargs)
                await db.commit()
            except BaseException:
                await db.rollback()
                raise
        self.start_background(result.get("search_id"))
        return result

    async def enqueue_opportunity(
        self,
        *,
        conv_id: str,
        origin_turn_id: str,
        intent_text: str,
    ) -> dict:
        if not self.enabled():
            return {"status": "disabled", "search_id": None}
        result = await self.repository.enqueue(
            conv_id=conv_id,
            origin_source="opportunity",
            origin_turn_id=origin_turn_id,
            intent_text=intent_text,
            now=time.time(),
        )
        self.start_background(result.get("search_id"))
        return result


web_search_service = WebSearchService()


__all__ = ["WebSearchService", "web_search_service"]
