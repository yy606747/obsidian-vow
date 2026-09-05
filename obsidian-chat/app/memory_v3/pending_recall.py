"""Durable asynchronous RecallIntent retrieval and one-shot selection."""

from __future__ import annotations

import asyncio
from app.background_tasks import create_tracked_task
import json
import time
from collections.abc import Iterable

from app.memory_v2.hybrid_recall import wide_chunk_recall
from memory import _call_flash_lite

from .config import load_memory_v3_config, normalize_memory_v3_config
from .repository import PendingRecallRepository


SELECTOR_SCOPE = "memory:pending_selector"
SELECTOR_REASON_CODES = {
    "topic_changed",
    "not_needed",
    "no_matching_candidate",
    "matched",
}


def _json_object(value: object) -> dict:
    if isinstance(value, dict):
        return dict(value)
    try:
        parsed = json.loads(str(value or "{}"))
    except (TypeError, json.JSONDecodeError):
        return {}
    return dict(parsed) if isinstance(parsed, dict) else {}


def _json_list(value: object) -> list:
    if isinstance(value, list):
        return list(value)
    try:
        parsed = json.loads(str(value or "[]"))
    except (TypeError, json.JSONDecodeError):
        return []
    return list(parsed) if isinstance(parsed, list) else []


def _recent_context_payload(messages: Iterable[dict]) -> list[dict]:
    result: list[dict] = []
    for message in list(messages)[-6:]:
        role = str(message.get("role") or "")
        content = str(message.get("content") or "").strip()
        if role not in {"user", "assistant"} or not content or content.startswith("["):
            continue
        result.append({"role": role, "content": content[:500]})
    return result[-4:]


def build_selector_prompt(
    *,
    intent_text: str,
    current_user_message: str,
    recent_messages: Iterable[dict],
    candidates: list[dict],
    select_max: int,
) -> str:
    candidate_payload = [
        {
            "candidate_id": str(item.get("candidate_id") or ""),
            "memory": str(item.get("readout_text") or "")[:800],
        }
        for item in candidates
    ]
    payload = {
        "previous_intent": intent_text,
        "current_user_message": current_user_message,
        "recent_context": _recent_context_payload(recent_messages),
        "candidates": candidate_payload,
    }
    return f"""你在做一次跨轮记忆 gate + selector。上一轮只知道可能需要找什么；现在用户真正的新消息已经到来。

先判断上一轮意图现在是否仍然需要。只有候选确实是同一件旧事、且作为当前回复背景有帮助时才选；同主题但不是同一件事不算匹配。允许一个都不选。不要回答用户。

最多选择 {max(int(select_max), 0)} 条。needs_raw_detail_ids 只能是已经选择、且当前问题确实需要原文具体细节的 candidate_id。严格只输出 JSON：
{{"decision":"none|select","selected_candidate_ids":["..."],"needs_raw_detail_ids":["..."],"reason_code":"topic_changed|not_needed|no_matching_candidate|matched"}}

输入：
{json.dumps(payload, ensure_ascii=False)}"""


def validate_selector_result(
    payload: dict,
    *,
    candidate_ids: set[str],
    select_max: int,
) -> dict:
    if not isinstance(payload, dict):
        raise ValueError("selector output must be an object")
    decision = str(payload.get("decision") or "").strip().lower()
    if decision not in {"none", "select"}:
        raise ValueError("selector decision must be none or select")
    selected = list(
        dict.fromkeys(str(value) for value in payload.get("selected_candidate_ids") or [])
    )
    raw_detail = list(
        dict.fromkeys(str(value) for value in payload.get("needs_raw_detail_ids") or [])
    )
    if any(value not in candidate_ids for value in selected):
        raise ValueError("selector returned an unknown candidate")
    if len(selected) > max(int(select_max), 0):
        raise ValueError("selector returned too many candidates")
    if any(value not in selected for value in raw_detail):
        raise ValueError("raw detail ids must be selected candidates")
    reason = str(payload.get("reason_code") or "").strip().lower()
    if reason not in SELECTOR_REASON_CODES:
        raise ValueError("selector reason code is invalid")
    if decision == "none":
        selected = []
        raw_detail = []
    elif not selected:
        raise ValueError("select decision requires at least one candidate")
    return {
        "decision": decision,
        "selected_candidate_ids": selected,
        "needs_raw_detail_ids": raw_detail,
        "reason_code": reason,
    }


