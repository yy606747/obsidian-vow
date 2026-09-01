"""Memory prompt assembly for legacy recall and V2 takeover modes."""

from __future__ import annotations

from app.memory_v2 import memory_service
from app.working_model import service as working_model_service
from app.working_model.runtime import working_model_v2_injection_enabled
from config import load_working_model

from .memory_context import (
    _inject_v2_prompt_block_if_enabled,
    _plan_v2_recall_debug,
    _v2_owns_memory_prompt,
)
from .prompt_builder import (
    build_background_memory_block,
    build_current_time_block,
    build_desire_block,
    build_related_memory_block,
    build_v2_working_model_block,
    build_working_model_block,
    insert_prompt_ack,
)


def _last_user_content(history: list[dict]) -> str:
    for message in reversed(history):
        content = str(message.get("content") or "")
        if message.get("role") == "user" and not content.startswith("["):
            return content[:200]
    return ""


def _build_recall_query(
    actual_recent: list[dict],
    *,
    current_user_content: str,
    topic: str,
    recall_keywords: list[str],
    history: list[dict],
) -> str:
    fallback_user = current_user_content[:500] if current_user_content else _last_user_content(history)
    parts = [fallback_user.strip()]
    if topic:
        parts.append(str(topic).strip()[:240])
    if recall_keywords:
        parts.append(" ".join(str(keyword) for keyword in recall_keywords if str(keyword).strip()))

    recent_lines = []
    for message in actual_recent[-8:]:
        role = message.get("role")
        content = str(message.get("content") or "").strip()
        if not content or content.startswith("["):
            continue
        limit = 220 if role == "user" else 90
        recent_lines.append(f"{role}: {content[:limit]}")
    if recent_lines:
        parts.append("\n".join(recent_lines))
    return "\n".join(part for part in parts if part).strip()


def _memory_debug_payload(recalled: list[dict], debug_top6: list[dict]) -> tuple[list[dict], list[dict]]:
    debug_recalled = [
        {
            "content": memory["content"],
            "type": memory["type"],
            "score": memory["score"],
            "vec_sim": memory.get("vec_sim"),
            "kw_score": memory.get("kw_score"),
            "importance": memory.get("importance"),
        }
        for memory in recalled
    ] if recalled else []
    debug_top6_data = [
        {
            "content": memory["content"][:100],
            "score": memory["score"],
            "vec_sim": memory.get("vec_sim"),
            "kw_score": memory.get("kw_score"),
            "importance": memory.get("importance"),
        }
        for memory in debug_top6
    ] if debug_top6 else []
    return debug_recalled, debug_top6_data


async def inject_working_model_prompt(
    history: list[dict],
    *,
    cap_idx: int,
    inject_offset: int,
) -> int:
    """Keep the relationship model in the reusable prefix until it changes."""

    if not working_model_v2_injection_enabled():
        wm_block = build_working_model_block(load_working_model())
        if not wm_block:
            return inject_offset
        return insert_prompt_ack(
            history,
            cap_idx=cap_idx,
            inject_offset=inject_offset,
            content=wm_block,
            ack="（嗯，这是我此刻对她的认识。）",
        )

    working_model_head, desire_head = await working_model_service.load_v2_prompt_heads()
    wm_block = build_v2_working_model_block(working_model_head)
    desire_block = build_desire_block(desire_head)
    if wm_block:
        inject_offset = insert_prompt_ack(
            history,
            cap_idx=cap_idx,
            inject_offset=inject_offset,
            content=wm_block,
            ack="（嗯，这是我此刻对她的认识。）",
        )
    if desire_block:
        inject_offset = insert_prompt_ack(
            history,
            cap_idx=cap_idx,
            inject_offset=inject_offset,
            content=desire_block,
            ack="（嗯，这是我此刻想带进这段关系里的姿态。）",
        )
    return inject_offset


