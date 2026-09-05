import asyncio
import json

import pytest

from app.sentinel import (
    CORE_WAKE_EXECUTION_MODE_DISABLED,
    CORE_WAKE_EXECUTION_MODE_DRY_RUN,
    CORE_WAKE_EXECUTION_MODE_FULL,
    CORE_WAKE_EXECUTION_MODE_TEST_EXECUTE,
    CORE_WAKE_EXECUTION_SCHEMA_VERSION,
    attention_snapshot_builder,
    build_core_wake_preflight,
    build_core_wake_package,
    build_layer2_handoff,
    evaluate_case,
    evaluate_sentinel_gate,
    run_core_wake_orchestrator_dry_run,
    run_core_wake_orchestrator_full_execute,
    run_core_wake_orchestrator_test_execute,
)
from sentinel_replay_eval import load_cases


CASES_PATH = "app/sentinel/eval_cases.json"
FORBIDDEN_EXECUTION_MARKERS = (
    "debug_trace",
    "feature_tags",
    "source_records",
    "raw_signal_count",
    "recent_chat_count",
    "EvidenceRecord",
    '"messages"',
    '"core_prompt"',
    "raw_output",
)


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


def _execution_context(**overrides):
    payload = {
        "conv_id": "conv_sentinel",
        "model_key": "mock-model",
        "last_user_message_age_sec": 3720,
        "user_name": "用户",
        "ai_name": "Arden",
        "recent_messages": [
            {"id": "m1", "role": "user", "content": "我先刷一会。"},
            {"id": "m2", "role": "assistant", "content": "好。"},
        ],
    }
    payload.update(overrides)
    return payload


def _step_status(trace, name):
    return next(item["status"] for item in trace["execution_steps"] if item["name"] == name)


class _FakeCoreWakePorts:
    def __init__(
        self,
        *,
        timeline_context=None,
        timeline_error: Exception | None = None,
        core_response: str = "刚好想到你。",
        core_error: Exception | None = None,
        core_results=None,
    ):
        self.calls = []
        self.monitor_logs = []
        self.broadcasts = []
        self.stream_messages = []
        self.sleeps = []
        self.screen_requests = []
        self.mobile_screen_requests = []
        self.ring_touches = []
        self.timeline_error = timeline_error
        self.timeline_context = timeline_context if timeline_context is not None else {
            "status": "injected",
            "block": "[最近三天的事]\n· 今天 21:00 用户说忙完了。",
            "entries": [{"text": "用户说忙完了。"}],
        }
        self.core_response = core_response
        self.core_error = core_error
        self.core_results = list(core_results) if core_results is not None else None
        self._now = 1_700_000_000.0

    def now(self):
        self._now += 1
        return self._now

    async def load_timeline_prompt_context(self, *, visible_message_ids, now=None):
        self.calls.append(("load_timeline_prompt_context", list(visible_message_ids), now))
        if self.timeline_error:
            raise self.timeline_error
        return dict(self.timeline_context)

    async def record_timeline_injection_usage(
        self,
        timeline_meta,
        *,
        conv_id,
        assistant_message_id,
        response_text,
    ):
        self.calls.append((
            "record_timeline_injection_usage",
            timeline_meta.get("status"),
            conv_id,
            assistant_message_id,
            response_text,
        ))
        return {"status": "recorded", "count": len(timeline_meta.get("entries") or [])}

    async def broadcast_monitor_alert(self, content):
        self.calls.append(("broadcast_monitor_alert", content))
        self.broadcasts.append({"type": "monitor_alert", "content": content})

    async def insert_system_wake_notice(self, *, conv_id, content, created_at):
        self.calls.append(("insert_system_wake_notice", conv_id, content, created_at))
        return {
            "id": "sys_1",
            "conv_id": conv_id,
            "role": "system",
            "content": content,
            "created_at": created_at,
            "attachments": [],
        }

    async def stream_core(self, *, messages, model_key, temperature=None):
        self.calls.append(("stream_core", model_key, temperature))
        self.stream_messages = list(messages)
        if self.core_results is not None:
            if not self.core_results:
                raise AssertionError("fake core_results exhausted")
            result = self.core_results.pop(0)
            if isinstance(result, Exception):
                raise result
            return result
        if self.core_error:
            raise self.core_error
        return self.core_response

    async def sleep(self, seconds):
        self.calls.append(("sleep", seconds))
        self.sleeps.append(seconds)

    async def insert_assistant_message(self, *, conv_id, content, created_at):
        self.calls.append(("insert_assistant_message", conv_id, content, created_at))
        return {
            "id": "core_1",
            "conv_id": conv_id,
            "role": "assistant",
            "content": content,
            "created_at": created_at,
            "attachments": [],
        }

    async def update_conversation(self, *, conv_id, updated_at):
        self.calls.append(("update_conversation", conv_id, updated_at))

    async def broadcast_msg_created(self, message):
        self.calls.append(("broadcast_msg_created", message["id"]))
        self.broadcasts.append({"type": "msg_created", "data": dict(message)})

    async def broadcast_toy_command(
        self,
        *,
        commands,
        msg_id,
        conv_id=None,
        toy_capability_allowed=False,
        control_session_id=None,
        control_epoch=None,
        owner_client_id=None,
        control_device_id=None,
        request_id=None,
        wake_id=None,
    ):
        self.calls.append(("broadcast_toy_command", tuple(commands), msg_id, request_id, wake_id))
        self.broadcasts.append({
            "type": "toy_command",
            "data": {"type": "toy_command", "commands": list(commands), "msg_id": msg_id},
        })
        return {"status": "broadcasted", "broadcast": True, "commands": list(commands)}

    async def request_screen_check(
        self,
        *,
        conv_id,
        msg_id,
        model_key,
        reason,
        request_id=None,
        wake_id=None,
    ):
        payload = {
            "status": "pending",
            "request_id": "screen_1",
            "conv_id": conv_id,
            "msg_id": msg_id,
            "model_key": model_key,
            "reason": reason,
            "wake_id": wake_id,
            "broadcast": True,
        }
        self.calls.append(("request_screen_check", conv_id, msg_id, model_key, reason, request_id, wake_id))
        self.screen_requests.append(payload)
        return payload

    async def request_mobile_screen_check(
        self,
        *,
        conv_id,
        msg_id,
        model_key,
        target,
        reason,
        request_id=None,
        wake_id=None,
    ):
        payload = {
            "status": "pending",
            "request_id": "mobile_screen_1",
            "conv_id": conv_id,
            "msg_id": msg_id,
            "model_key": model_key,
            "target": target,
            "target_device_id": target,
            "reason": reason,
            "wake_id": wake_id,
            "broadcast": True,
        }
        self.calls.append(("request_mobile_screen_check", conv_id, msg_id, model_key, target, reason, request_id, wake_id))
        self.mobile_screen_requests.append(payload)
        return payload

    async def execute_ring_touch(self, *, touch_descriptions, conv_id, msg_id, model_key, request_id=None, wake_id=None):
        payload = {
            "status": "executed",
            "count": len(tuple(touch_descriptions)),
            "touch_descriptions": list(touch_descriptions),
            "conv_id": conv_id,
            "msg_id": msg_id,
            "request_id": request_id,
            "wake_id": wake_id,
        }
        self.calls.append(("execute_ring_touch", payload["count"], conv_id, msg_id, request_id, wake_id))
        self.ring_touches.append(payload)
        return payload

    async def write_monitor_log(self, entry):
        self.calls.append(("write_monitor_log", entry["status"]))
        self.monitor_logs.append(dict(entry))