def selection_prompt_items(selection: dict, candidates: list[dict]) -> list[dict]:
    selected_ids = [str(value) for value in selection.get("selected_candidate_ids") or []]
    raw_ids = {str(value) for value in selection.get("needs_raw_detail_ids") or []}
    by_id = {str(item.get("candidate_id") or ""): item for item in candidates}
    items: list[dict] = []
    for candidate_id in selected_ids:
        candidate = by_id.get(candidate_id)
        if candidate is None:
            continue
        needs_raw = candidate_id in raw_ids
        has_card = candidate.get("readout_type") == "relational_card"
        items.append(
            {
                "id": candidate_id,
                "candidate_id": candidate_id,
                "source_type": candidate.get("source_type", "chunk"),
                "attachment_url": candidate.get("attachment_url"),
                "kind": "image_observation" if candidate.get("source_type") == "image" else "raw_chunk",
                "lane": "pending",
                "readout_type": (
                    "image_observation" if candidate.get("source_type") == "image" else "raw_full" if needs_raw else "relational_card" if has_card else "raw"
                ),
                "needs_raw_detail": needs_raw and candidate.get("source_type") != "image",
                "content": str(candidate.get("raw_content") or ""),
                "raw_content": str(candidate.get("raw_content") or ""),
                "preview": str(candidate.get("readout_text") or ""),
                "card_id": candidate.get("card_id"),
                "card_version": candidate.get("card_version"),
                "source_message_ids": list(candidate.get("source_message_ids") or []),
                "source_start_ts": candidate.get("source_start_ts"),
                "source_end_ts": candidate.get("source_end_ts"),
                "score": candidate.get("score"),
                "semantic_similarity": candidate.get("semantic_similarity"),
                "keyword_relevance": candidate.get("keyword_relevance"),
                "cooldown_penalty": candidate.get("cooldown_penalty"),
                "reason": "pending_selector",
                "prompt_priority": 1,
            }
        )
    return items


