import asyncio
from contextlib import asynccontextmanager
import importlib
import time

import aiosqlite

from app.memory_v2.embedding import pack_embedding
from app.memory_v2.prompt_block import build_v2_memory_prompt_block
from app.memory_v2.service import MemoryService
import app.memory_v2.service as memory_service_module
import app.memory_v3.config as memory_v3_config_module
import app.memory_v3.repository as v3_repositories


hybrid = importlib.import_module("app.memory_v2.hybrid_recall")


def _chunk() -> dict:
    return {
        "id": "chunk-1",
        "conv_id": "conv",
        "message_ids_json": '["m1"]',
        "content": "[08-01 10:00] User: 原始对话里有非常长的细节。",
        "created_at": time.time() - 100,
        "updated_at": time.time() - 100,
        "embedding": pack_embedding([1.0, 0.0]),
        "keywords_json": '["旧事"]',
        "metadata_json": '{"source_start_ts":1,"source_end_ts":2}',
        "card_id": "card-1",
        "card_version": 2,
        "card_content": "那次旧事里，她希望重要边界先被认真听见。",
        "card_prompt_version": "v1",
    }


def _note(note_id: str, *, origin_type: str, content: str) -> dict:
    return {
        "id": note_id,
        "origin_type": origin_type,
        "content": content,
        "kind": "episode",
        "namespace": "normal",
        "importance": 0.5,
        "confidence": 0.7,
        "created_at": time.time() - 100,
        "updated_at": time.time() - 100,
        "last_used_at": None,
        "source_conv": "conv",
        "source_start_ts": 1,
        "source_end_ts": 2,
        "embedding": pack_embedding([1.0, 0.0]),
        "keywords_json": '["旧事"]',
        "metadata_json": '{"source_message_ids":["m1"]}',
    }


def test_note_recency_uses_source_time_not_usage_polluted_updated_at():
    now = time.time()
    old_source = now - 100 * 24 * 60 * 60
    recent_source = now - 24 * 60 * 60
    old = {
        **_note("old", origin_type="manual", content="old"),
        "source_start_ts": old_source,
        "source_end_ts": old_source,
        "created_at": old_source,
        "updated_at": now,
    }
    recent = {
        **_note("recent", origin_type="manual", content="recent"),
        "source_start_ts": recent_source,
        "source_end_ts": recent_source,
        "created_at": recent_source,
        "updated_at": old_source,
    }

    old_score = hybrid._score_note(old, [], 0.5, None)["score"]
    recent_score = hybrid._score_note(recent, [], 0.5, None)["score"]

    assert recent_score > old_score
    assert [row["id"] for row in hybrid._sort_rows_by_recency([old, recent])] == [
        "recent",
        "old",
    ]


def test_card_readout_uses_raw_embedding_identity_and_ai_note_keeps_its_own_lane(
    monkeypatch,
):
    fetch_calls = []

    async def fake_fetch_chunks(_limit, *, include_cards=False):
        fetch_calls.append(("chunks", include_cards))
        return [_chunk()]

    async def fake_fetch_notes(_limit, *, origin_type=None, exclude_origin_types=None):
        fetch_calls.append(("notes", origin_type, set(exclude_origin_types or set())))
        if origin_type == "ai_note":
            return [_note("ai-note", origin_type="ai_note", content="我当时主动记下了这件旧事。")]
        candidates = [
            _note("digest", origin_type="auto_digest", content="自动 digest"),
            _note("manual", origin_type="manual", content="人工旧摘要"),
        ]
        excluded = set(exclude_origin_types or set())
        return [row for row in candidates if row["origin_type"] not in excluded]

    async def fake_embedding(_text):
        return [1.0, 0.0]

    async def fake_usage(_ids):
        return {}

    monkeypatch.setattr(hybrid, "_fetch_chunks", fake_fetch_chunks)
    monkeypatch.setattr(hybrid, "_fetch_notes", fake_fetch_notes)
    monkeypatch.setattr(hybrid.embedding, "get_embedding", fake_embedding)
    monkeypatch.setattr(hybrid, "_recent_usage", fake_usage)

    result = asyncio.run(
        hybrid.hybrid_recall(
            "那件旧事",
            ["旧事"],
            top_k=3,
            relational_cards_enabled=True,
            card_readout_mode="card_preferred",
            ai_note_lane_enabled=True,
            ai_note_top_k=3,
            ai_note_max_items=1,
        )
    )

    selected_by_id = {item["id"]: item for item in result["selected"]}
    # The ordinary manual note points at the exact same source as the raw
    # chunk and is deduped. The AI note is a separate perspective and keeps
    # its reserved lane even with identical source IDs.
    assert set(selected_by_id) == {"chunk-1", "ai-note"}
    assert selected_by_id["chunk-1"]["candidate_id"] == "chunk-1"
    assert selected_by_id["chunk-1"]["content"].startswith("[08-01")
    assert selected_by_id["chunk-1"]["preview"].startswith("那次旧事")
    assert selected_by_id["chunk-1"]["readout_type"] == "relational_card"
    assert selected_by_id["chunk-1"]["card_id"] == "card-1"
    assert selected_by_id["ai-note"]["lane"] == "ai_note"
    assert "digest" not in selected_by_id
    assert result["ai_note_selected_count"] == 1
    assert ("chunks", True) in fetch_calls
    assert ("notes", None, {"ai_note"}) in fetch_calls

    block = build_v2_memory_prompt_block(
        result,
        min_score=0.0,
        max_items=3,
        user_name="小栀",
    )
    assert block["enabled"] is True
    assert "[可能相关的关系摘要]" in block["content"]
    assert "不是小栀逐字确认" in block["content"]
    assert "[你当时主动想记住的内容]" in block["content"]
    assert "不自动获得事实权威" in block["content"]
    assert "原始对话里有非常长的细节" not in block["content"]
    assert len([item for item in block["items"] if item["id"] == "chunk-1"]) == 1