class _RelationshipCoreWakePorts(_FakeCoreWakePorts):
    def __init__(self, *, relationship_context=("", ""), relationship_error=None, **kwargs):
        super().__init__(**kwargs)
        self.relationship_context = relationship_context
        self.relationship_error = relationship_error

    async def load_working_model_prompt_context(self):
        self.calls.append(("load_working_model_prompt_context",))
        if self.relationship_error is not None:
            raise self.relationship_error
        return self.relationship_context


class _WebSearchCoreWakePorts(_FakeCoreWakePorts):
    async def prepare_web_search_turn(self, *, conv_id, bound_turn_id):
        self.calls.append(("prepare_web_search_turn", conv_id, bound_turn_id))
        return {
            "status": "bound",
            "bound_turn_id": bound_turn_id,
            "block": "WEB_RUNTIME_TAIL",
        }

    async def finalize_web_search_turn(self, **kwargs):
        self.calls.append(("finalize_web_search_turn", kwargs))
        return {"status": "queued", "search_id": "web-next"}


def test_core_wake_orchestrator_disabled_trace_has_no_side_effects_or_prompt_payload():
    trace = run_core_wake_orchestrator_dry_run(
        wake_package=_wake_package(),
        execution_context=_execution_context(),
        execution_mode=CORE_WAKE_EXECUTION_MODE_DISABLED,
        request_id="wake-disabled",
    )
    serialized = json.dumps(trace, ensure_ascii=False)

    assert trace["schema_version"] == CORE_WAKE_EXECUTION_SCHEMA_VERSION
    assert trace["runtime_mode"] == "dry_run"
    assert trace["execution_mode"] == CORE_WAKE_EXECUTION_MODE_DISABLED
    assert trace["status"] == "disabled"
    assert trace["request_id"] == "wake-disabled"
    assert trace["side_effects"] == []
    assert trace["production_side_effects"] == []
    assert trace["fallback_used"] is False
    assert trace["fallback_reason"] == ""
    assert trace["execution_enabled"] is False
    assert trace["would_call_core"] is True
    assert trace["would_write_monitor_log"] == "core_orchestrator_disabled"
    assert trace["conv_id"] == "conv_sentinel"
    assert trace["model_key"] == "mock-model"
    assert trace["preflight"]["status"] == "ready"
    assert trace["core_request"]["persona_message_count"] == 0
    assert trace["core_request"]["history_message_count"] == 2
    assert trace["core_request"]["prompt_char_count"] > 0
    assert trace["core_request"]["system_notice"].startswith("💭 Arden的哨兵唤醒了主脑")
    assert _step_status(trace, "stream_core") == "planned"
    assert _step_status(trace, "insert_assistant_message") == "planned"

    for marker in FORBIDDEN_EXECUTION_MARKERS:
        assert marker not in serialized


def test_core_wake_preflight_toy_prompt_uses_frozen_live_capability():
    no_control = build_core_wake_preflight(
        wake_package=_wake_package(),
        execution_context=_execution_context(toy_capability_allowed=False),
    )
    incomplete_snapshot = build_core_wake_preflight(
        wake_package=_wake_package(),
        execution_context=_execution_context(toy_capability_allowed=True),
    )
    with_control = build_core_wake_preflight(
        wake_package=_wake_package(),
        execution_context=_execution_context(
            toy_capability_allowed=True,
            toy_capability_reason="allowed",
            control_session_id="ctrl_1",
            control_kind="whisper",
            control_status="active",
            control_epoch=0,
            owner_client_id="tab1",
            control_device_id="browser_toy_bridge",
        ),
    )

    assert "[TOY:1]~[TOY:9]" not in no_control["core_request"]["core_prompt"]
    assert "[TOY:1]~[TOY:9]" not in incomplete_snapshot["core_request"]["core_prompt"]
    assert "[TOY:1]~[TOY:9]" in with_control["core_request"]["core_prompt"]
    assert with_control["toy_capability_allowed"] is True
    assert with_control["control_session_id"] == "ctrl_1"


