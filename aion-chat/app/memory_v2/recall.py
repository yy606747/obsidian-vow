"""
记忆召回边界。

Batch 2.0 保持旧召回算法；后续 V2 recall planner 会先在这里落地。
"""

from __future__ import annotations

from memory import build_surfacing_memories as _legacy_build_surfacing_memories
from memory import fetch_source_details as _legacy_fetch_source_details
from memory import recall_memories as _legacy_recall_memories


async def recall_memories(query_text: str, query_keywords: list[str] = None,
                          top_k: int = 5, threshold: float = 0.45) -> tuple[list[dict], list[dict]]:
    return await _legacy_recall_memories(query_text, query_keywords, top_k, threshold)


async def fetch_source_details(memories: list[dict], keywords: list[str]) -> str:
    return await _legacy_fetch_source_details(memories, keywords)


async def build_surfacing_memories(topic: str = "", keywords: list[str] = None,
                                   max_unresolved: int = 3, max_topic: int = 3,
                                   max_total: int = 5) -> tuple[list[dict], set]:
    return await _legacy_build_surfacing_memories(
        topic=topic,
        keywords=keywords,
        max_total=max_total,
    )
