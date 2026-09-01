import asyncio
import json

import pytest

from app.sentinel import (
    GATE_STATUS_BLOCKED,
    LEGACY_SENTINEL_RUNTIME_CONTEXT_SCHEMA_VERSION,
    SENTINEL_RUNTIME_CONTEXT_SCHEMA_VERSION,
    build_sentinel_runtime_context,
    run_sentinel_chain_dry_run,
    validate_sentinel_runtime_context,
)
from sentinel_replay_eval import load_cases


CASES_PATH = "app/sentinel/eval_cases.json"


def _case_input(case_id: str) -> dict:
    case = next(item for item in load_cases(CASES_PATH) if item["id"] == case_id)
    return case["input"]


def _wake_judgment(**overrides):
    payload = {
        "monitoringlog": "她可能空闲，适合轻轻出现。",
        "summary": "当前是一个轻唤醒窗口。",
        "score": 7,
        "confidence": 0.72,
        "wake_intent": True,
        "call_core": True,
        "core_reason": "可能处在空闲或轻度娱乐窗口，适合轻轻出现。",
        "restraint_reason": "",
        "uncertainty": "不知道她是否愿意聊天。",
        "suggested_next_check_sec": 900,
        "tone_hint": "轻轻出现",
    }
    payload.update(overrides)
    return payload


def test_runtime_context_prepares_prompt_gate_and_wake_inputs_without_side_effects():
    context = build_sentinel_runtime_context({
        "now": "2026-05-14 22:10",
        "user_name": "云",
        "ai_name": "Aion",
        "last_user_chat_time": "12分钟前",
        "sentinel_call_core_criteria": "好时机可以主动出现，但刚聊完要克制。",
        "recent_chat": [
            {"role": "user", "content": "我先刷一会。"},
            {"role": "assistant", "content": "好。"},
        ],
        "recent_sentinel_logs": [
            {
                "time": "21:30:00",
                "score": 4,
                "call_core": False,
                "status": "decided",
                "monitoringlog": "信号不足。",
            },
            {
                "time": "22:00:00",
                "score": 7,
                "call_core": True,
                "status": "core_wake_requested",
                "monitoringlog": "可能是好时机。",
            },
        ],
        "last_user_message_age_sec": 720,
        "last_wake_age_sec": 2000,
        "quiet_hours_active": False,
        "clear_sleep": False,
    })

    assert context["schema_version"] == SENTINEL_RUNTIME_CONTEXT_SCHEMA_VERSION
    assert context["runtime_mode"] == "dry_run"
    assert context["side_effects"] == []
    assert context["fallback_used"] is False
    assert context["judgment_context"]["now"] == "2026-05-14 22:10"
    assert context["judgment_context"]["user_name"] == "云"
    assert context["judgment_context"]["ai_name"] == "Aion"
    assert context["judgment_context"]["recent_chat"] == ["云: 我先刷一会。", "Aion: 好。"]
    assert context["judgment_context"]["recent_sentinel_logs"] == [
        "[21:30:00] score:4 status:decided 信号不足。",
        "[22:00:00] score:7 ->wake status:core_wake_requested 可能是好时机。",
    ]
    assert context["gate_context"] == {
        "clear_sleep": False,
        "last_user_message_age_sec": 720.0,
        "last_wake_age_sec": 2000.0,
        "quiet_hours_active": False,
    }
    assert context["wake_context"]["recent_sentinel_logs"] == context["judgment_context"]["recent_sentinel_logs"]
    assert context["wake_context"]["recent_chat"] == context["judgment_context"]["recent_chat"]
    assert context["wake_context"]["context_projection"] == context["judgment_context"]["context_projection"]
    assert context["wake_context"]["context_projection"]["schema_version"] == "context_delivery_projection.v2"
    assert context["metrics"] == {
        "recent_chat_count": 2,
        "recent_sentinel_log_count": 2,
        "context_projection_schema_version": "context_delivery_projection.v2",
        "gate_context_keys": [
            "clear_sleep",
            "last_user_message_age_sec",
            "last_wake_age_sec",
            "quiet_hours_active",
        ],
    }


