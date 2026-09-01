import asyncio
import json

import pytest

from app.sentinel import (
    SENTINEL_JUDGMENT_SCHEMA_VERSION,
    SENTINEL_JUDGMENT_RUN_SCHEMA_VERSION,
    attention_snapshot_builder,
    build_layer2_handoff,
    build_sentinel_judgment_messages,
    evaluate_case,
    judgment_to_monitor_log_fields,
    parse_sentinel_judgment,
    run_sentinel_judgment_dry_run,
)
from app.sentinel.judgment import normalize_sentinel_judgment
from sentinel_replay_eval import load_cases


CASES_PATH = "app/sentinel/eval_cases.json"


def _base_payload(**overrides):
    payload = {
        "monitoringlog": "信号事实清楚，但仍有不确定。",
        "summary": "整体适合轻轻出现。",
        "score": 7,
        "confidence": 0.72,
        "wake_intent": True,
        "call_core": True,
        "core_reason": "这是一个轻唤醒窗口。",
        "restraint_reason": "",
        "uncertainty": "不知道她是否愿意聊天。",
        "suggested_next_check_sec": 600,
        "tone_hint": "轻轻出现，不要审问",
    }
    payload.update(overrides)
    return payload


def _attention_handoff(case_id="idle_possible_good_timing"):
    case = next(item for item in load_cases(CASES_PATH) if item["id"] == case_id)
    record = evaluate_case(case, snapshot_builder=attention_snapshot_builder)

    assert record["ok"] is True
    return build_layer2_handoff(record["trace"]["snapshot"])


def test_sentinel_judgment_prompt_uses_handoff_without_debug_trace():
    handoff = _attention_handoff()
    messages = build_sentinel_judgment_messages(
        handoff,
        context={
            "now": "2026-05-14 22:10",
            "user_name": "用户",
            "ai_name": "Aion",
            "last_user_chat_time": "2小时前",
            "recent_chat": ["用户: 我先刷一会。"],
            "recent_sentinel_logs": ["21:30 score:4 信号不足。"],
            "sentinel_call_core_criteria": "好时机也可以唤醒。",
        },
    )
    prompt_text = "\n".join(message["content"] for message in messages)

    assert [message["role"] for message in messages] == ["system", "user"]
    assert "严格只输出 JSON" in messages[0]["content"]
    assert "Attention 简报" in messages[1]["content"]
    assert handoff["compact_text"] in prompt_text
    assert "debug_trace" not in prompt_text
    assert "feature_tags" not in prompt_text
    assert "source_records" not in prompt_text
    assert "raw_signal_count" not in prompt_text
    assert "最近亲口说的情况，永远压过设备信号" in prompt_text
    assert "骗你、撒谎、编故事，或者被你抓到了" in prompt_text
    assert "都只是当时的念头" in prompt_text


def test_sentinel_judgment_renders_projection_without_serializing_diagnostics():
    projection = {
        "schema_version": "context_delivery_projection.v2",
        "generated_at": 1000,
        "observations": [
            {"key": "phone.screen", "value": "on", "source": "android.sensing", "observed_at": 990, "received_at": 991, "freshness_sec": 10, "since_at": 980, "confidence": 1},
            {"key": "phone.light_lux", "value": 12000, "source": "android.sensing", "observed_at": 990, "received_at": 991, "freshness_sec": 10, "since_at": None, "confidence": 1},
            {"key": "phone.wifi", "value": "NJU-WLAN", "source": "android.sensing", "observed_at": 990, "received_at": 991, "freshness_sec": 10, "since_at": None, "confidence": 1},
        ],
        "device_derived": [],
        "recent_events": [],
        "baseline_deviations": [],
        "availability": [],
        "metrics": {"motion_confidence": 0.17},
    }

    messages = build_sentinel_judgment_messages(
        _attention_handoff(),
        context={"user_name": "阿玖", "context_projection": projection},
    )
    prompt = "\n".join(message["content"] for message in messages)

    assert "手机报告屏幕亮起" in prompt
    assert "12000" not in prompt
    assert "NJU-WLAN" not in prompt
    assert "motion_confidence" not in prompt
    assert "0.17" not in prompt


def test_parse_sentinel_judgment_accepts_strict_json_and_code_fence():
    raw = "```json\n" + json.dumps(_base_payload(), ensure_ascii=False) + "\n```"

    judgment = parse_sentinel_judgment(raw)

    assert judgment["schema_version"] == SENTINEL_JUDGMENT_SCHEMA_VERSION
    assert judgment["wake_intent"] is True
    assert judgment["call_core"] is True
    assert judgment["score"] == 7
    assert judgment["confidence"] == 0.72
    assert judgment["tone_hint"] == "轻轻出现，不要审问"