def test_core_prompt_resolves_hard_limit_name_without_rewriting_quoted_pronouns():
    preflight = build_core_wake_preflight(
        wake_package=_wake_package(),
        execution_context=_execution_context(
            user_name="阿玖",
            recent_messages=[{
                "id": "m1",
                "role": "user",
                "content": "她们今天都去上课了。",
            }],
        ),
    )

    prompt = preflight["core_request"]["core_prompt"]
    messages = preflight["core_request"]["messages"]
    assert "阿玖最近亲口说的情况" in prompt
    assert any(item["content"] == "她们今天都去上课了。" for item in messages)
    assert "阿玖们" not in "\n".join(item["content"] for item in messages)


def test_core_wake_orchestrator_fails_loud_when_preflight_has_no_conversation_or_model():
    no_conversation = run_core_wake_orchestrator_dry_run(
        wake_package=_wake_package(),
        execution_context=_execution_context(conv_id=""),
    )

    assert no_conversation["status"] == "preflight_failed"
    assert no_conversation["would_call_core"] is False
    assert no_conversation["planned_production_side_effects"] == []
    assert no_conversation["error_type"] == "no_conversation"
    assert no_conversation["preflight"]["status"] == "failed"
    assert _step_status(no_conversation, "validate_preflight") == "failed"
    assert _step_status(no_conversation, "stream_core") == "skipped"

    missing_model = run_core_wake_orchestrator_dry_run(
        wake_package=_wake_package(),
        execution_context=_execution_context(model_key=""),
    )

    assert missing_model["status"] == "preflight_failed"
    assert missing_model["error_type"] == "missing_model_key"
    assert missing_model["would_write_monitor_log"] == "core_preflight_missing_model_key"


def test_core_wake_orchestrator_rejects_unsupported_execution_modes_from_dry_run_entrypoint():
    with pytest.raises(ValueError, match="full execution requires full_execute"):
        run_core_wake_orchestrator_dry_run(
            wake_package=_wake_package(),
            execution_context=_execution_context(),
            execution_mode="full",
        )

    with pytest.raises(ValueError, match="test_execute requires test ports"):
        run_core_wake_orchestrator_dry_run(
            wake_package=_wake_package(),
            execution_context=_execution_context(),
            execution_mode=CORE_WAKE_EXECUTION_MODE_TEST_EXECUTE,
        )


def test_core_wake_orchestrator_dry_run_ready_trace_has_no_side_effects_or_fake_outcomes():
    trace = run_core_wake_orchestrator_dry_run(
        wake_package=_wake_package(),
        execution_context=_execution_context(),
        execution_mode=CORE_WAKE_EXECUTION_MODE_DRY_RUN,
    )
    serialized = json.dumps(trace, ensure_ascii=False)

    assert trace["status"] == "dry_run_ready"
    assert trace["side_effects"] == []
    assert trace["production_side_effects"] == []
    assert trace["would_write_monitor_log"] == "core_dry_run"
    assert trace["context_errors"] == []
    assert trace["fallback_used"] is False
    assert "simulated_outcome" not in trace
    assert _step_status(trace, "load_timeline_context") == "planned"
    assert _step_status(trace, "stream_core") == "planned"
    assert _step_status(trace, "insert_assistant_message") == "planned"
    for marker in FORBIDDEN_EXECUTION_MARKERS:
        assert marker not in serialized


def test_core_wake_orchestrator_test_execute_success_uses_fake_ports_in_order():
    ports = _FakeCoreWakePorts()

    trace = asyncio.run(run_core_wake_orchestrator_test_execute(
        wake_package=_wake_package(),
        execution_context=_execution_context(),
        ports=ports,
        request_id="wake-test",
        temperature=0.3,
    ))
    serialized = json.dumps(trace, ensure_ascii=False)

    assert trace["schema_version"] == CORE_WAKE_EXECUTION_SCHEMA_VERSION
    assert trace["runtime_mode"] == "dry_run"
    assert trace["execution_mode"] == CORE_WAKE_EXECUTION_MODE_TEST_EXECUTE
    assert trace["status"] == "core_succeeded"
    assert trace["request_id"] == "wake-test"
    assert trace["execution_enabled"] is True
    assert trace["using_fake_ports"] is True
    assert trace["execution_policy"] == {
        "pre_core_delay_sec": 0,
        "max_core_attempts": 1,
        "retry_delay_sec": 0,
    }
    assert trace["core_attempts"] == [{"attempt": 1, "status": "succeeded"}]
    assert trace["timing_events"] == []
    assert trace["production_side_effects"] == []
    assert trace["fallback_used"] is False
    assert trace["would_write_monitor_log"] == "core_succeeded"
    assert trace["system_msg_id"] == "sys_1"
    assert trace["core_msg_id"] == "core_1"
    assert _step_status(trace, "load_timeline_context") == "succeeded"
    assert _step_status(trace, "pre_core_delay") == "skipped"
    assert _step_status(trace, "stream_core") == "succeeded"
    assert _step_status(trace, "write_monitor_log") == "succeeded"
    assert [call[0] for call in ports.calls] == [
        "load_timeline_prompt_context",
        "stream_core",
        "broadcast_monitor_alert",
        "insert_system_wake_notice",
        "broadcast_msg_created",
        "insert_assistant_message",
        "record_timeline_injection_usage",
        "update_conversation",
        "broadcast_msg_created",
        "write_monitor_log",
    ]
    assert ports.calls[0][1] == ["m1", "m2"]
    assert ports.calls[1] == ("stream_core", "mock-model", 0.3)
    assert ports.calls[2] == ("broadcast_monitor_alert", "哨兵唤醒了Arden")
    assert ports.calls[4] == ("broadcast_msg_created", "sys_1")
    assert ports.sleeps == []
    assert ports.broadcasts[1]["data"]["id"] == "sys_1"
    assert ports.broadcasts[2]["data"]["id"] == "core_1"
    assert ports.stream_messages[0]["content"].startswith("[最近三天的事]")
    assert not any(
        marker in message["content"]
        for marker in ("[相关记忆]", "可参考记忆")
        for message in ports.stream_messages
    )
    assert ports.monitor_logs == [{
        "status": "core_succeeded",
        "call_core": False,
        "conv_id": "conv_sentinel",
        "core_reason": "可能处在空闲或轻度娱乐窗口，适合轻轻出现。",
        "summary": "当前是一个轻唤醒窗口。",
        "context_errors": [],
        "core_msg_id": "core_1",
    }]

    for marker in FORBIDDEN_EXECUTION_MARKERS:
        assert marker not in serialized


