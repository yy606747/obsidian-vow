"""Natural-language WebSearchIntent marker contract."""

from __future__ import annotations

import re

from app.chat.private_markers import strip_paired_private_marker


MAX_WEB_SEARCH_INTENT_CHARS = 300
WEB_SEARCH_INTENT_OPEN = "[WEB_SEARCH_INTENT]"
WEB_SEARCH_INTENT_CLOSE = "[/WEB_SEARCH_INTENT]"
WEB_SEARCH_INTENT_PATTERN = re.compile(
    re.escape(WEB_SEARCH_INTENT_OPEN)
    + r"([\s\S]*?)"
    + re.escape(WEB_SEARCH_INTENT_CLOSE),
    re.IGNORECASE,
)


def strip_web_search_intent_markers(text: str) -> str:
    return strip_paired_private_marker(
        text,
        WEB_SEARCH_INTENT_OPEN,
        WEB_SEARCH_INTENT_CLOSE,
    )


def extract_web_search_intent(text: str) -> tuple[str, str]:
    raw = str(text or "")
    matches = [" ".join(value.split()) for value in WEB_SEARCH_INTENT_PATTERN.findall(raw)]
    cleaned = strip_web_search_intent_markers(raw)
    lowered = raw.lower()
    if (
        len(matches) != 1
        or lowered.count(WEB_SEARCH_INTENT_OPEN.lower()) != 1
        or lowered.count(WEB_SEARCH_INTENT_CLOSE.lower()) != 1
    ):
        return cleaned, ""
    intent = matches[0].strip()
    if not intent or len(intent) > MAX_WEB_SEARCH_INTENT_CHARS:
        return cleaned, ""
    return cleaned, intent


def web_search_ability_block(*, allow_silent: bool = False) -> str:
    silent = (
        "本轮可以只有这条私有意图而不说话。"
        if allow_silent
        else "它必须附在一条正常可见回复之后；只想安静查询时不要在这里使用。"
    )
    return f"""[可选的跨轮联网查询]
只有你确实想知道外部的新信息时，才可在末尾写一条：
{WEB_SEARCH_INTENT_OPEN}用自然语言写清想查的问题、对象和时间范围{WEB_SEARCH_INTENT_CLOSE}
查询会在本轮结束后后台完成，本轮绝不能引用尚未返回的结果。以后看到结果也没有必须提起的义务。{silent}
每轮最多一条，最多 {MAX_WEB_SEARCH_INTENT_CHARS} 字；不要把它当例行任务。"""


__all__ = [
    "MAX_WEB_SEARCH_INTENT_CHARS",
    "WEB_SEARCH_INTENT_CLOSE",
    "WEB_SEARCH_INTENT_OPEN",
    "extract_web_search_intent",
    "strip_web_search_intent_markers",
    "web_search_ability_block",
]
