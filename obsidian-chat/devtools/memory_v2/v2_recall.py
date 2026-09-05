"""
Deprecated Memory V2 recall planner.

This module is retained only for historical replay/comparison tests from the old
taxonomy-based V2 design. The primary chat path now uses
app.memory_v2.hybrid_recall, which searches raw chunks and notes without
namespace/kind gates.
"""

from __future__ import annotations

import json
import math
import time

import numpy as np

from app.memory_v2 import embedding
from app.memory_v2.diagnostics import build_recall_trace, infer_abstain_reason
from app.memory_v2.migrations import normalize_keywords
from app.memory_v2.taxonomy import (
    KIND_BASE_WEIGHT,
    RECALL_NAMESPACE_PRIORITY,
    TAXONOMY_SOURCE,
    classify_query,
    detect_emotion,
    detect_intents,
    detect_namespaces,
    emotion_valence,
    extract_terms,
    meaningful_keywords,
)
from app.memory_v2.v2_repository import MemoryRepository


DEPRECATED = True


def _safe_json(value: str | None, default):
    if not value:
        return default
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError:
        return default
    return parsed


def _norm(text: str) -> str:
    return (text or "").lower()


def analyze_turn(query_text: str, keywords: list[str] | None = None,
                 mode: str = "normal", namespace: str | None = None) -> dict:
    keywords = keywords or []
    clean_keywords = meaningful_keywords(keywords)
    current_emotion = detect_emotion(query_text, keywords)
    detected_namespaces = detect_namespaces(
        query_text,
        keywords,
        order=RECALL_NAMESPACE_PRIORITY,
    )
    detected_kinds = detect_intents(query_text, keywords)
    if current_emotion and "emotional" not in detected_kinds:
        detected_kinds.insert(0, "emotional")
    needs_memory = bool(
        detected_namespaces or detected_kinds or clean_keywords or current_emotion
    )
    if not needs_memory:
        detected_kinds = []
    elif not detected_kinds:
        detected_kinds = ["interaction", "semantic", "episode"]

    active_namespace = namespace or (detected_namespaces[0] if detected_namespaces else mode or "normal")
    return {
        "mode": mode or "normal",
        "namespace": active_namespace,
        "detected_namespaces": detected_namespaces,
        "preferred_kinds": detected_kinds,
        "needs_memory": needs_memory,
        "emotion": current_emotion if needs_memory else "",
        "terms": extract_terms(query_text, clean_keywords) if needs_memory else [],
    }


def allowed_namespaces(plan: dict) -> list[str]:
    mode = plan.get("mode") or "normal"
    namespace = plan.get("namespace") or "normal"
    detected = set(plan.get("detected_namespaces") or [])

    if mode == "intimate" or namespace == "intimate":
        allowed = {"intimate", "normal", "health"}
    else:
        allowed = {"normal", "work", "schedule"}
        for ns in ("device", "location", "health"):
            if ns in detected or namespace == ns:
                allowed.add(ns)
        if "intimate" in detected:
            allowed.add("intimate")
    if namespace and namespace != "normal":
        allowed.add(namespace)
    return [ns for ns in ("intimate", "health", "device", "location", "work", "schedule", "normal") if ns in allowed]


def _keyword_score(terms: list[str], item_keywords: list[str], content: str) -> tuple[float, list[str]]:
    if not terms:
        return 0.0, []
    content_lower = _norm(content)
    keywords_lower = [str(kw).lower() for kw in item_keywords]
    hits = []
    for term in terms:
        term_lower = term.lower()
        if not term_lower:
            continue
        if any(term_lower in kw or kw in term_lower for kw in keywords_lower if kw):
            hits.append(term)
            continue
        if term_lower in content_lower:
            hits.append(term)
    score = len(set(hits)) / max(len(terms), 1)
    return min(score, 1.0), list(dict.fromkeys(hits))


def _text_overlap_score(terms: list[str], content: str) -> float:
    if not terms:
        return 0.0
    content_lower = _norm(content)
    hits = sum(1 for term in terms if term.lower() in content_lower)
    return min(hits / max(len(terms), 1), 1.0)


def _recency_score(created_at: float | None) -> float:
    if not created_at:
        return 0.0
    days = max((time.time() - float(created_at)) / 86400, 0.0)
    return 1 / (1 + math.log1p(days))


def _cooldown_penalty(last_used_at: float | None) -> float:
    if not last_used_at:
        return 0.0
    age = time.time() - float(last_used_at)
    if age < 30 * 60:
        return 0.25
    if age < 2 * 60 * 60:
        return 0.12
    return 0.0


