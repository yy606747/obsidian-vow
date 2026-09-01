import asyncio
from contextlib import asynccontextmanager
import importlib
import json
from pathlib import Path
import time

from app.memory_v2 import v2_repository
from app.memory_v2.diagnostics import build_recall_trace, format_recall_trace_markdown
import app.memory_v2.chunks as chunks
hybrid = importlib.import_module("app.memory_v2.hybrid_recall")
from app.memory_v2 import taxonomy
from app.memory_v2.embedding import pack_embedding
from app.memory_v2.migrations import infer_kind, infer_namespace
from app.memory_v2.prompt_block import build_v2_memory_prompt_block
from app.memory_v2.recall_config import (
    merge_recall_config,
    normalize_recall_config,
    prompt_injection_decision,
    recall_runtime,
)
from app.memory_v2.service import MemoryService
from devtools.memory_v2 import v2_recall
from devtools.memory_v2.gold_eval import DEFAULT_CASES_PATH, evaluate_cases, load_cases
from devtools.memory_v2.prompt_block_replay import summarize_records
from devtools.memory_v2.replay_eval import classify_message, has_protected_leak
from devtools.memory_v2.v2_recall import (
    V2RecallPlanner,
    allowed_namespaces,
    analyze_turn,
    score_item,
)


def test_taxonomy_loads_from_external_config():
    source = Path(taxonomy.TAXONOMY_SOURCE)

    assert source.name in {
        "memory_taxonomy.private.json",
        "memory_taxonomy.example.json",
    }
    assert taxonomy.INTIMATE_HINTS


def test_gold_eval_cases_pass():
    result = evaluate_cases(load_cases(DEFAULT_CASES_PATH))

    assert result["metrics"]["total"] == 64
    assert result["metrics"]["failed"] == 0


def test_low_signal_name_does_not_trigger_memory():
    plan = analyze_turn("daddy我想你了", ["daddy"])

    assert plan["needs_memory"] is False
    assert plan["detected_namespaces"] == []
    assert plan["terms"] == []


def test_actionable_schedule_prefers_open_loop():
    plan = analyze_turn("下午两点要上课，记得提醒我", [])

    assert "schedule" in plan["detected_namespaces"]
    assert "open_loop" in plan["preferred_kinds"]
    assert plan["needs_memory"] is True


def test_plain_schedule_topic_is_not_forced_open_loop():
    classification = classify_message("那咋了，上课摸鱼多正常啊")

    assert classification["query_type"] == "schedule"
    assert classification["is_open_loop_query"] is False


def test_protected_context_uses_dedicated_namespace():
    plan = analyze_turn("特殊模式里安全词和边界怎么处理？", [])

    assert plan["namespace"] == "intimate"
    assert "intimate" in allowed_namespaces(plan)


def test_work_query_does_not_allow_protected_namespace():
    plan = analyze_turn("Gemini 503 之后我的服务器端点该怎么诊断？", [])

    assert plan["namespace"] == "work"
    assert "work" in allowed_namespaces(plan)
    assert "intimate" not in allowed_namespaces(plan)


def test_memory_v2_never_recalls_control_ledger_namespace_in_normal_recall():
    normal = analyze_turn("还记得上次我最后怎么停下来的吗", ["停下来"])
    explicit_control = analyze_turn("control note", ["control"], namespace="control")

    assert "control" not in allowed_namespaces(normal)
    assert "control" not in allowed_namespaces(explicit_control)


def test_migration_and_replay_share_namespace_rules():
    content = "特殊模式需要记录安全词和边界。"

    assert infer_namespace(content, []) == "intimate"
    assert classify_message(content)["query_type"] == "intimate"


def test_private_relationship_boundary_is_protected_namespace():
    content = "二人世界相关承诺需要单独隔离。"

    assert infer_namespace(content, []) == "intimate"
    assert classify_message(content)["query_type"] == "intimate"


