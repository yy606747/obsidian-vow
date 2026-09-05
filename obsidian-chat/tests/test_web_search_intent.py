import asyncio

from app.chat.private_markers import PairedPrivateMarkerStreamFilter
from app.web_search.intent import (
    WEB_SEARCH_INTENT_CLOSE,
    WEB_SEARCH_INTENT_OPEN,
    extract_web_search_intent,
    strip_web_search_intent_markers,
)


def test_web_search_intent_accepts_exactly_one_bounded_marker():
    cleaned, intent = extract_web_search_intent(
        "正文[WEB_SEARCH_INTENT]查最近一周的 AI 应用[/WEB_SEARCH_INTENT]"
    )
    assert cleaned == "正文"
    assert intent == "查最近一周的 AI 应用"

    for text in (
        "正文[WEB_SEARCH_INTENT][/WEB_SEARCH_INTENT]",
        "正文[WEB_SEARCH_INTENT]一[/WEB_SEARCH_INTENT][WEB_SEARCH_INTENT]二[/WEB_SEARCH_INTENT]",
        "正文[WEB_SEARCH_INTENT]一[WEB_SEARCH_INTENT]二[/WEB_SEARCH_INTENT][/WEB_SEARCH_INTENT]",
        "正文[/WEB_SEARCH_INTENT][WEB_SEARCH_INTENT]一[/WEB_SEARCH_INTENT]",
        "正文[WEB_SEARCH_INTENT]" + "长" * 301 + "[/WEB_SEARCH_INTENT]",
        "正文[WEB_SEARCH_INTENT]半截",
    ):
        visible, rejected = extract_web_search_intent(text)
        assert visible == "正文"
        assert rejected == ""


def test_web_search_stream_filter_hides_every_chunk_split_and_unfinished_tail():
    async def collect(parts):
        stream_filter = PairedPrivateMarkerStreamFilter(
            WEB_SEARCH_INTENT_OPEN,
            WEB_SEARCH_INTENT_CLOSE,
        )
        return "".join(stream_filter.feed(part) for part in parts) + stream_filter.flush()

    assert asyncio.run(collect([
        "正文[WEB_SEARCH_",
        "INTENT]秘密[/WEB_",
        "SEARCH_INTENT]继续",
    ])) == "正文继续"
    assert asyncio.run(collect(["正文[WEB_SEARCH_INTENT]半截"])) == "正文"
    assert asyncio.run(collect(["正文[WEB_SEARCH_IN"])) == "正文"
    assert strip_web_search_intent_markers("正文[WEB_SEARCH_IN") == "正文"
