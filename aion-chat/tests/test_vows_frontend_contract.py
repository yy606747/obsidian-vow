from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _memory_source() -> str:
    return (ROOT / "static/memory.html").read_text(encoding="utf-8")


def test_open_vow_histories_reload_after_list_refresh():
    source = _memory_source()
    load_body = source[
        source.index("async function loadVows"):
        source.index("function _vowDate")
    ]

    assert "const generation = ++_vowGeneration" in load_body
    assert "_vowChainCache.clear()" in load_body
    assert "await _reloadOpenVowHistories(generation)" in load_body
    assert load_body.index("_vowChainCache.clear()") < load_body.index(
        "await _reloadOpenVowHistories(generation)"
    )


def test_stale_vow_chain_responses_cannot_overwrite_current_generation():
    source = _memory_source()
    chain_body = source[
        source.index("async function _loadVowChain"):
        source.index("async function _reloadOpenVowHistories")
    ]

    stale_guard = "generation !== _vowGeneration || !_vowHistoryOpen.has(rootId)"
    assert stale_guard in chain_body
    assert chain_body.index(stale_guard) < chain_body.index(
        "_vowChainCache.set(rootId"
    )
    assert "_vowChainLoading.get(rootId) === requestToken" in chain_body


def test_vow_ui_uses_bounded_list_and_chain_pages():
    source = _memory_source()

    assert "fulfilled_limit=${VOW_PAGE_SIZE}&fulfilled_offset=0" in source
    assert "async function loadMoreFulfilledVows" in source
    assert "chain?limit=${VOW_PAGE_SIZE}&offset=${offset}" in source
    assert "async function loadMoreVowHistory" in source