def _cosine_similarity(a: list[float], b: list[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(float(x) * float(y) for x, y in zip(a, b))
    norm_a = math.sqrt(sum(float(x) * float(x) for x in a))
    norm_b = math.sqrt(sum(float(y) * float(y) for y in b))
    if norm_a == 0 or norm_b == 0:
        return 0.0
    return max(min(dot / (norm_a * norm_b), 1.0), -1.0)


def _embedding_blob_to_vector(blob) -> list[float]:
    if not blob:
        return []
    try:
        if isinstance(blob, memoryview):
            blob = blob.tobytes()
        elif isinstance(blob, bytearray):
            blob = bytes(blob)
        elif not isinstance(blob, bytes):
            return []
        return embedding.unpack_embedding(blob)
    except Exception:
        return []


def _embedding_similarity(query_embedding: list[float] | None, item_embedding) -> float:
    if not query_embedding:
        return 0.0
    item_vector = _embedding_blob_to_vector(item_embedding)
    return _cosine_similarity(query_embedding, item_vector)


def _embedding_blob_to_array(blob):
    if not blob:
        return None
    try:
        if isinstance(blob, memoryview):
            blob = blob.tobytes()
        if not isinstance(blob, (bytes, bytearray)):
            return None
        vector = np.frombuffer(blob, dtype=np.float32)
    except (TypeError, ValueError):
        return None
    return vector if vector.size else None


def _batch_embedding_similarities(
    items: list[dict],
    query_embedding: list[float] | None,
) -> list[float]:
    if not items or not query_embedding:
        return [0.0 for _item in items]

    query = np.asarray(query_embedding, dtype=np.float32)
    query_norm = np.linalg.norm(query)
    if query.size == 0 or query_norm == 0:
        return [0.0 for _item in items]

    indexes = []
    vectors = []
    for index, item in enumerate(items):
        vector = _embedding_blob_to_array(item.get("embedding"))
        if vector is None or vector.size != query.size:
            continue
        indexes.append(index)
        vectors.append(vector)

    similarities = [0.0 for _item in items]
    if not vectors:
        return similarities

    matrix = np.vstack(vectors)
    row_norms = np.linalg.norm(matrix, axis=1)
    valid = row_norms > 0
    values = np.zeros(len(vectors), dtype=np.float32)
    values[valid] = (matrix[valid] @ query) / (row_norms[valid] * query_norm)
    values = np.clip(values, -1.0, 1.0)
    for index, value in zip(indexes, values):
        similarities[index] = float(value)
    return similarities


def _normalize_emotion(value: str | None) -> str:
    return str(value or "").strip().lower()


def _emotion_resonance(current: str | None, item_emotion: str | None) -> float:
    current = _normalize_emotion(current)
    item_emotion = _normalize_emotion(item_emotion)
    if not current or not item_emotion:
        return 0.0
    if current == item_emotion:
        return 1.0
    current_valence = emotion_valence(current)
    item_valence = emotion_valence(item_emotion)
    if current_valence and current_valence == item_valence:
        return 0.6
    if current_valence and item_valence:
        return 0.25
    return 0.0


def score_item(
    item: dict,
    plan: dict,
    query_embedding: list[float] | None = None,
    *,
    semantic_similarity: float | None = None,
) -> dict:
    terms = plan.get("terms") or []
    preferred_kinds = plan.get("preferred_kinds") or []
    item_keywords = _safe_json(item.get("keywords_json"), [])
    if not isinstance(item_keywords, list):
        item_keywords = normalize_keywords(item_keywords)
    metadata = _safe_json(item.get("metadata_json"), {})
    secondary = metadata.get("secondary_namespaces") if isinstance(metadata, dict) else []
    if not isinstance(secondary, list):
        secondary = []

    kw_score, hits = _keyword_score(terms, item_keywords, item.get("content") or "")
    text_score = _text_overlap_score(terms, item.get("content") or "")
    keyword_relevance = max(kw_score, text_score * 0.85)
    if semantic_similarity is None:
        semantic_similarity = _embedding_similarity(query_embedding, item.get("embedding"))
    relevance = max(keyword_relevance, semantic_similarity)

    kind = item.get("kind") or "episode"
    namespace = item.get("namespace") or "normal"
    kind_weight = KIND_BASE_WEIGHT.get(kind, 0.5)
    kind_bonus = 0.18 if kind in preferred_kinds else 0.0
    namespace_bonus = 0.08 if namespace == plan.get("namespace") else 0.0
    if any(ns in (plan.get("detected_namespaces") or []) for ns in secondary):
        namespace_bonus += 0.05

    importance = float(item.get("importance") or 0.5)
    confidence = float(item.get("confidence") or 0.7)
    recency = _recency_score(item.get("created_at"))
    cooldown = _cooldown_penalty(item.get("last_used_at"))
    item_emotion = _normalize_emotion(item.get("emotion"))
    if not item_emotion:
        item_emotion = detect_emotion(item.get("content") or "", item_keywords)
    emotion_resonance = _emotion_resonance(plan.get("emotion"), item_emotion)

    has_semantic_query = bool(query_embedding)
    semantic_weight = 0.35 if has_semantic_query else 0.0
    keyword_weight = 0.16 if has_semantic_query else 0.46
    kind_weight_factor = 0.12 if has_semantic_query else 0.14

    score = (
        semantic_similarity * semantic_weight
        + keyword_relevance * keyword_weight
        + kind_weight * kind_weight_factor
        + kind_bonus
        + namespace_bonus
        + importance * 0.10
        + confidence * 0.07
        + emotion_resonance * 0.08
        + recency * 0.05
        - cooldown
    )
    reasons = []
    if semantic_similarity >= 0.35 or semantic_similarity <= -0.2:
        reasons.append(f"semantic:{semantic_similarity:.3f}")
    if hits:
        reasons.append("keyword:" + ",".join(hits[:5]))
    if emotion_resonance:
        reasons.append(f"emotion:{plan.get('emotion')}->{item_emotion}")
    if kind in preferred_kinds:
        reasons.append(f"kind:{kind}")
    if namespace == plan.get("namespace"):
        reasons.append(f"namespace:{namespace}")
    if secondary:
        reasons.append("secondary:" + ",".join(secondary[:4]))
    if cooldown:
        reasons.append("cooldown")

    return {
        "id": item["id"],
        "legacy_memory_id": item.get("legacy_memory_id"),
        "content": item.get("content") or "",
        "kind": kind,
        "namespace": namespace,
        "secondary_namespaces": secondary,
        "score": round(max(score, 0.0), 4),
        "relevance": round(relevance, 4),
        "semantic_similarity": round(semantic_similarity, 4),
        "keyword_relevance": round(keyword_relevance, 4),
        "emotion": item_emotion,
        "emotion_resonance": round(emotion_resonance, 4),
        "importance": round(importance, 2),
        "confidence": round(confidence, 2),
        "created_at": item.get("created_at"),
        "reason": "; ".join(reasons) if reasons else "policy_candidate",
    }


class V2RecallPlanner:
    def __init__(self, repository: MemoryRepository | None = None):
        self.repository = repository or MemoryRepository()

    async def plan(
        self,
        query_text: str,
        keywords: list[str] | None = None,
        *,
        mode: str = "normal",
        namespace: str | None = None,
        top_k: int = 8,
        candidate_limit: int = 500,
        include_trace: bool = False,
    ) -> dict:
        turn_plan = analyze_turn(query_text, keywords, mode, namespace)
        namespaces = allowed_namespaces(turn_plan)
        if not turn_plan.get("needs_memory"):
            result = {
                "query": query_text,
                "keywords": keywords or [],
                "turn_plan": turn_plan,
                "allowed_namespaces": namespaces,
                "candidate_count": 0,
                "abstain_reason": "no_memory_signal",
                "semantic_query": False,
                "selected": [],
                "debug_top": [],
            }
            if include_trace:
                result["trace"] = build_recall_trace(
                    result,
                    classification=classify_query(query_text),
                    taxonomy_source=TAXONOMY_SOURCE,
                )
            return result
        candidates = await self.repository.fetch_items_for_recall(
            namespaces=namespaces,
            status="active",
            visibility="prompt",
            limit=candidate_limit,
        )
        query_embedding = None
        if candidates:
            try:
                query_embedding = await embedding.get_embedding(query_text)
            except Exception:
                query_embedding = None
        semantic_scores = _batch_embedding_similarities(candidates, query_embedding)
        scored = [
            score_item(
                item,
                turn_plan,
                query_embedding=query_embedding,
                semantic_similarity=semantic_scores[index],
            )
            for index, item in enumerate(candidates)
        ]
        scored.sort(key=lambda item: item["score"], reverse=True)
        selected = [item for item in scored if item["score"] > 0][:top_k]
        result = {
            "query": query_text,
            "keywords": keywords or [],
            "turn_plan": turn_plan,
            "allowed_namespaces": namespaces,
            "candidate_count": len(candidates),
            "semantic_query": bool(query_embedding),
            "selected": selected,
            "debug_top": scored[: max(top_k, 12)],
        }
        result["abstain_reason"] = infer_abstain_reason(result)
        if include_trace:
            result["trace"] = build_recall_trace(
                result,
                classification=classify_query(query_text),
                taxonomy_source=TAXONOMY_SOURCE,
            )
        return result