def test_core_wake_web_search_is_runtime_injected_stripped_and_finalized():
    ports = _WebSearchCoreWakePorts(
        core_response=(
            "可见正文[WEB_SEARCH_INTENT]查明天的新消息[/WEB_SEARCH_INTENT]"
        )
    )
    trace = asyncio.run(run_core_wake_orchestrator_test_execute(
        wake_package=_wake_package(),
        execution_context=_execution_context(),
        ports=ports,
        request_id="wake-web",
    ))

    assert trace["status"] == "core_succeeded"
    assert ports.stream_messages[-3:] == [
        {"role": "user", "content": "WEB_RUNTIME_TAIL"},
        {"role": "assistant", "content": "（嗯，查询能力和已经返回的资料我都清楚。）"},
        ports.stream_messages[-1],
    ]
    insert = next(call for call in ports.calls if call[0] == "insert_assistant_message")
    assert insert[2] == "可见正文"
    finalized = next(call for call in ports.calls if call[0] == "finalize_web_search_turn")[1]
    assert finalized["bound_turn_id"] == "sentinel:wake-web"
    assert finalized["intent_text"] == "查明天的新消息"
    assert "WEB_SEARCH_INTENT" not in str(ports.broadcasts)


def test_core_wake_failure_monitor_does_not_expose_web_search_marker():
    ports = _WebSearchCoreWakePorts(
        core_response=(
            "[请求出错: provider down]"
            "[WEB_SEARCH_INTENT]私有查询[/WEB_SEARCH_INTENT]"
        )
    )
    trace = asyncio.run(run_core_wake_orchestrator_test_execute(
        wake_package=_wake_package(),
        execution_context=_execution_context(),
        ports=ports,
        request_id="wake-web-error",
    ))

    assert trace["status"] == "core_failed"
    assert "WEB_SEARCH_INTENT" not in str(trace)
    assert "WEB_SEARCH_INTENT" not in str(ports.monitor_logs)


def test_core_wake_orchestrator_injects_working_model_desire_and_timeline_only():
    ports = _RelationshipCoreWakePorts(relationship_context=(
        "[你对她的当前认识]\n她看重边界。",
        "[你此刻想以怎样的姿态与她相处]\n主动而尊重。",
    ))

    trace = asyncio.run(run_core_wake_orchestrator_test_execute(
        wake_package=_wake_package(),
        execution_context=_execution_context(),
        ports=ports,
    ))

    assert trace["status"] == "core_succeeded"
    assert ports.stream_messages[:6] == [
        {"role": "user", "content": "[你对她的当前认识]\n她看重边界。"},
        {"role": "assistant", "content": "（嗯，这是我此刻对她的认识。）"},
        {"role": "user", "content": "[你此刻想以怎样的姿态与她相处]\n主动而尊重。"},
        {"role": "assistant", "content": "（嗯，这是我此刻想带进这段关系里的姿态。）"},
        {"role": "user", "content": "[最近三天的事]\n· 今天 21:00 用户说忙完了。"},
        {"role": "assistant", "content": "（嗯，近几天的事我还记得。）"},
    ]
    assert ports.calls[0] == ("load_working_model_prompt_context",)
    assert ports.calls[1][0] == "load_timeline_prompt_context"
    assert not any(
        marker in message["content"]
        for marker in ("[相关记忆]", "可参考记忆")
        for message in ports.stream_messages
    )


def test_core_wake_orchestrator_timeline_gate_off_injects_no_memory_layer():
    ports = _FakeCoreWakePorts(timeline_context={
        "status": "disabled",
        "block": "",
        "entries": [],
    })

    trace = asyncio.run(run_core_wake_orchestrator_test_execute(
        wake_package=_wake_package(),
        execution_context=_execution_context(),
        ports=ports,
    ))

    assert trace["status"] == "core_succeeded"
    assert _step_status(trace, "load_timeline_context") == "succeeded"
    assert not any(
        marker in message["content"]
        for marker in ("[最近三天的事]", "[相关记忆]", "可参考记忆")
        for message in ports.stream_messages
    )


