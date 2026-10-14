import asyncio
from copy import deepcopy
import json

import pytest

from app.sentinel import (
    CORE_WAKE_PREFLIGHT_SCHEMA_VERSION,
    GATE_STATUS_BLOCKED,
    LEGACY_CORE_WAKE_PACKAGE_SCHEMA_VERSION,
    PLANNED_CORE_WAKE_PRODUCTION_EFFECTS,
    attention_snapshot_builder,
    build_core_wake_package,
    build_core_wake_preflight,
    build_layer2_handoff,
    evaluate_case,
    evaluate_sentinel_gate,
    run_sentinel_chain_dry_run,
)
from sentinel_replay_eval import load_cases


CASES_PATH = "app/sentinel/eval_cases.json"
FORBIDDEN_PREFLIGHT_MARKERS = (
    "debug_trace",
    "feature_tags",
    "source_records",
    "raw_signal_count",
    "recent_chat_count",
    "EvidenceRecord",
    '"hypotheses"',
)


def _case(case_id="idle_possible_good_timing"):
    return next(item for item in load_cases(CASES_PATH) if item["id"] == case_id)


def _handoff(case_id="idle_possible_good_timing"):
    record = evaluate_case(_case(case_id), snapshot_builder=attention_snapshot_builder)

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


def _provider(payload):
    async def provider(_messages):
        return json.dumps(payload, ensure_ascii=False)

    return provider


def _wake_package(judgment=None):
    judgment = judgment or _wake_judgment()
    return build_core_wake_package(
        handoff=_handoff(),
        judgment=judgment,
        gate_result=evaluate_sentinel_gate(judgment),
        context={
            "recent_sentinel_logs": ["21:30 score:4 信号不足。", "22:00 score:7 可能是好时机。"],
            "recent_chat": ["用户: 我先刷一会。", "Arden: 好。"],
        },
    )


def test_core_wake_preflight_builds_ready_trace_without_side_effects_or_raw_payload():
    trace = build_core_wake_preflight(
        wake_package=_wake_package(),
        execution_context={
            "conv_id": "conv_sentinel",
            "model_key": "mock-model",
            "last_user_message_age_sec": 3720,
            "user_name": "用户",
            "ai_name": "Arden",
            "ai_persona": "Arden 是温柔但有掌控感的伴侣。",
            "user_persona": "用户最近压力偏大。",
            "toy_capability_allowed": True,
            "toy_capability_reason": "allowed",
            "control_session_id": "ctrl_1",
            "control_kind": "whisper",
            "control_status": "active",
            "control_epoch": 0,
            "owner_client_id": "tab1",
            "control_device_id": "browser_toy_bridge",
            "recent_messages": [
                {"id": "m1", "role": "user", "content": "我先刷一会。"},
                {"id": "m2", "role": "assistant", "content": "好。"},
            ],
        },
    )
    serialized = json.dumps(trace, ensure_ascii=False)

    assert trace["schema_version"] == CORE_WAKE_PREFLIGHT_SCHEMA_VERSION
    assert trace["runtime_mode"] == "dry_run"
    assert trace["status"] == "ready"
    assert trace["side_effects"] == []
    assert trace["production_side_effects"] == []
    assert trace["planned_production_side_effects"] == list(PLANNED_CORE_WAKE_PRODUCTION_EFFECTS)
    assert trace["fallback_used"] is False
    assert trace["fallback_reason"] == ""
    assert trace["conv_id"] == "conv_sentinel"
    assert trace["model_key"] == "mock-model"
    assert trace["would_call_core"] is True
    assert trace["would_write_monitor_log"] == "core_preflight_ready"
    assert trace["sentinel"]["summary"] == "当前是一个轻唤醒窗口。"
    assert trace["sentinel"]["tone_hint"] == "轻轻出现，像终于忍不住看她一眼"
    assert trace["gate"]["wake_allowed"] is True
    assert trace["visible_message_ids"] == ["m1", "m2"]

    core_request = trace["core_request"]
    assert core_request["persona_message_count"] == 8  # two persona pairs, shared life, presentation
    assert core_request["history_message_count"] == 2
    assert core_request["messages"][0]["content"].startswith("[关于你自己：Arden]")
    assert core_request["messages"][2]["content"].startswith("[关于她]")
    assert core_request["messages"][-1]["role"] == "user"
    assert core_request["messages"][-1]["content"] == core_request["core_prompt"]
    assert core_request["core_prompt"].startswith("【能力使用】你是Arden。")
    assert "已经1小时2分钟没有和你说话了" in core_request["core_prompt"]
    assert "这次想找她的原因" in core_request["core_prompt"]
    assert "语气提示" not in core_request["core_prompt"]
    assert "可参考记忆" not in core_request["core_prompt"]
    assert "相关记忆" not in core_request["core_prompt"]
    assert "硬限制" in core_request["core_prompt"]
    assert "密语模式" in core_request["core_prompt"]
    assert "[TOY:1]~[TOY:9]" in core_request["core_prompt"]
    assert core_request["system_notice"].startswith("💭 Arden的哨兵唤醒了主脑")
    assert core_request["monitor_alert"] == "哨兵唤醒了Arden"

    for marker in FORBIDDEN_PREFLIGHT_MARKERS:
        assert marker not in serialized


