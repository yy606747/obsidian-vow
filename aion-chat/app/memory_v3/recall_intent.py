"""Private RecallIntent marker contract shared by all model-output paths."""

from __future__ import annotations

import re

from app.chat.private_markers import strip_paired_private_marker


MAX_RECALL_INTENT_CHARS = 240
RECALL_INTENT_OPEN = "[RECALL_INTENT]"
RECALL_INTENT_CLOSE = "[/RECALL_INTENT]"
RECALL_INTENT_PATTERN = re.compile(
    r"\[RECALL_INTENT\]([\s\S]*?)\[/RECALL_INTENT\]",
    re.IGNORECASE,
)
UNFINISHED_RECALL_INTENT_PATTERN = re.compile(
    r"\[RECALL_INTENT\][\s\S]*$",
    re.IGNORECASE,
)


def strip_recall_intent_markers(text: str) -> str:
    return strip_paired_private_marker(text, RECALL_INTENT_OPEN, RECALL_INTENT_CLOSE)


def extract_recall_intent(text: str) -> tuple[str, str]:
    """Strip all markers and accept exactly one bounded, non-empty intent."""
    raw = str(text or "")
    matches = [" ".join(value.split()) for value in RECALL_INTENT_PATTERN.findall(raw)]
    cleaned = strip_recall_intent_markers(raw)
    if len(matches) != 1:
        return cleaned, ""
    intent = matches[0].strip()
    if not intent or len(intent) > MAX_RECALL_INTENT_CHARS:
        return cleaned, ""
    return cleaned, intent


def recall_intent_ability_block(*, user_name: str = "她") -> str:
    return f"""[可选的跨轮回忆意图]
如果你在回复时明确意识到：当前可见内容不足，而下一轮可能需要一段具体旧背景，才可在回复末尾额外写一条私有标记：
[RECALL_INTENT]描述要寻找的旧事，不要猜答案[/RECALL_INTENT]
它不会展示给{user_name}。当前上下文已经够用、只是同主题联想、或没有具体要找的旧事时不要写。每次最多一条，内容最多 240 字。"""


__all__ = [
    "MAX_RECALL_INTENT_CHARS",
    "RECALL_INTENT_CLOSE",
    "RECALL_INTENT_OPEN",
    "extract_recall_intent",
    "recall_intent_ability_block",
    "strip_recall_intent_markers",
]
