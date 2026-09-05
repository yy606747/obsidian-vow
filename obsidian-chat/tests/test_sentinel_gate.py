import pytest

from app.sentinel import (
    GATE_ACTION_ALLOW_CORE_WAKE,
    GATE_ACTION_BLOCK_CORE_WAKE,
    GATE_ACTION_OBSERVE,
    GATE_REASON_CHAT_COOLDOWN,
    GATE_REASON_CLEAR_SLEEP,
    GATE_REASON_DEVICE_GATE,
    GATE_REASON_LOW_CONFIDENCE,
    GATE_REASON_QUIET_HOURS,
    GATE_REASON_WAKE_COOLDOWN,
    GATE_STATUS_BLOCKED,
    GATE_STATUS_NOT_REQUESTED,
    GATE_STATUS_PASSED,
    SENTINEL_GATE_RESULT_SCHEMA_VERSION,
    evaluate_sentinel_gate,
)


def _judgment(**overrides):
    payload = {
        "monitoringlog": "她可能空闲，适合轻轻出现。",
        "summary": "当前是一个轻唤醒窗口。",
        "score": 7,
        "confidence": 0.72,
        "wake_intent": True,
        "call_core": True,
        "core_reason": "适合轻轻出现。",
        "restraint_reason": "",
        "uncertainty": "不知道她是否愿意聊天。",
        "suggested_next_check_sec": 900,
        "tone_hint": "轻轻出现",
    }
    payload.update(overrides)
    return payload


def test_gate_passes_when_sentinel_requests_wake_and_no_hard_boundary_blocks():
    result = evaluate_sentinel_gate(_judgment())

    assert result["schema_version"] == SENTINEL_GATE_RESULT_SCHEMA_VERSION
    assert result["runtime_mode"] == "dry_run"
    assert result["status"] == GATE_STATUS_PASSED
    assert result["action"] == GATE_ACTION_ALLOW_CORE_WAKE
    assert result["wake_requested"] is True
    assert result["wake_allowed"] is True
    assert result["blocked_reasons"] == []
    assert result["side_effects"] == []
    assert result["fallback_used"] is False


def test_gate_does_not_turn_high_score_into_wake_request():
    result = evaluate_sentinel_gate(_judgment(
        score=10,
        wake_intent=False,
        call_core=False,
        core_reason="",
        restraint_reason="Sentinel 选择克制。",
        tone_hint="",
    ))

    assert result["status"] == GATE_STATUS_NOT_REQUESTED
    assert result["action"] == GATE_ACTION_OBSERVE
    assert result["wake_requested"] is False
    assert result["wake_allowed"] is False
    assert result["blocked_reasons"] == []


def test_gate_blocks_chat_cooldown_without_second_soft_decision():
    result = evaluate_sentinel_gate(
        _judgment(score=9),
        context={"last_user_message_age_sec": 120},
    )

    assert result["status"] == GATE_STATUS_BLOCKED
    assert result["action"] == GATE_ACTION_BLOCK_CORE_WAKE
    assert result["blocked_reasons"] == [GATE_REASON_CHAT_COOLDOWN]
    assert result["judgment"]["score"] == 9
    assert result["judgment"]["wake_intent"] is True


def test_gate_blocks_wake_cooldown_with_high_score_shorter_cooldown():
    low_score_blocked = evaluate_sentinel_gate(
        _judgment(score=8),
        context={"last_wake_age_sec": 700},
    )
    high_score_passed = evaluate_sentinel_gate(
        _judgment(score=9),
        context={"last_wake_age_sec": 700},
    )

    assert low_score_blocked["blocked_reasons"] == [GATE_REASON_WAKE_COOLDOWN]
    assert high_score_passed["status"] == GATE_STATUS_PASSED
    assert high_score_passed["wake_allowed"] is True


def test_gate_blocks_quiet_hours_clear_sleep_low_confidence_and_device_gate():
    result = evaluate_sentinel_gate(
        _judgment(confidence=0.2),
        context={
            "quiet_hours_active": True,
            "clear_sleep": True,
            "device_effect_requested": True,
            "device_effect_allowed": False,
        },
    )

    assert result["status"] == GATE_STATUS_BLOCKED
    assert result["blocked_reasons"] == [
        GATE_REASON_QUIET_HOURS,
        GATE_REASON_CLEAR_SLEEP,
        GATE_REASON_LOW_CONFIDENCE,
        GATE_REASON_DEVICE_GATE,
    ]


def test_gate_allows_urgent_risk_to_bypass_clear_sleep_only():
    result = evaluate_sentinel_gate(
        _judgment(),
        context={
            "clear_sleep": True,
            "urgent_risk": True,
        },
    )

    assert result["status"] == GATE_STATUS_PASSED
    assert result["blocked_reasons"] == []


def test_gate_fails_loud_on_malformed_inputs():
    with pytest.raises(ValueError, match="judgment missing fields"):
        evaluate_sentinel_gate({"wake_intent": True})

    with pytest.raises(ValueError, match="context must be an object"):
        evaluate_sentinel_gate(_judgment(), context=[])

    with pytest.raises(ValueError, match="quiet_hours_active must be a boolean"):
        evaluate_sentinel_gate(_judgment(), context={"quiet_hours_active": "yes"})

    with pytest.raises(ValueError, match="last_wake_age_sec must be non-negative"):
        evaluate_sentinel_gate(_judgment(), context={"last_wake_age_sec": -1})
