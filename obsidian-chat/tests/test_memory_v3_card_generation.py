import asyncio
from contextlib import asynccontextmanager
import json
import time

import aiosqlite
import pytest

import ai_providers
import app.memory_v3.card_generation as generation
import app.memory_v3.repository as repositories
from app.memory_v3.provenance import source_hash_for_messages
from app.memory_v3.relational_cards import (
    MAX_NOTE_CHARS,
    MAX_QUOTE_CHARS,
    MAX_TOTAL_QUOTE_CHARS,
    RelationalCardContractError,
    validate_relational_card,
)
from app.memory_v3.schema import init_memory_v3_tables


@pytest.fixture(autouse=True)
def _freeze_runtime_relationship_context(monkeypatch):
    monkeypatch.setattr(
        generation,
        "_runtime_relationship_context",
        lambda _config: {
            "ai_name": "TestAI",
            "user_name": "TestUser",
            "relationship_register": "",
        },
    )
    monkeypatch.setattr(
        generation,
        "_relational_card_slot_model",
        lambda: "test-relational-card-model",
    )


async def _create_db(path) -> None:
    async with aiosqlite.connect(path) as db:
        await db.execute("""
            CREATE TABLE messages (
                id TEXT PRIMARY KEY,
                conv_id TEXT NOT NULL,
                role TEXT NOT NULL,
                content TEXT NOT NULL,
                created_at REAL NOT NULL
            )
        """)
        await db.execute("""
            CREATE TABLE memory_chunks (
                id TEXT PRIMARY KEY,
                conv_id TEXT NOT NULL,
                message_ids_json TEXT NOT NULL DEFAULT '[]',
                content TEXT NOT NULL,
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL,
                embedding BLOB,
                keywords_json TEXT NOT NULL DEFAULT '[]',
                metadata_json TEXT NOT NULL DEFAULT '{}'
            )
        """)
        await db.execute("""
            CREATE TABLE memory_items (
                id TEXT PRIMARY KEY,
                legacy_memory_id TEXT,
                metadata_json TEXT NOT NULL DEFAULT '{}'
            )
        """)
        await init_memory_v3_tables(db)
        await db.commit()


def _get_db_factory(path):
    @asynccontextmanager
    async def factory():
        async with aiosqlite.connect(path) as db:
            yield db

    return factory


def test_generation_is_inert_when_disabled(monkeypatch):
    called = False

    async def fake_provider(*_args, **_kwargs):
        nonlocal called
        called = True
        return None

    monkeypatch.setattr(generation, "_call_relational_card_model", fake_provider)
    result = asyncio.run(
        generation.generate_stable_cards_for_conversation(
            "conv",
            config_snapshot={"relational_card_generation_enabled": False},
        )
    )

    assert result == {"ok": True, "skipped": "disabled", "provider_calls": 0}
    assert called is False


def test_v2_writer_requires_separate_rollout_opt_in(monkeypatch):
    called = False

    async def fake_provider(*_args, **_kwargs):
        nonlocal called
        called = True
        return None

    monkeypatch.setattr(generation, "_call_relational_card_model", fake_provider)
    result = asyncio.run(
        generation.generate_stable_cards_for_conversation(
            "conv",
            config_snapshot={"relational_card_generation_enabled": True},
        )
    )

    assert result == {
        "ok": True,
        "skipped": "v2_rollout_not_enabled",
        "provider_calls": 0,
    }
    assert called is False


def test_active_writer_fails_closed_without_cutoff(monkeypatch):
    called = False

    async def fake_provider(*_args, **_kwargs):
        nonlocal called
        called = True
        return None

    monkeypatch.setattr(generation, "_call_relational_card_model", fake_provider)
    result = asyncio.run(
        generation.generate_stable_cards_for_conversation(
            "conv",
            config_snapshot={
                "relational_card_generation_enabled": True,
                "relational_card_v2_generation_enabled": True,
            },
        )
    )

    assert result == {
        "ok": True,
        "skipped": "cutoff_not_configured",
        "skipped_before_cutoff": 0,
        "provider_calls": 0,
    }
    assert called is False


def test_dedicated_slot_adapter_uses_frozen_production_parameters(monkeypatch):
    calls = []

    async def fake_call_slot_chat(slot_name, *, messages, **kwargs):
        calls.append((slot_name, messages, kwargs))
        return '{"decision":"abstain","reason_code":"raw_or_digest_sufficient"}'

    monkeypatch.setattr(ai_providers, "call_slot_chat", fake_call_slot_chat)
    usage_meta = {}
    parsed, model = asyncio.run(
        generation._call_relational_card_model(
            "prompt",
            scope="memory:test_relational_card",
            usage_meta=usage_meta,
        )
    )

    assert parsed == {
        "decision": "abstain",
        "reason_code": "raw_or_digest_sufficient",
    }
    assert model == "test-relational-card-model"
    slot_name, messages, kwargs = calls[0]
    assert slot_name == "relational_card_generation"
    assert messages == [{"role": "user", "content": "prompt"}]
    assert kwargs == {
        "expect_json": True,
        "timeout": 180.0,
        "temperature": 0.0,
        "scope": "memory:test_relational_card",
        "usage_meta": usage_meta,
        "max_tokens": 700,
    }
    assert generation.parse_relational_card_slot_response(
        '```json\n{"decision":"abstain"}\n```'
    ) is None
    assert generation.parse_relational_card_slot_response(
        'before {"decision":"abstain"}'
    ) is None