def test_ai_note_with_schedule_action_becomes_open_loop():
    assert infer_kind("ai_note", 0, "明天下午要上课，记得提醒她吃早饭。", []) == "open_loop"


def test_open_loop_bonus_can_beat_episode_for_actionable_schedule():
    plan = analyze_turn("下午两点要上课，记得提醒我", ["上课", "提醒"])
    open_loop = {
        "id": "open",
        "content": "明天下午两点上课，记得提醒她。",
        "kind": "open_loop",
        "namespace": "schedule",
        "importance": 0.5,
        "confidence": 0.7,
        "created_at": 0,
        "keywords_json": '["上课","提醒"]',
        "metadata_json": "{}",
    }
    episode = {
        "id": "episode",
        "content": "她上课时讨论过这件事。",
        "kind": "episode",
        "namespace": "schedule",
        "importance": 0.5,
        "confidence": 0.7,
        "created_at": 0,
        "keywords_json": '["上课"]',
        "metadata_json": "{}",
    }

    assert score_item(open_loop, plan)["score"] > score_item(episode, plan)["score"]


def test_remember_query_prefers_episode_memory():
    plan = analyze_turn("你还记得上次我们一起看电影那件事吗", ["看电影"])
    episode = {
        "id": "episode",
        "content": "上次我们一起看电影之后聊了很久剧情。",
        "kind": "episode",
        "namespace": "normal",
        "importance": 0.5,
        "confidence": 0.7,
        "created_at": 0,
        "keywords_json": '["看电影"]',
        "metadata_json": "{}",
    }
    semantic = {
        "id": "semantic",
        "content": "用户喜欢看电影。",
        "kind": "semantic",
        "namespace": "normal",
        "importance": 0.5,
        "confidence": 0.7,
        "created_at": 0,
        "keywords_json": '["看电影"]',
        "metadata_json": "{}",
    }

    assert "episode" in plan["preferred_kinds"]
    assert score_item(episode, plan)["score"] > score_item(semantic, plan)["score"]


def test_recently_used_memory_gets_cooldown_penalty():
    plan = analyze_turn("你还记得上次我们一起看电影那件事吗", ["看电影"])
    base_item = {
        "id": "memory",
        "content": "上次我们一起看电影之后聊了很久剧情。",
        "kind": "episode",
        "namespace": "normal",
        "importance": 0.5,
        "confidence": 0.7,
        "created_at": 0,
        "keywords_json": '["看电影"]',
        "metadata_json": "{}",
    }
    fresh = {**base_item, "last_used_at": None}
    recently_used = {**base_item, "last_used_at": time.time()}

    assert "cooldown" in score_item(recently_used, plan)["reason"]
    assert score_item(fresh, plan)["score"] > score_item(recently_used, plan)["score"]


def test_score_item_uses_embedding_similarity_without_keyword_overlap():
    plan = analyze_turn("我今天心情不好", [])
    semantic_match = {
        "id": "semantic_match",
        "content": "上次她失眠之后整个人也很低落。",
        "kind": "episode",
        "namespace": "normal",
        "emotion": "sad",
        "importance": 0.5,
        "confidence": 0.7,
        "created_at": 0,
        "keywords_json": "[]",
        "metadata_json": "{}",
        "embedding": pack_embedding([1.0, 0.0]),
    }
    unrelated = {
        **semantic_match,
        "id": "unrelated",
        "content": "她之前聊过一段代码重构计划。",
        "emotion": "positive",
        "embedding": pack_embedding([0.0, 1.0]),
    }
    opposite = {
        **semantic_match,
        "id": "opposite",
        "content": "这条语义方向和查询相反。",
        "embedding": pack_embedding([-1.0, 0.0]),
    }

    matched = score_item(semantic_match, plan, query_embedding=[1.0, 0.0])
    missed = score_item(unrelated, plan, query_embedding=[1.0, 0.0])
    opposite_score = score_item(opposite, plan, query_embedding=[1.0, 0.0])

    assert matched["semantic_similarity"] == 1.0
    assert opposite_score["semantic_similarity"] == -1.0
    assert matched["score"] > missed["score"]
    assert opposite_score["score"] < missed["score"]
    assert "semantic:" in matched["reason"]