def test_visible_source_candidates_are_filtered_before_scoring_and_slots(monkeypatch):
    visible_chunk = {**_chunk(), "id": "visible-chunk", "message_ids_json": '["visible"]'}
    replacement_chunk = {
        **_chunk(),
        "id": "replacement-chunk",
        "message_ids_json": '["old-chunk"]',
        "card_id": None,
        "card_content": "",
    }
    visible_note = _note("visible-note", origin_type="manual", content="可见 note")
    visible_note["metadata_json"] = '{"source_message_ids":["visible"]}'
    legacy_note = _note("legacy-note", origin_type="manual", content="无 provenance 的旧 note")
    legacy_note["metadata_json"] = "{}"
    visible_ai = _note("visible-ai", origin_type="ai_note", content="可见 AI note")
    visible_ai["metadata_json"] = '{"source_message_ids":["visible"]}'
    eligible_ai = _note("eligible-ai", origin_type="ai_note", content="旧 AI note")
    eligible_ai["metadata_json"] = '{"source_message_ids":["old-ai"]}'

    async def fake_fetch_chunks(_limit, *, include_cards=False):
        assert include_cards is True
        return [visible_chunk, replacement_chunk]

    async def fake_fetch_notes(_limit, *, origin_type=None, exclude_origin_types=None):
        if origin_type == "ai_note":
            return [visible_ai, eligible_ai]
        return [visible_note, legacy_note]

    async def fake_embedding(_text):
        return [1.0, 0.0]

    async def fake_usage(_ids):
        return {}

    monkeypatch.setattr(hybrid, "_fetch_chunks", fake_fetch_chunks)
    monkeypatch.setattr(hybrid, "_fetch_notes", fake_fetch_notes)
    monkeypatch.setattr(hybrid.embedding, "get_embedding", fake_embedding)
    monkeypatch.setattr(hybrid, "_recent_usage", fake_usage)

    result = asyncio.run(
        hybrid.hybrid_recall(
            "旧事",
            ["旧事"],
            top_k=4,
            slot_min_score=0.0,
            visible_message_ids=["visible"],
            relational_cards_enabled=True,
            card_readout_mode="card_preferred",
            ai_note_lane_enabled=True,
            ai_note_top_k=3,
            ai_note_max_items=2,
        )
    )

    selected_ids = {item["id"] for item in result["selected"]}
    assert "visible-chunk" not in selected_ids
    assert "visible-note" not in selected_ids
    assert "visible-ai" not in selected_ids
    assert "replacement-chunk" in selected_ids
    assert "legacy-note" in selected_ids
    assert "eligible-ai" in selected_ids
    assert result["visible_source_excluded"] == {
        "chunks": 1,
        "notes": 1,
        "ai_notes": 1,
        "total": 3,
    }
    visible_ids = {"visible"}
    assert all(
        not (set(item.get("source_message_ids") or []) & visible_ids)
        for item in result["selected"]
    )


