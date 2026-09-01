"""Deterministic reflection preparation and provenance adaptation."""

from __future__ import annotations

import random
import re
from collections.abc import Callable, Sequence
from dataclasses import asdict, dataclass
from typing import Any


REFLECTION_RECENT_CLUE_EXCLUSION = 8

_SENTENCE_BOUNDARY_RE = re.compile(r"(?<=[。！？!?；;])\s*|\n+")


@dataclass(frozen=True)
class ProvenanceItem:
    id: str
    provenance: str
    label: str
    text: str
    source_type: str
    origin_type: str
    readout_type: str

    def to_dict(self) -> dict[str, str]:
        return asdict(self)


def split_working_model_sentences(content: str) -> list[str]:
    """Temporarily split prose/bullets without creating durable item IDs."""

    seen: set[str] = set()
    result: list[str] = []
    for part in _SENTENCE_BOUNDARY_RE.split(str(content or "")):
        value = part.strip().lstrip("-—•* ").strip()
        if len(value) < 2 or value in seen:
            continue
        seen.add(value)
        result.append(value)
    return result


def sample_clue(
    working_model_content: str,
    *,
    recent_clues: Sequence[str] = (),
    chooser: Callable[[Sequence[str]], str] = random.choice,
) -> str | None:
    excluded = {str(value).strip() for value in recent_clues if str(value).strip()}
    candidates = [
        sentence
        for sentence in split_working_model_sentences(working_model_content)
        if sentence not in excluded
    ]
    return str(chooser(candidates)) if candidates else None


def _one_line(value: Any) -> str:
    return " ".join(str(value or "").split())


def adapt_retrieved_item(item: dict[str, Any]) -> ProvenanceItem:
    source_type = str(item.get("source_type") or "").strip()
    origin_type = str(item.get("origin_type") or "legacy").strip() or "legacy"
    readout_type = str(item.get("readout_type") or "").strip()
    item_id = str(item.get("candidate_id") or item.get("id") or "").strip()

    if source_type == "chunk" and readout_type == "relational_card":
        provenance = "digest_relationship"
        label = "digest / 关系卡"
        item_id = str(item.get("card_id") or item_id).strip()
        text = str(item.get("preview") or item.get("content") or "").strip()
    elif source_type == "chunk":
        provenance = "conversation_excerpt"
        label = "conversation_excerpt（对话摘录，保留双方说话者边界）"
        # memory_chunks are stored with timestamp + speaker prefixes. Keep the
        # exact multiline form; collapsing it would erase the hard boundary.
        text = str(item.get("raw_content") or item.get("content") or "").strip()
    elif source_type == "ai_note" or origin_type == "ai_note":
        provenance = "ai_note"
        label = "ai_note（她过去自己记下的想法）"
        text = str(item.get("content") or "").strip()
    elif origin_type == "auto_digest":
        provenance = "digest_relationship"
        label = "digest / 关系卡"
        text = str(item.get("content") or "").strip()
    elif origin_type == "manual":
        provenance = "user_event"
        label = "用户原话 / 事件"
        text = str(item.get("content") or "").strip()
    else:
        # In particular, schema-default `legacy` is never promoted to user
        # evidence merely because it lacks better provenance.
        provenance = "unknown"
        label = "unknown（来源不明）"
        text = str(item.get("content") or item.get("preview") or "").strip()

    return ProvenanceItem(
        id=item_id,
        provenance=provenance,
        label=label,
        text=text,
        source_type=source_type,
        origin_type=origin_type,
        readout_type=readout_type,
    )


def adapt_retrieved_items(items: Sequence[dict[str, Any]]) -> list[dict[str, str]]:
    return [adapt_retrieved_item(dict(item)).to_dict() for item in items]


def render_labeled_items(items: Sequence[dict[str, Any]]) -> str:
    lines: list[str] = []
    for index, item in enumerate(items, 1):
        label = str(item.get("label") or "unknown（来源不明）")
        item_id = str(item.get("id") or "unknown")
        text = str(item.get("text") or "").strip()
        lines.append(f"[{index}] 来源：{label}；条目 id：{item_id}\n{text}")
    return "\n\n".join(lines)


__all__ = [
    "ProvenanceItem",
    "REFLECTION_RECENT_CLUE_EXCLUSION",
    "adapt_retrieved_item",
    "adapt_retrieved_items",
    "render_labeled_items",
    "sample_clue",
    "split_working_model_sentences",
]