def test_chunk_builder_formats_raw_messages_and_splits_mechanically(monkeypatch):
    monkeypatch.setattr(chunks, "load_worldbook", lambda: {"user_name": "Ithil", "ai_name": "Aion"})
    base_ts = 1_716_038_200.0
    messages = [
        {
            "id": f"msg_{index}",
            "conv_id": "conv_chunks",
            "role": "user" if index % 2 else "assistant",
            "content": f"第{index}条消息，提到了那首歌",
            "created_at": base_ts + index,
        }
        for index in range(7)
    ]

    built = chunks.build_chunks_from_messages(messages)
    built_again = chunks.build_chunks_from_messages(messages)

    assert len(built) == 2
    assert built[0]["id"] == built_again[0]["id"]
    assert "Ithil: 第1条消息" in built[0]["content"]
    assert "Aion: 第2条消息" in built[0]["content"]
    assert len(json.loads(built[0]["message_ids_json"])) == 6
    assert len(json.loads(built[1]["message_ids_json"])) == 1


def test_hybrid_recall_searches_chunks_and_notes_without_namespace_gate(monkeypatch):
    async def fake_get_embedding(_text):
        return [1.0, 0.0]

    async def fake_fetch_chunks(_limit):
        return [{
            "id": "chunk_song",
            "conv_id": "conv_1",
            "message_ids_json": '["msg_song"]',
            "content": "[05-18 21:30] Ithil: 那首歌是晴天，别忘了",
            "created_at": time.time(),
            "updated_at": time.time(),
            "embedding": pack_embedding([1.0, 0.0]),
            "keywords_json": '["晴天","那首歌"]',
            "metadata_json": '{"source_start_ts": 100, "source_end_ts": 101}',
        }]

    async def fake_fetch_notes(_limit):
        return [{
            "id": "note_location",
            "content": "Ithil 之前说学校附近那家日料看起来不错。",
            "kind": "episode",
            "namespace": "location",
            "importance": 0.4,
            "confidence": 0.7,
            "created_at": time.time(),
            "updated_at": time.time(),
            "last_used_at": None,
            "source_conv": "conv_1",
            "source_start_ts": 90,
            "source_end_ts": 91,
            "embedding": pack_embedding([0.0, 1.0]),
            "keywords_json": '["学校附近","日料"]',
            "metadata_json": '{"source_message_ids":["msg_food"]}',
        }]

    async def fake_recent_usage(_ids):
        return {}

    monkeypatch.setattr(hybrid.embedding, "get_embedding", fake_get_embedding)
    monkeypatch.setattr(hybrid, "_fetch_chunks", fake_fetch_chunks)
    monkeypatch.setattr(hybrid, "_fetch_notes", fake_fetch_notes)
    monkeypatch.setattr(hybrid, "_recent_usage", fake_recent_usage)

    result = asyncio.run(hybrid.hybrid_recall("那首歌叫什么", ["那首歌"], top_k=3))

    assert result["retrieval_mode"] == "hybrid_chunk_note"
    assert result["semantic_query"] is True
    assert result["selected"][0]["id"] == "chunk_song"
    assert result["selected"][0]["source_type"] == "chunk"
    assert result["allowed_namespaces"] == ["all"]
    assert any(item["namespace"] == "location" for item in result["debug_top"])


def test_emotion_resonance_boosts_mood_congruent_memory():
    plan = analyze_turn("我今天心情不好", [])
    sad_memory = {
        "id": "sad",
        "content": "她那天也说自己很低落。",
        "kind": "emotional",
        "namespace": "normal",
        "emotion": "sad",
        "importance": 0.5,
        "confidence": 0.7,
        "created_at": 0,
        "keywords_json": "[]",
        "metadata_json": "{}",
    }
    happy_memory = {**sad_memory, "id": "happy", "emotion": "positive"}

    sad_score = score_item(sad_memory, plan)
    happy_score = score_item(happy_memory, plan)

    assert plan["emotion"] == "sad"
    assert sad_score["emotion_resonance"] == 1.0
    assert sad_score["score"] > happy_score["score"]
    assert "emotion:sad->sad" in sad_score["reason"]