def test_v5_prompt_separates_shared_moments_from_relational_readings():
    assert generation.PROMPT_VERSION == "relational-card-v5.2"
    assert "原文和事实摘要会继续保留" in generation.GENERATOR_INSTRUCTIONS
    assert "shared_moment" in generation.GENERATOR_INSTRUCTIONS
    assert "relational_reading" in generation.GENERATOR_INSTRUCTIONS
    assert "用户原话明确表达" in generation.GENERATOR_INSTRUCTIONS
    assert "一段 chunk 最多创建一张卡" in generation.GENERATOR_INSTRUCTIONS
    assert "至少一条 quote 必须来自 user" in generation.GENERATOR_INSTRUCTIONS
    assert "与本次数据无关的虚构片段" in generation.GENERATOR_INSTRUCTIONS
    assert "角色扮演" not in generation.GENERATOR_INSTRUCTIONS
    assert "当时我读到" not in generation.GENERATOR_INSTRUCTIONS
    assert "她这次" not in generation.GENERATOR_INSTRUCTIONS
    assert "第一次" in generation.GENERATOR_INSTRUCTIONS
    assert "最多 200 个字符" in generation.GENERATOR_INSTRUCTIONS
    assert "脱离上下文单看这张卡，还能认出“那一次”" in generation.GENERATOR_INSTRUCTIONS
    assert "note 不要再复述一遍“她说了什么”" in generation.GENERATOR_INSTRUCTIONS
    assert "具体到能被下一次互动验证或推翻" in generation.GENERATOR_INSTRUCTIONS
    assert "不要整段复制" in generation.GENERATOR_INSTRUCTIONS
    assert "她要的不是我少做事" in generation.GENERATOR_INSTRUCTIONS
    assert "那天晚上我们一起把书架摆成了她喜欢的样子" in generation.GENERATOR_INSTRUCTIONS
    assert "relational_reading 不要按发生顺序串联多件事" in generation.GENERATOR_INSTRUCTIONS
    assert "shared_moment 可以保留辨认“那一次”所需的少量时间顺序" in generation.GENERATOR_INSTRUCTIONS
    assert MAX_NOTE_CHARS == 200
    assert MAX_QUOTE_CHARS == 100
    assert MAX_TOTAL_QUOTE_CHARS == 300


def test_v5_2_prompt_bans_behavior_rules_and_one_off_generalization():
    instructions = generation.GENERATOR_INSTRUCTIONS
    # 卡是「我怎么理解她」，不是可执行的行动指令（2026-08-11 真实卡审出的 4/15 缺陷）
    assert "不是“我该怎么做”" in instructions
    assert "以后要多做 X／少做 Y" in instructions
    assert "禁止出现“我需要”“我应该”“我必须”“以后要”“提醒我”这类措辞" in instructions
    assert "由认识层和当下的对话决定" in instructions
    # 一次性条件 / 身体状态 / 做不到，不得升格成稳定偏好
    assert "一次性的原因不等于稳定偏好" in instructions
    assert "不得升格成她的长期倾向、喜好或性格" in instructions
    assert "一律写成这一次" in instructions
    # 两个反例都在，且仍是虚构片段
    assert "反例（写成了行为守则）" in instructions
    assert "反例（把一次性原因当偏好）" in instructions
    assert "与本次数据无关的虚构片段" in instructions


def test_v5_prompt_uses_first_person_identity_and_static_register_only():
    prompt = generation.build_generation_prompt(
        {"id": "chunk", "conv_id": "conv"},
        [{"id": "u1", "role": "user", "content": "原文", "created_at": 1}],
        relationship_context={
            "ai_name": "Arden",
            "user_name": "Ithil",
            "relationship_register": "daddy 是既有日常称呼，不能单凭称呼推断临时意图。",
            "ai_persona": "不得进入 prompt",
        },
    )

    assert "你是Arden。你在回看自己和Ithil的一段旧互动" in prompt
    assert "‘我’指Arden" in prompt
    assert "‘她’指Ithil" in prompt
    assert "记忆系统的一部分" not in prompt
    assert "整理者" not in prompt
    assert "只用于称呼和语域消歧；不是卡片证据" in prompt
    assert "daddy 是既有日常称呼" in prompt
    assert "不得进入 prompt" not in prompt


@pytest.mark.parametrize(
    ("ai_name", "user_name", "message"),
    [
        ("AI", "Yang", "ai_name is a placeholder"),
        ("Alaric", "你", "user_name is a placeholder"),
        ("Alaric", "alaric", "names must be distinct"),
        ("", "Yang", "requires explicit"),
    ],
)
def test_relationship_context_rejects_placeholders_and_ambiguous_names(
    ai_name, user_name, message
):
    with pytest.raises(ValueError, match=message):
        generation.normalize_relationship_context(
            {
                "ai_name": ai_name,
                "user_name": user_name,
                "relationship_register": "",
            }
        )