def test_ai_note_slots_use_final_prompt_threshold_and_return_unused_slots(monkeypatch):
    ordinary_scores = [0.90, 0.86, 0.82, 0.78, 0.74]
    ai_scores = [0.59, 0.52]

    async def fake_fetch_chunks(_limit, **_kwargs):
        return [
            {
                **_chunk(),
                "id": f"ordinary-{index}",
                "content": f"ordinary candidate {index} distinct detail {index * 17}",
                "message_ids_json": f'["old-{index}"]',
                "forced_score": score,
                "card_id": None,
                "card_content": "",
            }
            for index, score in enumerate(ordinary_scores)
        ]

    async def fake_fetch_notes(_limit, *, origin_type=None, **_kwargs):
        if origin_type != "ai_note":
            return []
        return [
            {
                **_note(f"ai-{index}", origin_type="ai_note", content=f"AI {index}"),
                "forced_score": score,
            }
            for index, score in enumerate(ai_scores)
        ]

    async def fake_embedding(_text):
        return [1.0, 0.0]

    async def fake_usage(_ids):
        return {}

    def forced_chunk_score(row, _terms, _semantic, _used, **_kwargs):
        return {
            "id": row["id"],
            "candidate_id": row["id"],
            "source_type": "chunk",
            "lane": "ordinary",
            "content": row["content"],
            "source_message_ids": hybrid._source_message_ids(row),
            "score": row["forced_score"],
            "semantic_similarity": 0.8,
            "keyword_relevance": 0.0,
        }

    def forced_note_score(row, _terms, _semantic, _used, *, as_ai_note=False):
        return {
            "id": row["id"],
            "candidate_id": row["id"],
            "source_type": "ai_note" if as_ai_note else "note",
            "lane": "ai_note" if as_ai_note else "ordinary",
            "content": row["content"],
            "source_message_ids": hybrid._source_message_ids(row),
            "score": row["forced_score"],
            "semantic_similarity": 0.8,
            # Deliberately high: planner keyword bypass must not consume a
            # final prompt slot below slot_min_score.
            "keyword_relevance": 0.9,
        }

    monkeypatch.setattr(hybrid, "_fetch_chunks", fake_fetch_chunks)
    monkeypatch.setattr(hybrid, "_fetch_notes", fake_fetch_notes)
    monkeypatch.setattr(hybrid.embedding, "get_embedding", fake_embedding)
    monkeypatch.setattr(hybrid, "_recent_usage", fake_usage)
    monkeypatch.setattr(hybrid, "_score_chunk", forced_chunk_score)
    monkeypatch.setattr(hybrid, "_score_note", forced_note_score)

    expected = (
        ([0.59, 0.52], 0, 5),
        ([0.72, 0.59], 1, 4),
        ([0.72, 0.71], 2, 3),
    )
    for scores, ai_count, ordinary_count in expected:
        ai_scores[:] = scores
        result = asyncio.run(
            hybrid.hybrid_recall(
                "旧事",
                ["旧事"],
                top_k=5,
                slot_min_score=0.67,
                ai_note_lane_enabled=True,
                ai_note_top_k=3,
                ai_note_max_items=2,
            )
        )
        assert result["ai_note_selected_count"] == ai_count
        assert len([item for item in result["selected"] if item["lane"] == "ordinary"]) == ordinary_count
        assert len(result["selected"]) == 5


def test_embedding_failure_keeps_debug_candidates_but_fails_prompt_closed(monkeypatch):
    async def fake_fetch_chunks(_limit, **_kwargs):
        return [{**_chunk(), "keywords_json": '["旧事"]'}]

    async def fake_fetch_notes(_limit, **_kwargs):
        return []

    async def no_embedding(_text):
        return None

    async def fake_usage(_ids):
        return {}

    monkeypatch.setattr(hybrid, "_fetch_chunks", fake_fetch_chunks)
    monkeypatch.setattr(hybrid, "_fetch_notes", fake_fetch_notes)
    monkeypatch.setattr(hybrid.embedding, "get_embedding", no_embedding)
    monkeypatch.setattr(hybrid, "_recent_usage", fake_usage)

    result = asyncio.run(
        hybrid.hybrid_recall(
            "旧事",
            ["旧事"],
            top_k=2,
            slot_min_score=0.05,
            ai_note_lane_enabled=True,
        )
    )
    block = build_v2_memory_prompt_block(result, min_score=0.05, max_items=2)

    assert result["semantic_query"] is False
    assert result["selected"] == []
    assert result["debug_top"]
    assert block["enabled"] is False
    assert block["items"] == []


def test_chat_service_wires_visible_ids_and_final_threshold_into_planner(monkeypatch):
    captured = {}

    async def fake_hybrid(query_text, keywords, **kwargs):
        captured.update(kwargs)
        return {
            "query": query_text,
            "keywords": keywords or [],
            "turn_plan": {"needs_memory": True},
            "allowed_namespaces": ["all"],
            "candidate_count": 0,
            "selected": [],
            "debug_top": [],
            "semantic_query": True,
            "abstain_reason": "no_candidates",
        }

    monkeypatch.setattr(
        memory_service_module.recall_config,
        "load_recall_config",
        lambda: {
            "mode": "full",
            "top_k": 5,
            "candidate_limit": 1000,
            "include_trace": False,
            "canary_ratio": 0.0,
            "prompt_min_score": 0.67,
        },
    )
    monkeypatch.setattr(
        memory_v3_config_module,
        "load_memory_v3_config",
        lambda: {
            "relational_cards_enabled": False,
            "card_readout_mode": "current_raw",
            "ai_note_lane_enabled": True,
            "ai_note_top_k": 3,
            "ai_note_max_items": 2,
            "pending_recall_enabled": False,
        },
    )
    monkeypatch.setattr(hybrid, "hybrid_recall", fake_hybrid)

    result = asyncio.run(
        MemoryService().plan_v2_recall_for_chat(
            "旧事",
            ["旧事"],
            visible_message_ids=["visible-1", "visible-2"],
        )
    )

    assert captured["slot_min_score"] == 0.67
    assert captured["visible_message_ids"] == ["visible-1", "visible-2"]
    assert captured["full_corpus"] is True
    assert result["prompt_block"]["enabled"] is False