def test_protected_leak_checker_uses_shared_rules():
    results = [{"namespace": "normal", "content": "特殊模式需要单独处理。"}]

    assert has_protected_leak(results, {}, v2=True, query_type="normal") is True


def test_recall_trace_explains_no_memory_abstain():
    turn_plan = analyze_turn("daddy我想你了", ["daddy"])
    plan = {
        "query": "daddy我想你了",
        "keywords": ["daddy"],
        "turn_plan": turn_plan,
        "allowed_namespaces": allowed_namespaces(turn_plan),
        "candidate_count": 0,
        "selected": [],
        "debug_top": [],
    }

    trace = build_recall_trace(plan, classification=classify_message(plan["query"]))

    assert trace["planner"] == "memory_v2"
    assert trace["turn_plan"]["needs_memory"] is False
    assert trace["abstain_reason"] == "no_memory_signal"
    assert trace["steps"][-1]["status"] == "abstained"


class FakeRecallRepository:
    async def fetch_items_for_recall(self, **_kwargs):
        return [
            {
                "id": "memv2_schedule",
                "legacy_memory_id": "legacy_schedule",
                "content": "明天下午两点上课，记得提前提醒她。",
                "kind": "open_loop",
                "namespace": "schedule",
                "importance": 0.8,
                "confidence": 0.9,
                "created_at": 0,
                "last_used_at": None,
                "keywords_json": '["上课","提醒"]',
                "metadata_json": "{}",
            }
        ]


class FakeUsageRepository:
    def __init__(self):
        self.calls = []

    async def record_usage(self, **kwargs):
        self.calls.append(kwargs)
        return {
            "id": f"usage_{len(self.calls)}",
            "memory_id": kwargs["memory_id"],
            "score": kwargs.get("score"),
            "rank": kwargs.get("rank"),
        }


def test_v2_planner_fetches_query_embedding_for_semantic_ranking(monkeypatch):
    async def fake_get_embedding(_text):
        return [1.0, 0.0]

    class FakeSemanticRepository:
        async def fetch_items_for_recall(self, **_kwargs):
            return [
                {
                    "id": "mem_semantic",
                    "content": "上次她失眠之后整个人也很低落。",
                    "kind": "episode",
                    "namespace": "normal",
                    "emotion": "sad",
                    "importance": 0.5,
                    "confidence": 0.7,
                    "created_at": 0,
                    "last_used_at": None,
                    "keywords_json": "[]",
                    "metadata_json": "{}",
                    "embedding": pack_embedding([1.0, 0.0]),
                },
                {
                    "id": "mem_unrelated",
                    "content": "她之前聊过一段代码重构计划。",
                    "kind": "episode",
                    "namespace": "normal",
                    "emotion": "",
                    "importance": 0.5,
                    "confidence": 0.7,
                    "created_at": 0,
                    "last_used_at": None,
                    "keywords_json": "[]",
                    "metadata_json": "{}",
                    "embedding": pack_embedding([0.0, 1.0]),
                },
                {
                    "id": "mem_opposite",
                    "content": "这条语义方向和查询相反。",
                    "kind": "episode",
                    "namespace": "normal",
                    "emotion": "",
                    "importance": 0.5,
                    "confidence": 0.7,
                    "created_at": 0,
                    "last_used_at": None,
                    "keywords_json": "[]",
                    "metadata_json": "{}",
                    "embedding": pack_embedding([-1.0, 0.0]),
                },
            ]

    monkeypatch.setattr(v2_recall.embedding, "get_embedding", fake_get_embedding)
    result = asyncio.run(V2RecallPlanner(FakeSemanticRepository()).plan(
        "我今天心情不好",
        [],
        top_k=2,
    ))

    assert result["semantic_query"] is True
    assert result["selected"][0]["id"] == "mem_semantic"
    assert result["debug_top"][0]["semantic_similarity"] == 1.0
    assert any(item["semantic_similarity"] == -1.0 for item in result["debug_top"])