async def inject_memory_prompt(
    history: list[dict],
    *,
    conv_id: str,
    cap_idx: int,
    inject_offset: int,
    actual_recent: list[dict],
    fast_mode: bool,
    whisper_mode: bool,
    ai_dom_mode: bool,
    prompt_source: str,
    user_name: str = "她",
    current_user_content: str = "",
    pending_items: list[dict] | None = None,
    visible_message_ids: list[str] | None = None,
    include_working_model: bool = True,
) -> tuple[int, dict]:
    recall_keywords_str = ""
    recalled: list[dict] = []
    detail_text = ""
    topic = ""
    recall_query = ""
    debug_top6: list[dict] = []
    memory_v2_recall_debug = None

    if fast_mode:
        inject_offset = insert_prompt_ack(
            history,
            cap_idx=cap_idx,
            inject_offset=inject_offset,
            content=build_current_time_block(),
            ack="（嗯，我知道现在是什么时候。）",
        )
        return inject_offset, {
            "recall_keywords": recall_keywords_str,
            "recall_query": recall_query,
            "recall_topic": topic,
            # Compatibility-only debug field; V2 recall is no longer router-gated.
            "is_search_needed": False,
            "recalled_memories": [],
            "debug_top6": [],
            "memory_v2_recall": memory_v2_recall_debug,
        }

    digest_result = memory_service.local_instant_digest(actual_recent)
    recall_keywords = digest_result.get("keywords", [])
    recall_keywords_str = "、".join(recall_keywords) if recall_keywords else ""
    topic = digest_result.get("topic", "")

    if include_working_model:
        inject_offset = await inject_working_model_prompt(
            history,
            cap_idx=cap_idx,
            inject_offset=inject_offset,
        )

    recall_query = _build_recall_query(
        actual_recent,
        current_user_content=current_user_content,
        topic=topic,
        recall_keywords=recall_keywords,
        history=history,
    )

    if recall_query:
        plan_kwargs = {
            "whisper_mode": whisper_mode,
            "ai_dom_mode": ai_dom_mode,
            "prompt_seed": f"{conv_id}:{prompt_source}:{recall_query}",
        }
        # Preserve the legacy call shape when pending recall is disabled.  Apart
        # from keeping old adapters compatible, this makes the default-off path
        # observably identical instead of merely producing the same prompt.
        if pending_items:
            plan_kwargs["pending_items"] = pending_items
        if visible_message_ids is not None:
            plan_kwargs["visible_message_ids"] = visible_message_ids
        memory_v2_recall_debug = await _plan_v2_recall_debug(
            recall_query,
            recall_keywords,
            **plan_kwargs,
        )

    v2_owns_memory_prompt = _v2_owns_memory_prompt(memory_v2_recall_debug)
    surfaced_ids = set()
    if not v2_owns_memory_prompt:
        surfaced, surfaced_ids = await memory_service.build_surfacing_memories(topic, recall_keywords)
        inject_offset = insert_prompt_ack(
            history,
            cap_idx=cap_idx,
            inject_offset=inject_offset,
            content=build_background_memory_block(surfaced),
            ack="（这些也在我心里，不一定要说出来。）",
        )

    if recall_query and not v2_owns_memory_prompt:
        _, debug_top6 = await memory_service.recall_memories(recall_query, query_keywords=recall_keywords)

    if recall_query and not v2_owns_memory_prompt:
        recalled = [
            memory
            for memory in debug_top6
            if memory["score"] >= 0.45 and memory["id"] not in surfaced_ids
        ][:5]

    if recalled:
        inject_offset = insert_prompt_ack(
            history,
            cap_idx=cap_idx,
            inject_offset=inject_offset,
            content=build_related_memory_block(recalled, detail_text),
            ack="（这些事我记得；和眼前有关时，自然会想起来。）",
        )

    inject_offset = _inject_v2_prompt_block_if_enabled(
        history,
        cap_idx=cap_idx,
        inject_offset=inject_offset,
        memory_v2_recall_debug=memory_v2_recall_debug,
    )

    debug_recalled, debug_top6_data = _memory_debug_payload(recalled, debug_top6)
    return inject_offset, {
        "recall_keywords": recall_keywords_str,
        "recall_query": recall_query,
        "recall_topic": topic,
        # Compatibility-only debug field; V2 recall is no longer router-gated.
        "is_search_needed": False,
        "recalled_memories": debug_recalled,
        "debug_top6": debug_top6_data,
        "memory_v2_recall": memory_v2_recall_debug,
    }