def test_core_wake_preflight_fails_loud_without_conversation():
    trace = build_core_wake_preflight(
        wake_package=_wake_package(),
        execution_context={"model_key": "mock-model"},
    )

    assert trace["status"] == "failed"
    assert trace["error_type"] == "no_conversation"
    assert trace["would_call_core"] is False
    assert trace["side_effects"] == []
    assert trace["production_side_effects"] == []
    assert trace["planned_production_side_effects"] == []
    assert trace["would_write_monitor_log"] == "core_preflight_no_conversation"


def test_core_wake_preflight_accepts_legacy_v1_package_during_migration():
    package = _wake_package()
    package["schema_version"] = LEGACY_CORE_WAKE_PACKAGE_SCHEMA_VERSION
    package["context"].pop("context_projection")

    trace = build_core_wake_preflight(
        wake_package=package,
        execution_context={"conv_id": "conv_sentinel", "model_key": "mock-model"},
    )

    assert trace["status"] == "ready"
    assert trace["wake_package_schema_version"] == LEGACY_CORE_WAKE_PACKAGE_SCHEMA_VERSION


def test_core_wake_package_v2_rejects_embedded_v1_projection():
    package = _wake_package()
    package["context"]["context_projection"]["schema_version"] = "context_delivery_projection.v1"

    with pytest.raises(ValueError, match="must use 'context_delivery_projection.v2'"):
        build_core_wake_preflight(
            wake_package=package,
            execution_context={"conv_id": "conv_sentinel", "model_key": "mock-model"},
        )


def test_core_wake_v2_renders_projection_without_leaking_diagnostic_fields():
    package = _wake_package()
    package["context"]["context_projection"] = {
        "schema_version": "context_delivery_projection.v2",
        "generated_at": 1000,
        "observations": [
            {"key": "phone.screen", "value": "on", "source": "android.sensing", "observed_at": 990, "received_at": 991, "freshness_sec": 10, "since_at": 980, "confidence": 1},
            {"key": "phone.light_lux", "value": 12000, "source": "android.sensing", "observed_at": 990, "received_at": 991, "freshness_sec": 10, "since_at": None, "confidence": 1},
            {"key": "phone.wifi", "value": "NJU-WLAN", "source": "android.sensing", "observed_at": 990, "received_at": 991, "freshness_sec": 10, "since_at": None, "confidence": 1},
        ],
        "device_derived": [
            {"key": "phone.motion", "value": "still", "source": "android.sensing", "observed_at": 990, "received_at": 991, "freshness_sec": 10, "since_at": None, "confidence": 0.17},
        ],
        "recent_events": [],
        "baseline_deviations": [],
        "availability": [],
        "metrics": {},
    }

    trace = build_core_wake_preflight(
        wake_package=package,
        execution_context={
            "conv_id": "conv_sentinel",
            "model_key": "mock-model",
            "user_name": "阿玖",
        },
    )
    prompt = trace["core_request"]["core_prompt"]

    assert "手机报告屏幕亮起" in prompt
    assert "手机运动分类为 静止" in prompt
    assert "12000" not in prompt
    assert "NJU-WLAN" not in prompt
    assert "0.17" not in prompt