def test_v2_planner_can_return_recall_trace():
    async def run():
        planner = V2RecallPlanner(FakeRecallRepository())
        return await planner.plan(
            "下午两点要上课，记得提醒我",
            ["上课", "提醒"],
            namespace="schedule",
            top_k=3,
            include_trace=True,
        )

    result = asyncio.run(run())
    trace = result["trace"]
    markdown = format_recall_trace_markdown(trace)

    assert result["abstain_reason"] is None
    assert trace["selected_count"] == 1
    assert trace["selected"][0]["legacy_memory_id"] == "legacy_schedule"
    assert trace["selected"][0]["kind"] == "open_loop"
    assert "open_loop" in markdown


def test_v2_planner_does_not_fetch_control_ledger_items_for_normal_chat():
    class ControlLedgerRepository:
        def __init__(self):
            self.namespaces = None

        async def fetch_items_for_recall(self, **kwargs):
            self.namespaces = list(kwargs.get("namespaces") or [])
            if "control" in self.namespaces:
                return [{
                    "id": "control_ledger",
                    "content": "上一轮控制会话里她最后主动停下。",
                    "kind": "episode",
                    "namespace": "control",
                    "importance": 1.0,
                    "confidence": 1.0,
                    "created_at": 0,
                    "last_used_at": None,
                    "keywords_json": '["停下"]',
                    "metadata_json": "{}",
                }]
            return []

    repo = ControlLedgerRepository()
    result = asyncio.run(V2RecallPlanner(repo).plan(
        "还记得我上次怎么停下的吗",
        ["停下"],
        mode="normal",
        top_k=3,
    ))

    assert repo.namespaces is not None
    assert "control" not in repo.namespaces
    assert result["selected"] == []


def test_v2_prompt_block_skips_when_no_selected_items():
    block = build_v2_memory_prompt_block({
        "selected": [],
        "abstain_reason": "no_memory_signal",
    })

    assert block["enabled"] is False
    assert block["content"] == ""
    assert block["skipped_reason"] == "no_memory_signal"


def test_v2_prompt_block_formats_selected_items_with_score_filter_and_budget():
    plan = {
        "selected": [
            {
                "id": "low",
                "content": "这条分数太低，不应该进入 prompt。",
                "kind": "episode",
                "namespace": "normal",
                "score": 0.2,
            },
            {
                "id": "high",
                "legacy_memory_id": "legacy_high",
                "content": "用户想把记忆库重构成更可解释、更方便扩展的 V2 架构。" * 4,
                "kind": "semantic",
                "namespace": "work",
                "score": 0.8123,
            },
        ],
    }

    block = build_v2_memory_prompt_block(
        plan,
        min_score=0.45,
        max_items=5,
        max_item_chars=40,
        max_block_chars=500,
    )

    assert block["enabled"] is True
    assert block["item_count"] == 1
    assert block["items"][0]["id"] == "high"
    assert "low" not in block["content"]
    assert "[可能相关的记忆]" in block["content"]
    assert "不要逐条复述" in block["content"]
    assert "心里会浮起这些记忆" not in block["content"]
    assert "score=" not in block["content"]
    assert "[work/semantic" not in block["content"]
    assert "稳定背景：" not in block["content"]
    assert len(block["items"][0]["preview"]) <= 40


