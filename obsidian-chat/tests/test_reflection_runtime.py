from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager

import aiosqlite
import pytest

from app.desire.schema import init_desire_tables
from app.memory_v2 import memory_service
from app.memory_v2 import recall_config as memory_recall_config
from app.memory_v3 import config as memory_v3_config
from app.reflection import repository as reflection_repository
from app.reflection.harness import (
    adapt_retrieved_items,
    sample_clue,
    split_working_model_sentences,
)
from app.reflection.schema import init_reflection_tables
from app.reflection.service import CapturedReflectionContext, run_reflection
from app.reflection.prompt import ReflectionParseError, parse_inverse_query
from app.working_model.runtime import (
    capture_reflection_working_model_pipeline_input,
    run_working_model_pipeline,
    stable_working_model_request_id,
)
from app.working_model.schema import init_working_model_tables
from app.working_model.writer import build_writer_identity_snapshot


def _db_factory(path):
    @asynccontextmanager
    async def factory():
        async with aiosqlite.connect(path) as db:
            yield db

    return factory


def _run(coro):
    async def heartbeat():
        # Keep the selector ticking while aiosqlite hands work back from its
        # worker thread.  This is only a test-runner aid; production's server
        # loop already has persistent timers and sockets.
        while True:
            await asyncio.sleep(0.001)

    async def wrapped():
        pulse = asyncio.create_task(heartbeat())
        try:
            return await coro
        finally:
            pulse.cancel()

    return asyncio.run(wrapped())


async def _init(path):
    async with aiosqlite.connect(path) as db:
        await init_working_model_tables(db)
        await init_desire_tables(db)
        await init_reflection_tables(db)
        await db.commit()


def _captured(identity):
    return CapturedReflectionContext(
        target_conv_id="conv_reflect",
        model_key="core-model",
        identity_snapshot=identity,
        working_model_head={
            "id": "wm_root",
            "content": "她在重要决定上要直接答案。\n她不喜欢被敷衍。",
        },
        candidate_clues=("她在重要决定上要直接答案。",),
    )


def test_sentence_sampling_excludes_recent_clues():
    content = "- 她要直接答案。\n- 她不喜欢被敷衍；她会追问来源。"
    assert split_working_model_sentences(content) == [
        "她要直接答案。",
        "她不喜欢被敷衍；",
        "她会追问来源。",
    ]
    assert sample_clue(
        content,
        recent_clues=["她要直接答案。", "她不喜欢被敷衍；"],
        chooser=lambda values: values[0],
    ) == "她会追问来源。"


def test_inverse_query_parser_rejects_non_text_payload():
    with pytest.raises(ReflectionParseError):
        parse_inverse_query('{"query":42}')


def test_reflection_retrieval_reuses_production_planner_with_fixed_top5(monkeypatch):
    calls = []

    async def fake_plan(query_text, keywords, **kwargs):
        calls.append((query_text, keywords, kwargs))
        return {"selected": []}

    monkeypatch.setattr(memory_service, "plan_v2_recall", fake_plan)
    monkeypatch.setattr(memory_recall_config, "load_recall_config", lambda: {})
    monkeypatch.setattr(
        memory_recall_config,
        "recall_runtime",
        lambda _config: {"v2_enabled": True, "candidate_limit": 40},
    )
    monkeypatch.setattr(
        memory_v3_config,
        "load_memory_v3_config",
        lambda: {
            "relational_cards_enabled": True,
            "card_readout_mode": "card_preferred",
            "ai_note_lane_enabled": True,
            "ai_note_top_k": 3,
            "ai_note_max_items": 2,
        },
    )

    result = asyncio.run(
        memory_service.plan_v2_recall_for_reflection("反向 query")
    )

    assert result == {"selected": []}
    assert calls == [
        (
            "反向 query",
            [],
            {
                "top_k": 5,
                "candidate_limit": 40,
                "full_corpus": True,
                "relational_cards_enabled": True,
                "card_readout_mode": "card_preferred",
                "ai_note_lane_enabled": True,
                "ai_note_top_k": 3,
                "ai_note_max_items": 2,
            },
        )
    ]


