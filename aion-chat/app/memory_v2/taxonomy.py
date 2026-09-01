"""
Shared taxonomy rules for Memory V2.

The classifier terms live in JSON config so project-specific private language
does not have to be embedded in Python source. Runtime loads the private config
from data first, then falls back to the example config shipped with the repo.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import re
from typing import Any


TAXONOMY_ENV = "AION_MEMORY_TAXONOMY_PATH"
PRIVATE_TAXONOMY_PATH = Path(__file__).resolve().parents[2] / "data" / "memory_taxonomy.private.json"
EXAMPLE_TAXONOMY_PATH = Path(__file__).with_name("memory_taxonomy.example.json")


def _as_tuple(value: Any) -> tuple[str, ...]:
    if not isinstance(value, list):
        return ()
    return tuple(str(item).strip() for item in value if str(item).strip())


def _load_json(path: Path) -> dict:
    with path.open("r", encoding="utf-8") as handle:
        data = json.load(handle)
    if not isinstance(data, dict):
        raise ValueError(f"taxonomy config must be a JSON object: {path}")
    return data


def _resolve_taxonomy_path() -> Path:
    env_path = os.environ.get(TAXONOMY_ENV)
    if env_path:
        return Path(env_path).expanduser()
    if PRIVATE_TAXONOMY_PATH.exists():
        return PRIVATE_TAXONOMY_PATH
    return EXAMPLE_TAXONOMY_PATH


def load_taxonomy_config(path: str | os.PathLike[str] | None = None) -> dict:
    config_path = Path(path).expanduser() if path else _resolve_taxonomy_path()
    config = _load_json(config_path)
    config["_source_path"] = str(config_path)
    return config


TAXONOMY_CONFIG = load_taxonomy_config()
TAXONOMY_SOURCE = TAXONOMY_CONFIG.get("_source_path", "")

LOW_SIGNAL_TERMS = frozenset(_as_tuple(TAXONOMY_CONFIG.get("low_signal_terms")))

NAMESPACE_PRIORITY = _as_tuple(TAXONOMY_CONFIG.get("namespace_priority"))
QUERY_TYPE_PRIORITY = _as_tuple(TAXONOMY_CONFIG.get("query_type_priority"))
RECALL_NAMESPACE_PRIORITY = _as_tuple(TAXONOMY_CONFIG.get("recall_namespace_priority"))

NAMESPACES = TAXONOMY_CONFIG.get("namespaces") if isinstance(TAXONOMY_CONFIG.get("namespaces"), dict) else {}
INTENTS = TAXONOMY_CONFIG.get("intents") if isinstance(TAXONOMY_CONFIG.get("intents"), dict) else {}


def _namespace_terms(namespace: str, key: str = "hints") -> tuple[str, ...]:
    spec = NAMESPACES.get(namespace)
    if not isinstance(spec, dict):
        return ()
    return _as_tuple(spec.get(key))


def _intent_terms(kind: str, key: str = "hints") -> tuple[str, ...]:
    spec = INTENTS.get(kind)
    if not isinstance(spec, dict):
        return ()
    return _as_tuple(spec.get(key))


INTIMATE_HINTS = _namespace_terms("intimate")
DEVICE_HINTS = _namespace_terms("device")
DEVICE_WEAK_HINTS = _namespace_terms("device", "weak_hints")
DEVICE_CONTEXT_HINTS = _namespace_terms("device", "context_hints")
LOCATION_HINTS = _namespace_terms("location")
SCHEDULE_HINTS = _namespace_terms("schedule")
SCHEDULE_WEAK_HINTS = _namespace_terms("schedule", "weak_hints")
SCHEDULE_TIME_HINTS = _namespace_terms("schedule", "time_hints")
HEALTH_HINTS = _namespace_terms("health")
WORK_HINTS = _namespace_terms("work")

NAMESPACE_HINTS = {
    "intimate": INTIMATE_HINTS,
    "device": DEVICE_HINTS,
    "location": LOCATION_HINTS,
    "schedule": SCHEDULE_HINTS,
    "health": HEALTH_HINTS,
    "work": WORK_HINTS,
}

OPEN_LOOP_STRONG_HINTS = _intent_terms("open_loop", "strong_hints")
OPEN_LOOP_ACTION_HINTS = _intent_terms("open_loop", "action_hints")
OPEN_LOOP_DEVICE_HINTS = _intent_terms("open_loop", "device_hints")
INTERACTION_HINTS = _intent_terms("interaction")
EMOTIONAL_HINTS = _intent_terms("emotional")
EPISODE_STRICT_HINTS = _intent_terms("episode", "strict_hints")
INTENT_HINTS = {
    "episode": _intent_terms("episode"),
    "open_loop": OPEN_LOOP_STRONG_HINTS,
    "interaction": INTERACTION_HINTS,
    "semantic": _intent_terms("semantic"),
    "emotional": EMOTIONAL_HINTS,
}

DEFAULT_EMOTION_HINTS = {
    "anxious": (
        "焦虑", "压力", "担心", "不安", "紧张", "害怕", "慌", "心里没底",
        "睡不着", "失眠",
    ),
    "sad": (
        "难受", "低落", "委屈", "崩溃", "难过", "想哭", "沮丧", "失落",
        "心情不好", "不开心",
    ),
    "angry": (
        "生气", "烦", "不爽", "恼火", "愤怒", "吵架", "矛盾", "闹矛盾",
    ),
    "positive": (
        "开心", "高兴", "安心", "期待", "舒服", "快乐", "兴奋", "满足",
    ),
    "shame": (
        "羞耻", "愧疚", "自责", "尴尬", "自我评价",
    ),
    "attachment": (
        "依赖", "舍不得", "黏",
    ),
}

DEFAULT_EMOTION_VALENCE = {
    "anxious": "negative",
    "sad": "negative",
    "angry": "negative",
    "shame": "negative",
    "positive": "positive",
    "attachment": "mixed",
}


def _emotion_spec() -> tuple[dict[str, tuple[str, ...]], dict[str, str]]:
    configured = TAXONOMY_CONFIG.get("emotions")
    if not isinstance(configured, dict):
        return dict(DEFAULT_EMOTION_HINTS), dict(DEFAULT_EMOTION_VALENCE)

    hints: dict[str, tuple[str, ...]] = {}
    valence: dict[str, str] = {}
    for raw_label, spec in configured.items():
        label = str(raw_label).strip().lower()
        if not label:
            continue
        if isinstance(spec, dict):
            label_hints = _as_tuple(spec.get("hints"))
            label_valence = str(spec.get("valence") or "").strip().lower()
        else:
            label_hints = _as_tuple(spec)
            label_valence = ""
        if label_hints:
            hints[label] = label_hints
        if label_valence:
            valence[label] = label_valence
    if not hints:
        hints = dict(DEFAULT_EMOTION_HINTS)
    for label, default_valence in DEFAULT_EMOTION_VALENCE.items():
        valence.setdefault(label, default_valence)
    return hints, valence


EMOTION_HINTS, EMOTION_VALENCE = _emotion_spec()

KIND_BASE_WEIGHT = {
    str(kind): float(weight)
    for kind, weight in (TAXONOMY_CONFIG.get("kind_base_weight") or {}).items()
}

PROTECTED_NAMESPACES = set(_as_tuple(TAXONOMY_CONFIG.get("protected_namespaces")))
PROTECTED_CONTENT_HINTS = tuple(
    dict.fromkeys(
        hint
        for namespace in PROTECTED_NAMESPACES
        for hint in _namespace_terms(namespace)
    )
)


def norm(text: str) -> str:
    return (text or "").lower()


def build_haystack(content: str, keywords: list[str] | tuple[str, ...] | None = None) -> str:
    return f"{content or ''} {' '.join(str(kw) for kw in (keywords or []))}".lower()


def contains_any(text: str, hints: tuple[str, ...]) -> bool:
    text_lower = norm(text)
    return any(hint.lower() in text_lower for hint in hints)


def matched_hints(text: str, hints: tuple[str, ...]) -> list[str]:
    text_lower = norm(text)
    return [hint for hint in hints if hint.lower() in text_lower]


def detect_emotion(content: str, keywords: list[str] | tuple[str, ...] | None = None) -> str:
    haystack = build_haystack(content, keywords)
    best_label = ""
    best_hits: list[str] = []
    for label, hints in EMOTION_HINTS.items():
        hits = matched_hints(haystack, hints)
        if len(hits) > len(best_hits):
            best_label = label
            best_hits = hits
    return best_label


def emotion_valence(label: str | None) -> str:
    return EMOTION_VALENCE.get(norm(label), "")


def meaningful_keywords(keywords: list[str] | None) -> list[str]:
    out = []
    for keyword in keywords or []:
        value = str(keyword).strip()
        if value and value.lower() not in LOW_SIGNAL_TERMS:
            out.append(value)
    return out


def extract_terms(text: str, keywords: list[str] | None) -> list[str]:
    clean_keywords = meaningful_keywords(keywords)
    terms = []
    for keyword in clean_keywords:
        value = keyword.lower()
        if value not in terms:
            terms.append(value)

    text_lower = norm(text)
    for token in re.findall(r"[a-zA-Z0-9_+#.-]{2,}", text_lower):
        if token not in LOW_SIGNAL_TERMS and token not in terms:
            terms.append(token)
    if clean_keywords:
        return terms[:24]

    for token in re.findall(r"[\u4e00-\u9fff]{2,}", text):
        if token not in terms:
            terms.append(token)
    return terms[:24]


def has_device_signal(text: str) -> bool:
    if contains_any(text, DEVICE_HINTS):
        return True
    return contains_any(text, DEVICE_WEAK_HINTS) and contains_any(text, DEVICE_CONTEXT_HINTS)


def has_schedule_signal(text: str) -> bool:
    if contains_any(text, SCHEDULE_HINTS):
        return True
    return contains_any(text, SCHEDULE_WEAK_HINTS) and contains_any(text, SCHEDULE_TIME_HINTS)


def namespace_hits(content: str, keywords: list[str] | None = None) -> dict[str, list[str]]:
    haystack = build_haystack(content, keywords)
    hits: dict[str, list[str]] = {}
    for namespace, hint_group in (
        ("intimate", INTIMATE_HINTS),
        ("health", HEALTH_HINTS),
        ("location", LOCATION_HINTS),
        ("work", WORK_HINTS),
    ):
        matched = matched_hints(haystack, hint_group)
        if matched:
            hits[namespace] = matched

    device_matched = matched_hints(haystack, DEVICE_HINTS)
    weak_device_matched = matched_hints(haystack, DEVICE_WEAK_HINTS)
    if device_matched or (
        weak_device_matched and contains_any(haystack, DEVICE_CONTEXT_HINTS)
    ):
        hits["device"] = device_matched or weak_device_matched

    schedule_matched = matched_hints(haystack, SCHEDULE_HINTS)
    weak_schedule_matched = matched_hints(haystack, SCHEDULE_WEAK_HINTS)
    if schedule_matched or (
        weak_schedule_matched and contains_any(haystack, SCHEDULE_TIME_HINTS)
    ):
        hits["schedule"] = schedule_matched or weak_schedule_matched
    return hits


def ordered_namespaces(hits: dict[str, list[str]], order: tuple[str, ...]) -> list[str]:
    return [namespace for namespace in order if namespace in hits]


def detect_namespaces(
    content: str,
    keywords: list[str] | None = None,
    *,
    order: tuple[str, ...] = RECALL_NAMESPACE_PRIORITY,
) -> list[str]:
    return ordered_namespaces(namespace_hits(content, keywords), order)


def has_open_loop_signal(content: str, keywords: list[str] | None = None) -> bool:
    haystack = build_haystack(content, keywords)
    if contains_any(haystack, OPEN_LOOP_STRONG_HINTS):
        return True
    if has_schedule_signal(haystack):
        if contains_any(haystack, OPEN_LOOP_ACTION_HINTS):
            return True
        if re.search(r"\d{1,2}\s*[点:：]", haystack):
            return True
    if has_device_signal(haystack) and contains_any(
        haystack,
        OPEN_LOOP_DEVICE_HINTS,
    ):
        return True
    return False


def detect_intents(content: str, keywords: list[str] | None = None) -> list[str]:
    haystack = build_haystack(content, keywords)
    detected = [
        kind for kind, hints in INTENT_HINTS.items()
        if contains_any(haystack, hints)
    ]
    if has_open_loop_signal(content, keywords) and "open_loop" not in detected:
        detected.append("open_loop")
    if "open_loop" in detected and "episode" in detected:
        if not contains_any(haystack, EPISODE_STRICT_HINTS):
            detected = [kind for kind in detected if kind != "episode"]
    return detected


def classify_query(content: str, *, keyword_limit: int = 8) -> dict:
    hits = namespace_hits(content)
    intents = detect_intents(content)
    is_open_loop = has_open_loop_signal(content)
    query_type = "normal"
    for namespace in QUERY_TYPE_PRIORITY:
        if namespace in hits:
            query_type = namespace
            break

    keywords = []
    for namespace in QUERY_TYPE_PRIORITY:
        for hint in hits.get(namespace, []):
            if hint not in keywords:
                keywords.append(hint)

    if not keywords and is_open_loop:
        haystack = build_haystack(content)
        for hint_group in (OPEN_LOOP_STRONG_HINTS, SCHEDULE_HINTS, DEVICE_HINTS):
            for hint in matched_hints(haystack, hint_group):
                if hint not in keywords:
                    keywords.append(hint)

    needs_memory = bool(hits) or bool(intents)
    return {
        "query_type": query_type,
        "namespace_hits": hits,
        "keywords": keywords[:keyword_limit],
        "needs_memory": needs_memory,
        "is_open_loop_query": is_open_loop,
    }