def test_v2_prompt_block_uses_source_time_instead_of_recall_update_time():
    now = time.time()
    block = build_v2_memory_prompt_block({
        "selected": [
            {
                "id": "old_memory_recalled_now",
                "content": "这是较早发生、后来再次召回的事件。",
                "kind": "episode",
                "namespace": "normal",
                "score": 0.9,
                "source_end_ts": None,
                "source_start_ts": now - 8 * 24 * 60 * 60,
                "created_at": now - 100 * 24 * 60 * 60,
                "updated_at": now,
            }
        ]
    })

    assert block["enabled"] is True
    assert "[上周 | 摘要]" in block["content"]
    assert "今天" not in block["content"]


def test_v2_prompt_block_adds_realtime_guard_for_device_items():
    block = build_v2_memory_prompt_block({
        "selected": [
            {
                "id": "device_memory",
                "content": "用户后续可能接入一个智能戒指设备。",
                "kind": "semantic",
                "namespace": "device",
                "score": 0.9,
            }
        ]
    })

    assert block["enabled"] is True
    assert "设备状态、定位、日程执行和健康状态必须以当前工具或服务返回为准" in block["content"]
    assert "device_memory" == block["items"][0]["id"]


def test_phase7_preflight_device_status_memory_is_guarded_before_content():
    block = build_v2_memory_prompt_block({
        "selected": [
            {
                "id": "device_status_memory",
                "content": "上次测试时智能戒指显示在线，但这只是历史状态。",
                "kind": "episode",
                "namespace": "device",
                "score": 0.9,
            }
        ]
    })

    assert block["enabled"] is True
    assert "这些内容可能和当前对话有关，只作为背景" in block["content"]
    assert "设备状态、定位、日程执行和健康状态必须以当前工具或服务返回为准" in block["content"]
    assert block["content"].index("必须以当前工具或服务返回为准") < block["content"].index("上次测试时智能戒指显示在线")


def test_phase7_preflight_device_events_do_not_create_long_term_memory(monkeypatch):
    calls = []

    class FakeDb:
        async def execute(self, sql, params=()):
            calls.append((sql, params))

        async def commit(self):
            calls.append(("COMMIT", ()))

    @asynccontextmanager
    async def fake_get_db():
        yield FakeDb()

    monkeypatch.setattr(v2_repository, "get_db", fake_get_db)
    repo = v2_repository.MemoryRepository()

    event = asyncio.run(repo.create_event(
        source="device",
        namespace="device",
        conv_id="conv_device",
        role="tool",
        content="mock ring reported battery=80",
        metadata_json='{"device_id":"ring_mock"}',
        created_at=123.0,
    ))

    sql_text = "\n".join(sql for sql, _params in calls)
    assert event["source"] == "device"
    assert event["namespace"] == "device"
    assert "INSERT INTO memory_events" in sql_text
    assert "memory_items" not in sql_text
    assert "memory_links" not in sql_text


def test_record_usage_updates_last_used_without_mutating_memory_updated_at(monkeypatch):
    calls = []

    class FakeDb:
        async def execute(self, sql, params=()):
            calls.append((sql, params))

        async def commit(self):
            calls.append(("COMMIT", ()))

    @asynccontextmanager
    async def fake_get_db():
        yield FakeDb()

    monkeypatch.setattr(v2_repository, "get_db", fake_get_db)
    repo = v2_repository.MemoryRepository()

    usage = asyncio.run(repo.record_usage(memory_id="mem_old", touch_last_used=True))

    updates = [(sql, params) for sql, params in calls if sql.startswith("UPDATE memory_items")]
    assert updates == [
        (
            "UPDATE memory_items SET last_used_at=? WHERE id=?",
            (usage["used_at"], "mem_old"),
        )
    ]


def test_memory_service_exposes_v2_prompt_block_builder_without_chat_injection():
    service = MemoryService()
    block = service.build_v2_prompt_block({
        "selected": [
            {
                "id": "work_memory",
                "content": "ChatService 后续应通过 PromptBuilder 消费记忆块。",
                "kind": "semantic",
                "namespace": "work",
                "score": 0.7,
            }
        ]
    })

    assert block["enabled"] is True
    assert "ChatService 后续应通过 PromptBuilder 消费记忆块" in block["content"]
    assert block["metadata"]["builder"] == "memory_v2_prompt_block"
    assert block["metadata"]["composer"] == "possible_related_memory"