def test_reflection_retrieval_honors_v2_disabled(monkeypatch):
    async def unexpected_plan(*_args, **_kwargs):
        raise AssertionError("disabled V2 recall reached the planner")

    monkeypatch.setattr(memory_service, "plan_v2_recall", unexpected_plan)
    monkeypatch.setattr(
        memory_recall_config,
        "load_recall_config",
        lambda: {"mode": "legacy"},
    )

    result = asyncio.run(
        memory_service.plan_v2_recall_for_reflection("反向 query")
    )

    assert result == {"selected": []}


def test_provenance_adapter_keeps_unknown_and_speaker_boundaries():
    items = adapt_retrieved_items(
        [
            {
                "id": "chunk",
                "source_type": "chunk",
                "readout_type": "raw",
                "raw_content": "[08-01 10:00] 用户: 原话\n[08-01 10:01] AI: 回答",
            },
            {"id": "legacy", "source_type": "note", "origin_type": "legacy", "content": "旧条目"},
            {"id": "manual", "source_type": "note", "origin_type": "manual", "content": "用户事件"},
            {"id": "ai", "source_type": "ai_note", "origin_type": "ai_note", "content": "我的旧想法"},
            {"id": "digest", "source_type": "note", "origin_type": "auto_digest", "content": "整理"},
            {
                "candidate_id": "chunk-for-card",
                "card_id": "card-7",
                "source_type": "chunk",
                "readout_type": "relational_card",
                "preview": "关系卡整理",
            },
        ]
    )
    assert [item["provenance"] for item in items] == [
        "conversation_excerpt",
        "unknown",
        "user_event",
        "ai_note",
        "digest_relationship",
        "digest_relationship",
    ]
    assert "用户: 原话\n" in items[0]["text"]
    assert items[1]["label"].startswith("unknown")
    assert items[-1]["id"] == "card-7"


def test_no_evidence_is_a_logged_non_verdict(tmp_path):
    path = tmp_path / "reflection.sqlite3"
    _run(_init(path))
    factory = _db_factory(path)
    identity = build_writer_identity_snapshot(
        {"ai_name": "AI", "user_name": "用户", "ai_persona": "诚实"},
        vow_block="[誓约] 不伪造",
    )

    async def run():
        return await run_reflection(
            _captured(identity),
            db_factory=factory,
            query_generator=lambda _clue: "她接受含糊答案并不追问",
            retriever=lambda _query: [],
            chooser=lambda values: values[0],
        )

    result = _run(run())
    assert result["entered"] is True
    assert result["status"] == "no_evidence"
    assert result["log"]["outcome"] == "no_evidence"
    assert result["log"]["verdict"] is None
    assert result["log"]["inverse_query"] == "她接受含糊答案并不追问"


def test_incomplete_retrieval_item_is_a_failure_not_no_evidence(tmp_path):
    path = tmp_path / "incomplete.sqlite3"
    _run(_init(path))
    factory = _db_factory(path)
    identity = build_writer_identity_snapshot(
        {"ai_name": "AI", "user_name": "用户", "ai_persona": "诚实"},
        vow_block="[誓约] 不伪造",
    )

    result = _run(
        run_reflection(
            _captured(identity),
            db_factory=factory,
            query_generator=lambda _clue: "反向 query",
            retriever=lambda _query: [
                {
                    "id": "chunk-empty",
                    "source_type": "chunk",
                    "readout_type": "raw",
                    "raw_content": "",
                }
            ],
            chooser=lambda values: values[0],
        )
    )

    assert result["status"] == "retrieval_failed"
    assert result["log"]["outcome"] == "retrieval_failed"
    assert result["log"]["verdict"] is None


@pytest.mark.parametrize(
    ("failure_stage", "expected_outcome"),
    [
        ("query", "query_failed"),
        ("retrieval", "retrieval_failed"),
        ("provider", "reflection_provider_failed"),
        ("parse", "reflection_parse_failed"),
    ],
)
def test_reflection_technical_failures_never_become_unclear(
    tmp_path,
    failure_stage,
    expected_outcome,
):
    path = tmp_path / f"{failure_stage}.sqlite3"
    _run(_init(path))
    factory = _db_factory(path)
    identity = build_writer_identity_snapshot(
        {"ai_name": "AI", "user_name": "用户", "ai_persona": "诚实"},
        vow_block="[誓约] 不伪造",
    )

    def fail_query(_clue):
        raise RuntimeError("query failed")

    def fail_retrieval(_query):
        raise RuntimeError("retrieval failed")

    def evidence(_query):
        return [
            {
                "id": "chunk-1",
                "source_type": "chunk",
                "readout_type": "raw",
                "raw_content": "用户: 我想先保留一点空间。",
            }
        ]

    def fail_provider(_messages):
        raise RuntimeError("provider failed")

    kwargs = {
        "query_generator": fail_query if failure_stage == "query" else (lambda _clue: "反向 query"),
        "retriever": fail_retrieval if failure_stage == "retrieval" else evidence,
        "reflection_provider": (
            fail_provider
            if failure_stage == "provider"
            else (lambda _messages: "not-json")
            if failure_stage == "parse"
            else None
        ),
    }

    async def run():
        return await run_reflection(
            _captured(identity),
            db_factory=factory,
            chooser=lambda values: values[0],
            **kwargs,
        )

    result = _run(run())
    assert result["status"] == expected_outcome
    assert result["log"]["outcome"] == expected_outcome
    assert result["log"]["verdict"] is None