def test_judgment_parser_does_not_derive_wake_from_score():
    high_score_restrained = parse_sentinel_judgment(json.dumps(_base_payload(
        score=10,
        wake_intent=False,
        call_core=False,
        core_reason="",
        restraint_reason="刚聊完，不重复叫醒。",
        tone_hint="",
    ), ensure_ascii=False))
    low_score_wake = parse_sentinel_judgment(json.dumps(_base_payload(
        score=2,
        wake_intent=True,
        call_core=True,
        core_reason="关系未收束，低分但仍想轻轻出现。",
    ), ensure_ascii=False))

    assert high_score_restrained["wake_intent"] is False
    assert high_score_restrained["call_core"] is False
    assert low_score_wake["wake_intent"] is True
    assert low_score_wake["call_core"] is True


def test_judgment_parser_fails_loud_on_bad_contract():
    with pytest.raises(ValueError, match="missing fields"):
        normalize_sentinel_judgment({"monitoringlog": "x"})

    with pytest.raises(ValueError, match="unknown fields"):
        normalize_sentinel_judgment(_base_payload(extra="nope"))

    with pytest.raises(ValueError, match="call_core must equal wake_intent"):
        normalize_sentinel_judgment(_base_payload(wake_intent=True, call_core=False))

    with pytest.raises(ValueError, match="core_reason is required"):
        normalize_sentinel_judgment(_base_payload(core_reason=""))

    with pytest.raises(ValueError, match="restraint_reason is required"):
        normalize_sentinel_judgment(_base_payload(
            wake_intent=False,
            call_core=False,
            core_reason="",
            restraint_reason="",
        ))

    with pytest.raises(ValueError, match="core_reason must be empty"):
        normalize_sentinel_judgment(_base_payload(
            wake_intent=False,
            call_core=False,
            core_reason="想出现但先忍住。",
            restraint_reason="正在忙，先不打扰。",
        ))

    with pytest.raises(ValueError, match="restraint_reason must be empty"):
        normalize_sentinel_judgment(_base_payload(
            restraint_reason="虽然想出现，但也有一点犹豫。",
        ))


def test_tone_hint_is_only_length_limited_not_enum_mapped():
    long_tone = "轻轻出现，像终于忍不住看她一眼，但不要审问，也不要把弱证据说死"

    judgment = parse_sentinel_judgment(
        json.dumps(_base_payload(tone_hint=long_tone), ensure_ascii=False),
        tone_hint_max_chars=12,
    )

    assert judgment["tone_hint"] == long_tone[:12]
    assert "action" not in judgment


def test_judgment_to_monitor_log_fields_preserves_old_compatibility():
    judgment = parse_sentinel_judgment(json.dumps(_base_payload(), ensure_ascii=False))

    log_fields = judgment_to_monitor_log_fields(judgment)

    assert log_fields["monitoringlog"] == judgment["monitoringlog"]
    assert log_fields["summary"] == judgment["summary"]
    assert log_fields["score"] == 7
    assert log_fields["call_core"] is True
    assert log_fields["wake_intent"] is True
    assert log_fields["core_reason"] == "这是一个轻唤醒窗口。"
    assert log_fields["tone_hint"] == "轻轻出现，不要审问"


def test_judgment_dry_run_runner_calls_injected_provider_without_side_effects():
    handoff = _attention_handoff()
    captured = {}

    async def provider(messages):
        captured["messages"] = messages
        return json.dumps(_base_payload(), ensure_ascii=False)

    result = asyncio.run(run_sentinel_judgment_dry_run(
        handoff,
        provider=provider,
        context={"now": "2026-05-14 22:10", "ai_name": "Aion", "user_name": "用户"},
        request_id="req-layer2",
    ))
    prompt_text = "\n".join(message["content"] for message in captured["messages"])

    assert result["schema_version"] == SENTINEL_JUDGMENT_RUN_SCHEMA_VERSION
    assert result["request_id"] == "req-layer2"
    assert result["side_effects"] == []
    assert result["fallback_used"] is False
    assert result["judgment"]["wake_intent"] is True
    assert "debug_trace" not in prompt_text
    assert "feature_tags" not in prompt_text


def test_judgment_dry_run_runner_fails_loud_on_bad_provider_output():
    with pytest.raises(ValueError, match="must be JSON"):
        asyncio.run(run_sentinel_judgment_dry_run(
            _attention_handoff(),
            provider=lambda _messages: "not json",
        ))