class PendingRecallService:
    def __init__(self, repository: PendingRecallRepository | None = None):
        self.repository = repository or PendingRecallRepository()
        self._tasks: dict[str, asyncio.Task] = {}
        self._registry_lock = asyncio.Lock()
        self._selection_locks: dict[str, asyncio.Lock] = {}

    async def _task_for(self, pending_id: str) -> asyncio.Task:
        async with self._registry_lock:
            existing = self._tasks.get(pending_id)
            if existing is not None and not existing.done():
                return existing
            task = create_tracked_task(
                self._retrieve(pending_id),
                name=f"pending_recall:{pending_id}",
            )
            self._tasks[pending_id] = task
            task.add_done_callback(
                lambda done, key=pending_id: self._drop_task(key, done)
            )
            return task

    def _drop_task(self, pending_id: str, task: asyncio.Task) -> None:
        if self._tasks.get(pending_id) is task:
            self._tasks.pop(pending_id, None)
        try:
            task.exception()
        except (asyncio.CancelledError, Exception):
            pass

    def start_background(self, pending_id: str) -> None:
        async def start() -> None:
            await self._task_for(pending_id)

        create_tracked_task(start(), name=f"pending_recall_start:{pending_id}")

    async def _retrieve(self, pending_id: str) -> None:
        row = await self.repository.get(pending_id)
        if row is None or row.get("status") != "queued":
            return
        now = time.time()
        deadline = float(row.get("retrieval_deadline_at") or 0)
        remaining = deadline - now
        if remaining <= 0:
            await self.repository.mark_failed(
                pending_id, reason="retrieval_deadline", now=now
            )
            return
        await self.repository.mark_retrieval_started(pending_id, now=now)
        config = normalize_memory_v3_config(_json_object(row.get("config_json")))
        try:
            candidates = await asyncio.wait_for(
                wide_chunk_recall(
                    str(row.get("intent_text") or ""),
                    top_k=config["pending_candidate_k"],
                    candidate_limit=config["pending_candidate_pool_limit"],
                    as_of_ts=float(row.get("created_at") or now),
                    exclude_message_id=str(row.get("origin_assistant_message_id") or ""),
                    relational_cards_enabled=config["relational_cards_enabled"],
                    full_corpus_enabled=config["pending_full_corpus_enabled"],
                    ai_note_lane_enabled=config["ai_note_lane_enabled"],
                ),
                timeout=max(remaining, 0.001),
            )
        except asyncio.TimeoutError:
            await self.repository.mark_failed(
                pending_id, reason="retrieval_timeout", now=time.time()
            )
            return
        except Exception as exc:
            await self.repository.mark_failed(
                pending_id,
                reason=f"retrieval_error:{exc.__class__.__name__}",
                now=time.time(),
            )
            return
        await self.repository.mark_ready(
            pending_id,
            candidates=candidates,
            now=time.time(),
        )

    async def _ensure_candidates(self, row: dict, *, join_max_wait: float) -> dict | None:
        if row.get("status") in {"ready", "selected"}:
            return row
        if row.get("status") != "queued":
            return None
        now = time.time()
        remaining = float(row.get("retrieval_deadline_at") or 0) - now
        if remaining <= 0:
            await self.repository.mark_failed(
                str(row["id"]), reason="retrieval_deadline", now=now
            )
            return None
        task = await self._task_for(str(row["id"]))
        wait_for = min(max(float(join_max_wait), 0.0), remaining)
        if wait_for > 0:
            try:
                await asyncio.wait_for(asyncio.shield(task), timeout=wait_for)
            except asyncio.TimeoutError:
                pass
        refreshed = await self.repository.get(str(row["id"]))
        return refreshed if refreshed and refreshed.get("status") in {"ready", "selected"} else None

    async def prepare_for_user(
        self,
        *,
        conv_id: str,
        user_message_id: str,
        current_user_message: str,
        recent_messages: Iterable[dict],
        config_snapshot: dict | None = None,
    ) -> dict:
        config = normalize_memory_v3_config(
            config_snapshot if config_snapshot is not None else load_memory_v3_config()
        )
        if not config["pending_recall_enabled"]:
            return {"status": "disabled", "items": []}
        row = await self.repository.active_for_conversation(conv_id)
        if row is None:
            return {"status": "none", "items": []}
        ready = await self._ensure_candidates(
            row,
            join_max_wait=config["pending_join_max_wait_sec"],
        )
        if ready is None:
            current = await self.repository.get(str(row["id"]))
            if current and current.get("status") == "queued":
                await self.repository.record_deferred(
                    str(row["id"]),
                    user_message_id=user_message_id,
                    now=time.time(),
                )
                return {"status": "deferred", "pending_id": row["id"], "items": []}
            return {
                "status": str((current or {}).get("status") or "unavailable"),
                "pending_id": row["id"],
                "items": [],
            }

        pending_id = str(ready["id"])
        lock = self._selection_locks.setdefault(pending_id, asyncio.Lock())
        async with lock:
            bound = await self.repository.bind_target(
                pending_id,
                user_message_id=user_message_id,
                now=time.time(),
            )
            if bound is None:
                return {"status": "claimed_by_other_turn", "pending_id": pending_id, "items": []}
            if bound.get("status") == "selected":
                selection = _json_object(bound.get("selected_json"))
                return {
                    "status": "selected",
                    "pending_id": pending_id,
                    "items": list(selection.get("items") or []),
                    "selection": selection,
                }

            candidates = _json_list(bound.get("candidate_json"))
            started = time.monotonic()
            validated: dict | None = None
            if not candidates or config["pending_select_max"] <= 0:
                validated = {
                    "decision": "none",
                    "selected_candidate_ids": [],
                    "needs_raw_detail_ids": [],
                    "reason_code": "no_matching_candidate",
                }
            else:
                selector_prompt = build_selector_prompt(
                    intent_text=str(bound.get("intent_text") or ""),
                    current_user_message=current_user_message,
                    recent_messages=recent_messages,
                    candidates=candidates,
                    select_max=config["pending_select_max"],
                )
                # The configured selector timeout is a user-visible *total*
                # budget, not a per-attempt budget.  A malformed first reply
                # may be retried, but two attempts must not silently turn an
                # eight-second ceiling into sixteen seconds.
                selector_deadline = (
                    time.monotonic() + config["pending_selector_timeout_sec"]
                )
                for _attempt in range(config["pending_selector_attempts"]):
                    remaining = selector_deadline - time.monotonic()
                    if remaining <= 0:
                        break
                    try:
                        raw = await asyncio.wait_for(
                            _call_flash_lite(
                                selector_prompt,
                                scope=SELECTOR_SCOPE,
                                timeout=remaining,
                            ),
                            timeout=remaining,
                        )
                    except asyncio.TimeoutError:
                        break
                    if raw is None:
                        continue
                    try:
                        validated = validate_selector_result(
                            raw,
                            candidate_ids={
                                str(item.get("candidate_id") or "") for item in candidates
                            },
                            select_max=config["pending_select_max"],
                        )
                        break
                    except ValueError:
                        continue
            if validated is None:
                await self.repository.mark_failed(
                    pending_id,
                    reason="selector_invalid_or_failed",
                    now=time.time(),
                )
                return {"status": "failed", "pending_id": pending_id, "items": []}
            items = selection_prompt_items(validated, candidates)
            selection = {
                **validated,
                "items": items,
                "selector_scope": SELECTOR_SCOPE,
                "selector_latency_ms": int((time.monotonic() - started) * 1000),
            }
            saved = await self.repository.save_selection(
                pending_id,
                user_message_id=user_message_id,
                selection=selection,
                now=time.time(),
            )
            if saved is None:
                return {"status": "selection_race_lost", "pending_id": pending_id, "items": []}
            persisted = _json_object(saved.get("selected_json"))
            return {
                "status": "selected",
                "pending_id": pending_id,
                "items": list(persisted.get("items") or []),
                "selection": persisted,
            }

    async def replay_for_assistant(self, assistant_message_id: str) -> dict:
        row = await self.repository.selected_for_consumed_assistant(assistant_message_id)
        if row is None:
            return {"status": "none", "items": []}
        selection = _json_object(row.get("selected_json"))
        return {
            "status": "replay",
            "pending_id": row["id"],
            "target_user_message_id": row.get("target_user_message_id"),
            "items": list(selection.get("items") or []),
            "selection": selection,
        }

    async def apply_after_assistant_in_tx(
        self,
        db,
        *,
        conv_id: str,
        assistant_message_id: str,
        created_at: float,
        current_user_message_id: str | None,
        selected_pending_id: str | None,
        recall_intent: str,
        allow_new_intent: bool,
        config_snapshot: dict | None,
    ) -> dict:
        consumed = False
        if selected_pending_id and current_user_message_id:
            consumed = await self.repository.consume_selected_in_tx(
                db,
                pending_id=selected_pending_id,
                target_user_message_id=current_user_message_id,
                assistant_message_id=assistant_message_id,
                now=created_at,
            )
        created_pending_id = None
        config = normalize_memory_v3_config(config_snapshot)
        if (
            allow_new_intent
            and config["pending_recall_enabled"]
            and str(recall_intent or "").strip()
        ):
            created_pending_id = await self.repository.create_queued_in_tx(
                db,
                conv_id=conv_id,
                origin_assistant_message_id=assistant_message_id,
                intent_text=str(recall_intent).strip(),
                retrieval_deadline_at=(
                    created_at + config["pending_retrieval_timeout_sec"]
                ),
                config=config,
                created_at=created_at,
            )
        return {
            "consumed": consumed,
            "created_pending_id": created_pending_id,
        }


pending_recall_service = PendingRecallService()


__all__ = [
    "PendingRecallService",
    "build_selector_prompt",
    "pending_recall_service",
    "selection_prompt_items",
    "validate_selector_result",
]