def test_reflection_holds_uses_identity_and_no_recent_chat(tmp_path):
    path = tmp_path / "reflection.sqlite3"
    _run(_init(path))
    factory = _db_factory(path)
    identity = build_writer_identity_snapshot(
        {"ai_name": "AI", "user_name": "用户", "ai_persona": "诚实"},
        vow_block="[誓约] 不伪造",
    )

    async def reflection_provider(messages):
        rendered = json.dumps(messages, ensure_ascii=False)
        assert "[誓约] 不伪造" in rendered
        assert "conversation_excerpt" in rendered
        assert "RECENT_SECRET" not in rendered
        return json.dumps(
            {
                "verdict": "holds",
                "reason": "对话摘录里她确实追问了依据。",
                "proposed_statement": "",
            },
            ensure_ascii=False,
        )

    async def run():
        return await run_reflection(
            _captured(identity),
            db_factory=factory,
            query_generator=lambda _clue: "她接受含糊答案并不追问",
            retriever=lambda _query: [
                {
                    "id": "chunk-1",
                    "source_type": "chunk",
                    "readout_type": "raw",
                    "raw_content": "用户: 这个结论来源呢？\nAI: 我去核对。",
                }
            ],
            reflection_provider=reflection_provider,
            chooser=lambda values: values[0],
        )

    result = _run(run())
    assert result["status"] == "ok"
    assert result["log"]["verdict"] == "holds"
    assert result["log"]["reason"]
    assert result["log"]["reflection_prompt_version"].startswith(
        "wm_reflection_core.v1.identity-"
    )


def test_conflict_keeps_reflection_ok_when_linked_gate_fails(tmp_path):
    path = tmp_path / "reflection.sqlite3"
    _run(_init(path))
    factory = _db_factory(path)
    identity = build_writer_identity_snapshot(
        {"ai_name": "AI", "user_name": "用户", "ai_persona": "诚实"},
        vow_block="[誓约] 不伪造",
    )

    async def pipeline(value):
        return await run_working_model_pipeline(
            value,
            db_factory=factory,
            gate_provider=lambda _messages: "not-json",
        )

    result = _run(
        run_reflection(
            _captured(identity),
            db_factory=factory,
            query_generator=lambda _clue: "她会保留不确定，不立刻要答案",
            retriever=lambda _query: [
                {
                    "id": "chunk-1",
                    "source_type": "chunk",
                    "readout_type": "raw",
                    "raw_content": "用户: 这次我想先自己想想。\nAI: 好。",
                }
            ],
            reflection_provider=lambda _messages: json.dumps(
                {
                    "verdict": "conflicts",
                    "reason": "材料显示她有时会先保留空间。",
                    "proposed_statement": "她在重要决定上有时会先保留空间。",
                },
                ensure_ascii=False,
            ),
            pipeline_runner=pipeline,
            chooser=lambda values: values[0],
        )
    )

    assert result["status"] == "ok"
    assert result["log"]["outcome"] == "ok"
    request_id = result["log"]["resulting_request_id"]
    assert request_id

    async def load_request():
        async with factory() as db:
            cursor = await db.execute(
                "SELECT status, failure_code FROM working_model_requests WHERE id=?",
                (request_id,),
            )
            return await cursor.fetchone()

    request = _run(load_request())
    assert tuple(request) == ("failed", "parse_failed")