def test_core_wake_orchestrator_working_model_failure_skips_wake_side_effects():
    ports = _RelationshipCoreWakePorts(
        relationship_error=RuntimeError("working model unavailable"),
    )

    trace = asyncio.run(run_core_wake_orchestrator_test_execute(
        wake_package=_wake_package(),
        execution_context=_execution_context(),
        ports=ports,
    ))

    assert trace["status"] == "working_model_read_failed"
    assert trace["error_type"] == "working_model_read_failed"
    assert trace["error"] == "working model unavailable"
    assert _step_status(trace, "load_timeline_context") == "skipped"
    assert _step_status(trace, "broadcast_monitor_alert") == "skipped"
    assert _step_status(trace, "stream_core") == "skipped"
    assert [call[0] for call in ports.calls] == [
        "load_working_model_prompt_context",
        "write_monitor_log",
    ]
    assert ports.monitor_logs[-1]["status"] == "working_model_read_failed"


def test_core_wake_orchestrator_timeline_failure_is_non_blocking():
    ports = _FakeCoreWakePorts(timeline_error=RuntimeError("timeline down"))

    trace = asyncio.run(run_core_wake_orchestrator_test_execute(
        wake_package=_wake_package(),
        execution_context=_execution_context(),
        ports=ports,
    ))

    assert trace["status"] == "core_succeeded"
    assert trace["would_write_monitor_log"] == "core_succeeded"
    assert trace["context_errors"] == ["timeline_context_failed: RuntimeError: timeline down"]
    assert _step_status(trace, "load_timeline_context") == "failed_non_blocking"
    assert _step_status(trace, "stream_core") == "succeeded"
    assert ports.stream_messages[0]["role"] == "user"
    assert ports.monitor_logs[0]["context_errors"] == ["timeline_context_failed: RuntimeError: timeline down"]


def test_core_wake_orchestrator_test_execute_core_empty_skips_assistant_side_effects():
    ports = _FakeCoreWakePorts(core_response="  ")

    trace = asyncio.run(run_core_wake_orchestrator_test_execute(
        wake_package=_wake_package(),
        execution_context=_execution_context(),
        ports=ports,
    ))

    assert trace["status"] == "core_empty"
    assert trace["would_write_monitor_log"] == "core_empty"
    assert trace["error_type"] == "core_empty"
    assert trace["fallback_used"] is False
    assert trace["core_attempts"] == [{"attempt": 1, "status": "empty"}]
    assert trace["timing_events"] == []
    assert _step_status(trace, "broadcast_monitor_alert") == "skipped"
    assert _step_status(trace, "insert_system_wake_notice") == "skipped"
    assert _step_status(trace, "broadcast_system_msg_created") == "skipped"
    assert _step_status(trace, "pre_core_delay") == "skipped"
    assert _step_status(trace, "stream_core") == "empty"
    assert _step_status(trace, "insert_assistant_message") == "skipped"
    assert _step_status(trace, "update_conversation") == "skipped"
    assert _step_status(trace, "broadcast_msg_created") == "skipped"
    assert not {
        "broadcast_monitor_alert",
        "insert_system_wake_notice",
        "insert_assistant_message",
    }.intersection(call[0] for call in ports.calls)
    assert ports.monitor_logs[0]["status"] == "core_empty"


def test_core_wake_orchestrator_test_execute_provider_failure_is_loud():
    ports = _FakeCoreWakePorts(core_error=RuntimeError("core down"))

    trace = asyncio.run(run_core_wake_orchestrator_test_execute(
        wake_package=_wake_package(),
        execution_context=_execution_context(),
        ports=ports,
    ))

    assert trace["status"] == "core_failed"
    assert trace["would_write_monitor_log"] == "core_failed"
    assert trace["error_type"] == "RuntimeError"
    assert trace["error"] == "core down"
    assert trace["fallback_used"] is False
    assert trace["core_attempts"] == [{
        "attempt": 1,
        "status": "failed",
        "error_type": "RuntimeError",
        "error": "core down",
    }]
    assert trace["timing_events"] == []
    assert _step_status(trace, "broadcast_monitor_alert") == "skipped"
    assert _step_status(trace, "insert_system_wake_notice") == "skipped"
    assert _step_status(trace, "broadcast_system_msg_created") == "skipped"
    assert _step_status(trace, "pre_core_delay") == "skipped"
    assert _step_status(trace, "stream_core") == "failed"
    assert _step_status(trace, "insert_assistant_message") == "skipped"
    assert ports.monitor_logs[0]["status"] == "core_failed"
    assert ports.monitor_logs[0]["error_type"] == "RuntimeError"


def test_core_wake_orchestrator_cancellation_before_reply_has_no_visible_wake():
    class _CancellingCoreWakePorts(_FakeCoreWakePorts):
        async def stream_core(self, *, messages, model_key, temperature=None):
            self.calls.append(("stream_core", model_key, temperature))
            self.stream_messages = list(messages)
            raise asyncio.CancelledError

    async def run_cancelled_wake():
        ports = _CancellingCoreWakePorts()
        with pytest.raises(asyncio.CancelledError):
            await run_core_wake_orchestrator_test_execute(
                wake_package=_wake_package(),
                execution_context=_execution_context(),
                ports=ports,
            )
        return ports

    ports = asyncio.run(run_cancelled_wake())

    assert [call[0] for call in ports.calls] == [
        "load_timeline_prompt_context",
        "stream_core",
    ]
    assert ports.broadcasts == []
    assert ports.monitor_logs == []