def test_slim_core_prompt_keeps_context_without_sentinel_reply_directions(monkeypatch):
    from app.sentinel import core_wake_orchestrator
    from app.context_delivery import render_context_delivery_projection

    monkeypatch.setattr(core_wake_orchestrator, "_sentinel_ring_touch_enabled", lambda: False)
    judgment = _wake_judgment(
        core_reason="隔了一阵没聊，想再去关心一下小栀。",
        summary="小栀先前说没睡醒，阿澈问过近况；这不是当前状态的确认。",
        uncertainty="还不知道小栀是否已经起床、是否有空。",
        monitoringlog="仅供监控的最新日志，不应进入主脑。",
        tone_hint="先催小栀起床，再追问身体，用半哄半催的口吻。",
    )
    package = _wake_package(judgment)
    package["context"]["recent_chat"] = ["小栀：阿澈，我没睡醒，你呢？", "阿澈：昨晚睡得怎么样？"]
    package["context"]["recent_sentinel_logs"] = ["仅供监控的历史日志，不应进入主脑。"]
    package["context"]["context_projection"]["generated_at"] = 1000
    package["context"]["context_projection"]["observations"] = [{
        "key": "phone.screen", "value": "on", "source": "android.sensing",
        "observed_at": 990, "received_at": 991, "freshness_sec": 10,
        "since_at": 980, "confidence": 1,
    }]
    rendered = render_context_delivery_projection(
        package["context"]["context_projection"], user_name="小栀", ai_name="阿澈",
    )
    package["attention"]["compact_text"] = next(
        line.removeprefix("- ") for line in rendered.splitlines() if line.startswith("- ")
    )
    original = deepcopy(package)

    trace = build_core_wake_preflight(
        wake_package=package,
        execution_context={
            "conv_id": "conv_sentinel", "model_key": "mock-model",
            "user_name": "小栀", "ai_name": "阿澈",
            "last_user_message_age_sec": 4440,
            "recent_messages": [{"id": "m1", "role": "user", "content": "我没睡醒。"}],
        },
    )
    prompt = trace["core_request"]["core_prompt"]

    for text in (judgment["core_reason"], judgment["summary"], judgment["uncertainty"],
                 *package["context"]["recent_chat"]):
        assert text in prompt
    assert prompt.startswith("【能力使用】你是阿澈。")
    assert "小栀是你的人" in prompt
    assert "已经1小时14分钟没有和你说话了" in prompt
    assert "小栀最近亲口说的情况，永远压过设备信号" in prompt
    assert "小栀骗你、撒谎、编故事" in prompt
    assert prompt.count("【能力使用】") == 1
    assert "不必等小栀提出要求" in prompt
    assert "用不用、什么时候用、怎样表达，由你决定" in prompt
    assert "调用不等于成功" in prompt
    assert "只基于本轮注入的上下文" in prompt
    assert "手机报告屏幕亮起" in prompt
    assert prompt.count("[设备与环境上下文]") == 1
    assert "最近聊天片段" in prompt
    assert trace["core_request"]["history_message_count"] == 1
    for removed in (judgment["monitoringlog"], judgment["tone_hint"],
                    *package["context"]["recent_sentinel_logs"],
                    "语气提示", "最新一条哨兵日志", "最近的哨兵记录", "Attention 简报"):
        assert removed not in prompt
    assert prompt.count(package["attention"]["compact_text"]) == 1
    assert all(generic not in prompt for generic in ("用户", "TA"))
    assert package == original
    assert trace["sentinel"]["tone_hint"] == judgment["tone_hint"]
    assert trace["gate"] == original["gate"]


def test_slim_core_prompt_preserves_attention_not_covered_by_device_snapshot():
    package = _wake_package()
    package["context"]["context_projection"]["generated_at"] = 1000
    package["context"]["context_projection"]["observations"] = [{
        "key": "phone.screen", "value": "on", "source": "android.sensing",
        "observed_at": 990, "received_at": 991, "freshness_sec": 10,
        "since_at": 980, "confidence": 1,
    }]
    package["attention"]["compact_text"] = "小栀刚说正在开会；屏幕亮起不代表可以聊天。"
    trace = build_core_wake_preflight(
        wake_package=package,
        execution_context={"conv_id": "conv_sentinel", "model_key": "mock-model",
                           "user_name": "小栀", "ai_name": "阿澈"},
    )

    prompt = trace["core_request"]["core_prompt"]
    assert "手机报告屏幕亮起" in prompt
    assert package["attention"]["compact_text"] in prompt


@pytest.mark.parametrize("legacy", [False, True])
def test_slim_core_prompt_retains_attention_without_shared_device_context(legacy):
    package = _wake_package()
    if legacy:
        package["schema_version"] = LEGACY_CORE_WAKE_PACKAGE_SCHEMA_VERSION
        package["context"].pop("context_projection")

    trace = build_core_wake_preflight(
        wake_package=package,
        execution_context={"conv_id": "conv_sentinel", "model_key": "mock-model",
                           "user_name": "小栀", "ai_name": "阿澈"},
    )

    prompt = trace["core_request"]["core_prompt"]
    assert "Attention 简报" in prompt
    assert package["attention"]["compact_text"].replace("用户", "小栀") in prompt
    assert "语气提示" not in prompt
    assert "最近聊天片段" in prompt