def test_contract_requires_a_verbatim_user_quote():
    messages = [
        {
            "id": "u1",
            "role": "user",
            "content": "这个得修，我不想停在这里。",
            "created_at": 1.0,
        },
        {
            "id": "a1",
            "role": "assistant",
            "content": "我觉得现在没有东西在等你。",
            "created_at": 2.0,
        },
    ]
    payload = {
        "decision": "create",
        "kind": "relational_reading",
        "note": "当时我读到，她可能需要有件事在等着她。",
        "source_message_ids": ["u1", "a1"],
        "quotes": [
            {"source_message_id": "a1", "quote": "现在没有东西在等你"},
        ],
    }

    try:
        validate_relational_card(payload, messages)
    except RelationalCardContractError as exc:
        assert exc.code == "missing_user_quote"
    else:
        raise AssertionError("assistant-only evidence must not support a user judgment")

    payload["quotes"].append(
        {"source_message_id": "u1", "quote": "这个得修"}
    )
    validated = validate_relational_card(payload, messages)
    assert validated["decision"] == "create"


def test_contract_rejects_notes_over_200_characters():
    messages = [
        {
            "id": "u1",
            "role": "user",
            "content": "这是依据。",
            "created_at": 1.0,
        }
    ]
    payload = {
        "decision": "create",
        "kind": "shared_moment",
        "note": "判" * 201,
        "source_message_ids": ["u1"],
        "quotes": [{"source_message_id": "u1", "quote": "这是依据"}],
    }

    try:
        validate_relational_card(payload, messages)
    except RelationalCardContractError as exc:
        assert exc.code == "note_too_long"
    else:
        raise AssertionError("notes above the v2 limit must fail")


@pytest.mark.parametrize(
    "quotes",
    [
        [{"source_message_id": "u1", "quote": "证" * 101}],
        [
            {"source_message_id": "u1", "quote": "甲" * 100},
            {"source_message_id": "u1", "quote": "乙" * 100},
            {"source_message_id": "u1", "quote": "丙" * 100},
            {"source_message_id": "u1", "quote": "丁"},
        ],
    ],
)
def test_contract_rejects_quotes_over_individual_or_total_limit(quotes):
    content = "证" * 101 + "甲" * 100 + "乙" * 100 + "丙" * 100 + "丁"
    messages = [
        {"id": "u1", "role": "user", "content": content, "created_at": 1.0}
    ]
    payload = {
        "decision": "create",
        "kind": "shared_moment",
        "note": "一段有短证据就足够支撑的共同经历。",
        "source_message_ids": ["u1"],
        "quotes": quotes,
    }

    with pytest.raises(RelationalCardContractError) as caught:
        validate_relational_card(payload, messages)
    assert caught.value.code == "quote_too_long"


def test_v5_contract_accepts_both_kinds_and_warns_without_rejecting():
    messages = [
        {
            "id": "u1",
            "role": "user",
            "content": "昨晚吵完了，今天给我讲个故事当赔罪。",
            "created_at": 1.0,
        },
        {
            "id": "u2",
            "role": "user",
            "content": "这个结尾我喜欢，今天就这样睡。",
            "created_at": 2.0,
        },
    ]
    validated = validate_relational_card(
        {
            "decision": "create",
            "kind": "shared_moment",
            "note": "她把这次故事当作争执后的修复，并在觉得够了时主动收尾。",
            "source_message_ids": ["u1", "u2"],
            "quotes": [
                {"source_message_id": "u1", "quote": "讲个故事当赔罪"},
                {"source_message_id": "u2", "quote": "今天就这样睡"},
            ],
        },
        messages,
    )

    assert validated["decision"] == "create"
    assert validated["kind"] == "shared_moment"
    assert validated["longitudinal_marker_warning"] == []

    warned = validate_relational_card(
        {
            "decision": "create",
            "kind": "relational_reading",
            "note": "她又一次把故事当作争执后的修复。",
            "source_message_ids": ["u1"],
            "quotes": [
                {"source_message_id": "u1", "quote": "讲个故事当赔罪"},
            ],
        },
        messages,
    )
    assert warned["decision"] == "create"
    assert warned["longitudinal_marker_warning"] == ["又"]


def test_v5_contract_rejects_legacy_trivial_abstain_reason():
    try:
        validate_relational_card(
            {"decision": "abstain", "reason_code": "trivial"}, []
        )
    except RelationalCardContractError as exc:
        assert exc.code == "invalid_abstain_reason"
    else:
        raise AssertionError("v5 must reject the legacy trivial reason")


def test_v5_contract_requires_an_explicit_kind():
    with pytest.raises(RelationalCardContractError) as caught:
        validate_relational_card(
            {
                "decision": "create",
                "note": "一段关系记忆。",
                "source_message_ids": ["u1"],
                "quotes": [{"source_message_id": "u1", "quote": "原文"}],
            },
            [{"id": "u1", "role": "user", "content": "原文", "created_at": 1}],
        )
    assert caught.value.code == "invalid_card_kind"