def test_core_wake_orchestrator_test_execute_provider_error_text_skips_assistant_message():
    ports = _FakeCoreWakePorts(core_response="[硅基流动错误: HTTP 429 rate limit]")

    trace = asyncio.run(run_core_wake_orchestrator_test_execute(
        wake_package=_wake_package(),
        execution_context=_execution_context(),
        ports=ports,
    ))

    assert trace["status"] == "core_failed"
    assert trace["would_write_monitor_log"] == "core_failed"
    assert trace["error_type"] == "core_provider_error_text"
    assert "429" in trace["error"]
    assert trace["core_attempts"] == [{
        "attempt": 1,
        "status": "provider_error_text",
        "error_type": "core_provider_error_text",
        "error": "[硅基流动错误: HTTP 429 rate limit]",
    }]
    assert _step_status(trace, "stream_core") == "failed"
    assert _step_status(trace, "insert_assistant_message") == "skipped"
    assert _step_status(trace, "broadcast_msg_created") == "skipped"
    assert "insert_assistant_message" not in [call[0] for call in ports.calls]
    assert [b for b in ports.broadcasts if b.get("data", {}).get("role") == "assistant"] == []
    assert ports.monitor_logs[0]["status"] == "core_failed"
    assert ports.monitor_logs[0]["error_type"] == "core_provider_error_text"


def test_core_wake_orchestrator_test_execute_default_policy_does_not_sleep_or_retry():
    ports = _FakeCoreWakePorts(core_results=[RuntimeError("core down"), "第二次不该调用。"])

    trace = asyncio.run(run_core_wake_orchestrator_test_execute(
        wake_package=_wake_package(),
        execution_context=_execution_context(),
        ports=ports,
    ))

    assert trace["status"] == "core_failed"
    assert trace["execution_policy"] == {
        "pre_core_delay_sec": 0,
        "max_core_attempts": 1,
        "retry_delay_sec": 0,
    }
    assert trace["core_attempts"] == [{
        "attempt": 1,
        "status": "failed",
        "error_type": "RuntimeError",
        "error": "core down",
    }]
    assert [call[0] for call in ports.calls].count("stream_core") == 1
    assert "sleep" not in [call[0] for call in ports.calls]
    assert ports.sleeps == []


def test_core_wake_orchestrator_test_execute_validates_ports_and_preflight_first():
    with pytest.raises(ValueError, match="ports missing methods"):
        asyncio.run(run_core_wake_orchestrator_test_execute(
            wake_package=_wake_package(),
            execution_context=_execution_context(),
            ports=object(),
        ))

    ports = _FakeCoreWakePorts()
    trace = asyncio.run(run_core_wake_orchestrator_test_execute(
        wake_package=_wake_package(),
        execution_context=_execution_context(conv_id=""),
        ports=ports,
    ))

    assert trace["status"] == "preflight_failed"
    assert trace["error_type"] == "no_conversation"
    assert trace["side_effects"] == []
    assert ports.calls == []


def test_core_wake_orchestrator_rejects_bad_execution_policy():
    ports = _FakeCoreWakePorts()

    with pytest.raises(ValueError, match="unknown fields"):
        asyncio.run(run_core_wake_orchestrator_test_execute(
            wake_package=_wake_package(),
            execution_context=_execution_context(),
            ports=ports,
            execution_policy={"surprise": 1},
        ))

    with pytest.raises(ValueError, match="max_core_attempts must be positive"):
        asyncio.run(run_core_wake_orchestrator_test_execute(
            wake_package=_wake_package(),
            execution_context=_execution_context(),
            ports=ports,
            execution_policy={"max_core_attempts": 0},
        ))

    with pytest.raises(ValueError, match="pre_core_delay_sec must be non-negative"):
        asyncio.run(run_core_wake_orchestrator_test_execute(
            wake_package=_wake_package(),
            execution_context=_execution_context(),
            ports=ports,
            execution_policy={"pre_core_delay_sec": -1},
        ))

    assert ports.calls == []


def test_core_wake_orchestrator_full_execute_requires_explicit_production_opt_in():
    ports = _FakeCoreWakePorts()

    with pytest.raises(ValueError, match="allow_production_side_effects=true"):
        asyncio.run(run_core_wake_orchestrator_full_execute(
            wake_package=_wake_package(),
            execution_context=_execution_context(),
            ports=ports,
        ))

    assert ports.calls == []


def test_core_wake_orchestrator_full_execute_records_production_side_effects():
    ports = _FakeCoreWakePorts()

    trace = asyncio.run(run_core_wake_orchestrator_full_execute(
        wake_package=_wake_package(),
        execution_context=_execution_context(),
        ports=ports,
        request_id="wake-full",
        temperature=0.2,
        allow_production_side_effects=True,
    ))

    assert trace["runtime_mode"] == "full"
    assert trace["execution_mode"] == CORE_WAKE_EXECUTION_MODE_FULL
    assert trace["status"] == "core_succeeded"
    assert trace["execution_enabled"] is True
    assert "using_fake_ports" not in trace
    assert trace["execution_policy"] == {
        "pre_core_delay_sec": 5,
        "max_core_attempts": 2,
        "retry_delay_sec": 10,
    }
    assert trace["core_attempts"] == [{"attempt": 1, "status": "succeeded"}]
    assert trace["timing_events"] == [{"name": "pre_core_delay", "seconds": 5}]
    assert trace["side_effects"] == [
        "memory.timeline.prompt_context",
        "core.provider.stream_ai",
        "websocket.broadcast.monitor_alert",
        "database.insert.system_wake_notice",
        "websocket.broadcast.msg_created.system_wake_notice",
        "database.insert.assistant_message",
        "memory.timeline.record_usage",
        "database.update.conversation",
        "websocket.broadcast.msg_created.assistant_message",
        "monitor_log.write.core_result",
    ]
    assert trace["production_side_effects"] == [
        "core.provider.stream_ai",
        "websocket.broadcast.monitor_alert",
        "database.insert.system_wake_notice",
        "websocket.broadcast.msg_created.system_wake_notice",
        "database.insert.assistant_message",
        "memory.timeline.record_usage",
        "database.update.conversation",
        "websocket.broadcast.msg_created.assistant_message",
        "monitor_log.write.core_result",
    ]
    assert trace["fallback_used"] is False
    assert _step_status(trace, "pre_core_delay") == "succeeded"
    assert _step_status(trace, "broadcast_toy_command") == "skipped"
    assert ports.calls[1] == ("sleep", 5)
    assert ports.calls[2] == ("stream_core", "mock-model", 0.2)
    assert ports.sleeps == [5]