def test_conflict_links_request_before_type_gate_and_reloads_labeled_source(tmp_path):
    path = tmp_path / "reflection.sqlite3"
    _run(_init(path))
    factory = _db_factory(path)
    identity = build_writer_identity_snapshot(
        {"ai_name": "AI", "user_name": "用户", "ai_persona": "诚实"},
        vow_block="[誓约] 不伪造",
    )

    async def seed():
        async with factory() as db:
            await db.execute(
                "INSERT INTO working_model_versions "
                "(id, previous_version_id, content, created_at, reason, flagged) "
                "VALUES (?,?,?,?,?,0)",
                ("wm_root", None, "她总要直接答案。", 1.0, "root"),
            )
            await db.execute(
                "INSERT INTO desire_versions "
                "(id, previous_version_id, content, change_note, origin_request_id, "
                "working_model_id, created_at) VALUES (?,?,?,?,?,?,?)",
                ("desire_root", None, "我想诚实地陪着她。", "root", "root", "wm_root", 1.0),
            )
            await reflection_repository.insert_log(
                db,
                log_id="refl_test",
                target_conv_id="conv_reflect",
                clue="她总要直接答案。",
                working_model_id="wm_root",
                created_at=2.0,
            )
            items = [
                {
                    "id": "chunk-1",
                    "provenance": "conversation_excerpt",
                    "label": "conversation_excerpt（对话摘录，保留双方说话者边界）",
                    "text": "用户: 这次我想先自己想想。\nAI: 好。",
                    "source_type": "chunk",
                    "origin_type": "legacy",
                    "readout_type": "raw",
                }
            ]
            await reflection_repository.update_log(
                db,
                "refl_test",
                inverse_query="她会保留不确定，不立刻要答案",
                query_model="cheap",
                query_prompt_version="query.v1",
                retrieved_items_json=json.dumps(items, ensure_ascii=False),
                verdict="conflicts",
                reason="材料显示她会保留空间。",
                proposed_statement="她在重要决定上有时会先保留空间。",
                outcome="ok",
                reflection_model="core-model",
                reflection_prompt_version="reflection.v1.identity-test",
            )
            await db.commit()

    _run(seed())
    value = capture_reflection_working_model_pipeline_input(
        conv_id="conv_reflect",
        reflection_log_id="refl_test",
        statement="她在重要决定上有时会先保留空间。",
        source=(
            "[反思线索]\n她总要直接答案。\n\n[反向检索 query]\n她会保留不确定，不立刻要答案\n\n"
            "[带来源标注的历史材料]\n[1] 来源：conversation_excerpt（对话摘录，保留双方说话者边界）；"
            "条目 id：chunk-1\n用户: 这次我想先自己想想。\nAI: 好。"
        ),
        model_key="core-model",
        identity_snapshot=identity,
    )
    expected_request_id = stable_working_model_request_id(value)

    async def gate_provider(messages):
        rendered = json.dumps(messages, ensure_ascii=False)
        assert "最近一条用户原话" not in rendered
        async with factory() as db:
            log = await reflection_repository.get_log(db, "refl_test")
        assert log["resulting_request_id"] == expected_request_id
        return '{"route":"working_model","reason":"这是跨情境理解"}'

    async def writer_provider(messages):
        rendered = json.dumps(messages, ensure_ascii=False)
        payload = json.loads(messages[-1]["content"])
        assert "original_user_message" not in payload
        assert "statement_source" not in payload
        assert "[誓约] 不伪造" in rendered
        assert "conversation_excerpt" in rendered
        assert "用户: 这次我想先自己想想。" in rendered
        return json.dumps(
            {
                "disposition": "noop",
                "working_model": "她总要直接答案。",
                "desire": "我想诚实地陪着她。",
                "change_note": "先不改。",
            },
            ensure_ascii=False,
        )

    result = _run(
        run_working_model_pipeline(
            value,
            db_factory=factory,
            gate_provider=gate_provider,
            writer_provider=writer_provider,
        )
    )
    assert result["request"]["id"] == expected_request_id
    assert result["request"]["status"] == "writer_noop"
    assert result["request"]["writer_model"] == "core-model"
    assert result["request"]["gate_prompt_version"] == "wm_reflection_type_router.v1"
    assert "conversation_excerpt" in result["request"]["source"]
    assert "她会保留不确定，不立刻要答案" not in result["request"]["source"]
    async def load_log():
        async with factory() as db:
            return await reflection_repository.get_log(db, "refl_test")

    log = _run(load_log())
    assert log["outcome"] == "ok"
    assert log["resulting_request_id"] == expected_request_id