def test_embedding_failure_contract_does_not_block_pending_route():
    block = build_v2_memory_prompt_block(
        {
            "semantic_query": False,
            "selected": [
                {
                    "id": "ordinary",
                    "content": "纯词法 ordinary 不得注入",
                    "lane": "ordinary",
                    "score": 0.9,
                },
                {
                    "id": "pending",
                    "content": "selector 已批准的 pending",
                    "lane": "pending",
                    "score": 0.1,
                    "prompt_priority": 1,
                },
            ],
        },
        min_score=0.0,
        max_items=2,
    )

    assert [item["id"] for item in block["items"]] == ["pending"]
    assert "纯词法 ordinary" not in block["content"]


def test_toggle_unresolved_invalidates_cached_note_recency(monkeypatch):
    invalidations = []

    async def fake_toggle(mem_id):
        assert mem_id == "note-1"
        return {"ok": True, "unresolved": 1}

    monkeypatch.setattr(memory_service_module.repository, "toggle_unresolved", fake_toggle)
    monkeypatch.setattr(
        hybrid,
        "invalidate_full_corpus_cache",
        lambda **kwargs: invalidations.append(kwargs),
    )

    result = asyncio.run(MemoryService().toggle_unresolved("note-1"))

    assert result["ok"] is True
    assert invalidations == [{"notes": True}]


def test_v3_disabled_keeps_legacy_fetch_signatures_and_prompt_shape(monkeypatch):
    async def fake_fetch_chunks(_limit):
        row = _chunk()
        for key in ("card_id", "card_version", "card_content", "card_prompt_version"):
            row.pop(key)
        return [row]

    async def fake_fetch_notes(_limit):
        return [_note("ai-note", origin_type="ai_note", content="旧 AI note")]

    async def fake_embedding(_text):
        return [1.0, 0.0]

    async def fake_usage(_ids):
        return {}

    monkeypatch.setattr(hybrid, "_fetch_chunks", fake_fetch_chunks)
    monkeypatch.setattr(hybrid, "_fetch_notes", fake_fetch_notes)
    monkeypatch.setattr(hybrid.embedding, "get_embedding", fake_embedding)
    monkeypatch.setattr(hybrid, "_recent_usage", fake_usage)

    result = asyncio.run(hybrid.hybrid_recall("旧事", ["旧事"], top_k=2))
    block = build_v2_memory_prompt_block(result, min_score=0.0, max_items=2)

    assert result["retrieval_mode"] == "hybrid_chunk_note"
    assert result["card_readout_mode"] == "current_raw"
    assert "[可能相关的关系摘要]" not in block["content"]
    assert "[AI 当时主动想记住的内容]" not in block["content"]
    assert "[可能相关的记忆]" in block["content"]
    assert "原始对话里有非常长的细节" in block["content"]


def test_historical_auto_digest_remains_in_ordinary_recall_when_cards_are_enabled(
    monkeypatch,
):
    async def fake_fetch_chunks(_limit, *, include_cards=False):
        assert include_cards is True
        return []

    async def fake_fetch_notes(_limit, *, origin_type=None, exclude_origin_types=None):
        assert origin_type is None
        assert not exclude_origin_types
        return [_note("digest", origin_type="auto_digest", content="历史自动摘要仍可召回")]

    async def fake_embedding(_text):
        return [1.0, 0.0]

    async def fake_usage(_ids):
        return {}

    monkeypatch.setattr(hybrid, "_fetch_chunks", fake_fetch_chunks)
    monkeypatch.setattr(hybrid, "_fetch_notes", fake_fetch_notes)
    monkeypatch.setattr(hybrid.embedding, "get_embedding", fake_embedding)
    monkeypatch.setattr(hybrid, "_recent_usage", fake_usage)

    result = asyncio.run(
        hybrid.hybrid_recall(
            "历史自动摘要",
            ["历史", "摘要"],
            top_k=2,
            relational_cards_enabled=True,
            card_readout_mode="card_preferred",
        )
    )
    assert [item["id"] for item in result["selected"]] == ["digest"]