def test_core_wake_orchestrator_full_execute_retries_empty_reply_after_retry_delay():
    ports = _FakeCoreWakePorts(core_results=["  ", "第二次成功。"])

    trace = asyncio.run(run_core_wake_orchestrator_full_execute(
        wake_package=_wake_package(),
        execution_context=_execution_context(),
        ports=ports,
        allow_production_side_effects=True,
    ))

    assert trace["status"] == "core_succeeded"
    assert trace["core_attempts"] == [
        {"attempt": 1, "status": "empty", "retry_after_sec": 10},
        {"attempt": 2, "status": "succeeded"},
    ]
    assert trace["timing_events"] == [
        {"name": "pre_core_delay", "seconds": 5},
        {"name": "core_retry_delay", "after_attempt": 1, "seconds": 10},
    ]
    assert ports.sleeps == [5, 10]
    assert [call[0] for call in ports.calls].count("stream_core") == 2
    assert ports.calls[1] == ("sleep", 5)
    assert ports.calls[2] == ("stream_core", "mock-model", None)
    assert ports.calls[3] == ("sleep", 10)
    assert ports.calls[4] == ("stream_core", "mock-model", None)
    assert trace["production_side_effects"].count("core.provider.stream_ai") == 2
    assert ports.monitor_logs[0]["status"] == "core_succeeded"
    assistant_insert = next(call for call in ports.calls if call[0] == "insert_assistant_message")
    assert assistant_insert[2] == "第二次成功。"


def test_core_wake_orchestrator_full_execute_retries_provider_failure_and_logs_final_failure():
    ports = _FakeCoreWakePorts(core_results=[
        RuntimeError("core down 1"),
        RuntimeError("core down 2"),
    ])

    trace = asyncio.run(run_core_wake_orchestrator_full_execute(
        wake_package=_wake_package(),
        execution_context=_execution_context(),
        ports=ports,
        allow_production_side_effects=True,
    ))

    assert trace["status"] == "core_failed"
    assert trace["would_write_monitor_log"] == "core_failed"
    assert trace["error_type"] == "RuntimeError"
    assert trace["error"] == "core down 2"
    assert trace["core_attempts"] == [
        {
            "attempt": 1,
            "status": "failed",
            "error_type": "RuntimeError",
            "error": "core down 1",
            "retry_after_sec": 10,
        },
        {
            "attempt": 2,
            "status": "failed",
            "error_type": "RuntimeError",
            "error": "core down 2",
        },
    ]
    assert trace["timing_events"] == [
        {"name": "pre_core_delay", "seconds": 5},
        {"name": "core_retry_delay", "after_attempt": 1, "seconds": 10},
    ]
    assert trace["production_side_effects"].count("core.provider.stream_ai") == 2
    assert "database.insert.assistant_message" not in trace["production_side_effects"]
    assert _step_status(trace, "stream_core") == "failed"
    assert _step_status(trace, "broadcast_monitor_alert") == "skipped"
    assert _step_status(trace, "insert_system_wake_notice") == "skipped"
    assert _step_status(trace, "insert_assistant_message") == "skipped"
    assert ports.sleeps == [5, 10]
    assert not {
        "broadcast_monitor_alert",
        "insert_system_wake_notice",
        "insert_assistant_message",
    }.intersection(call[0] for call in ports.calls)
    assert ports.monitor_logs[0]["status"] == "core_failed"
    assert ports.monitor_logs[0]["error"] == "core down 2"


def test_core_wake_orchestrator_full_execute_strips_toy_commands_and_broadcasts_them():
    ports = _FakeCoreWakePorts(core_response="刚好想到你。[TOY:2] [TOY:STOP]")

    trace = asyncio.run(run_core_wake_orchestrator_full_execute(
        wake_package=_wake_package(),
        execution_context=_execution_context(),
        ports=ports,
        allow_production_side_effects=True,
    ))

    assert trace["status"] == "core_succeeded"
    assert trace["toy_commands"] == ["2", "STOP"]
    assert trace["production_side_effects"][-2:] == [
        "websocket.broadcast.toy_command",
        "monitor_log.write.core_result",
    ]
    assert _step_status(trace, "broadcast_toy_command") == "succeeded"
    assert ports.calls[-2] == ("broadcast_toy_command", ("2", "STOP"), "core_1", "core_1", "core_1")
    assert ports.broadcasts[-1] == {
        "type": "toy_command",
        "data": {"type": "toy_command", "commands": ["2", "STOP"], "msg_id": "core_1"},
    }
    assistant_insert = next(call for call in ports.calls if call[0] == "insert_assistant_message")
    assert assistant_insert[2] == "刚好想到你。"
    assert ports.monitor_logs[0]["toy_commands"] == ["2", "STOP"]