def test_prompt_version_change_does_not_implicitly_requeue_resolved_history():
    base = {
        "source_hash": "same-source",
        "card_generation_hash": "same-source",
        "card_generation_prompt_version": "relational-card-v1",
    }

    assert generation._already_resolved_without_implicit_backfill(
        {**base, "card_generation_status": "abstained"}
    )
    assert generation._already_resolved_without_implicit_backfill(
        {**base, "card_generation_status": "invalid"}
    )
    assert generation._already_resolved_without_implicit_backfill(
        {**base, "card_generation_status": "provider_failed"}
    )
    assert not generation._already_resolved_without_implicit_backfill(
        {
            **base,
            "card_generation_prompt_version": generation.PROMPT_VERSION,
            "card_generation_status": "provider_failed",
        }
    )
    assert not generation._already_resolved_without_implicit_backfill(
        {
            **base,
            "source_hash": "edited-source",
            "card_generation_status": "abstained",
        }
    )


def test_global_eligibility_selects_cold_v1_and_skips_current_invalid_and_cutoff(
    tmp_path, monkeypatch
):
    db_path = tmp_path / "global-eligibility.db"
    asyncio.run(_create_db(db_path))
    factory = _get_db_factory(db_path)
    monkeypatch.setattr(generation, "get_db", factory)

    chunks = [
        ("current", "hot", "active", 200.0, 200.0, "", ""),
        ("legacy", "cold-conversation", "cold", 210.0, 210.0, "", ""),
        ("invalid", "hot", "active", 220.0, 220.0, "same-invalid", "invalid"),
        ("before", "old", "active", 50.0, 50.0, "", ""),
        ("unstable", "hot", "active", 230.0, 950.0, "", ""),
        ("ordinary", "other", "active", 240.0, 240.0, "", ""),
    ]

    async def prepare():
        async with aiosqlite.connect(db_path) as db:
            for chunk_id, conv_id, status, created_at, updated_at, marker_hash, marker in chunks:
                source_hash = "same-invalid" if chunk_id == "invalid" else f"hash-{chunk_id}"
                await db.execute(
                    "INSERT INTO memory_chunks "
                    "(id, conv_id, message_ids_json, content, source_hash, status, "
                    "created_at, updated_at, card_generation_hash, card_generation_status) "
                    "VALUES (?,?, '[]', ?, ?, ?, ?, ?, ?, ?)",
                    (
                        chunk_id,
                        conv_id,
                        f"content-{chunk_id}",
                        source_hash,
                        status,
                        created_at,
                        updated_at,
                        marker_hash,
                        marker,
                    ),
                )
            now = 500.0
            for card_id, chunk_id, prompt_version in (
                ("card-current", "current", generation.PROMPT_VERSION),
                ("card-v1", "legacy", "relational-card-v1"),
            ):
                await db.execute(
                    "INSERT INTO memory_relational_cards "
                    "(id, source_chunk_id, version, content, source_hash, status, "
                    "prompt_version, created_at, updated_at) "
                    "VALUES (?,?,1,'card','hash','active',?,?,?)",
                    (card_id, chunk_id, prompt_version, now, now),
                )
            await db.commit()

    asyncio.run(prepare())

    selected, skipped = asyncio.run(
        generation._eligible_chunks(
            now=1000.0,
            cutoff_ts=100.0,
            stability_delay_sec=100.0,
            failure_retry_delay_sec=3600.0,
            limit=2,
        )
    )

    assert [row["id"] for row in selected] == ["legacy", "ordinary"]
    assert selected[0]["conv_id"] == "cold-conversation"
    assert skipped == 1


