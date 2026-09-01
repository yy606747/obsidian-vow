"""
MemoryService：记忆系统对外入口。

第一阶段只统一边界，不改变旧记忆行为。
"""

from __future__ import annotations

from . import chunks, digest, diagnostics, embedding, hybrid_recall, prompt_block, recall, recall_config, repository
from .v2_repository import MemoryRepository


_PAST_REFERENCE_MARKERS = (
    "你之前",
    "你上次",
    "那次",
    "当时你",
    "我记得",
    "还记得",
    "以前你",
)


def _response_overlap_proxy(memory_text: str, response_text: str) -> float:
    memory_terms = {value.lower() for value in chunks.extract_keywords(memory_text, limit=32)}
    response_terms = {value.lower() for value in chunks.extract_keywords(response_text, limit=64)}
    if not memory_terms or not response_terms:
        return 0.0
    return round(len(memory_terms & response_terms) / len(memory_terms), 4)


class MemoryService:
    def __init__(self, v2_repository: MemoryRepository | None = None):
        self.v2_repository = v2_repository or MemoryRepository()

    async def get_embedding(self, text: str) -> list[float] | None:
        return await embedding.get_embedding(text)

    def pack_embedding(self, values: list[float]) -> bytes:
        return embedding.pack_embedding(values)

    def unpack_embedding(self, blob: bytes) -> list[float]:
        return embedding.unpack_embedding(blob)

    async def instant_digest(self, recent_messages: list[dict]) -> dict:
        return await digest.instant_digest(recent_messages)

    def local_instant_digest(self, recent_messages: list[dict]) -> dict:
        return digest.local_instant_digest(recent_messages)

    async def manual_digest(self) -> dict:
        result = await digest.manual_digest()
        if int(result.get("new_memories_count") or 0) > 0:
            hybrid_recall.invalidate_full_corpus_cache(notes=True)
        return result

    async def ensure_conversation_chunks(self, conv_id: str, **kwargs) -> dict:
        result = await chunks.ensure_conversation_chunks(conv_id, **kwargs)
        if any(
            int(result.get(key) or 0) > 0
            for key in (
                "inserted_chunks",
                "updated_chunks",
                "reactivated_chunks",
                "retired_chunks",
                "embedding_success",
                "cards_invalidated",
            )
        ):
            hybrid_recall.invalidate_full_corpus_cache(chunk_conv_id=conv_id)
        return result

    async def generate_stable_relational_cards(
        self,
        *,
        config_snapshot: dict | None = None,
    ) -> dict:
        from app.memory_v3.card_generation import generate_stable_relational_cards

        result = await generate_stable_relational_cards(config_snapshot=config_snapshot)
        if int(result.get("created") or 0) > 0:
            hybrid_recall.invalidate_full_corpus_cache(chunks=True)
        return result

    async def embed_pending_chunks(self, **kwargs) -> dict:
        result = await chunks.embed_pending_chunks(**kwargs)
        if int(result.get("embedded") or 0) > 0:
            hybrid_recall.invalidate_full_corpus_cache(chunks=True)
        return result

    def load_digest_anchor(self) -> float:
        return digest.load_digest_anchor()

    def save_digest_anchor(self, ts: float):
        digest.save_digest_anchor(ts)

    async def recall_memories(self, query_text: str, query_keywords: list[str] = None,
                              top_k: int = 5, threshold: float = 0.45) -> tuple[list[dict], list[dict]]:
        return await recall.recall_memories(query_text, query_keywords, top_k, threshold)

    async def fetch_source_details(self, memories: list[dict], keywords: list[str]) -> str:
        return await recall.fetch_source_details(memories, keywords)

    async def build_surfacing_memories(self, topic: str = "", keywords: list[str] = None,
                                       max_unresolved: int = 3, max_topic: int = 3,
                                       max_total: int = 5) -> tuple[list[dict], set]:
        return await recall.build_surfacing_memories(
            topic, keywords, max_unresolved, max_topic, max_total
        )

    async def list_memories(self) -> list[dict]:
        return await repository.list_memories()

    async def list_memories_page(self, **kwargs) -> dict:
        return await repository.list_memories_page(**kwargs)

    async def create_memory(self, content: str, memory_type: str = "event", **kwargs) -> dict:
        result = await repository.create_memory(content, memory_type, **kwargs)
        hybrid_recall.invalidate_full_corpus_cache(notes=True)
        return result

    async def prepare_ai_note_embedding(self, content: str) -> bytes | None:
        values = await embedding.get_document_embedding(content)
        return embedding.pack_embedding(values) if values else None

    async def create_working_model_ai_note_in_tx(self, db, **kwargs) -> dict:
        # The caller owns this transaction and invalidates only after commit.
        return await repository.create_working_model_ai_note_in_tx(db, **kwargs)

    async def update_memory(self, mem_id: str, content: str, **kwargs) -> dict:
        result = await repository.update_memory(mem_id, content, **kwargs)
        hybrid_recall.invalidate_full_corpus_cache(notes=True)
        return result

    async def delete_memory(self, mem_id: str) -> dict:
        result = await repository.delete_memory(mem_id)
        hybrid_recall.invalidate_full_corpus_cache(notes=True)
        return result

    async def toggle_unresolved(self, mem_id: str) -> dict:
        result = await repository.toggle_unresolved(mem_id)
        if result.get("ok"):
            hybrid_recall.invalidate_full_corpus_cache(notes=True)
        return result

    async def get_memory_source(self, mem_id: str) -> dict:
        return await repository.get_memory_source(mem_id)

    def get_debug_trace(self) -> dict:
        return diagnostics.empty_trace()

    async def memory_v2_migration_status(self) -> dict:
        from . import migrations
        return await migrations.migration_status()

    async def preview_legacy_migration(self, limit: int | None = None) -> dict:
        from . import migrations
        return await migrations.migrate_legacy_memories(apply=False, limit=limit)

    async def migrate_legacy_memories(self, limit: int | None = None) -> dict:
        from . import migrations
        result = await migrations.migrate_legacy_memories(apply=True, limit=limit)
        if int(result.get("inserted") or 0) > 0:
            hybrid_recall.invalidate_full_corpus_cache(notes=True)
        return result

    async def list_v2_items(self, **filters) -> list[dict]:
        return await self.v2_repository.list_items(**filters)

    async def plan_v2_recall(self, query_text: str, keywords: list[str] = None, **kwargs) -> dict:
        return await hybrid_recall.hybrid_recall(query_text, keywords, **kwargs)

    async def plan_v2_recall_for_reflection(self, query_text: str) -> dict:
        """Use the production hybrid planner for one fixed top-5 reflection read.

        This is intentionally not a fallback chain: query failure, planner
        failure, and a successful zero-hit result remain distinguishable to the
        reflection audit log.
        """

        from app.memory_v3.config import load_memory_v3_config

        config = recall_config.load_recall_config()
        runtime = recall_config.recall_runtime(config)
        if not runtime["v2_enabled"] or not str(query_text or "").strip():
            return {"selected": []}
        memory_v3 = load_memory_v3_config()
        return await self.plan_v2_recall(
            query_text,
            [],
            top_k=5,
            candidate_limit=runtime["candidate_limit"],
            full_corpus=True,
            relational_cards_enabled=memory_v3["relational_cards_enabled"],
            card_readout_mode=memory_v3["card_readout_mode"],
            ai_note_lane_enabled=memory_v3["ai_note_lane_enabled"],
            ai_note_top_k=memory_v3["ai_note_top_k"],
            ai_note_max_items=memory_v3["ai_note_max_items"],
        )

    def build_v2_prompt_block(self, plan_result: dict | None, **kwargs) -> dict:
        return prompt_block.build_v2_memory_prompt_block(plan_result, **kwargs)

    async def record_v2_prompt_usage(
        self,
        recall_debug: dict | None,
        *,
        conv_id: str | None = None,
        request_id: str | None = None,
        response_text: str = "",
    ) -> dict:
        runtime = (recall_debug or {}).get("runtime") or {}
        block = (recall_debug or {}).get("prompt_block") or {}
        decision = (recall_debug or {}).get("prompt_decision") or {}
        if not runtime.get("v2_enabled"):
            return {"status": "skipped", "reason": "v2_disabled", "count": 0}
        if not block.get("enabled"):
            return {
                "status": "skipped",
                "reason": block.get("skipped_reason") or "prompt_block_empty",
                "count": 0,
            }
        items = [item for item in (block.get("items") or []) if item.get("id")]
        if not items:
            return {"status": "skipped", "reason": "no_usage_items", "count": 0}

        injected = bool(decision.get("inject"))
        usage_type = "injected" if injected else "preview_only"
        mode = runtime.get("mode") or decision.get("mode") or "unknown"
        decision_reason = decision.get("reason") or ""
        reason = ":".join(part for part in (usage_type, mode, decision_reason) if part)
        records = []
        for rank, item in enumerate(items, 1):
            usage = await self.v2_repository.record_usage(
                memory_id=item["id"],
                conv_id=conv_id,
                request_id=request_id,
                reason=reason,
                score=item.get("score"),
                rank=rank,
                touch_last_used=injected,
            )
            records.append({
                "id": usage["id"],
                "memory_id": usage["memory_id"],
                "score": usage["score"],
                "rank": usage["rank"],
            })
        event_records: list[str] = []
        event_errors = 0
        memory_v3 = (recall_debug or {}).get("memory_v3") or {}
        v3_logging_enabled = bool(
            memory_v3.get("relational_cards_enabled")
            or memory_v3.get("ai_note_lane_enabled")
            or memory_v3.get("pending_recall_enabled")
        )
        if v3_logging_enabled and conv_id:
            from app.memory_v3.repository import InjectionEventRepository

            event_repository = InjectionEventRepository()
            past_reference = int(
                any(marker in str(response_text or "") for marker in _PAST_REFERENCE_MARKERS)
            )
            for rank, item in enumerate(items, 1):
                source_type = str(item.get("source_type") or "")
                is_chunk = source_type == "chunk" or item.get("readout_type") in {
                    "raw",
                    "raw_full",
                    "relational_card",
                }
                try:
                    event_id = await event_repository.record(
                        {
                            "request_id": request_id,
                            "conv_id": conv_id,
                            "assistant_message_id": request_id,
                            "route": (
                                "ai_note"
                                if item.get("lane") == "ai_note"
                                else "pending"
                                if item.get("lane") == "pending"
                                else "ordinary"
                            ),
                            "candidate_id": item.get("candidate_id") or item.get("id"),
                            "source_chunk_id": item.get("id") if is_chunk else None,
                            "memory_item_id": None if is_chunk else item.get("id"),
                            "card_id": item.get("card_id"),
                            "card_version": item.get("card_version"),
                            "score": item.get("score"),
                            "rank": rank,
                            "cooldown_penalty": item.get("cooldown_penalty"),
                            "outcome": usage_type,
                            "reason": reason,
                            "rendered_chars": len(str(item.get("preview") or "")),
                            "response_overlap_proxy": _response_overlap_proxy(
                                str(item.get("preview") or ""), response_text
                            ),
                            "past_reference_proxy": past_reference,
                            "metadata": {
                                "readout_type": item.get("readout_type"),
                                "source_type": source_type,
                            },
                        }
                    )
                    event_records.append(event_id)
                except Exception:
                    event_errors += 1
        return {
            "status": "recorded",
            "usage_type": usage_type,
            "reason": reason,
            "count": len(records),
            "touch_last_used": injected,
            "records": records,
            "injection_event_count": len(event_records),
            "injection_event_errors": event_errors,
        }

    async def trace_v2_recall(self, query_text: str, keywords: list[str] = None, **kwargs) -> dict:
        plan = await self.plan_v2_recall(
            query_text,
            keywords,
            **kwargs,
        )
        return diagnostics.build_recall_trace(
            plan,
            classification={
                "query_type": "hybrid",
                "needs_memory": bool(query_text),
                "keywords": keywords or [],
            },
            taxonomy_source="hybrid_chunk_note",
        )

    def get_v2_recall_config(self) -> dict:
        return recall_config.describe_recall_config()

    def update_v2_recall_config(self, updates: dict) -> dict:
        config = recall_config.save_recall_config(updates)
        return {
            "config": config,
            "runtime": recall_config.recall_runtime(config),
            "valid_modes": list(recall_config.VALID_MODES),
        }

    def get_memory_v3_config(self) -> dict:
        from app.memory_v3.config import load_memory_v3_config

        return load_memory_v3_config()

    def update_memory_v3_config(self, updates: dict) -> dict:
        from app.memory_v3.config import save_memory_v3_config
        from app.memory_v3.timeline import timeline_service

        config = save_memory_v3_config(updates)
        # Enabling can warm the timeline immediately; disabling cancels any
        # sleeping/in-flight refresh so rollback also stops paid background work.
        timeline_service.start_background_refresh(config)
        return config

    async def invalidate_relational_card(self, card_id: str, *, reason: str) -> dict:
        from app.memory_v3.repository import RelationalCardRepository

        result = await RelationalCardRepository().invalidate_active(
            card_id,
            reason=reason,
            actor="owner_api",
        )
        if result.get("ok"):
            hybrid_recall.invalidate_full_corpus_cache(chunks=True)
        return result

    async def plan_v2_recall_for_chat(
        self,
        query_text: str,
        keywords: list[str] = None,
        prompt_seed: str = "",
        pending_items: list[dict] | None = None,
        visible_message_ids: list[str] | None = None,
        user_name: str | None = None,
        **kwargs,
    ) -> dict:
        from app.memory_v3.config import load_memory_v3_config

        config = recall_config.load_recall_config()
        runtime = recall_config.recall_runtime(config)
        memory_v3 = load_memory_v3_config()
        rollout_decision = recall_config.prompt_injection_decision(
            config,
            seed=prompt_seed or query_text,
        )
        result = {
            "runtime": runtime,
            "summary": None,
            "trace": None,
            "prompt_block": None,
            "rollout_decision": rollout_decision,
            "prompt_decision": dict(rollout_decision),
            "memory_v3": memory_v3,
        }
        if not runtime["v2_enabled"] or not query_text:
            return result
        plan = await hybrid_recall.hybrid_recall(
            query_text,
            keywords,
            top_k=runtime["top_k"],
            candidate_limit=runtime["candidate_limit"],
            full_corpus=True,
            slot_min_score=runtime["prompt_min_score"],
            visible_message_ids=visible_message_ids,
            relational_cards_enabled=memory_v3["relational_cards_enabled"],
            card_readout_mode=memory_v3["card_readout_mode"],
            ai_note_lane_enabled=memory_v3["ai_note_lane_enabled"],
            ai_note_top_k=memory_v3["ai_note_top_k"],
            ai_note_max_items=memory_v3["ai_note_max_items"],
        )
        plan = hybrid_recall.merge_pending_items(
            plan,
            pending_items,
            top_k=runtime["top_k"],
        )
        result["summary"] = diagnostics.summarize_recall_plan(plan)
        if runtime["include_trace"]:
            result["trace"] = plan.get("trace") or diagnostics.build_recall_trace(
                plan,
                classification={
                    "query_type": "hybrid",
                    "needs_memory": bool(query_text),
                    "keywords": keywords or [],
                },
                taxonomy_source="hybrid_chunk_note",
            )
        if runtime["prompt_block_enabled"]:
            block = prompt_block.build_v2_memory_prompt_block(
                plan,
                min_score=runtime["prompt_min_score"],
                user_name=user_name,
            )
            result["prompt_block"] = block
            if not block.get("enabled"):
                result["prompt_decision"] = {
                    **result["prompt_decision"],
                    "inject": False,
                    "reason": block.get("skipped_reason") or "prompt_block_empty",
                }
        return result


memory_service = MemoryService()