def test_core_wake_orchestrator_full_execute_requests_screen_check_from_marker():
    ports = _FakeCoreWakePorts(core_response="我有点想确认你是不是还在写代码。[SCREEN_CHECK:确认她是不是还在写代码]")

    trace = asyncio.run(run_core_wake_orchestrator_full_execute(
        wake_package=_wake_package(),
        execution_context=_execution_context(),
        ports=ports,
        allow_production_side_effects=True,
    ))

    assert trace["status"] == "core_succeeded"
    assert trace["screen_check_reasons"] == ["确认她是不是还在写代码"]
    assert trace["screen_check_requests"][0]["status"] == "pending"
    assert _step_status(trace, "request_screen_check") == "succeeded"
    assert "pc_screen.request" in trace["production_side_effects"]
    assert ("request_screen_check", "conv_sentinel", "core_1", "mock-model", "确认她是不是还在写代码", "core_1", "core_1") in ports.calls
    assistant_insert = next(call for call in ports.calls if call[0] == "insert_assistant_message")
    assert assistant_insert[2] == "我有点想确认你是不是还在写代码。"
    assert ports.monitor_logs[0]["screen_check_reasons"] == ["确认她是不是还在写代码"]


def test_core_wake_preflight_exposes_locked_mobile_screen_target_only_when_supplied():
    without_target = build_core_wake_preflight(
        wake_package=_wake_package(),
        execution_context=_execution_context(),
    )
    with_target = build_core_wake_preflight(
        wake_package=_wake_package(),
        execution_context=_execution_context(
            autonomous_mobile_screen_target={
                "device_id": "android_tab",
                "device_name": "华为平板",
                "device_type": "tablet",
                "label": "华为平板",
            },
        ),
    )

    assert "MOBILE_SCREEN_CHECK" not in without_target["core_request"]["core_prompt"]
    prompt = with_target["core_request"]["core_prompt"]
    assert "[MOBILE_SCREEN_CHECK:android_tab|简短原因]" in prompt
    assert "目标已经由系统锁定" in prompt


def test_core_wake_orchestrator_full_execute_requests_mobile_screen_check_from_marker():
    ports = _FakeCoreWakePorts(core_response="我想确认一下。[MOBILE_SCREEN_CHECK:android_tab|确认她平板上在做什么]")

    trace = asyncio.run(run_core_wake_orchestrator_full_execute(
        wake_package=_wake_package(),
        execution_context=_execution_context(
            autonomous_mobile_screen_target={
                "device_id": "android_tab",
                "device_name": "华为平板",
                "device_type": "tablet",
                "label": "华为平板",
            },
        ),
        ports=ports,
        allow_production_side_effects=True,
    ))

    assert trace["status"] == "core_succeeded"
    assert trace["mobile_screen_checks"] == [{"target": "android_tab", "reason": "确认她平板上在做什么"}]
    assert trace["mobile_screen_check_requests"][0]["status"] == "pending"
    assert _step_status(trace, "request_mobile_screen_check") == "succeeded"
    assert "mobile_screen.request" in trace["production_side_effects"]
    assert ("request_mobile_screen_check", "conv_sentinel", "core_1", "mock-model", "android_tab", "确认她平板上在做什么", "core_1", "core_1") in ports.calls
    assistant_insert = next(call for call in ports.calls if call[0] == "insert_assistant_message")
    assert assistant_insert[2] == "我想确认一下。"
    assert ports.monitor_logs[0]["mobile_screen_checks"] == [{"target": "android_tab", "reason": "确认她平板上在做什么"}]


def test_core_wake_orchestrator_executes_mobile_screen_even_when_marker_only():
    ports = _FakeCoreWakePorts(core_response="[MOBILE_SCREEN_CHECK:android_tab|确认她平板上在做什么]")

    trace = asyncio.run(run_core_wake_orchestrator_full_execute(
        wake_package=_wake_package(),
        execution_context=_execution_context(),
        ports=ports,
        allow_production_side_effects=True,
    ))

    assert trace["status"] == "core_succeeded"
    assert trace["mobile_screen_check_requests"][0]["status"] == "pending"
    assistant_insert = next(call for call in ports.calls if call[0] == "insert_assistant_message")
    assert assistant_insert[2] == "我想确认一下你现在在做什么。"


def test_core_wake_orchestrator_full_execute_runs_ring_touch_marker():
    ports = _FakeCoreWakePorts(core_response="我轻轻敲了你两下。[RING:轻轻敲两下，确认她还在]")

    trace = asyncio.run(run_core_wake_orchestrator_full_execute(
        wake_package=_wake_package(),
        execution_context=_execution_context(),
        ports=ports,
        allow_production_side_effects=True,
    ))

    assert trace["status"] == "core_succeeded"
    assert trace["ring_touch_descriptions"] == ["轻轻敲两下，确认她还在"]
    assert trace["ring_touch_delivery"]["status"] == "executed"
    assert ("execute_ring_touch", 1, "conv_sentinel", "core_1", "core_1", "core_1") in ports.calls
    assistant_insert = next(call for call in ports.calls if call[0] == "insert_assistant_message")
    assert assistant_insert[2] == "我轻轻敲了你两下。"
    assert ports.monitor_logs[0]["ring_touch_descriptions"] == ["轻轻敲两下，确认她还在"]