def test_memory_service_records_injected_v2_prompt_usage_and_touches_cooldown():
    repo = FakeUsageRepository()
    service = MemoryService(v2_repository=repo)
    result = asyncio.run(service.record_v2_prompt_usage({
        "runtime": {"v2_enabled": True, "mode": "full"},
        "prompt_decision": {"inject": True, "reason": "full", "mode": "full"},
        "prompt_block": {
            "enabled": True,
            "items": [
                {"id": "mem_a", "score": 0.9},
                {"id": "mem_b", "score": 0.7},
            ],
        },
    }, conv_id="conv_1", request_id="msg_1"))

    assert result["status"] == "recorded"
    assert result["usage_type"] == "injected"
    assert result["touch_last_used"] is True
    assert result["count"] == 2
    assert repo.calls[0]["memory_id"] == "mem_a"
    assert repo.calls[0]["rank"] == 1
    assert repo.calls[0]["touch_last_used"] is True
    assert repo.calls[0]["reason"] == "injected:full:full"


def test_memory_service_records_preview_v2_prompt_usage_without_touching_cooldown():
    repo = FakeUsageRepository()
    service = MemoryService(v2_repository=repo)
    result = asyncio.run(service.record_v2_prompt_usage({
        "runtime": {"v2_enabled": True, "mode": "debug"},
        "prompt_decision": {"inject": False, "reason": "debug_preview_only", "mode": "debug"},
        "prompt_block": {
            "enabled": True,
            "items": [{"id": "mem_preview", "score": 0.8}],
        },
    }, conv_id="conv_1", request_id="msg_1"))

    assert result["status"] == "recorded"
    assert result["usage_type"] == "preview_only"
    assert result["touch_last_used"] is False
    assert repo.calls[0]["touch_last_used"] is False
    assert repo.calls[0]["reason"] == "preview_only:debug:debug_preview_only"


def test_memory_service_skips_v2_prompt_usage_when_legacy_disabled():
    repo = FakeUsageRepository()
    service = MemoryService(v2_repository=repo)
    result = asyncio.run(service.record_v2_prompt_usage({
        "runtime": {"v2_enabled": False, "mode": "legacy"},
        "prompt_block": {
            "enabled": True,
            "items": [{"id": "mem_should_not_write", "score": 0.8}],
        },
    }, conv_id="conv_1", request_id="msg_1"))

    assert result["status"] == "skipped"
    assert result["reason"] == "v2_disabled"
    assert repo.calls == []


def test_recall_rollout_config_defaults_to_full():
    config = normalize_recall_config({"mode": "nonsense", "top_k": 999})
    runtime = recall_runtime(config)

    assert config["mode"] == "full"
    assert config["top_k"] == 20
    assert runtime["v2_enabled"] is True
    assert runtime["prompt_injection_enabled"] is True


def test_empty_rollout_config_defaults_to_full_prompt_injection():
    config = normalize_recall_config(None)
    runtime = recall_runtime(config)
    decision = prompt_injection_decision(config, seed="memory-v2-full-default")

    assert config == {
        "mode": "full",
        "top_k": 8,
        "candidate_limit": 1000,
        "include_trace": False,
        "canary_ratio": 0.0,
        "prompt_min_score": 0.18,
    }
    assert runtime["v2_enabled"] is True
    assert runtime["prompt_block_enabled"] is True
    assert runtime["prompt_injection_enabled"] is True
    assert decision == {"inject": True, "reason": "full", "mode": "full"}


def test_recall_rollout_one_key_legacy_rollback_resets_config():
    config = merge_recall_config(
        {
            "mode": "full",
            "include_trace": True,
            "canary_ratio": 1.0,
            "top_k": 3,
            "candidate_limit": 50,
            "prompt_min_score": 0.2,
        },
        {"mode": "legacy"},
    )

    assert config == {
        "mode": "legacy",
        "top_k": 8,
        "candidate_limit": 1000,
        "include_trace": False,
        "canary_ratio": 0.0,
        "prompt_min_score": 0.18,
    }


