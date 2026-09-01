import json

import pytest

from app.sentinel import (
    CORE_WAKE_HARD_LIMITS,
    CORE_WAKE_PACKAGE_SCHEMA_VERSION,
    GATE_STATUS_BLOCKED,
    attention_snapshot_builder,
    build_core_wake_package,
    build_layer2_handoff,
    evaluate_case,
    evaluate_sentinel_gate,
)
from sentinel_replay_eval import load_cases


CASES_PATH = "app/sentinel/eval_cases.json"


def _handoff(case_id="idle_possible_good_timing"):
    case = next(item for item in load_cases(CASES_PATH) if item["id"] == case_id)
    record = evaluate_case(case, snapshot_builder=attention_snapshot_builder)

    assert record["ok"] is True
    return build_layer2_handoff(record["trace"]["snapshot"])


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
        "tone_hint": "轻轻出现，像终于忍不住看她一眼",
    }
    payload.update(overrides)
    return payload


def _passed_gate(judgment=None):
    return evaluate_sentinel_gate(judgment or _wake_judgment())


def test_core_wake_package_contains_only_short_prompt_payload():
    handoff = _handoff()
    judgment = _wake_judgment()
    gate_result = _passed_gate(judgment)

    package = build_core_wake_package(
        handoff=handoff,
        judgment=judgment,
        gate_result=gate_result,
        context={
            "recent_sentinel_logs": ["21:30 score:4 信号不足。", "22:00 score:7 可能是好时机。"],
            "recent_chat": ["用户: 我先刷一会。", "Aion: 好。"],
        },
    )
    serialized = json.dumps(package, ensure_ascii=False)

    assert package["schema_version"] == CORE_WAKE_PACKAGE_SCHEMA_VERSION
    assert package["runtime_mode"] == "dry_run"
    assert package["trigger"] == "sentinel"
    assert package["side_effects"] == []
    assert package["fallback_used"] is False
    assert package["wake_reason"] == judgment["core_reason"]
    assert package["sentinel"]["tone_hint"] == judgment["tone_hint"]
    assert package["sentinel"]["uncertainty"] == judgment["uncertainty"]
    assert package["attention"]["compact_text"] == handoff["compact_text"]
    assert package["attention"]["attention_targets"] == handoff["attention_targets"]
    assert package["attention"]["hypothesis_labels"] == ["可能空闲或轻度娱乐"]
    assert package["gate"]["wake_allowed"] is True
    assert package["context"]["context_projection"]["schema_version"] == "context_delivery_projection.v2"
    assert package["hard_limits"] == list(CORE_WAKE_HARD_LIMITS)
    assert any("最近亲口说的情况，永远压过设备信号" in item for item in package["hard_limits"])
    assert any("骗你、撒谎、编故事，或者被你抓到了" in item for item in package["hard_limits"])
    assert any("都只是当时的念头" in item for item in package["hard_limits"])

    assert "debug_trace" not in serialized
    assert "feature_tags" not in serialized
    assert "source_records" not in serialized
    assert "raw_signal_count" not in serialized
    assert "recent_chat_count" not in serialized
    assert "EvidenceRecord" not in serialized
    assert '"hypotheses"' not in serialized
    assert "support" not in package["attention"]
    assert "against" not in package["attention"]
    assert "missing" not in package["attention"]


def test_core_wake_package_fails_loud_when_gate_did_not_pass():
    judgment = _wake_judgment(confidence=0.2)
    blocked_gate = evaluate_sentinel_gate(judgment)

    assert blocked_gate["status"] == GATE_STATUS_BLOCKED
    with pytest.raises(ValueError, match="requires passed gate status"):
        build_core_wake_package(
            handoff=_handoff(),
            judgment=judgment,
            gate_result=blocked_gate,
        )


def test_core_wake_package_fails_loud_when_judgment_did_not_request_wake():
    judgment = _wake_judgment(
        wake_intent=False,
        call_core=False,
        core_reason="",
        restraint_reason="Sentinel 选择克制。",
        tone_hint="",
    )
    gate_result = evaluate_sentinel_gate(_wake_judgment())

    with pytest.raises(ValueError, match="requires judgment wake_intent true"):
        build_core_wake_package(
            handoff=_handoff(),
            judgment=judgment,
            gate_result=gate_result,
        )


def test_core_wake_package_limits_context_to_short_recent_items():
    handoff = _handoff("relationship_unsettled_after_conflict")
    judgment = _wake_judgment(
        core_reason="关系未收束，适合让 Core 出来接住情绪。",
        tone_hint="别审问，先把刚才的冷淡接住",
    )
    package = build_core_wake_package(
        handoff=handoff,
        judgment=judgment,
        gate_result=_passed_gate(judgment),
        context={
            "recent_sentinel_logs": ["log1", "log2", "log3", "log4"],
            "recent_chat": [f"chat{i}" for i in range(8)],
        },
    )

    assert package["context"]["recent_sentinel_logs"] == ["log2", "log3", "log4"]
    assert package["context"]["recent_chat"] == ["chat2", "chat3", "chat4", "chat5", "chat6", "chat7"]
    assert "memories" not in package["context"]
    assert package["attention"]["hypothesis_labels"] == ["关系未收束"]


def test_core_wake_package_fails_loud_on_malformed_context():
    with pytest.raises(ValueError, match="recent_chat must be a list"):
        build_core_wake_package(
            handoff=_handoff(),
            judgment=_wake_judgment(),
            gate_result=_passed_gate(),
            context={"recent_chat": "用户: hi"},
        )

    with pytest.raises(ValueError, match="unknown fields: \\['memories'\\]"):
        build_core_wake_package(
            handoff=_handoff(),
            judgment=_wake_judgment(),
            gate_result=_passed_gate(),
            context={"memories": ["旧 query recall 不得进入唤醒包"]},
        )