def test_chunk_fetch_reads_only_current_card_family(tmp_path, monkeypatch):
    db_path = tmp_path / "readout-versions.db"

    async def with_heartbeat(awaitable):
        async def heartbeat():
            while True:
                await asyncio.sleep(0.001)

        task = asyncio.create_task(heartbeat())
        try:
            return await awaitable
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def prepare():
        async with aiosqlite.connect(db_path) as db:
            await db.execute(
                "CREATE TABLE memory_chunks ("
                "id TEXT PRIMARY KEY, conv_id TEXT, message_ids_json TEXT, content TEXT, "
                "created_at REAL, updated_at REAL, source_hash TEXT, embedding BLOB, "
                "keywords_json TEXT, metadata_json TEXT, status TEXT)"
            )
            await db.execute(
                "CREATE TABLE memory_relational_cards ("
                "id TEXT PRIMARY KEY, source_chunk_id TEXT, version INTEGER, content TEXT, "
                "prompt_version TEXT, status TEXT)"
            )
            for index, chunk_id in enumerate(("legacy", "current", "raw"), 1):
                await db.execute(
                    "INSERT INTO memory_chunks VALUES (?,?, '[]', ?, ?, ?, '', NULL, '[]', '{}', 'active')",
                    (chunk_id, "conv", f"raw-{chunk_id}", index, index),
                )
            await db.execute(
                "INSERT INTO memory_relational_cards VALUES "
                "('v1','legacy',1,'legacy-card','relational-card-v1','active')"
            )
            await db.execute(
                "INSERT INTO memory_relational_cards VALUES "
                "('v5','current',1,'current-card','relational-card-v5.2','active')"
            )
            await db.commit()

    asyncio.run(with_heartbeat(prepare()))

    @asynccontextmanager
    async def get_db():
        async with aiosqlite.connect(db_path) as db:
            yield db

    monkeypatch.setattr(hybrid, "get_db", get_db)
    rows = asyncio.run(
        with_heartbeat(hybrid._fetch_chunks(10, include_cards=True))
    )
    by_id = {row["id"]: row for row in rows}

    assert by_id["legacy"]["card_id"] is None
    assert by_id["legacy"]["card_content"] is None
    assert by_id["current"]["card_id"] == "v5"
    assert by_id["current"]["card_content"] == "current-card"
    assert by_id["raw"]["card_id"] is None


def test_candidate_window_saturation_is_visible_for_ordinary_and_pending(
    monkeypatch, caplog
):
    async def fake_fetch_chunks(_limit, *, include_cards=False):
        return [_chunk()]

    async def fake_fetch_notes(_limit, **_kwargs):
        return []

    async def fake_embedding(_text):
        return [1.0, 0.0]

    async def fake_usage(_ids):
        return {}

    monkeypatch.setattr(hybrid, "_fetch_chunks", fake_fetch_chunks)
    monkeypatch.setattr(hybrid, "_fetch_notes", fake_fetch_notes)
    monkeypatch.setattr(hybrid.embedding, "get_embedding", fake_embedding)
    monkeypatch.setattr(hybrid, "_recent_usage", fake_usage)
    hybrid._candidate_window_last_logged.clear()
    caplog.set_level("WARNING", logger=hybrid.__name__)

    ordinary = asyncio.run(
        hybrid.hybrid_recall(
            "旧事",
            ["旧事"],
            top_k=1,
            candidate_limit=1,
        )
    )
    pending = asyncio.run(
        hybrid.wide_chunk_recall(
            "旧事",
            top_k=1,
            candidate_limit=1,
            as_of_ts=time.time() + 1,
            full_corpus_enabled=False,
        )
    )

    assert ordinary["candidate_windows"]["chunks"] == {
        "returned": 1,
        "limit": 1,
        "saturated": True,
    }
    assert pending[0]["candidate_id"] == "chunk-1"
    assert "lane=ordinary_chunks" in caplog.text
    assert "lane=pending_chunks" in caplog.text


def test_full_corpus_scan_disables_recent_window_and_reports_all_scored_rows(
    monkeypatch,
):
    seen_limits = []

    async def fake_fetch_chunks(limit, **_kwargs):
        seen_limits.append(("chunks", limit))
        return [
            {
                **_chunk(),
                "id": f"chunk-{index}",
                "message_ids_json": f'["old-{index}"]',
                "content": f"old memory {index}",
            }
            for index in range(3)
        ]

    async def fake_fetch_notes(limit, **_kwargs):
        seen_limits.append(("notes", limit))
        return []

    async def fake_embedding(_text):
        return [1.0, 0.0]

    async def fake_usage(_ids):
        return {}

    monkeypatch.setattr(hybrid, "_fetch_chunks", fake_fetch_chunks)
    monkeypatch.setattr(hybrid, "_fetch_notes", fake_fetch_notes)
    monkeypatch.setattr(hybrid.embedding, "get_embedding", fake_embedding)
    monkeypatch.setattr(hybrid, "_recent_usage", fake_usage)
    hybrid.clear_full_corpus_cache()

    try:
        result = asyncio.run(
            hybrid.hybrid_recall(
                "旧记忆",
                ["旧记忆"],
                top_k=2,
                candidate_limit=1,
                full_corpus=True,
            )
        )
    finally:
        hybrid.clear_full_corpus_cache()

    assert seen_limits == [("chunks", None), ("notes", None)]
    assert result["candidate_scope"] == "full_corpus"
    assert result["chunk_candidate_count"] == 3
    assert result["candidate_windows"]["chunks"] == {
        "returned": 3,
        "limit": 0,
        "saturated": False,
    }