def test_recall_rollout_full_mode_enables_prompt_injection():
    runtime = recall_runtime({"mode": "full", "include_trace": False})

    assert runtime["mode"] == "full"
    assert runtime["effective_mode"] == "full"
    assert runtime["v2_enabled"] is True
    assert runtime["include_trace"] is True
    assert runtime["prompt_block_enabled"] is True
    assert runtime["prompt_injection_enabled"] is True


def test_recall_rollout_debug_previews_prompt_block_without_injection():
    runtime = recall_runtime({"mode": "debug", "include_trace": False})
    decision = prompt_injection_decision({"mode": "debug"}, seed="fixed")

    assert runtime["prompt_block_enabled"] is True
    assert runtime["prompt_injection_enabled"] is False
    assert decision["inject"] is False
    assert decision["reason"] == "debug_preview_only"


def test_recall_rollout_canary_decision_is_deterministic():
    cfg = {"mode": "canary", "canary_ratio": 1.0}
    selected = prompt_injection_decision(cfg, seed="fixed")
    skipped = prompt_injection_decision({"mode": "canary", "canary_ratio": 0.0}, seed="fixed")

    assert selected["inject"] is True
    assert selected["reason"] == "canary_selected"
    assert skipped["inject"] is False
    assert skipped["reason"] == "canary_ratio_zero"


def test_prompt_block_replay_summary_checks_budget_namespace_and_usage():
    records = [
        {
            "query_type": "work",
            "needs_memory": True,
            "allowed_namespaces": ["work", "normal"],
            "block_enabled": True,
            "block_chars": 96,
            "block_item_count": 1,
            "block_items": [
                {"id": "mem_work", "namespace": "work", "kind": "semantic", "score": 0.7, "preview": "work memory"}
            ],
            "block_warnings": [],
            "block_content": "[你现在想到的]\n- work memory",
            "decision_inject": True,
            "usage": {"status": "recorded", "usage_type": "injected", "count": 1, "touch_last_used": True},
            "_content_lookup": {"mem_work": "work memory"},
        },
        {
            "query_type": "normal",
            "needs_memory": False,
            "allowed_namespaces": ["normal", "work", "schedule"],
            "block_enabled": False,
            "block_chars": 0,
            "block_item_count": 0,
            "block_skipped_reason": "no_memory_signal",
            "block_items": [],
            "decision_inject": False,
            "usage": {"status": "skipped", "reason": "no_memory_signal", "count": 0},
            "_content_lookup": {},
        },
    ]

    summary = summarize_records(records, max_block_chars=1200, min_score=0.45)

    assert summary["block_enabled_count"] == 1
    assert summary["block_abstain_no_memory_rate"] == 1.0
    assert summary["usage_rows"] == 1
    assert summary["usage_touched_last_used_rows"] == 1
    assert all(summary["checks"].values())


def test_prompt_block_replay_summary_flags_protected_leaks():
    records = [
        {
            "query_type": "normal",
            "needs_memory": True,
            "allowed_namespaces": ["normal", "work", "schedule"],
            "block_enabled": True,
            "block_chars": 80,
            "block_item_count": 1,
            "block_items": [
                {"id": "mem_private", "namespace": "intimate", "kind": "episode", "score": 0.8, "preview": "private"}
            ],
            "block_content": "[你现在想到的]\n- private",
            "decision_inject": False,
            "usage": {"status": "recorded", "usage_type": "preview_only", "count": 1, "touch_last_used": False},
            "_content_lookup": {"mem_private": "private"},
        },
    ]

    summary = summarize_records(records, max_block_chars=1200, min_score=0.45)

    assert summary["checks"]["no_protected_leak"] is False
    assert summary["checks"]["no_disallowed_namespace"] is False
    assert summary["check_counts"]["protected_leak_count"] == 1