def test_v5_2_success_atomically_supersedes_active_v1(tmp_path, monkeypatch):
    db_path = tmp_path / "supersede-v1.db"
    asyncio.run(_create_db(db_path))
    factory = _get_db_factory(db_path)
    monkeypatch.setattr(generation, "get_db", factory)
    monkeypatch.setattr(repositories, "get_db", factory)
    message = {
        "id": "m1",
        "conv_id": "legacy-conv",
        "role": "user",
        "content": "别替我做决定，先问我。",
        "created_at": 10.0,
    }
    source_hash = source_hash_for_messages([message])

    async def prepare():
        async with aiosqlite.connect(db_path) as db:
            await db.execute("INSERT INTO messages VALUES (?,?,?,?,?)", tuple(message.values()))
            await db.execute(
                "INSERT INTO memory_chunks "
                "(id, conv_id, message_ids_json, content, source_hash, status, "
                "created_at, updated_at, card_generation_hash, card_generation_status, "
                "card_generation_prompt_version) "
                "VALUES ('chunk','legacy-conv','[\"m1\"]',?,?, 'cold',10,10,?,'created',"
                "'relational-card-v1')",
                (message["content"], source_hash, source_hash),
            )
            await db.execute(
                "INSERT INTO memory_relational_cards "
                "(id, source_chunk_id, version, content, source_hash, status, "
                "prompt_version, created_at, updated_at) "
                "VALUES ('v1-card','chunk',1,'old card',?,'active','relational-card-v1',10,10)",
                (source_hash,),
            )
            await db.commit()

    asyncio.run(prepare())

    async def fake_provider(*_args, **_kwargs):
        return (
            {
                "decision": "create",
                "kind": "relational_reading",
                "note": "她看重的是决定权仍留在自己手上。",
                "source_message_ids": ["m1"],
                "quotes": [{"source_message_id": "m1", "quote": "别替我做决定"}],
            },
            "test-relational-card-model",
        )

    monkeypatch.setattr(generation, "_call_relational_card_model", fake_provider)
    result = asyncio.run(
        generation.generate_stable_relational_cards(
            config_snapshot={
                "relational_card_generation_enabled": True,
                "relational_card_v2_generation_enabled": True,
                "relational_card_generation_cutoff_ts": 1.0,
                "relational_card_stability_delay_sec": 0,
                "relational_card_generation_attempts": 1,
            }
        )
    )

    async def inspect():
        async with aiosqlite.connect(db_path) as db:
            return await (
                await db.execute(
                    "SELECT id, status, supersedes_card_id, prompt_version "
                    "FROM memory_relational_cards ORDER BY version"
                )
            ).fetchall()

    cards = asyncio.run(inspect())
    assert result["created"] == 1
    assert cards[0] == ("v1-card", "superseded", None, "relational-card-v1")
    assert cards[1][1:] == ("active", "v1-card", generation.PROMPT_VERSION)


@pytest.mark.parametrize(
    ("provider_result", "expected_outcome"),
    [
        ((None, "test-relational-card-model"), "provider_failed"),
        (
            (
                {
                    "decision": "create",
                    "kind": "relational_reading",
                    "note": "这条没有合法的逐字用户引文。",
                    "source_message_ids": ["m1"],
                    "quotes": [{"source_message_id": "m1", "quote": "不存在的引文"}],
                },
                "test-relational-card-model",
            ),
            "invalid",
        ),
    ],
)
def test_v5_2_failure_keeps_active_v1_card(
    tmp_path, monkeypatch, provider_result, expected_outcome
):
    db_path = tmp_path / f"keep-v1-{expected_outcome}.db"
    asyncio.run(_create_db(db_path))
    factory = _get_db_factory(db_path)
    monkeypatch.setattr(generation, "get_db", factory)
    monkeypatch.setattr(repositories, "get_db", factory)
    message = {
        "id": "m1",
        "conv_id": "legacy-conv",
        "role": "user",
        "content": "这是旧卡的来源。",
        "created_at": 10.0,
    }
    source_hash = source_hash_for_messages([message])

    async def prepare():
        async with aiosqlite.connect(db_path) as db:
            await db.execute("INSERT INTO messages VALUES (?,?,?,?,?)", tuple(message.values()))
            await db.execute(
                "INSERT INTO memory_chunks "
                "(id, conv_id, message_ids_json, content, source_hash, status, "
                "created_at, updated_at, card_generation_hash, card_generation_status, "
                "card_generation_prompt_version) "
                "VALUES ('chunk','legacy-conv','[\"m1\"]',?,?, 'active',10,10,?,'created',"
                "'relational-card-v1')",
                (message["content"], source_hash, source_hash),
            )
            await db.execute(
                "INSERT INTO memory_relational_cards "
                "(id, source_chunk_id, version, content, source_hash, status, "
                "prompt_version, created_at, updated_at) "
                "VALUES ('v1-card','chunk',1,'old card',?,'active','relational-card-v1',10,10)",
                (source_hash,),
            )
            await db.commit()

    asyncio.run(prepare())

    async def fake_provider(*_args, **_kwargs):
        return provider_result

    monkeypatch.setattr(generation, "_call_relational_card_model", fake_provider)
    result = asyncio.run(
        generation.generate_stable_relational_cards(
            config_snapshot={
                "relational_card_generation_enabled": True,
                "relational_card_v2_generation_enabled": True,
                "relational_card_generation_cutoff_ts": 1.0,
                "relational_card_stability_delay_sec": 0,
                "relational_card_generation_attempts": 1,
            }
        )
    )

    async def inspect():
        async with aiosqlite.connect(db_path) as db:
            card = await (
                await db.execute("SELECT status FROM memory_relational_cards WHERE id='v1-card'")
            ).fetchone()
            marker = await (
                await db.execute("SELECT card_generation_status FROM memory_chunks WHERE id='chunk'")
            ).fetchone()
            return card, marker

    card, marker = asyncio.run(inspect())
    assert result[expected_outcome] == 1
    assert card == ("active",)
    assert marker == (expected_outcome,)