def test_full_corpus_keeps_ai_notes_out_of_ordinary_lane_when_ai_quota_is_zero(
    monkeypatch,
):
    calls = []

    async def fake_fetch_chunks(limit, **_kwargs):
        assert limit is None
        return []

    async def fake_fetch_notes(limit, *, origin_type=None, exclude_origin_types=None):
        assert limit is None
        calls.append((origin_type, exclude_origin_types))
        if origin_type == "ai_note":
            return [_note("ai", origin_type="ai_note", content="AI note")]
        assert exclude_origin_types == {"ai_note"}
        return [_note("manual", origin_type="manual", content="manual note")]

    async def fake_embedding(_text):
        return [1.0, 0.0]

    async def fake_usage(_ids):
        return {}

    monkeypatch.setattr(hybrid, "_fetch_chunks", fake_fetch_chunks)
    monkeypatch.setattr(hybrid, "_fetch_notes", fake_fetch_notes)
    monkeypatch.setattr(hybrid.embedding, "get_embedding", fake_embedding)
    monkeypatch.setattr(hybrid, "_recent_usage", fake_usage)
    hybrid.clear_full_corpus_cache()

    try:
        result = asyncio.run(
            hybrid.hybrid_recall(
                "manual note",
                ["manual note"],
                top_k=2,
                slot_min_score=0.0,
                full_corpus=True,
                ai_note_lane_enabled=True,
                ai_note_top_k=0,
                ai_note_max_items=0,
            )
        )
    finally:
        hybrid.clear_full_corpus_cache()

    assert calls == [(None, {"ai_note"}), ("ai_note", None)]
    assert result["note_candidate_count"] == 1
    assert result["ai_note_candidate_count"] == 0
    assert [item["id"] for item in result["selected"]] == ["manual"]


def test_slow_full_scan_warning_is_rate_limited(monkeypatch, caplog):
    monotonic_values = iter([10.0, 11.0, 400.0])
    monkeypatch.setattr(hybrid.time, "monotonic", lambda: next(monotonic_values))
    monkeypatch.setattr(hybrid, "_slow_recall_last_logged_at", None)
    caplog.set_level("WARNING", logger=hybrid.__name__)

    assert hybrid._maybe_warn_slow_recall(
        0.301,
        chunks=3900,
        notes=4760,
        ai_notes=74,
        scope="full_corpus",
    ) is True
    assert hybrid._maybe_warn_slow_recall(
        0.500,
        chunks=3900,
        notes=4760,
        ai_notes=74,
        scope="full_corpus",
    ) is False
    assert hybrid._maybe_warn_slow_recall(
        0.500,
        chunks=3900,
        notes=4760,
        ai_notes=74,
        scope="full_corpus",
    ) is True
    assert caplog.text.count("memory recall slow") == 2
    assert "chunks=3900 notes=4760 ai_notes=74 scope=full_corpus" in caplog.text


def test_cached_matrix_keeps_mixed_embedding_dimensions_exact():
    rows = [
        {"id": "two", "embedding": pack_embedding([1.0, 0.0])},
        {"id": "three", "embedding": pack_embedding([0.0, 1.0, 0.0])},
    ]
    cached = hybrid._cached_embedding_matrix(rows)

    assert cached.similarities(rows, [1.0, 0.0]) == [1.0, 0.0]
    assert cached.similarities(rows, [0.0, 1.0, 0.0]) == [0.0, 1.0]


def test_cached_static_preprocessing_preserves_scoring_and_provenance(monkeypatch):
    monkeypatch.setattr(hybrid.time, "time", lambda: 1_800_000_000.0)
    chunk = {
        **_chunk(),
        "keywords_json": '["旧事","DADDY"]',
        "metadata_json": '{"source_message_ids":["source-1"],"source_start_ts":1,"source_end_ts":2}',
    }
    note = {
        **_note("note", origin_type="manual", content="DADDY 记得旧事"),
        "keywords_json": '["旧事","DADDY"]',
        "metadata_json": '{"source_message_ids":["source-2"]}',
    }
    raw_chunk = hybrid._score_chunk(chunk, ["旧事", "daddy"], 0.8, None)
    raw_note = hybrid._score_note(note, ["旧事", "daddy"], 0.8, None)

    hybrid._prepare_cached_rows([chunk, note])
    cached_chunk = hybrid._score_chunk(chunk, ["旧事", "daddy"], 0.8, None)
    cached_note = hybrid._score_note(note, ["旧事", "daddy"], 0.8, None)

    assert cached_chunk == raw_chunk
    assert cached_note == raw_note
    assert hybrid._source_message_ids(chunk) == ["source-1"]
    assert hybrid._source_message_ids(note) == ["source-2"]


