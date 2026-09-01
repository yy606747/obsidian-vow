import asyncio
import json

import pytest

from app.sentinel import (
    GATE_REASON_CHAT_COOLDOWN,
    GATE_STATUS_BLOCKED,
    GATE_STATUS_NOT_REQUESTED,
    GATE_STATUS_PASSED,
    SENTINEL_CHAIN_DRY_RUN_SCHEMA_VERSION,
    run_sentinel_chain_dry_run,
)
from sentinel_replay_eval import load_cases


CASES_PATH = "app/sentinel/eval_cases.json"
FORBIDDEN_PROMPT_AND_PACKAGE_MARKERS = (
    "debug_trace",
    "feature_tags",
    "source_records",
    "raw_signal_count",
    "recent_chat_count",
    "EvidenceRecord",
    '"hypotheses"',
)


def _case_input(case_id: str) -> dict:
    case = next(item for item in load_cases(CASES_PATH) if item["id"] == case_id)
    return case["input"]


def _judgment(**overrides):
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
        "tone_hint": "轻轻出现，像终于忍不住看她一眼",
    }
    payload.update(overrides)
    return payload


def _provider(payload):
    async def provider(_messages):
        return json.dumps(payload, ensure_ascii=False)

    return provider


def _serialized_messages(result: dict) -> str:
    return "\n".join(message["content"] for message in result["judgment_run"]["messages"])


def _assert_prompt_and_package_do_not_contain_debug_payload(result: dict) -> None:
    prompt_text = _serialized_messages(result)
    package_text = json.dumps(result["wake_package"], ensure_ascii=False)

    for marker in FORBIDDEN_PROMPT_AND_PACKAGE_MARKERS:
        assert marker not in prompt_text
        assert marker not in package_text


def test_sentinel_chain_dry_run_creates_wake_package_only_after_gate_passes():
    result = asyncio.run(run_sentinel_chain_dry_run(
        _case_input("idle_possible_good_timing"),
        judgment_provider=_provider(_judgment()),
        judgment_context={
            "now": "2026-05-14 22:10",
            "user_name": "用户",
            "ai_name": "Aion",
            "recent_chat": ["用户: 我先刷一会。"],
            "recent_sentinel_logs": ["21:30 score:4 信号不足。"],
        },
        wake_context={
            "recent_sentinel_logs": ["21:30 score:4 信号不足。", "22:00 score:7 好时机。"],
            "recent_chat": ["用户: 我先刷一会。", "Aion: 好。"],
        },
        request_id="chain-pass",
    ))

    assert result["schema_version"] == SENTINEL_CHAIN_DRY_RUN_SCHEMA_VERSION
    assert result["runtime_mode"] == "dry_run"
    assert result["request_id"] == "chain-pass"
    assert result["side_effects"] == []
    assert result["fallback_used"] is False
    assert result["fallback_reason"] == ""
    assert result["attention_snapshot"]["debug_trace"]
    assert "debug_trace" not in result["handoff"]
    assert result["judgment_run"]["side_effects"] == []
    assert result["gate_result"]["status"] == GATE_STATUS_PASSED
    assert result["gate_result"]["side_effects"] == []
    assert result["wake_package"]["side_effects"] == []
    assert result["metrics"] == {
        "wake_requested": True,
        "wake_allowed": True,
        "wake_package_created": True,
        "blocked_reasons": [],
    }
    assert result["wake_package"]["attention"]["hypothesis_labels"] == ["可能空闲或轻度娱乐"]
    assert result["wake_package"]["sentinel"]["tone_hint"] == "轻轻出现，像终于忍不住看她一眼"
    _assert_prompt_and_package_do_not_contain_debug_payload(result)


def test_sentinel_chain_dry_run_does_not_derive_wake_from_high_score():
    result = asyncio.run(run_sentinel_chain_dry_run(
        _case_input("idle_possible_good_timing"),
        judgment_provider=_provider(_judgment(
            score=10,
            wake_intent=False,
            call_core=False,
            core_reason="",
            restraint_reason="Sentinel 选择克制，不叫醒 Core。",
            tone_hint="",
        )),
        request_id="chain-no-wake",
    ))

    assert result["gate_result"]["status"] == GATE_STATUS_NOT_REQUESTED
    assert result["gate_result"]["wake_requested"] is False
    assert result["gate_result"]["wake_allowed"] is False
    assert result["wake_package"] is None
    assert result["metrics"] == {
        "wake_requested": False,
        "wake_allowed": False,
        "wake_package_created": False,
        "blocked_reasons": [],
    }


def test_sentinel_chain_dry_run_does_not_build_package_when_gate_blocks():
    result = asyncio.run(run_sentinel_chain_dry_run(
        _case_input("idle_possible_good_timing"),
        judgment_provider=_provider(_judgment(score=9)),
        gate_context={"last_user_message_age_sec": 60},
        request_id="chain-blocked",
    ))

    assert result["gate_result"]["status"] == GATE_STATUS_BLOCKED
    assert result["gate_result"]["blocked_reasons"] == [GATE_REASON_CHAT_COOLDOWN]
    assert result["wake_package"] is None
    assert result["metrics"] == {
        "wake_requested": True,
        "wake_allowed": False,
        "wake_package_created": False,
        "blocked_reasons": [GATE_REASON_CHAT_COOLDOWN],
    }


def test_sentinel_chain_dry_run_keeps_location_transition_available_to_core_package():
    result = asyncio.run(run_sentinel_chain_dry_run(
        _case_input("outside_transition_strong_signal"),
        judgment_provider=_provider(_judgment(
            monitoringlog="设备定位越过家围栏边界，适合交给 Core 再看一眼。",
            summary="设备定位从家围栏内变为围栏外，不能确认用户本人位置。",
            score=8,
            confidence=0.82,
            core_reason="设备围栏边界发生变化，适合让 Core 保守确认。",
            uncertainty="不知道设备是否与用户在同一处，也不知道用户具体位置和活动。",
            suggested_next_check_sec=300,
            tone_hint="轻一点，不把设备位置说成用户位置",
        )),
        request_id="chain-location",
    ))

    assert result["gate_result"]["status"] == GATE_STATUS_PASSED
    assert result["wake_package"]["attention"]["attention_targets"] == ["location_transition"]
    assert result["wake_package"]["attention"]["hypothesis_labels"] == ["设备定位越出家围栏"]
    assert "设备是否与用户在同一处" in result["wake_package"]["sentinel"]["uncertainty"]
    provider_material = json.dumps(result["wake_package"], ensure_ascii=False)
    assert "刚离开家" not in provider_material
    assert "她刚从在家变为外出" not in provider_material
    assert "明确离家信号" not in provider_material
    _assert_prompt_and_package_do_not_contain_debug_payload(result)


def test_sentinel_chain_dry_run_fails_loud_on_bad_provider_output_and_input():
    with pytest.raises(ValueError, match="must be JSON"):
        asyncio.run(run_sentinel_chain_dry_run(
            _case_input("idle_possible_good_timing"),
            judgment_provider=lambda _messages: "not json",
        ))

    with pytest.raises(ValueError, match="input_payload must be an object"):
        asyncio.run(run_sentinel_chain_dry_run(
            [],
            judgment_provider=_provider(_judgment()),
        ))