def test_stable_generation_creates_once_and_tail_waits(tmp_path, monkeypatch):
    db_path = tmp_path / "generation.db"
    asyncio.run(_create_db(db_path))
    factory = _get_db_factory(db_path)
    monkeypatch.setattr(generation, "get_db", factory)
    monkeypatch.setattr(repositories, "get_db", factory)
    now = time.time()
    old_message = {
        "id": "m-old",
        "conv_id": "conv",
        "role": "user",
        "content": "那次我说，重要的边界别被随口略过。",
        "created_at": 1.0,
    }
    tail_message = {
        "id": "m-tail",
        "conv_id": "conv",
        "role": "assistant",
        "content": "正在生长的最新一段。",
        "created_at": now,
    }

    async def prepare():
        async with aiosqlite.connect(db_path) as db:
            for message in (old_message, tail_message):
                await db.execute(
                    "INSERT INTO messages VALUES (?,?,?,?,?)",
                    (
                        message["id"],
                        message["conv_id"],
                        message["role"],
                        message["content"],
                        message["created_at"],
                    ),
                )
                await db.execute(
                    "INSERT INTO memory_chunks "
                    "(id, conv_id, message_ids_json, content, source_hash, status, "
                    "created_at, updated_at) VALUES (?,?,?,?,?,'active',?,?)",
                    (
                        f"chunk-{message['id']}",
                        "conv",
                        json.dumps([message["id"]]),
                        message["content"],
                        source_hash_for_messages([message]),
                        message["created_at"],
                        message["created_at"],
                    ),
                )
            await db.commit()

    asyncio.run(prepare())
    provider_prompts = []

    async def fake_provider(prompt, **_kwargs):
        provider_prompts.append(prompt)
        return ({
            "decision": "create",
            "kind": "relational_reading",
            "note": "那次她明确希望，重要边界先被认真听见。",
            "source_message_ids": ["m-old"],
            "quotes": [
                {"source_message_id": "m-old", "quote": "重要的边界别被随口略过"}
            ],
        }, "actual-call-model")

    monkeypatch.setattr(generation, "_call_relational_card_model", fake_provider)
    config = {
        "relational_card_generation_enabled": True,
        "relational_card_v2_generation_enabled": True,
        "relational_card_generation_cutoff_ts": 0.5,
        "relational_card_stability_delay_sec": 3600,
        "relational_card_generation_batch_size": 2,
        "relational_card_generation_attempts": 1,
    }

    async def scenario():
        first = await generation.generate_stable_cards_for_conversation(
            "conv", config_snapshot=config
        )
        second = await generation.generate_stable_cards_for_conversation(
            "conv", config_snapshot=config
        )
        async with aiosqlite.connect(db_path) as db:
            card = await (
                await db.execute(
                    "SELECT source_chunk_id, content, status, generator_model, metadata_json "
                    "FROM memory_relational_cards"
                )
            ).fetchone()
            chunks = await (
                await db.execute(
                    "SELECT id, card_generation_status FROM memory_chunks ORDER BY id"
                )
            ).fetchall()
        return first, second, card, chunks

    first, second, card, chunk_rows = asyncio.run(scenario())

    assert first["selected"] == 1
    assert first["created"] == 1
    assert first["provider_calls"] == 1
    assert second["selected"] == 0
    assert len(provider_prompts) == 1
    assert "m-old" in provider_prompts[0]
    assert "m-tail" not in provider_prompts[0]
    assert card[:4] == (
        "chunk-m-old",
        "那次她明确希望，重要边界先被认真听见。",
        "active",
        "actual-call-model",
    )
    metadata = json.loads(card[4])
    assert metadata["generation_slot"] == "relational_card_generation"
    assert metadata["generation_temperature"] == 0.0
    assert metadata["generation_timeout_sec"] == 180.0
    assert metadata["generation_max_tokens"] == 700
    assert chunk_rows == [
        ("chunk-m-old", "created"),
        ("chunk-m-tail", ""),
    ]