def test_expired_full_corpus_cache_serves_stale_while_background_rebuilds(
    monkeypatch,
):
    now = [100.0]
    fetch_calls = 0
    release_rebuild = None

    def chunk(chunk_id: str) -> dict:
        return {
            **_chunk(),
            "id": chunk_id,
            "content": chunk_id,
            "message_ids_json": f'["{chunk_id}-message"]',
            "card_id": None,
            "card_content": "",
        }

    async def fake_fetch_chunks(limit, **_kwargs):
        nonlocal fetch_calls
        assert limit is None
        fetch_calls += 1
        if fetch_calls == 2:
            await release_rebuild.wait()
            return [chunk("fresh")]
        return [chunk("stale")]

    async def fake_fetch_notes(limit, **_kwargs):
        assert limit is None
        return []

    monkeypatch.setattr(hybrid, "_fetch_chunks", fake_fetch_chunks)
    monkeypatch.setattr(hybrid, "_fetch_notes", fake_fetch_notes)
    monkeypatch.setattr(hybrid.embedding, "embedding_signature", lambda: "test-signature")
    monkeypatch.setattr(hybrid.time, "monotonic", lambda: now[0])
    hybrid.clear_full_corpus_cache()

    async def run():
        nonlocal release_rebuild
        release_rebuild = asyncio.Event()
        initial = await hybrid._full_corpus_candidates(
            include_cards=False,
            ai_note_lane_enabled=False,
        )
        now[0] += hybrid.FULL_CORPUS_CACHE_TTL_SECONDS + 1
        stale = await hybrid._full_corpus_candidates(
            include_cards=False,
            ai_note_lane_enabled=False,
        )
        task = stale.background_refresh_task
        assert stale is initial
        assert [row["id"] for row in stale.chunks] == ["stale"]
        assert task is not None
        await asyncio.sleep(0)
        assert task.done() is False
        release_rebuild.set()
        await task
        fresh = await hybrid._full_corpus_candidates(
            include_cards=False,
            ai_note_lane_enabled=False,
        )
        return initial, fresh

    initial, fresh = asyncio.run(run())
    try:
        assert fresh is not initial
        assert [row["id"] for row in fresh.chunks] == ["fresh"]
        assert fetch_calls == 2
    finally:
        hybrid.clear_full_corpus_cache()


def test_full_corpus_cache_reuses_matrix_and_refreshes_dirty_conversation(monkeypatch):
    calls = {"full_chunks": 0, "incremental_chunks": 0, "notes": 0}

    def chunk(chunk_id: str, conv_id: str, vector: list[float]) -> dict:
        return {
            **_chunk(),
            "id": chunk_id,
            "conv_id": conv_id,
            "message_ids_json": f'["{chunk_id}-message"]',
            "content": chunk_id,
            "embedding": pack_embedding(vector),
            "card_id": None,
            "card_content": "",
        }

    async def fake_fetch_chunks(limit, **_kwargs):
        assert limit is None
        calls["full_chunks"] += 1
        return [chunk("old-a", "conv-a", [1.0, 0.0]), chunk("old-b", "conv-b", [0.0, 1.0])]

    async def fake_fetch_notes(limit, **_kwargs):
        assert limit is None
        calls["notes"] += 1
        return []

    async def fake_incremental(conv_ids, **_kwargs):
        assert conv_ids == {"conv-a"}
        calls["incremental_chunks"] += 1
        return [chunk("new-a", "conv-a", [1.0, 0.0])]

    monkeypatch.setattr(hybrid, "_fetch_chunks", fake_fetch_chunks)
    monkeypatch.setattr(hybrid, "_fetch_notes", fake_fetch_notes)
    monkeypatch.setattr(hybrid, "_fetch_chunks_for_conversations", fake_incremental)
    hybrid.clear_full_corpus_cache()

    async def run():
        first = await hybrid._full_corpus_candidates(
            include_cards=False,
            ai_note_lane_enabled=False,
        )
        built_at = first.built_at
        first_matrix = first.chunk_matrix
        first_rows = list(first.chunks)
        second = await hybrid._full_corpus_candidates(
            include_cards=False,
            ai_note_lane_enabled=False,
        )
        assert first is second
        assert first.chunk_matrix.similarities(first.chunks, [1.0, 0.0]) == [1.0, 0.0]
        hybrid.invalidate_full_corpus_cache(chunk_conv_id="conv-a")
        refreshed = await hybrid._full_corpus_candidates(
            include_cards=False,
            ai_note_lane_enabled=False,
        )
        refreshed_chunk_matrix = refreshed.chunk_matrix
        hybrid.invalidate_full_corpus_cache(notes=True)
        note_refreshed = await hybrid._full_corpus_candidates(
            include_cards=False,
            ai_note_lane_enabled=False,
        )
        return (
            first,
            refreshed,
            note_refreshed,
            built_at,
            first_matrix,
            first_rows,
            refreshed_chunk_matrix,
        )

    (
        first,
        refreshed,
        note_refreshed,
        built_at,
        first_matrix,
        first_rows,
        refreshed_chunk_matrix,
    ) = asyncio.run(run())
    try:
        assert calls == {"full_chunks": 1, "incremental_chunks": 1, "notes": 2}
        assert first is refreshed is note_refreshed
        assert note_refreshed.built_at == built_at
        assert note_refreshed.chunk_matrix is refreshed_chunk_matrix
        assert first_matrix.similarities(first_rows, [1.0, 0.0]) == [1.0, 0.0]
        assert {row["id"] for row in note_refreshed.chunks} == {"new-a", "old-b"}
        assert note_refreshed.dirty_chunk_conversations == set()
    finally:
        hybrid.clear_full_corpus_cache()