def test_monitor_notes_cannot_expand_or_direct_the_core_prompt():
    short = _wake_package(_wake_judgment(monitoringlog="短日志", tone_hint="短备注"))
    long = deepcopy(short)
    long["sentinel"]["monitoringlog"] = "长日志里的回复指导。" * 30
    long["sentinel"]["tone_hint"] = "先催促再追问。" * 10
    long["context"]["recent_sentinel_logs"] = ["只用于监控的重复记录。" * 15] * 3
    context = {"conv_id": "conv_sentinel", "model_key": "mock-model",
               "user_name": "小栀", "ai_name": "阿澈"}

    short_trace = build_core_wake_preflight(wake_package=short, execution_context=context)
    long_trace = build_core_wake_preflight(wake_package=long, execution_context=context)

    assert short_trace["core_request"]["core_prompt"] == long_trace["core_request"]["core_prompt"]
    assert long_trace["sentinel"]["tone_hint"] != short_trace["sentinel"]["tone_hint"]


def test_core_wake_preflight_fails_loud_without_model_key():
    trace = build_core_wake_preflight(
        wake_package=_wake_package(),
        execution_context={"conv_id": "conv_sentinel"},
    )

    assert trace["status"] == "failed"
    assert trace["error_type"] == "missing_model_key"
    assert trace["would_call_core"] is False
    assert trace["planned_production_side_effects"] == []


def test_core_wake_preflight_rejects_blocked_gate_package():
    package = _wake_package()
    package["gate"] = {
        **package["gate"],
        "status": GATE_STATUS_BLOCKED,
        "wake_allowed": False,
        "blocked_reasons": ["quiet_hours"],
    }

    with pytest.raises(ValueError, match="requires passed gate status"):
        build_core_wake_preflight(
            wake_package=package,
            execution_context={"conv_id": "conv_sentinel", "model_key": "mock-model"},
        )


def test_core_wake_preflight_rejects_malformed_wake_package_payload():
    package = _wake_package()
    package["schema_version"] = "wrong"

    with pytest.raises(ValueError, match="schema_version"):
        build_core_wake_preflight(
            wake_package=package,
            execution_context={"conv_id": "conv_sentinel", "model_key": "mock-model"},
        )


def test_core_wake_preflight_rejects_malformed_execution_context():
    package = _wake_package()

    with pytest.raises(ValueError, match="unknown fields"):
        build_core_wake_preflight(
            wake_package=package,
            execution_context={
                "conv_id": "conv_sentinel",
                "model_key": "mock-model",
                "raw_db_row": {},
            },
        )

    with pytest.raises(ValueError, match="role must be user or assistant"):
        build_core_wake_preflight(
            wake_package=package,
            execution_context={
                "conv_id": "conv_sentinel",
                "model_key": "mock-model",
                "recent_messages": [{"role": "system", "content": "hidden"}],
            },
        )

    with pytest.raises(ValueError, match="last_user_message_age_sec must be non-negative"):
        build_core_wake_preflight(
            wake_package=package,
            execution_context={
                "conv_id": "conv_sentinel",
                "model_key": "mock-model",
                "last_user_message_age_sec": -1,
            },
        )

    with pytest.raises(ValueError, match="toy_capability_allowed must be a boolean"):
        build_core_wake_preflight(
            wake_package=package,
            execution_context={
                "conv_id": "conv_sentinel",
                "model_key": "mock-model",
                "toy_capability_allowed": "yes",
            },
        )


def test_sentinel_chain_can_attach_core_wake_preflight_when_execution_context_is_given():
    result = asyncio.run(run_sentinel_chain_dry_run(
        _case("idle_possible_good_timing")["input"],
        judgment_provider=_provider(_wake_judgment()),
        core_execution_context={
            "conv_id": "conv_sentinel",
            "model_key": "mock-model",
            "recent_messages": [{"role": "user", "content": "我刚忙完。"}],
        },
        request_id="chain-core-preflight",
    ))

    assert result["wake_package"] is not None
    assert result["core_wake_preflight"]["status"] == "ready"
    assert result["core_wake_preflight"]["would_call_core"] is True

    blocked = asyncio.run(run_sentinel_chain_dry_run(
        _case("idle_possible_good_timing")["input"],
        judgment_provider=_provider(_wake_judgment(score=9)),
        gate_context={"last_user_message_age_sec": 60},
        core_execution_context={"conv_id": "conv_sentinel", "model_key": "mock-model"},
        request_id="chain-core-preflight-blocked",
    ))

    assert blocked["wake_package"] is None
    assert blocked["core_wake_preflight"] is None