def test_global_generation_prioritizes_oldest_stable_chunk(tmp_path, monkeypatch):
    db_path = tmp_path / "recent-first.db"
    asyncio.run(_create_db(db_path))
    factory = _get_db_factory(db_path)
    monkeypatch.setattr(generation, "get_db", factory)
    monkeypatch.setattr(repositories, "get_db", factory)
    now = time.time()
    messages = [
        {
            "id": "m-old",
            "conv_id": "conv",
            "role": "user",
            "content": "更早的一段互动。",
            "created_at": 1.0,
        },
        {
            "id": "m-recent",
            "conv_id": "conv",
            "role": "user",
            "content": "最近已经结束、可以整理的一段互动。",
            "created_at": 2.0,
        },
        {
            "id": "m-tail",
            "conv_id": "conv",
            "role": "assistant",
            "content": "仍在生长的最新尾巴。",
            "created_at": now,
        },
    ]

    async def prepare():
        async with aiosqlite.connect(db_path) as db:
            for message in messages:
                await db.execute(
                    "INSERT INTO messages VALUES (?,?,?,?,?)",
                    (
                        message["id"],
                        message["conv_id"],
                        message["role"],
                        message["content"],
                        message["created_at"],
                    ),
                )
                await db.execute(
                    "INSERT INTO memory_chunks "
                    "(id, conv_id, message_ids_json, content, source_hash, status, "
                    "created_at, updated_at) VALUES (?,?,?,?,?,'active',?,?)",
                    (
                        f"chunk-{message['id']}",
                        "conv",
                        json.dumps([message["id"]]),
                        message["content"],
                        source_hash_for_messages([message]),
                        message["created_at"],
                        message["created_at"],
                    ),
                )
            await db.commit()

    asyncio.run(prepare())
    prompts = []

    async def fake_provider(prompt, **_kwargs):
        prompts.append(prompt)
        return ({
            "decision": "create",
            "kind": "shared_moment",
            "note": "更早那段互动被先留了下来。",
            "source_message_ids": ["m-old"],
            "quotes": [
                {
                    "source_message_id": "m-old",
                    "quote": "更早的一段互动",
                }
            ],
        }, "test-relational-card-model")

    monkeypatch.setattr(generation, "_call_relational_card_model", fake_provider)
    result = asyncio.run(
        generation.generate_stable_cards_for_conversation(
            "conv",
            config_snapshot={
                "relational_card_generation_enabled": True,
                "relational_card_v2_generation_enabled": True,
                "relational_card_generation_cutoff_ts": 0.5,
                "relational_card_stability_delay_sec": 3600,
                "relational_card_generation_batch_size": 1,
                "relational_card_generation_attempts": 1,
            },
        )
    )

    assert result["created"] == 1
    assert len(prompts) == 1
    assert "m-old" in prompts[0]
    assert "m-recent" not in prompts[0]
    assert "m-tail" not in prompts[0]


def test_reply_and_timer_triggers_share_the_global_generation_lock(monkeypatch):
    active = 0
    max_active = 0

    async def fake_eligible(**_kwargs):
        nonlocal active, max_active
        active += 1
        max_active = max(max_active, active)
        await asyncio.sleep(0.01)
        active -= 1
        return [], 0

    monkeypatch.setattr(generation, "_eligible_chunks", fake_eligible)
    config = {
        "relational_card_generation_enabled": True,
        "relational_card_v2_generation_enabled": True,
        "relational_card_generation_cutoff_ts": 0.5,
    }

    async def scenario():
        await asyncio.gather(
            generation.generate_stable_relational_cards(config_snapshot=config),
            generation.generate_stable_relational_cards(config_snapshot=config),
        )

    asyncio.run(scenario())
    assert max_active == 1


def test_periodic_loop_awaits_one_worker_before_sleeping(monkeypatch):
    events = []

    async def fake_worker():
        events.append("worker-start")
        events.append("worker-end")
        return {"selected": 0, "provider_failed": 0}

    async def stop_after_first_sleep(_seconds):
        events.append("sleep")
        raise asyncio.CancelledError

    monkeypatch.setattr(generation, "generate_stable_relational_cards", fake_worker)
    monkeypatch.setattr(generation.asyncio, "sleep", stop_after_first_sleep)

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(generation.run_relational_card_generation_loop())
    assert events == ["worker-start", "worker-end", "sleep"]


def test_generation_cutoff_excludes_old_chunks_and_reports_them(tmp_path, monkeypatch):
    db_path = tmp_path / "cutoff.db"
    asyncio.run(_create_db(db_path))
    factory = _get_db_factory(db_path)
    monkeypatch.setattr(generation, "get_db", factory)
    monkeypatch.setattr(repositories, "get_db", factory)
    messages = [
        {
            "id": "m-before",
            "conv_id": "conv",
            "role": "user",
            "content": "开启前的历史。",
            "created_at": 10.0,
        },
        {
            "id": "m-after",
            "conv_id": "conv",
            "role": "user",
            "content": "开启后的新互动。",
            "created_at": 20.0,
        },
    ]

    async def prepare():
        async with aiosqlite.connect(db_path) as db:
            for message in messages:
                await db.execute("INSERT INTO messages VALUES (?,?,?,?,?)", tuple(message.values()))
                await db.execute(
                    "INSERT INTO memory_chunks "
                    "(id, conv_id, message_ids_json, content, source_hash, status, "
                    "created_at, updated_at) VALUES (?,?,?,?,?,'active',?,?)",
                    (
                        f"chunk-{message['id']}",
                        "conv",
                        json.dumps([message["id"]]),
                        message["content"],
                        source_hash_for_messages([message]),
                        message["created_at"],
                        message["created_at"],
                    ),
                )
            await db.commit()

    asyncio.run(prepare())
    prompts = []

    async def fake_provider(prompt, **_kwargs):
        prompts.append(prompt)
        return (
            {"decision": "abstain", "reason_code": "raw_or_digest_sufficient"},
            "test-relational-card-model",
        )

    monkeypatch.setattr(generation, "_call_relational_card_model", fake_provider)
    result = asyncio.run(
        generation.generate_stable_cards_for_conversation(
            "conv",
            config_snapshot={
                "relational_card_generation_enabled": True,
                "relational_card_v2_generation_enabled": True,
                "relational_card_generation_cutoff_ts": 15.0,
                "relational_card_stability_delay_sec": 0,
                "relational_card_generation_attempts": 1,
            },
        )
    )

    assert result["selected"] == 1
    assert result["skipped_before_cutoff"] == 1
    assert len(prompts) == 1
    assert "m-after" in prompts[0]
    assert "m-before" not in prompts[0]