def test_raw_full_readout_keeps_roles_and_drops_whole_item_over_budget():
    raw = "[08-01 10:00] User: 第一行\n[08-01 10:01] AI: 第二行"
    plan = {
        "selected": [
            {
                "id": "chunk",
                "candidate_id": "chunk",
                "source_type": "chunk",
                "kind": "raw_chunk",
                "namespace": "normal",
                "content": raw,
                "raw_content": raw,
                "readout_type": "raw_full",
                "lane": "ordinary",
                "score": 0.9,
            }
        ]
    }

    included = build_v2_memory_prompt_block(
        plan,
        min_score=0.0,
        max_items=1,
        max_block_chars=800,
    )
    dropped = build_v2_memory_prompt_block(
        plan,
        min_score=0.0,
        max_items=1,
        max_block_chars=150,
    )

    assert included["enabled"] is True
    assert raw in included["content"]
    assert "[可能相关的完整原文]" in included["content"]
    assert dropped["enabled"] is False
    assert "raw_full_budget_dropped" in dropped["warnings"]


def test_pending_merge_dedupes_same_chunk_and_has_prompt_priority():
    ordinary = {
        "id": "chunk-1",
        "candidate_id": "chunk-1",
        "source_type": "chunk",
        "content": "普通 RAG 原文",
        "raw_content": "普通 RAG 原文",
        "readout_type": "raw",
        "lane": "ordinary",
        "score": 0.7,
        "reason": "chunk",
    }
    pending = {
        **ordinary,
        "preview": "筛选后使用的关系背景",
        "readout_type": "relational_card",
        "lane": "pending",
        "score": 0.1,
        "prompt_priority": 1,
    }
    plan = {
        "selected": [ordinary, {**ordinary, "id": "chunk-2", "candidate_id": "chunk-2"}],
        "debug_top": [ordinary],
        "retrieval_mode": "hybrid_chunk_note",
    }

    merged = hybrid.merge_pending_items(plan, [pending], top_k=2)
    block = build_v2_memory_prompt_block(
        merged,
        min_score=0.45,
        max_items=2,
        max_block_chars=1000,
    )

    assert [item["candidate_id"] for item in merged["selected"]].count("chunk-1") == 1
    assert merged["selected"][0]["lane"] == "pending"
    assert merged["selected"][0]["retrieval_routes"] == ["ordinary", "pending"]
    assert "[上一轮提前寻找、现已通过筛选的背景]" in block["content"]
    assert "筛选后使用的关系背景" in block["content"]
    assert len([item for item in block["items"] if item["id"] == "chunk-1"]) == 1


def test_v3_prompt_usage_logs_source_chunk_identity_and_response_proxy(monkeypatch):
    class UsageRepository:
        def __init__(self):
            self.calls = []

        async def record_usage(self, **kwargs):
            self.calls.append(kwargs)
            return {
                "id": "usage",
                "memory_id": kwargs["memory_id"],
                "score": kwargs.get("score"),
                "rank": kwargs.get("rank"),
            }

    class EventRepository:
        calls = []

        async def record(self, event):
            self.calls.append(event)
            return "event-1"

    monkeypatch.setattr(v3_repositories, "InjectionEventRepository", EventRepository)
    usage_repository = UsageRepository()
    service = MemoryService(v2_repository=usage_repository)
    result = asyncio.run(
        service.record_v2_prompt_usage(
            {
                "runtime": {"v2_enabled": True, "mode": "full"},
                "memory_v3": {"relational_cards_enabled": True},
                "prompt_decision": {"inject": True, "reason": "full", "mode": "full"},
                "prompt_block": {
                    "enabled": True,
                    "items": [
                        {
                            "id": "chunk-1",
                            "candidate_id": "chunk-1",
                            "source_type": "chunk",
                            "readout_type": "relational_card",
                            "card_id": "card-1",
                            "card_version": 2,
                            "score": 0.8,
                            "preview": "那次她希望重要边界先被认真听见",
                        }
                    ],
                },
            },
            conv_id="conv",
            request_id="assistant-message",
            response_text="我记得，那次你希望重要边界先被认真听见。",
        )
    )

    assert usage_repository.calls[0]["memory_id"] == "chunk-1"
    assert usage_repository.calls[0]["touch_last_used"] is True
    assert result["injection_event_count"] == 1
    event = EventRepository.calls[0]
    assert event["candidate_id"] == "chunk-1"
    assert event["source_chunk_id"] == "chunk-1"
    assert event["card_id"] == "card-1"
    assert event["past_reference_proxy"] == 1
    assert event["response_overlap_proxy"] > 0