def test_runtime_context_limits_items_before_prompt_or_wake_package_use():
    context = build_sentinel_runtime_context({
        "recent_chat": [
            {"role": "user", "content": f"chat-{index}"}
            for index in range(12)
        ],
        "recent_sentinel_logs": [f"log-{index}" for index in range(22)],
    })

    assert context["judgment_context"]["recent_chat"][0] == "她: chat-2"
    assert len(context["judgment_context"]["recent_chat"]) == 10
    assert context["judgment_context"]["recent_sentinel_logs"][0] == "log-2"
    assert len(context["judgment_context"]["recent_sentinel_logs"]) == 20


def test_runtime_context_validator_accepts_legacy_v1_during_migration():
    legacy = {
        "schema_version": LEGACY_SENTINEL_RUNTIME_CONTEXT_SCHEMA_VERSION,
        "judgment_context": {},
        "gate_context": {},
        "wake_context": {},
    }

    assert validate_sentinel_runtime_context(legacy) == legacy


def test_runtime_context_v2_rejects_embedded_v1_projection():
    context = build_sentinel_runtime_context({})
    context["judgment_context"]["context_projection"]["schema_version"] = "context_delivery_projection.v1"
    context["wake_context"]["context_projection"]["schema_version"] = "context_delivery_projection.v1"

    with pytest.raises(ValueError, match="must use 'context_delivery_projection.v2'"):
        validate_sentinel_runtime_context(context)


def test_runtime_context_rejects_legacy_memory_material():
    with pytest.raises(ValueError, match="unknown fields: \\['memories'\\]"):
        build_sentinel_runtime_context({
            "memories": [{"content": "不应进入哨兵的旧记忆"}],
        })


def test_runtime_context_fails_loud_on_bad_runtime_material():
    with pytest.raises(ValueError, match="payload must be an object"):
        build_sentinel_runtime_context([])

    with pytest.raises(ValueError, match="unknown fields"):
        build_sentinel_runtime_context({"raw_timeline": []})

    with pytest.raises(ValueError, match="recent_chat must be a list"):
        build_sentinel_runtime_context({"recent_chat": "用户: hi"})

    with pytest.raises(ValueError, match="role must be user or assistant"):
        build_sentinel_runtime_context({"recent_chat": [{"role": "system", "content": "x"}]})

    with pytest.raises(ValueError, match="call_core must be a boolean"):
        build_sentinel_runtime_context({
            "recent_sentinel_logs": [{"monitoringlog": "x", "call_core": "yes"}],
        })

    with pytest.raises(ValueError, match="quiet_hours_active must be a boolean"):
        build_sentinel_runtime_context({"quiet_hours_active": "yes"})

    with pytest.raises(ValueError, match="last_wake_age_sec must be non-negative"):
        build_sentinel_runtime_context({"last_wake_age_sec": -1})


def test_chain_consumes_runtime_context_pack_and_preserves_gate_contract():
    context = build_sentinel_runtime_context({
        "recent_chat": [{"role": "user", "content": "刚刚说完，先别重复出现。"}],
        "last_user_message_age_sec": 60,
    })

    async def provider(_messages):
        return json.dumps(_wake_judgment(score=9), ensure_ascii=False)

    result = asyncio.run(run_sentinel_chain_dry_run(
        _case_input("idle_possible_good_timing"),
        judgment_provider=provider,
        runtime_context=context,
        request_id="runtime-context-chain",
    ))

    assert result["runtime_context"]["schema_version"] == SENTINEL_RUNTIME_CONTEXT_SCHEMA_VERSION
    assert result["gate_result"]["status"] == GATE_STATUS_BLOCKED
    assert result["wake_package"] is None
    prompt_text = "\n".join(message["content"] for message in result["judgment_run"]["messages"])
    assert "她: 刚刚说完，先别重复出现。" in prompt_text


def test_chain_rejects_runtime_context_mixed_with_manual_contexts():
    context = build_sentinel_runtime_context({})

    with pytest.raises(ValueError, match="cannot be combined"):
        asyncio.run(run_sentinel_chain_dry_run(
            _case_input("idle_possible_good_timing"),
            judgment_provider=lambda _messages: json.dumps(_wake_judgment(), ensure_ascii=False),
            runtime_context=context,
            gate_context={"last_user_message_age_sec": 60},
        ))