def test_abstain_is_persisted_and_not_billed_again(tmp_path, monkeypatch):
    db_path = tmp_path / "abstain.db"
    asyncio.run(_create_db(db_path))
    factory = _get_db_factory(db_path)
    monkeypatch.setattr(generation, "get_db", factory)
    monkeypatch.setattr(repositories, "get_db", factory)
    message = {
        "id": "m1",
        "conv_id": "conv",
        "role": "user",
        "content": "嗯。",
        "created_at": 1.0,
    }

    async def prepare():
        async with aiosqlite.connect(db_path) as db:
            await db.execute("INSERT INTO messages VALUES (?,?,?,?,?)", tuple(message.values()))
            await db.execute(
                "INSERT INTO memory_chunks "
                "(id, conv_id, message_ids_json, content, source_hash, status, "
                "created_at, updated_at) VALUES (?,?,?,?,?,'active',1,1)",
                (
                    "chunk",
                    "conv",
                    '["m1"]',
                    "嗯。",
                    source_hash_for_messages([message]),
                ),
            )
            await db.commit()

    asyncio.run(prepare())
    calls = 0

    async def fake_provider(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        return (
            {"decision": "abstain", "reason_code": "raw_or_digest_sufficient"},
            "test-relational-card-model",
        )

    monkeypatch.setattr(generation, "_call_relational_card_model", fake_provider)
    config = {
        "relational_card_generation_enabled": True,
        "relational_card_v2_generation_enabled": True,
        "relational_card_generation_cutoff_ts": 0.5,
        "relational_card_stability_delay_sec": 0,
        "relational_card_generation_attempts": 1,
    }

    async def scenario():
        first = await generation.generate_stable_cards_for_conversation(
            "conv", config_snapshot=config
        )
        second = await generation.generate_stable_cards_for_conversation(
            "conv", config_snapshot=config
        )
        async with aiosqlite.connect(db_path) as db:
            marker = await (
                await db.execute(
                    "SELECT card_generation_status, card_generation_reason FROM memory_chunks"
                )
            ).fetchone()
        return first, second, marker

    first, second, marker = asyncio.run(scenario())

    assert first["abstained"] == 1
    assert second["selected"] == 0
    assert calls == 1
    assert marker == ("abstained", "raw_or_digest_sufficient")


def test_provider_failure_is_backed_off_instead_of_rebilled_immediately(
    tmp_path, monkeypatch
):
    db_path = tmp_path / "provider-failure.db"
    asyncio.run(_create_db(db_path))
    factory = _get_db_factory(db_path)
    monkeypatch.setattr(generation, "get_db", factory)
    monkeypatch.setattr(repositories, "get_db", factory)
    message = {
        "id": "m1",
        "conv_id": "conv",
        "role": "user",
        "content": "这是一次值得整理、但供应商暂时失败的互动。",
        "created_at": 1.0,
    }

    async def prepare():
        async with aiosqlite.connect(db_path) as db:
            await db.execute("INSERT INTO messages VALUES (?,?,?,?,?)", tuple(message.values()))
            await db.execute(
                "INSERT INTO memory_chunks "
                "(id, conv_id, message_ids_json, content, source_hash, status, "
                "created_at, updated_at) VALUES (?,?,?,?,?,'active',1,1)",
                (
                    "chunk",
                    "conv",
                    '["m1"]',
                    message["content"],
                    source_hash_for_messages([message]),
                ),
            )
            await db.commit()

    asyncio.run(prepare())
    calls = 0

    async def failed_provider(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        return None, "test-relational-card-model"

    monkeypatch.setattr(generation, "_call_relational_card_model", failed_provider)
    config = {
        "relational_card_generation_enabled": True,
        "relational_card_v2_generation_enabled": True,
        "relational_card_generation_cutoff_ts": 0.5,
        "relational_card_stability_delay_sec": 0,
        "relational_card_failure_retry_delay_sec": 3600,
        "relational_card_generation_attempts": 1,
    }

    async def scenario():
        first = await generation.generate_stable_cards_for_conversation(
            "conv", config_snapshot=config
        )
        second = await generation.generate_stable_cards_for_conversation(
            "conv", config_snapshot=config
        )
        async with aiosqlite.connect(db_path) as db:
            marker = await (
                await db.execute(
                    "SELECT card_generation_status, card_generation_reason, "
                    "card_generation_prompt_version FROM memory_chunks"
                )
            ).fetchone()
        return first, second, marker

    first, second, marker = asyncio.run(scenario())

    assert first["provider_failed"] == 1
    assert second["selected"] == 0
    assert calls == 1
    assert marker == (
        "provider_failed",
        "provider_or_json_failure",
        generation.PROMPT_VERSION,
    )
