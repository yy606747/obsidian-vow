"""Dry-run Core wake execution orchestrator for Sentinel Layer 3."""

from __future__ import annotations

import re
import json
from collections.abc import Mapping, Sequence
from copy import deepcopy
from typing import Any

from app.chat.error_text import looks_like_model_error_text
from app.chat.worldbook import build_worldbook_prefix, resolve_worldbook_names
from app.context_delivery import (
    SCHEMA_VERSION as CONTEXT_DELIVERY_SCHEMA_VERSION,
    ContextDeliveryProjection,
    render_context_delivery_projection,
)
from app.tools.prompt_renderers import render_registered_capabilities
from app.tools.registry import validate_turn_advertisement
from app.vows.service import strip_vow_markers
from app.memory_v3.recall_intent import strip_recall_intent_markers
from app.web_search.intent import extract_web_search_intent, strip_web_search_intent_markers

from .core_wake_ports import (
    stored_message_to_dict,
    validate_core_wake_ports,
)
from .eval import RUNTIME_MODE_DRY_RUN
from .gate import GATE_STATUS_PASSED
from .wake_package import (
    CORE_WAKE_PACKAGE_SCHEMA_VERSION,
    SUPPORTED_CORE_WAKE_PACKAGE_SCHEMA_VERSIONS,
)


CORE_WAKE_EXECUTION_SCHEMA_VERSION = "sentinel_core_wake_execution.v1"
CORE_WAKE_EXECUTION_MODE_DISABLED = "disabled"
CORE_WAKE_EXECUTION_MODE_DRY_RUN = "dry_run"
CORE_WAKE_EXECUTION_MODE_TEST_EXECUTE = "test_execute"
CORE_WAKE_EXECUTION_MODE_FULL = "full"
CORE_WAKE_RUNTIME_MODE_FULL = "full"
DEFAULT_CORE_WAKE_PRE_CORE_DELAY_SEC = 5
DEFAULT_CORE_WAKE_MAX_CORE_ATTEMPTS = 2
DEFAULT_CORE_WAKE_RETRY_DELAY_SEC = 10
TEST_CORE_WAKE_PRE_CORE_DELAY_SEC = 0
TEST_CORE_WAKE_MAX_CORE_ATTEMPTS = 1
TEST_CORE_WAKE_RETRY_DELAY_SEC = 0

CORE_WAKE_EXECUTION_MODES = {
    CORE_WAKE_EXECUTION_MODE_DISABLED,
    CORE_WAKE_EXECUTION_MODE_DRY_RUN,
    CORE_WAKE_EXECUTION_MODE_FULL,
    CORE_WAKE_EXECUTION_MODE_TEST_EXECUTE,
}

CORE_WAKE_EXECUTION_STEP_DEFINITIONS = (
    ("validate_preflight", "", False),
    ("prepare_core_messages", "", False),
    ("load_timeline_context", "memory.timeline.prompt_context", False),
    ("pre_core_delay", "timing.sleep.pre_core_delay", False),
    ("stream_core", "core.provider.stream_ai", True),
    ("broadcast_monitor_alert", "websocket.broadcast.monitor_alert", True),
    ("insert_system_wake_notice", "database.insert.system_wake_notice", True),
    ("broadcast_system_msg_created", "websocket.broadcast.msg_created.system_wake_notice", True),
    ("insert_assistant_message", "database.insert.assistant_message", True),
    ("record_timeline_usage", "memory.timeline.record_usage", True),
    ("update_conversation", "database.update.conversation", True),
    ("broadcast_msg_created", "websocket.broadcast.msg_created.assistant_message", True),
    ("request_screen_check", "pc_screen.request", True),
    ("request_mobile_screen_check", "mobile_screen.request", True),
    ("broadcast_toy_command", "websocket.broadcast.toy_command", True),
    ("write_monitor_log", "monitor_log.write.core_result", True),
)
CORE_WAKE_EXECUTION_EFFECT_BY_STEP = {
    name: effect
    for name, effect, _production_side_effect in CORE_WAKE_EXECUTION_STEP_DEFINITIONS
    if effect
}
CORE_WAKE_EXECUTION_PRODUCTION_STEPS = {
    name
    for name, _effect, production_side_effect in CORE_WAKE_EXECUTION_STEP_DEFINITIONS
    if production_side_effect
}

CORE_WAKE_PREFLIGHT_SCHEMA_VERSION = "sentinel_core_wake_preflight.v1"
DEFAULT_CORE_WAKE_HISTORY_LIMIT = 20
DEFAULT_CORE_WAKE_MESSAGE_MAX_CHARS = 500

PLANNED_CORE_WAKE_PRODUCTION_EFFECTS = (
    "core.provider.stream_ai",
    "websocket.broadcast.monitor_alert",
    "database.insert.system_wake_notice",
    "websocket.broadcast.msg_created.system_wake_notice",
    "database.insert.assistant_message",
    "memory.timeline.record_usage",
    "database.update.conversation",
    "websocket.broadcast.msg_created.assistant_message",
    "pc_screen.request",
    "mobile_screen.request",
    "websocket.broadcast.toy_command",
    "monitor_log.write.core_result",
)

_EXECUTION_CONTEXT_ALLOWED_FIELDS = {
    "conv_id",
    "model_key",
    "recent_messages",
    "last_user_message_age_sec",
    "user_name",
    "ai_name",
    "ai_persona",
    "user_persona",
    "toy_capability_allowed",
    "toy_capability_reason",
    "control_session_id",
    "control_kind",
    "control_status",
    "control_epoch",
    "owner_client_id",
    "control_device_id",
    "autonomous_mobile_screen_target",
    "presence_identity_block",
}
_RECENT_MESSAGE_ALLOWED_FIELDS = {"id", "role", "content"}
_RECENT_MESSAGE_ROLES = {"user", "assistant"}
_AUTONOMOUS_MOBILE_TARGET_ALLOWED_FIELDS = {"device_id", "device_name", "device_type", "label"}
_RING_TOUCH_PATTERN = re.compile(r"\[RING:([^\]]+)\]")


def run_core_wake_orchestrator_dry_run(
    *,
    wake_package: Mapping[str, Any],
    execution_context: Mapping[str, Any] | None = None,
    execution_mode: str = CORE_WAKE_EXECUTION_MODE_DISABLED,
    request_id: str = "",
    preflight: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Plan a Core wake execution without running provider, DB, WS or device effects."""
    execution_mode = _validated_execution_mode(execution_mode)
    preflight_payload = _validated_or_built_preflight(
        wake_package=wake_package,
        execution_context=execution_context,
        preflight=preflight,
    )

    if preflight_payload["status"] != "ready":
        return _preflight_failed_execution(
            preflight=preflight_payload,
            execution_mode=execution_mode,
            request_id=request_id,
        )
    if execution_mode == CORE_WAKE_EXECUTION_MODE_DISABLED:
        return _ready_execution(
            preflight=preflight_payload,
            execution_mode=execution_mode,
            request_id=request_id,
            status="disabled",
            would_write_monitor_log="core_orchestrator_disabled",
            context_errors=[],
            error_type="",
            error="",
        )

    trace = _ready_execution(
        preflight=preflight_payload,
        execution_mode=execution_mode,
        request_id=request_id,
        status="dry_run_ready",
        would_write_monitor_log="core_dry_run",
        context_errors=[],
        error_type="",
        error="",
    )
    return trace


async def run_core_wake_orchestrator_test_execute(
    *,
    wake_package: Mapping[str, Any],
    execution_context: Mapping[str, Any] | None = None,
    ports: Any,
    request_id: str = "",
    preflight: Mapping[str, Any] | None = None,
    temperature: Any = None,
    execution_policy: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Execute the wake flow against fake/test ports only.

    This function is for locking execution order and failure semantics before
    real adapters exist. It records fake side effects and never marks production
    side effects as executed.
    """
    ports = validate_core_wake_ports(ports)
    preflight_payload = _validated_or_built_preflight(
        wake_package=wake_package,
        execution_context=execution_context,
        preflight=preflight,
    )
    if preflight_payload["status"] != "ready":
        return _preflight_failed_execution(
            preflight=preflight_payload,
            execution_mode=CORE_WAKE_EXECUTION_MODE_TEST_EXECUTE,
            request_id=request_id,
        )

    return await _execute_core_wake_flow(
        preflight_payload=preflight_payload,
        ports=ports,
        request_id=request_id,
        temperature=temperature,
        execution_mode=CORE_WAKE_EXECUTION_MODE_TEST_EXECUTE,
        runtime_mode=RUNTIME_MODE_DRY_RUN,
        using_fake_ports=True,
        record_production_side_effects=False,
        execution_policy=_validated_execution_policy(
            execution_policy,
            default_pre_core_delay_sec=TEST_CORE_WAKE_PRE_CORE_DELAY_SEC,
            default_max_core_attempts=TEST_CORE_WAKE_MAX_CORE_ATTEMPTS,
            default_retry_delay_sec=TEST_CORE_WAKE_RETRY_DELAY_SEC,
        ),
    )


async def run_core_wake_orchestrator_full_execute(
    *,
    wake_package: Mapping[str, Any],
    execution_context: Mapping[str, Any] | None = None,
    ports: Any,
    request_id: str = "",
    preflight: Mapping[str, Any] | None = None,
    temperature: Any = None,
    allow_production_side_effects: bool = False,
    execution_policy: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Execute the Core wake flow through real ports.

    The explicit ``allow_production_side_effects`` flag prevents accidental
    production execution while this path is still being built and tested.
    Runtime wiring must opt in deliberately.
    """
    if allow_production_side_effects is not True:
        raise ValueError("core wake full execution requires allow_production_side_effects=true")
    ports = validate_core_wake_ports(ports)
    preflight_payload = _validated_or_built_preflight(
        wake_package=wake_package,
        execution_context=execution_context,
        preflight=preflight,
    )
    if preflight_payload["status"] != "ready":
        return _preflight_failed_execution(
            preflight=preflight_payload,
            execution_mode=CORE_WAKE_EXECUTION_MODE_FULL,
            request_id=request_id,
            runtime_mode=CORE_WAKE_RUNTIME_MODE_FULL,
        )

    return await _execute_core_wake_flow(
        preflight_payload=preflight_payload,
        ports=ports,
        request_id=request_id,
        temperature=temperature,
        execution_mode=CORE_WAKE_EXECUTION_MODE_FULL,
        runtime_mode=CORE_WAKE_RUNTIME_MODE_FULL,
        using_fake_ports=False,
        record_production_side_effects=True,
        execution_policy=_validated_execution_policy(
            execution_policy,
            default_pre_core_delay_sec=DEFAULT_CORE_WAKE_PRE_CORE_DELAY_SEC,
            default_max_core_attempts=DEFAULT_CORE_WAKE_MAX_CORE_ATTEMPTS,
            default_retry_delay_sec=DEFAULT_CORE_WAKE_RETRY_DELAY_SEC,
        ),
    )


async def _execute_core_wake_flow(
    *,
    preflight_payload: Mapping[str, Any],
    ports: Any,
    request_id: str,
    temperature: Any,
    execution_mode: str,
    runtime_mode: str,
    using_fake_ports: bool,
    record_production_side_effects: bool,
    execution_policy: Mapping[str, Any],
) -> dict[str, Any]:
    state = _execution_state(
        preflight=preflight_payload,
        request_id=request_id,
        execution_mode=execution_mode,
        runtime_mode=runtime_mode,
        using_fake_ports=using_fake_ports,
        execution_policy=execution_policy,
    )
    _mark_step(state, "validate_preflight", "succeeded")
    messages = _core_messages_from_preflight(preflight_payload)
    _mark_step(state, "prepare_core_messages", "succeeded")

    # 誓约常驻注入（誓约设计 §5.1/§5.2）：经可选 port 读取——生产 ports 提供
    # load_vow_prompt_context，fake/test ports 不提供则不注入。读取失败 → 系统
    # 主动路径，跳过本次生成并记录，不插唤醒提示、不开口。
    vow_messages: list[dict[str, str]] = []
    load_vow_context = getattr(ports, "load_vow_prompt_context", None)
    if load_vow_context is not None:
        try:
            vow_block, _vow_ability = await load_vow_context()
        except Exception as exc:
            for name in (
                "load_timeline_context",
                "broadcast_monitor_alert",
                "insert_system_wake_notice",
                "broadcast_system_msg_created",
                "pre_core_delay",
                "stream_core",
            ):
                _mark_step(state, name, "skipped")
            _skip_after_stream_failure(state)
            state["status"] = "vow_read_failed"
            state["would_write_monitor_log"] = "vow_read_failed"
            state["error_type"] = "vow_read_failed"
            state["error"] = str(exc)
            await _write_result_monitor_log(
                ports,
                state,
                preflight_payload,
                record_production_side_effects=record_production_side_effects,
            )
            return state
        if vow_block:
            vow_messages = [
                {"role": "user", "content": vow_block},
                {"role": "assistant", "content": "（嗯，这些一直都算数。）"},
            ]

    # 认识层与欲望层共用 Working Model V2 的既有开关，并由生产 port
    # 一次读取两个 durable head。port 不提供（纯测试）或开关关闭时均不注入。
    relationship_messages: list[dict[str, str]] = []
    load_working_model_context = getattr(ports, "load_working_model_prompt_context", None)
    if load_working_model_context is not None:
        try:
            working_model_block, desire_block = await load_working_model_context()
        except Exception as exc:
            for name in (
                "load_timeline_context",
                "broadcast_monitor_alert",
                "insert_system_wake_notice",
                "broadcast_system_msg_created",
                "pre_core_delay",
                "stream_core",
            ):
                _mark_step(state, name, "skipped")
            _skip_after_stream_failure(state)
            state["status"] = "working_model_read_failed"
            state["would_write_monitor_log"] = "working_model_read_failed"
            state["error_type"] = "working_model_read_failed"
            state["error"] = str(exc)
            await _write_result_monitor_log(
                ports,
                state,
                preflight_payload,
                record_production_side_effects=record_production_side_effects,
            )
            return state
        if working_model_block:
            relationship_messages.extend([
                {"role": "user", "content": working_model_block},
                {"role": "assistant", "content": "（嗯，这是我此刻对她的认识。）"},
            ])
        if desire_block:
            relationship_messages.extend([
                {"role": "user", "content": desire_block},
                {"role": "assistant", "content": "（嗯，这是我此刻想带进这段关系里的姿态。）"},
            ])

    # P2: proactive Core wake gets the existing gated recent Timeline as
    # current narrative state. It deliberately does not perform query recall.
    timeline_meta: dict[str, Any] = {"status": "unavailable", "block": "", "entries": []}
    timeline_messages: list[dict[str, str]] = []
    load_timeline_context = getattr(ports, "load_timeline_prompt_context", None)
    if load_timeline_context is None:
        _mark_step(state, "load_timeline_context", "skipped")
    else:
        try:
            loaded_timeline = await load_timeline_context(
                visible_message_ids=preflight_payload.get("visible_message_ids") or [],
                now=ports.now(),
            )
            if not isinstance(loaded_timeline, Mapping):
                raise ValueError("timeline prompt context must be an object")
            timeline_meta = dict(loaded_timeline)
            timeline_block = str(timeline_meta.get("block") or "").strip()
            if timeline_block:
                timeline_messages = [
                    {"role": "user", "content": timeline_block},
                    {"role": "assistant", "content": "（嗯，近几天的事我还记得。）"},
                ]
            _record_step_effect(
                state,
                "load_timeline_context",
                record_production_side_effects=record_production_side_effects,
            )
            _mark_step(state, "load_timeline_context", "succeeded")
        except Exception as exc:
            _record_step_effect(
                state,
                "load_timeline_context",
                record_production_side_effects=record_production_side_effects,
            )
            state["context_errors"].append(
                f"timeline_context_failed: {type(exc).__name__}: {exc}"
            )
            _mark_step(state, "load_timeline_context", "failed_non_blocking")

    web_search_meta: dict[str, Any] = {"status": "disabled", "block": ""}
    prepare_web_search = getattr(ports, "prepare_web_search_turn", None)
    if prepare_web_search is not None:
        web_turn_id = f"sentinel:{request_id or int(ports.now() * 1000)}"
        try:
            prepared_web = await prepare_web_search(
                conv_id=preflight_payload["conv_id"],
                bound_turn_id=web_turn_id,
            )
            if isinstance(prepared_web, Mapping):
                web_search_meta = dict(prepared_web)
                web_search_meta.setdefault("bound_turn_id", web_turn_id)
        except Exception as exc:
            state["context_errors"].append(
                f"web_search_prepare_failed: {type(exc).__name__}: {exc}"
            )

    web_messages: list[dict[str, str]] = []
    if str(web_search_meta.get("block") or "").strip():
        web_messages = [
            {"role": "user", "content": str(web_search_meta["block"])},
            {"role": "assistant", "content": "（嗯，查询能力和已经返回的资料我都清楚。）"},
        ]
    core_messages = (
        vow_messages
        + relationship_messages
        + timeline_messages
        + messages[:-1]
        + web_messages
        + messages[-1:]
    )
    if execution_policy["pre_core_delay_sec"] > 0:
        await ports.sleep(execution_policy["pre_core_delay_sec"])
        state["timing_events"].append({
            "name": "pre_core_delay",
            "seconds": execution_policy["pre_core_delay_sec"],
        })
        _mark_step(state, "pre_core_delay", "succeeded")
    else:
        _mark_step(state, "pre_core_delay", "skipped")

    stream_result = await _stream_core_with_retry(
        ports=ports,
        state=state,
        preflight_payload=preflight_payload,
        core_messages=core_messages,
        temperature=temperature,
        execution_policy=execution_policy,
        record_production_side_effects=record_production_side_effects,
    )
    if stream_result["status"] != "ready":
        _skip_after_stream_failure(state)
        state["status"] = stream_result["status"]
        state["would_write_monitor_log"] = stream_result["would_write_monitor_log"]
        state["error_type"] = stream_result["error_type"]
        state["error"] = stream_result["error"]
        await _write_result_monitor_log(
            ports,
            state,
            preflight_payload,
            record_production_side_effects=record_production_side_effects,
        )
        return state

    full_content = stream_result["content"]
    toy_commands = stream_result["toy_commands"]
    screen_check_reasons = stream_result["screen_check_reasons"]
    mobile_screen_checks = stream_result.get("mobile_screen_checks") or []
    ring_touch_descriptions = stream_result.get("ring_touch_descriptions") or []
    web_search_intent = str(stream_result.get("web_search_intent") or "")
    if toy_commands:
        state["toy_commands"] = list(toy_commands)
    if screen_check_reasons:
        state["screen_check_reasons"] = list(screen_check_reasons)
    if mobile_screen_checks:
        state["mobile_screen_checks"] = list(mobile_screen_checks)
    if ring_touch_descriptions:
        state["ring_touch_descriptions"] = list(ring_touch_descriptions)

    # Do not tell the user that Sentinel woke Core until Core has produced a
    # usable reply. A deploy/shutdown may cancel the slow provider call; keeping
    # all visible wake effects after it prevents an orphan system notice.
    await ports.broadcast_monitor_alert(
        preflight_payload["core_request"].get("monitor_alert")
        or preflight_payload["core_request"]["system_notice"]
    )
    _record_step_effect(
        state,
        "broadcast_monitor_alert",
        record_production_side_effects=record_production_side_effects,
    )
    _mark_step(state, "broadcast_monitor_alert", "succeeded")

    system_message = await ports.insert_system_wake_notice(
        conv_id=preflight_payload["conv_id"],
        content=preflight_payload["core_request"]["system_notice"],
        created_at=ports.now(),
    )
    system_message = stored_message_to_dict(system_message, label="system")
    _record_step_effect(
        state,
        "insert_system_wake_notice",
        record_production_side_effects=record_production_side_effects,
    )
    state["system_msg_id"] = system_message["id"]
    _mark_step(state, "insert_system_wake_notice", "succeeded")

    await ports.broadcast_msg_created(system_message)
    _record_step_effect(
        state,
        "broadcast_system_msg_created",
        record_production_side_effects=record_production_side_effects,
    )
    _mark_step(state, "broadcast_system_msg_created", "succeeded")

    assistant_message = await ports.insert_assistant_message(
        conv_id=preflight_payload["conv_id"],
        content=full_content,
        created_at=ports.now(),
    )
    assistant_message = stored_message_to_dict(assistant_message, label="assistant")
    _record_step_effect(
        state,
        "insert_assistant_message",
        record_production_side_effects=record_production_side_effects,
    )
    state["core_msg_id"] = assistant_message["id"]
    _mark_step(state, "insert_assistant_message", "succeeded")

    record_timeline_usage = getattr(ports, "record_timeline_injection_usage", None)
    if record_timeline_usage is None:
        _mark_step(state, "record_timeline_usage", "skipped")
    else:
        try:
            await record_timeline_usage(
                timeline_meta,
                conv_id=preflight_payload["conv_id"],
                assistant_message_id=assistant_message["id"],
                response_text=full_content,
            )
            _record_step_effect(
                state,
                "record_timeline_usage",
                record_production_side_effects=record_production_side_effects,
            )
            _mark_step(state, "record_timeline_usage", "succeeded")
        except Exception as exc:
            _record_step_effect(
                state,
                "record_timeline_usage",
                record_production_side_effects=record_production_side_effects,
            )
            state["context_errors"].append(
                f"timeline_usage_record_failed: {type(exc).__name__}: {exc}"
            )
            _mark_step(state, "record_timeline_usage", "failed_non_blocking")

    finalize_web_search = getattr(ports, "finalize_web_search_turn", None)
    if finalize_web_search is not None:
        try:
            web_result = await finalize_web_search(
                conv_id=preflight_payload["conv_id"],
                bound_turn_id=str(web_search_meta.get("bound_turn_id") or ""),
                assistant_message_id=assistant_message["id"],
                intent_text=web_search_intent,
            )
            if isinstance(web_result, Mapping):
                state["web_search"] = dict(web_result)
        except Exception as exc:
            state["context_errors"].append(
                f"web_search_finalize_failed: {type(exc).__name__}: {exc}"
            )

    await ports.update_conversation(conv_id=preflight_payload["conv_id"], updated_at=assistant_message["created_at"])
    _record_step_effect(
        state,
        "update_conversation",
        record_production_side_effects=record_production_side_effects,
    )
    _mark_step(state, "update_conversation", "succeeded")

    await ports.broadcast_msg_created(assistant_message)
    _record_step_effect(
        state,
        "broadcast_msg_created",
        record_production_side_effects=record_production_side_effects,
    )
    _mark_step(state, "broadcast_msg_created", "succeeded")

    if screen_check_reasons:
        screen_requests = await _request_screen_checks(
            ports=ports,
            state=state,
            reasons=screen_check_reasons,
            conv_id=preflight_payload["conv_id"],
            msg_id=assistant_message["id"],
            model_key=preflight_payload["model_key"],
            request_id=request_id or assistant_message["id"],
            record_production_side_effects=record_production_side_effects,
        )
        if screen_requests:
            state["screen_check_requests"] = screen_requests
        _mark_step(state, "request_screen_check", "succeeded" if screen_requests else "skipped")
    else:
        _mark_step(state, "request_screen_check", "skipped")

    if mobile_screen_checks:
        mobile_requests = await _request_mobile_screen_checks(
            ports=ports,
            state=state,
            checks=mobile_screen_checks,
            conv_id=preflight_payload["conv_id"],
            msg_id=assistant_message["id"],
            model_key=preflight_payload["model_key"],
            request_id=request_id or assistant_message["id"],
            record_production_side_effects=record_production_side_effects,
        )
        if mobile_requests:
            state["mobile_screen_check_requests"] = mobile_requests
        _mark_step(state, "request_mobile_screen_check", "succeeded" if mobile_requests else "skipped")
    else:
        _mark_step(state, "request_mobile_screen_check", "skipped")

    if toy_commands:
        toy_delivery = await ports.broadcast_toy_command(
            commands=toy_commands,
            msg_id=assistant_message["id"],
            conv_id=preflight_payload["conv_id"],
            toy_capability_allowed=bool(preflight_payload.get("toy_capability_allowed", False)),
            control_session_id=preflight_payload.get("control_session_id") or None,
            control_epoch=preflight_payload.get("control_epoch"),
            owner_client_id=preflight_payload.get("owner_client_id") or None,
            control_device_id=preflight_payload.get("control_device_id") or None,
            request_id=request_id or assistant_message["id"],
            wake_id=request_id or assistant_message["id"],
        )
        toy_delivery = _normalize_toy_delivery(toy_delivery, commands=toy_commands)
        state["toy_command_delivery"] = toy_delivery
        _record_toy_delivery_effects(
            state,
            toy_delivery,
            record_production_side_effects=record_production_side_effects,
        )
        _mark_step(state, "broadcast_toy_command", "succeeded" if toy_delivery["broadcast"] else "skipped")
    else:
        _mark_step(state, "broadcast_toy_command", "skipped")

    if ring_touch_descriptions:
        ring_delivery = await _execute_ring_touches(
            ports=ports,
            state=state,
            touch_descriptions=ring_touch_descriptions,
            conv_id=preflight_payload["conv_id"],
            msg_id=assistant_message["id"],
            model_key=preflight_payload["model_key"],
            request_id=request_id or assistant_message["id"],
            record_production_side_effects=record_production_side_effects,
        )
        state["ring_touch_delivery"] = ring_delivery

    state["status"] = "core_succeeded"
    state["would_write_monitor_log"] = "core_succeeded"
    await _write_result_monitor_log(
        ports,
        state,
        preflight_payload,
        record_production_side_effects=record_production_side_effects,
    )
    return state


def _validated_execution_mode(value: str) -> str:
    if not isinstance(value, str):
        raise ValueError("core wake orchestrator execution_mode must be text")
    normalized = value.strip()
    if normalized == CORE_WAKE_EXECUTION_MODE_TEST_EXECUTE:
        raise ValueError("core wake orchestrator test_execute requires test ports")
    if normalized == CORE_WAKE_EXECUTION_MODE_FULL:
        raise ValueError("core wake orchestrator full execution requires full_execute")
    if normalized not in {CORE_WAKE_EXECUTION_MODE_DISABLED, CORE_WAKE_EXECUTION_MODE_DRY_RUN}:
        raise ValueError("core wake orchestrator only supports disabled/dry_run execution")
    return normalized


def _validated_execution_policy(
    value: Mapping[str, Any] | None,
    *,
    default_pre_core_delay_sec: int,
    default_max_core_attempts: int,
    default_retry_delay_sec: int,
) -> dict[str, int | float]:
    if value is None:
        value = {}
    if not isinstance(value, Mapping):
        raise ValueError("core wake execution_policy must be an object")
    allowed = {"max_core_attempts", "pre_core_delay_sec", "retry_delay_sec"}
    unknown = sorted(set(value.keys()).difference(allowed))
    if unknown:
        raise ValueError(f"core wake execution_policy unknown fields: {unknown!r}")
    return {
        "pre_core_delay_sec": _non_negative_number(
            value.get("pre_core_delay_sec", default_pre_core_delay_sec),
            key="pre_core_delay_sec",
        ),
        "max_core_attempts": _positive_int(
            value.get("max_core_attempts", default_max_core_attempts),
            key="max_core_attempts",
        ),
        "retry_delay_sec": _non_negative_number(
            value.get("retry_delay_sec", default_retry_delay_sec),
            key="retry_delay_sec",
        ),
    }


def _non_negative_number(value: Any, *, key: str) -> int | float:
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise ValueError(f"core wake execution_policy {key} must be a number")
    if value < 0:
        raise ValueError(f"core wake execution_policy {key} must be non-negative")
    return value


def _positive_int(value: Any, *, key: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"core wake execution_policy {key} must be an integer")
    if value <= 0:
        raise ValueError(f"core wake execution_policy {key} must be positive")
    return value


def _validated_or_built_preflight(
    *,
    wake_package: Mapping[str, Any],
    execution_context: Mapping[str, Any] | None,
    preflight: Mapping[str, Any] | None,
) -> dict[str, Any]:
    if preflight is None:
        return build_core_wake_preflight(
            wake_package=wake_package,
            execution_context=execution_context,
        )
    if not isinstance(preflight, Mapping):
        raise ValueError("core wake orchestrator preflight must be an object")
    return dict(preflight)


def _preflight_failed_execution(
    *,
    preflight: Mapping[str, Any],
    execution_mode: str,
    request_id: str,
    runtime_mode: str = RUNTIME_MODE_DRY_RUN,
) -> dict[str, Any]:
    trace = {
        "schema_version": CORE_WAKE_EXECUTION_SCHEMA_VERSION,
        "runtime_mode": runtime_mode,
        "execution_mode": execution_mode,
        "status": "preflight_failed",
        "request_id": request_id,
        "side_effects": [],
        "production_side_effects": [],
        "planned_production_side_effects": [],
        "fallback_used": False,
        "fallback_reason": "",
        "execution_enabled": False,
        "would_call_core": False,
        "would_write_monitor_log": preflight.get("would_write_monitor_log", "core_preflight_failed"),
        "conv_id": preflight.get("conv_id", ""),
        "model_key": preflight.get("model_key", ""),
        "wake_reason": preflight.get("wake_reason", ""),
        "preflight": _compact_preflight(preflight),
        "core_request": {},
        "execution_steps": _steps_for_preflight_failed(),
        "context_errors": [],
        "error_type": preflight.get("error_type", "preflight_failed"),
        "error": preflight.get("error", "core wake preflight failed"),
    }
    return trace


def _ready_execution(
    *,
    preflight: Mapping[str, Any],
    execution_mode: str,
    request_id: str,
    status: str,
    would_write_monitor_log: str,
    context_errors: list[str],
    error_type: str,
    error: str,
) -> dict[str, Any]:
    trace = {
        "schema_version": CORE_WAKE_EXECUTION_SCHEMA_VERSION,
        "runtime_mode": RUNTIME_MODE_DRY_RUN,
        "execution_mode": execution_mode,
        "status": status,
        "request_id": request_id,
        "side_effects": [],
        "production_side_effects": [],
        "planned_production_side_effects": list(preflight["planned_production_side_effects"]),
        "fallback_used": False,
        "fallback_reason": "",
        "execution_enabled": False,
        "would_call_core": preflight["would_call_core"],
        "would_write_monitor_log": would_write_monitor_log,
        "conv_id": preflight["conv_id"],
        "model_key": preflight["model_key"],
        "wake_reason": preflight["wake_reason"],
        "preflight": _compact_preflight(preflight),
        "core_request": _compact_core_request(preflight),
        "execution_steps": _all_planned_steps(),
        "context_errors": context_errors,
    }
    if error_type:
        trace["error_type"] = error_type
    if error:
        trace["error"] = error
    return trace


def _execution_state(
    *,
    preflight: Mapping[str, Any],
    request_id: str,
    execution_mode: str,
    runtime_mode: str,
    using_fake_ports: bool,
    execution_policy: Mapping[str, Any],
) -> dict[str, Any]:
    state = {
        "schema_version": CORE_WAKE_EXECUTION_SCHEMA_VERSION,
        "runtime_mode": runtime_mode,
        "execution_mode": execution_mode,
        "status": "running",
        "request_id": request_id,
        "side_effects": [],
        "production_side_effects": [],
        "planned_production_side_effects": list(preflight["planned_production_side_effects"]),
        "fallback_used": False,
        "fallback_reason": "",
        "execution_enabled": True,
        "would_call_core": preflight["would_call_core"],
        "would_write_monitor_log": "",
        "conv_id": preflight["conv_id"],
        "model_key": preflight["model_key"],
        "wake_reason": preflight["wake_reason"],
        "preflight": _compact_preflight(preflight),
        "core_request": _compact_core_request(preflight),
        "execution_policy": dict(execution_policy),
        "execution_steps": _steps_for_test_execute_initial(),
        "context_errors": [],
        "core_attempts": [],
        "timing_events": [],
    }
    if using_fake_ports:
        state["using_fake_ports"] = True
    return state


def _core_messages_from_preflight(preflight: Mapping[str, Any]) -> list[dict[str, str]]:
    core_request = preflight.get("core_request")
    if not isinstance(core_request, Mapping):
        raise ValueError("core wake test execution requires full preflight core_request")
    messages = core_request.get("messages")
    if not isinstance(messages, Sequence) or isinstance(messages, str | bytes):
        raise ValueError("core wake test execution requires full preflight core_request messages")
    result = []
    for index, message in enumerate(messages):
        if not isinstance(message, Mapping):
            raise ValueError(f"core wake test execution message {index} must be an object")
        role = message.get("role")
        content = message.get("content")
        if role not in {"assistant", "user"}:
            raise ValueError(f"core wake test execution message {index} role must be user or assistant")
        if not isinstance(content, str) or not content.strip():
            raise ValueError(f"core wake test execution message {index} content must be non-empty text")
        result.append({"role": role, "content": content.strip()})
    return result


async def _stream_core_with_retry(
    *,
    ports: Any,
    state: dict[str, Any],
    preflight_payload: Mapping[str, Any],
    core_messages: Sequence[Mapping[str, str]],
    temperature: Any,
    execution_policy: Mapping[str, Any],
    record_production_side_effects: bool,
) -> dict[str, Any]:
    max_attempts = int(execution_policy["max_core_attempts"])
    for attempt in range(1, max_attempts + 1):
        attempt_record: dict[str, Any] = {"attempt": attempt}
        try:
            full_content = await ports.stream_core(
                messages=core_messages,
                model_key=preflight_payload["model_key"],
                temperature=temperature,
            )
        except Exception as exc:
            _record_step_effect(
                state,
                "stream_core",
                record_production_side_effects=record_production_side_effects,
            )
            attempt_record.update({
                "status": "failed",
                "error_type": type(exc).__name__,
                "error": str(exc),
            })
            if attempt < max_attempts:
                attempt_record["retry_after_sec"] = execution_policy["retry_delay_sec"]
                state["core_attempts"].append(attempt_record)
                await _sleep_before_retry(ports, state, execution_policy, attempt=attempt)
                continue
            state["core_attempts"].append(attempt_record)
            _mark_step(state, "stream_core", "failed")
            return {
                "status": "core_failed",
                "would_write_monitor_log": "core_failed",
                "error_type": type(exc).__name__,
                "error": str(exc),
            }

        _record_step_effect(
            state,
            "stream_core",
            record_production_side_effects=record_production_side_effects,
        )
        if not isinstance(full_content, str):
            raise ValueError("core wake provider port must return text")

        full_content = full_content.strip()
        if not full_content:
            attempt_record["status"] = "empty"
            if attempt < max_attempts:
                attempt_record["retry_after_sec"] = execution_policy["retry_delay_sec"]
                state["core_attempts"].append(attempt_record)
                await _sleep_before_retry(ports, state, execution_policy, attempt=attempt)
                continue
            state["core_attempts"].append(attempt_record)
            _mark_step(state, "stream_core", "empty")
            return {
                "status": "core_empty",
                "would_write_monitor_log": "core_empty",
                "error_type": "core_empty",
                "error": "core provider returned empty content",
            }

        if looks_like_model_error_text(full_content):
            visible_error = strip_web_search_intent_markers(
                strip_recall_intent_markers(full_content)
            )[:500]
            attempt_record.update({
                "status": "provider_error_text",
                "error_type": "core_provider_error_text",
                "error": visible_error,
            })
            if attempt < max_attempts:
                attempt_record["retry_after_sec"] = execution_policy["retry_delay_sec"]
                state["core_attempts"].append(attempt_record)
                await _sleep_before_retry(ports, state, execution_policy, attempt=attempt)
                continue
            state["core_attempts"].append(attempt_record)
            _mark_step(state, "stream_core", "failed")
            return {
                "status": "core_failed",
                "would_write_monitor_log": "core_failed",
                "error_type": "core_provider_error_text",
                "error": visible_error,
            }

        full_content, _structured_actions = _extract_structured_reply(full_content)
        # strip 先于 toy / screen / ring 提取（誓约设计 §4.4 硬约束）：本路径不允许
        # 立约，[VOW:] 内部是惰性文本，剥除后其中的工具标记绝不进入解析。
        full_content = strip_vow_markers(full_content)
        full_content, web_search_intent = extract_web_search_intent(full_content)
        full_content = strip_recall_intent_markers(full_content)
        full_content, ring_touch_descriptions = _extract_ring_touch_markers(full_content)
        full_content, toy_commands = _strip_toy_commands(full_content)
        full_content, screen_check_reasons = _strip_screen_check_commands(full_content)
        full_content, mobile_screen_checks = _strip_mobile_screen_check_commands(full_content)
        if not full_content and (screen_check_reasons or mobile_screen_checks):
            full_content = _screen_tool_only_fallback_text(screen_check_reasons, mobile_screen_checks)
        if not full_content:
            attempt_record["status"] = "empty_after_tool_strip"
            if attempt < max_attempts:
                attempt_record["retry_after_sec"] = execution_policy["retry_delay_sec"]
                state["core_attempts"].append(attempt_record)
                await _sleep_before_retry(ports, state, execution_policy, attempt=attempt)
                continue
            state["core_attempts"].append(attempt_record)
            _mark_step(state, "stream_core", "empty_after_tool_strip")
            return {
                "status": "core_empty",
                "would_write_monitor_log": "core_empty",
                "error_type": "core_empty",
                "error": "core provider returned only toy commands",
            }

        attempt_record["status"] = "succeeded"
        if toy_commands:
            attempt_record["toy_command_count"] = len(toy_commands)
        if screen_check_reasons:
            attempt_record["screen_check_count"] = len(screen_check_reasons)
        if mobile_screen_checks:
            attempt_record["mobile_screen_check_count"] = len(mobile_screen_checks)
        if ring_touch_descriptions:
            attempt_record["ring_touch_count"] = len(ring_touch_descriptions)
        state["core_attempts"].append(attempt_record)
        _mark_step(state, "stream_core", "succeeded")
        return {
            "status": "ready",
            "content": full_content,
            "toy_commands": toy_commands,
            "screen_check_reasons": screen_check_reasons,
            "mobile_screen_checks": mobile_screen_checks,
            "ring_touch_descriptions": ring_touch_descriptions,
            "web_search_intent": web_search_intent,
        }

    raise ValueError("core wake retry loop exited unexpectedly")


async def _sleep_before_retry(
    ports: Any,
    state: dict[str, Any],
    execution_policy: Mapping[str, Any],
    *,
    attempt: int,
) -> None:
    seconds = execution_policy["retry_delay_sec"]
    if seconds <= 0:
        return
    await ports.sleep(seconds)
    state["timing_events"].append({
        "name": "core_retry_delay",
        "after_attempt": attempt,
        "seconds": seconds,
    })


def _steps_for_test_execute_initial() -> list[dict[str, Any]]:
    return [
        _step(name, effect, production_side_effect, "pending")
        for name, effect, production_side_effect in CORE_WAKE_EXECUTION_STEP_DEFINITIONS
    ]


def _mark_step(state: dict[str, Any], name: str, status: str) -> None:
    for step in state["execution_steps"]:
        if step["name"] == name:
            step["status"] = status
            return
    raise ValueError(f"core wake test execution unknown step {name!r}")


def _skip_after_stream_failure(state: dict[str, Any]) -> None:
    for name in (
        "broadcast_monitor_alert",
        "insert_system_wake_notice",
        "broadcast_system_msg_created",
        "insert_assistant_message",
        "record_timeline_usage",
        "update_conversation",
        "broadcast_msg_created",
        "request_screen_check",
        "request_mobile_screen_check",
        "broadcast_toy_command",
    ):
        _mark_step(state, name, "skipped")


async def _request_screen_checks(
    *,
    ports: Any,
    state: dict[str, Any],
    reasons: Sequence[str],
    conv_id: str,
    msg_id: str,
    model_key: str,
    request_id: str,
    record_production_side_effects: bool,
) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    for index, reason in enumerate(reasons, 1):
        try:
            result = await ports.request_screen_check(
                conv_id=conv_id,
                msg_id=msg_id,
                model_key=model_key,
                reason=reason,
                request_id=request_id,
                wake_id=request_id,
            )
            _record_step_effect(
                state,
                "request_screen_check",
                record_production_side_effects=record_production_side_effects,
            )
        except Exception as exc:
            state["context_errors"].append(f"screen_check_failed[{index}]: {type(exc).__name__}: {exc}")
            continue
        if isinstance(result, Mapping):
            payload = dict(result)
        else:
            payload = {"status": "unknown"}
        payload.setdefault("reason", reason)
        payload.setdefault("index", index)
        results.append(payload)
    return results


async def _request_mobile_screen_checks(
    *,
    ports: Any,
    state: dict[str, Any],
    checks: Sequence[Mapping[str, str]],
    conv_id: str,
    msg_id: str,
    model_key: str,
    request_id: str,
    record_production_side_effects: bool,
) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    for index, check in enumerate(checks, 1):
        target = str(check.get("target") or "")
        reason = str(check.get("reason") or "")
        try:
            result = await ports.request_mobile_screen_check(
                conv_id=conv_id,
                msg_id=msg_id,
                model_key=model_key,
                target=target,
                reason=reason,
                request_id=request_id,
                wake_id=request_id,
            )
            _record_step_effect(
                state,
                "request_mobile_screen_check",
                record_production_side_effects=record_production_side_effects,
            )
        except Exception as exc:
            state["context_errors"].append(
                f"mobile_screen_check_failed[{index}]: {type(exc).__name__}: {exc}")
            continue
        if isinstance(result, Mapping):
            payload = dict(result)
        else:
            payload = {"status": "unknown"}
        payload.setdefault("reason", reason)
        payload.setdefault("target", target)
        payload.setdefault("index", index)
        results.append(payload)
    return results


async def _execute_ring_touches(
    *,
    ports: Any,
    state: dict[str, Any],
    touch_descriptions: Sequence[str],
    conv_id: str,
    msg_id: str,
    model_key: str,
    request_id: str,
    record_production_side_effects: bool,
) -> dict[str, Any]:
    handler = getattr(ports, "execute_ring_touch", None)
    if not callable(handler):
        return {"status": "unsupported", "count": len(touch_descriptions)}
    try:
        result = await handler(
            touch_descriptions=touch_descriptions,
            conv_id=conv_id,
            msg_id=msg_id,
            model_key=model_key,
            request_id=request_id,
            wake_id=request_id,
        )
        _record_custom_effect(state, "device.ring_touch", record_production_side_effects=record_production_side_effects)
        return dict(result) if isinstance(result, Mapping) else {"status": "unknown", "count": len(touch_descriptions)}
    except Exception as exc:
        state["context_errors"].append(f"ring_touch_failed: {type(exc).__name__}: {exc}")
        return {"status": "failed", "count": len(touch_descriptions), "error": type(exc).__name__}


async def _write_result_monitor_log(
    ports: Any,
    state: dict[str, Any],
    preflight: Mapping[str, Any],
    *,
    record_production_side_effects: bool,
) -> None:
    entry = {
        "status": state["would_write_monitor_log"],
        "call_core": False,
        "conv_id": preflight["conv_id"],
        "core_reason": preflight["wake_reason"],
        "summary": preflight.get("sentinel", {}).get("summary", ""),
        "context_errors": list(state["context_errors"]),
    }
    if state.get("core_msg_id"):
        entry["core_msg_id"] = state["core_msg_id"]
    if state.get("toy_commands"):
        entry["toy_commands"] = list(state["toy_commands"])
    if state.get("toy_command_delivery"):
        entry["toy_command_delivery"] = dict(state["toy_command_delivery"])
    if state.get("screen_check_reasons"):
        entry["screen_check_reasons"] = list(state["screen_check_reasons"])
    if state.get("screen_check_requests"):
        entry["screen_check_requests"] = list(state["screen_check_requests"])
    if state.get("mobile_screen_checks"):
        entry["mobile_screen_checks"] = list(state["mobile_screen_checks"])
    if state.get("mobile_screen_check_requests"):
        entry["mobile_screen_check_requests"] = list(state["mobile_screen_check_requests"])
    if state.get("ring_touch_descriptions"):
        entry["ring_touch_descriptions"] = list(state["ring_touch_descriptions"])
    if state.get("ring_touch_delivery"):
        entry["ring_touch_delivery"] = dict(state["ring_touch_delivery"])
    if state.get("error_type"):
        entry["error_type"] = state["error_type"]
    if state.get("error"):
        entry["error"] = state["error"]
    await ports.write_monitor_log(entry)
    _record_step_effect(
        state,
        "write_monitor_log",
        record_production_side_effects=record_production_side_effects,
    )
    _mark_step(state, "write_monitor_log", "succeeded")


def _record_step_effect(
    state: dict[str, Any],
    step_name: str,
    *,
    record_production_side_effects: bool,
) -> None:
    effect = CORE_WAKE_EXECUTION_EFFECT_BY_STEP.get(step_name)
    if not effect:
        return
    if state.get("using_fake_ports"):
        state["side_effects"].append(f"fake.{effect}")
        return
    state["side_effects"].append(effect)
    if record_production_side_effects and step_name in CORE_WAKE_EXECUTION_PRODUCTION_STEPS:
        state["production_side_effects"].append(effect)


def _normalize_toy_delivery(delivery: Any, *, commands: Sequence[str]) -> dict[str, Any]:
    if not isinstance(delivery, Mapping):
        return {
            "status": "broadcasted",
            "broadcast": True,
            "commands": list(commands),
        }
    payload = dict(delivery)
    payload["commands"] = list(payload.get("commands") or commands)
    payload["broadcast"] = bool(payload.get("broadcast", False))
    payload["status"] = str(payload.get("status") or ("broadcasted" if payload["broadcast"] else "rejected"))
    return payload


def _record_toy_delivery_effects(
    state: dict[str, Any],
    delivery: Mapping[str, Any],
    *,
    record_production_side_effects: bool,
) -> None:
    status = str(delivery.get("status") or "")
    if status == "gateway_accepted":
        _record_custom_effect(state, "control.gateway.execute_toy_command", record_production_side_effects=record_production_side_effects)
    elif status in {"gateway_rejected", "rejected", "skipped"}:
        _record_custom_effect(state, "control.gateway.reject_toy_command", record_production_side_effects=record_production_side_effects)
    if delivery.get("broadcast"):
        _record_step_effect(
            state,
            "broadcast_toy_command",
            record_production_side_effects=record_production_side_effects,
        )


def _record_custom_effect(
    state: dict[str, Any],
    effect: str,
    *,
    record_production_side_effects: bool,
) -> None:
    if state.get("using_fake_ports"):
        state["side_effects"].append(f"fake.{effect}")
        return
    state["side_effects"].append(effect)
    if record_production_side_effects:
        state["production_side_effects"].append(effect)


def _strip_toy_commands(content: str) -> tuple[str, list[str]]:
    commands = [item.strip() for item in re.findall(r"\[TOY:([^\]]+)\]", content) if item.strip()]
    if not commands:
        return content.strip(), []
    return re.sub(r"\[TOY:[^\]]+\]", "", content).strip(), commands


def _strip_screen_check_commands(content: str) -> tuple[str, list[str]]:
    reasons = [item.strip() for item in re.findall(r"\[SCREEN_CHECK:([^\]]+)\]", content) if item.strip()]
    if not reasons:
        return content.strip(), []
    return re.sub(r"\[SCREEN_CHECK:[^\]]+\]", "", content).strip(), reasons


def _strip_mobile_screen_check_commands(content: str) -> tuple[str, list[dict[str, str]]]:
    """解析自动线路里的 [MOBILE_SCREEN_CHECK:目标|原因]，返回 (清洗后正文, 列表)。"""
    checks: list[dict[str, str]] = []
    for raw in re.findall(r"\[MOBILE_SCREEN_CHECK:([^\]]+)\]", content):
        raw = raw.strip()
        if not raw:
            continue
        if "|" in raw:
            target, reason = raw.split("|", 1)
        else:
            target, reason = "", raw
        reason = reason.strip()
        if reason:
            checks.append({"target": target.strip(), "reason": reason})
    if not checks:
        return content.strip(), []
    return re.sub(r"\[MOBILE_SCREEN_CHECK:[^\]]+\]", "", content).strip(), checks


def _screen_tool_only_fallback_text(screen_reasons: Sequence[str], mobile_checks: Sequence[Mapping[str, str]]) -> str:
    if mobile_checks:
        return "我想确认一下你现在在做什么。"
    if screen_reasons:
        return "我想确认一下你现在在做什么。"
    return ""


def _extract_ring_touch_markers(content: str) -> tuple[str, list[str]]:
    descriptions = [
        item.strip()[:120]
        for item in _RING_TOUCH_PATTERN.findall(content)
        if item.strip()
    ]
    if not descriptions:
        return content.strip(), []
    return _RING_TOUCH_PATTERN.sub("", content).strip(), descriptions[:1]


def _extract_structured_reply(text: str) -> tuple[str, list[dict]]:
    raw = str(text or "").strip()
    if raw.startswith("```"):
        lines = raw.splitlines()
        if lines and lines[0].strip().lower() in {"```", "```json", "```obsidian"} and lines[-1].strip() == "```":
            raw = "\n".join(lines[1:-1]).strip()
    if not raw.startswith("{"):
        return text.strip(), []
    try:
        payload, _ = json.JSONDecoder().raw_decode(raw)
    except (json.JSONDecodeError, ValueError):
        return text.strip(), []
    if not isinstance(payload, Mapping) or "assistant_text" not in payload or not isinstance(payload.get("actions"), list):
        return text.strip(), []
    return str(payload.get("assistant_text") or "").strip(), [item for item in payload["actions"] if isinstance(item, Mapping)]


def _compact_preflight(preflight: Mapping[str, Any]) -> dict[str, Any]:
    compact = {
        "schema_version": preflight["schema_version"],
        "runtime_mode": preflight["runtime_mode"],
        "status": preflight["status"],
        "side_effects": list(preflight["side_effects"]),
        "production_side_effects": list(preflight["production_side_effects"]),
        "planned_production_side_effects": list(preflight.get("planned_production_side_effects", [])),
        "fallback_used": preflight["fallback_used"],
        "fallback_reason": preflight["fallback_reason"],
        "conv_id": preflight.get("conv_id", ""),
        "model_key": preflight.get("model_key", ""),
        "would_call_core": preflight.get("would_call_core", False),
        "would_write_monitor_log": preflight.get("would_write_monitor_log", ""),
        "wake_reason": preflight.get("wake_reason", ""),
        "toy_capability_allowed": bool(preflight.get("toy_capability_allowed", False)),
        "toy_capability_reason": preflight.get("toy_capability_reason", ""),
        "control_session_id": preflight.get("control_session_id", ""),
        "control_kind": preflight.get("control_kind", ""),
        "control_status": preflight.get("control_status", ""),
        "control_epoch": preflight.get("control_epoch"),
        "owner_client_id": preflight.get("owner_client_id", ""),
        "control_device_id": preflight.get("control_device_id", ""),
    }
    if preflight.get("core_request"):
        compact["core_request"] = _compact_core_request(preflight)
    if preflight.get("gate"):
        compact["gate"] = {
            "status": preflight["gate"].get("status"),
            "wake_allowed": preflight["gate"].get("wake_allowed"),
            "blocked_reasons": list(preflight["gate"].get("blocked_reasons", [])),
        }
    if preflight.get("error_type"):
        compact["error_type"] = preflight["error_type"]
        compact["error"] = preflight.get("error", "")
    return compact


def _compact_core_request(preflight: Mapping[str, Any]) -> dict[str, Any]:
    core_request = preflight.get("core_request") or {}
    if not core_request:
        return {}
    return {
        "persona_message_count": core_request.get("persona_message_count", 0),
        "history_message_count": core_request["history_message_count"],
        "prompt_char_count": core_request["prompt_char_count"],
        "system_notice": core_request["system_notice"],
    }


def _steps_for_preflight_failed() -> list[dict[str, Any]]:
    steps = []
    for name, effect, production_side_effect in CORE_WAKE_EXECUTION_STEP_DEFINITIONS:
        if name == "validate_preflight":
            status = "failed"
        else:
            status = "skipped"
        steps.append(_step(name, effect, production_side_effect, status))
    return steps


def _all_planned_steps() -> list[dict[str, Any]]:
    return [
        _step(name, effect, production_side_effect, "planned")
        for name, effect, production_side_effect in CORE_WAKE_EXECUTION_STEP_DEFINITIONS
    ]


def _step(name: str, effect: str, production_side_effect: bool, status: str) -> dict[str, Any]:
    payload = {
        "name": name,
        "status": status,
        "production_side_effect": production_side_effect,
    }
    if effect:
        payload["effect"] = effect
    return payload



def build_core_wake_preflight(
    *,
    wake_package: Mapping[str, Any],
    execution_context: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Build a dry-run trace for the Core wake call without executing side effects."""
    package = _validated_wake_package(wake_package)
    context = _validated_execution_context(execution_context)

    if not context["conv_id"]:
        return _failed_preflight(
            package=package,
            context=context,
            error_type="no_conversation",
            error="core wake preflight requires an active conversation",
        )
    if not context["model_key"]:
        return _failed_preflight(
            package=package,
            context=context,
            error_type="missing_model_key",
            error="core wake preflight requires a model_key",
        )

    core_prompt = _build_core_prompt(package, context)
    persona_messages = _persona_messages(context)
    messages = persona_messages + list(context["recent_messages"]) + [{"role": "user", "content": core_prompt}]
    return {
        "schema_version": CORE_WAKE_PREFLIGHT_SCHEMA_VERSION,
        "runtime_mode": RUNTIME_MODE_DRY_RUN,
        "status": "ready",
        "trigger": "sentinel",
        "side_effects": [],
        "production_side_effects": [],
        "planned_production_side_effects": list(PLANNED_CORE_WAKE_PRODUCTION_EFFECTS),
        "fallback_used": False,
        "fallback_reason": "",
        "wake_package_schema_version": package["schema_version"],
        "conv_id": context["conv_id"],
        "model_key": context["model_key"],
        "toy_capability_allowed": context["toy_capability_allowed"],
        "toy_capability_reason": context["toy_capability_reason"],
        "control_session_id": context["control_session_id"],
        "control_kind": context["control_kind"],
        "control_status": context["control_status"],
        "control_epoch": context["control_epoch"],
        "owner_client_id": context["owner_client_id"],
        "control_device_id": context["control_device_id"],
        "visible_message_ids": list(context["visible_message_ids"]),
        "would_call_core": True,
        "would_write_monitor_log": "core_preflight_ready",
        "wake_reason": package["wake_reason"],
        "sentinel": {
            "summary": package["sentinel"]["summary"],
            "score": package["sentinel"]["score"],
            "confidence": package["sentinel"]["confidence"],
            "tone_hint": package["sentinel"]["tone_hint"],
            "uncertainty": package["sentinel"]["uncertainty"],
        },
        "attention": {
            "attention_targets": list(package["attention"]["attention_targets"]),
            "hypothesis_labels": list(package["attention"]["hypothesis_labels"]),
        },
        "gate": deepcopy(package["gate"]),
        "core_request": {
            "messages": messages,
            "persona_message_count": len(persona_messages),
            "history_message_count": len(context["recent_messages"]),
            "core_prompt": core_prompt,
            "prompt_char_count": len(core_prompt),
            "system_notice": _system_notice(package, context),
            "monitor_alert": _monitor_alert(context),
        },
    }


def _failed_preflight(
    *,
    package: Mapping[str, Any],
    context: Mapping[str, Any],
    error_type: str,
    error: str,
) -> dict[str, Any]:
    return {
        "schema_version": CORE_WAKE_PREFLIGHT_SCHEMA_VERSION,
        "runtime_mode": RUNTIME_MODE_DRY_RUN,
        "status": "failed",
        "trigger": "sentinel",
        "side_effects": [],
        "production_side_effects": [],
        "planned_production_side_effects": [],
        "fallback_used": False,
        "fallback_reason": "",
        "wake_package_schema_version": package["schema_version"],
        "conv_id": context["conv_id"],
        "model_key": context["model_key"],
        "would_call_core": False,
        "would_write_monitor_log": f"core_preflight_{error_type}",
        "wake_reason": package["wake_reason"],
        "gate": deepcopy(package["gate"]),
        "error_type": error_type,
        "error": error,
    }


def _validated_wake_package(wake_package: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(wake_package, Mapping):
        raise ValueError("core wake preflight wake_package must be an object")
    schema_version = wake_package.get("schema_version")
    if schema_version not in SUPPORTED_CORE_WAKE_PACKAGE_SCHEMA_VERSIONS:
        raise ValueError(
            "core wake preflight wake_package schema_version must be one of "
            f"{sorted(SUPPORTED_CORE_WAKE_PACKAGE_SCHEMA_VERSIONS)!r}"
        )
    if wake_package.get("runtime_mode") != RUNTIME_MODE_DRY_RUN:
        raise ValueError(f"core wake preflight wake_package runtime_mode must be {RUNTIME_MODE_DRY_RUN!r}")
    if wake_package.get("trigger") != "sentinel":
        raise ValueError("core wake preflight wake_package trigger must be 'sentinel'")
    if wake_package.get("side_effects") != []:
        raise ValueError("core wake preflight wake_package side_effects must be empty")
    if wake_package.get("fallback_used") is not False:
        raise ValueError("core wake preflight wake_package must not already be a fallback payload")
    if not isinstance(wake_package.get("wake_reason"), str) or not wake_package["wake_reason"].strip():
        raise ValueError("core wake preflight wake_package wake_reason must be non-empty text")
    gate = wake_package.get("gate")
    if not isinstance(gate, Mapping) or gate.get("status") != GATE_STATUS_PASSED:
        raise ValueError("core wake preflight requires passed gate status")
    if gate.get("wake_allowed") is not True:
        raise ValueError("core wake preflight requires gate wake_allowed true")
    package = dict(wake_package)
    if schema_version == CORE_WAKE_PACKAGE_SCHEMA_VERSION:
        package_context = package.get("context")
        if not isinstance(package_context, Mapping):
            raise ValueError("sentinel_core_wake_package.v2 requires context object")
        projection = package_context.get("context_projection")
        if not isinstance(projection, Mapping):
            raise ValueError("sentinel_core_wake_package.v2 requires context_projection")
        projection_value = ContextDeliveryProjection.from_dict(projection)
        if projection_value.schema_version != CONTEXT_DELIVERY_SCHEMA_VERSION:
            raise ValueError(
                "sentinel_core_wake_package.v2 context_projection must use "
                f"{CONTEXT_DELIVERY_SCHEMA_VERSION!r}"
            )
    package["wake_reason"] = package["wake_reason"].strip()
    return package


def _validated_execution_context(execution_context: Mapping[str, Any] | None) -> dict[str, Any]:
    if execution_context is None:
        execution_context = {}
    if not isinstance(execution_context, Mapping):
        raise ValueError("core wake preflight execution_context must be an object")
    context = _validate_exact_keys(
        execution_context,
        allowed=_EXECUTION_CONTEXT_ALLOWED_FIELDS,
        label="core wake preflight execution_context",
        require_all=False,
    )
    recent_messages, visible_message_ids = _validated_recent_messages(
        context.get("recent_messages")
    )
    user_name, ai_name = resolve_worldbook_names({
        "user_name": _preflight_optional_text(
            context.get("user_name"),
            key="user_name",
        ),
        "ai_name": _preflight_optional_text(
            context.get("ai_name"),
            key="ai_name",
        ),
    })
    return {
        "conv_id": _preflight_optional_text(context.get("conv_id"), key="conv_id"),
        "model_key": _preflight_optional_text(context.get("model_key"), key="model_key"),
        "recent_messages": recent_messages,
        "visible_message_ids": visible_message_ids,
        "last_user_message_age_sec": _preflight_optional_non_negative_int(
            context.get("last_user_message_age_sec"),
            key="last_user_message_age_sec",
        ),
        "user_name": user_name,
        "ai_name": ai_name,
        "ai_persona": _preflight_optional_text(context.get("ai_persona"), key="ai_persona"),
        "user_persona": _preflight_optional_text(context.get("user_persona"), key="user_persona"),
        "toy_capability_allowed": _preflight_optional_bool(
            context.get("toy_capability_allowed"),
            key="toy_capability_allowed",
        ),
        "toy_capability_reason": _preflight_optional_text(
            context.get("toy_capability_reason"),
            key="toy_capability_reason",
        ),
        "control_session_id": _preflight_optional_text(context.get("control_session_id"), key="control_session_id"),
        "control_kind": _preflight_optional_text(context.get("control_kind"), key="control_kind"),
        "control_status": _preflight_optional_text(context.get("control_status"), key="control_status"),
        "control_epoch": _preflight_optional_non_negative_int(context.get("control_epoch"), key="control_epoch"),
        "owner_client_id": _preflight_optional_text(context.get("owner_client_id"), key="owner_client_id"),
        "control_device_id": _preflight_optional_text(context.get("control_device_id"), key="control_device_id"),
        "autonomous_mobile_screen_target": _validated_autonomous_mobile_screen_target(
            context.get("autonomous_mobile_screen_target")
        ),
        "presence_identity_block": _preflight_optional_text(
            context.get("presence_identity_block"),
            key="presence_identity_block",
        ),
    }


def _validate_exact_keys(
    value: Any,
    *,
    allowed: set[str],
    label: str,
    require_all: bool = True,
) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{label} must be an object")
    missing = sorted(allowed.difference(value.keys())) if require_all else []
    if missing:
        raise ValueError(f"{label} missing fields: {missing!r}")
    unknown = sorted(set(value.keys()).difference(allowed))
    if unknown:
        raise ValueError(f"{label} unknown fields: {unknown!r}")
    return dict(value)


def _preflight_optional_text(value: Any, *, key: str) -> str:
    if value is None:
        return ""
    if not isinstance(value, str):
        raise ValueError(f"core wake preflight execution_context {key} must be text")
    return value.strip()


def _preflight_optional_non_negative_int(value: Any, *, key: str) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise ValueError(f"core wake preflight execution_context {key} must be a number")
    if value < 0:
        raise ValueError(f"core wake preflight execution_context {key} must be non-negative")
    return int(value)


def _preflight_optional_bool(value: Any, *, key: str) -> bool:
    if value is None:
        return False
    if not isinstance(value, bool):
        raise ValueError(f"core wake preflight execution_context {key} must be a boolean")
    return value


def _validated_autonomous_mobile_screen_target(value: Any) -> dict[str, str]:
    if value is None:
        return {}
    target = _validate_exact_keys(
        value,
        allowed=_AUTONOMOUS_MOBILE_TARGET_ALLOWED_FIELDS,
        label="core wake preflight execution_context autonomous_mobile_screen_target",
        require_all=False,
    )
    device_id = _preflight_optional_text(target.get("device_id"), key="autonomous_mobile_screen_target.device_id")
    if not device_id:
        return {}
    device_name = _preflight_optional_text(target.get("device_name"), key="autonomous_mobile_screen_target.device_name")
    device_type = _preflight_optional_text(target.get("device_type"), key="autonomous_mobile_screen_target.device_type")
    label = _preflight_optional_text(target.get("label"), key="autonomous_mobile_screen_target.label")
    return {
        "device_id": device_id,
        "device_name": device_name,
        "device_type": device_type,
        "label": label or device_name or device_type or "这台移动设备",
    }


def _validated_recent_messages(value: Any) -> tuple[list[dict[str, str]], list[str]]:
    if value is None:
        return [], []
    if not isinstance(value, Sequence) or isinstance(value, str | bytes):
        raise ValueError("core wake preflight execution_context recent_messages must be a list")
    messages: list[dict[str, str]] = []
    message_ids: list[str | None] = []
    for index, item in enumerate(value):
        message = _validate_exact_keys(
            item,
            allowed=_RECENT_MESSAGE_ALLOWED_FIELDS,
            label=f"core wake preflight execution_context recent_messages[{index}]",
            require_all=False,
        )
        role = message.get("role")
        content = message.get("content")
        if role not in _RECENT_MESSAGE_ROLES:
            raise ValueError(
                "core wake preflight execution_context "
                f"recent_messages[{index}] role must be user or assistant"
            )
        if not isinstance(content, str) or not content.strip():
            raise ValueError(
                "core wake preflight execution_context "
                f"recent_messages[{index}] content must be non-empty text"
            )
        text = content.strip()
        if len(text) > DEFAULT_CORE_WAKE_MESSAGE_MAX_CHARS:
            text = text[:DEFAULT_CORE_WAKE_MESSAGE_MAX_CHARS].rstrip()
        messages.append({"role": role, "content": text})
        message_id = message.get("id")
        if message_id is None:
            message_ids.append(None)
            continue
        if not isinstance(message_id, str) or not message_id.strip():
            raise ValueError(
                "core wake preflight execution_context "
                f"recent_messages[{index}] id must be non-empty text"
            )
        message_ids.append(message_id.strip())
    limited_messages = messages[-DEFAULT_CORE_WAKE_HISTORY_LIMIT:]
    limited_ids = message_ids[-DEFAULT_CORE_WAKE_HISTORY_LIMIT:]
    return limited_messages, [message_id for message_id in limited_ids if message_id]


def _sentinel_toy_capability_enabled(context: Mapping[str, Any]) -> bool:
    return bool(
        context.get("toy_capability_allowed") is True
        and context.get("control_status") == "active"
        and context.get("control_kind") in {"dom", "whisper"}
        and context.get("control_session_id")
        and context.get("control_epoch") is not None
        and context.get("owner_client_id")
        and context.get("control_device_id")
    )


def _sentinel_ring_touch_enabled() -> bool:
    try:
        from config import is_smart_ring_touch_active
        return is_smart_ring_touch_active()
    except Exception:
        return False


def _build_core_prompt(package: Mapping[str, Any], context: Mapping[str, Any]) -> str:
    user_name = context["user_name"]
    core_parts = [
        f"{user_name}是你的人——{user_name}这么久没动静，你该去看看了。",
        f"【{user_name}】已经{_format_age(context['last_user_message_age_sec'])}没有和你说话了。",
        f"哨兵唤醒你的原因：{_relationship_text(package['wake_reason'], context)}",
        f"这段时间{user_name}的整体状况：{_relationship_text(package['sentinel']['summary'], context)}",
        f"最新一条哨兵日志：{_relationship_text(package['sentinel']['monitoringlog'], context)}",
        f"Attention 简报：\n{_relationship_text(package['attention']['compact_text'], context)}",
    ]
    if package["sentinel"]["uncertainty"]:
        core_parts.append(f"不确定性：{_relationship_text(package['sentinel']['uncertainty'], context)}")
    if package["sentinel"]["tone_hint"]:
        core_parts.append(f"语气提示：{_relationship_text(package['sentinel']['tone_hint'], context)}")
    projection = package.get("context", {}).get("context_projection")
    if isinstance(projection, Mapping):
        rendered_projection = render_context_delivery_projection(
            projection,
            user_name=user_name,
            ai_name=str(context["ai_name"]),
        )
        if rendered_projection:
            core_parts.append(rendered_projection)
    _append_list_section(
        core_parts,
        title="最近的哨兵记录",
        items=[_relationship_text(item, context) for item in package["context"]["recent_sentinel_logs"]],
    )
    _append_list_section(
        core_parts,
        title="最近聊天片段",
        items=[_relationship_text(item, context) for item in package["context"]["recent_chat"]],
    )
    _append_list_section(
        core_parts,
        title="硬限制",
        items=[_hard_limit_relationship_text(item, context) for item in package["hard_limits"]],
    )
    available_tools: set[str] = set()
    toy_available = _sentinel_toy_capability_enabled(context)
    if toy_available:
        available_tools.add("device.toy")
    ring_available = _sentinel_ring_touch_enabled()
    if ring_available:
        available_tools.add("device.ring_touch")
    pc_state = package["attention"].get("world_state", {}).get("pc_activity")
    if pc_state == "active":
        available_tools.add("pc.screen_check")
    mobile_target = context.get("autonomous_mobile_screen_target") or {}
    if mobile_target:
        available_tools.add("mobile.screen_check")
    rendered_tools = render_registered_capabilities(
        "sentinel_v2",
        capabilities=available_tools,
        context={
            "user_name": user_name,
            "toy_available": toy_available,
            "toy_variant": "sentinel",
            "ring_available": ring_available,
            "pc_screen_available": pc_state == "active",
            "pc_foreground": package["attention"].get("world_state", {}).get(
                "pc_foreground"
            ),
            "mobile_screen_available": bool(mobile_target),
            "mobile_screen_target": mobile_target,
        },
    )
    advertised_tools = tuple(tool_name for tool_name, _prose in rendered_tools)
    validate_turn_advertisement(available_tools, advertised_tools)
    core_parts.extend(f"\n{prose}" for _tool_name, prose in rendered_tools)
    core_parts.append(
        f"信号不足不代表没话说——翻翻你们的记忆和最近聊天，找一个{user_name}会在意的点自然地搭话。"
        f"实在没有切入点，就用你了解的{user_name}的习惯或日常节点来关心{user_name}。"
        f"不要提 Sentinel、Attention、Gate 或系统内部结构。"
        f"不要说「我来看看你」「想你了所以来找你」这种暴露哨兵机制的话。"
    )
    return "\n".join(core_parts)


def _relationship_text(value: Any, context: Mapping[str, Any]) -> str:
    text = str(value or "").replace("用户", str(context["user_name"]))
    return re.sub(
        r"(?<![A-Za-z])AI(?![A-Za-z])",
        str(context["ai_name"]),
        text,
    )


def _hard_limit_relationship_text(value: Any, context: Mapping[str, Any]) -> str:
    """Resolve the controlled owner placeholder without rewriting quoted chat."""

    return _relationship_text(value, context).replace(
        "她",
        str(context["user_name"]),
    )


def _persona_messages(context: Mapping[str, Any]) -> list[dict[str, str]]:
    messages = build_worldbook_prefix(context)
    presence_identity = str(context.get("presence_identity_block") or "").strip()
    if presence_identity:
        messages.extend([
            {"role": "user", "content": presence_identity},
            {
                "role": "assistant",
                "content": "（嗯，这是我为自己留下的连续性基准。）",
            },
        ])
    return messages


def _append_list_section(core_parts: list[str], *, title: str, items: Sequence[str]) -> None:
    if not items:
        return
    lines = [f"- {str(item).strip()}" for item in items if str(item).strip()]
    if lines:
        core_parts.append(f"{title}：\n" + "\n".join(lines))


def _system_notice(package: Mapping[str, Any], context: Mapping[str, Any]) -> str:
    reason_short = package["wake_reason"][:60] if package["wake_reason"] else "该管管了"
    return f"💭 {context['ai_name']}的哨兵唤醒了主脑 · {reason_short}"


def _monitor_alert(context: Mapping[str, Any]) -> str:
    return f"哨兵唤醒了{context['ai_name']}"


def _format_age(age_sec: int | None) -> str:
    if age_sec is None:
        return "一段时间"
    if age_sec < 60:
        return f"{age_sec}秒"
    minutes = age_sec // 60
    if minutes < 60:
        return f"{minutes}分钟"
    hours = minutes // 60
    rest_minutes = minutes % 60
    if rest_minutes == 0:
        return f"{hours}小时"
    return f"{hours}小时{rest_minutes}分钟"


__all__ = [
    "CORE_WAKE_EXECUTION_MODE_DISABLED",
    "CORE_WAKE_EXECUTION_MODE_DRY_RUN",
    "CORE_WAKE_EXECUTION_MODE_FULL",
    "CORE_WAKE_EXECUTION_MODE_TEST_EXECUTE",
    "CORE_WAKE_EXECUTION_SCHEMA_VERSION",
    "CORE_WAKE_EXECUTION_STEP_DEFINITIONS",
    "CORE_WAKE_PREFLIGHT_SCHEMA_VERSION",
    "DEFAULT_CORE_WAKE_HISTORY_LIMIT",
    "DEFAULT_CORE_WAKE_MESSAGE_MAX_CHARS",
    "PLANNED_CORE_WAKE_PRODUCTION_EFFECTS",
    "build_core_wake_preflight",
    "run_core_wake_orchestrator_dry_run",
    "run_core_wake_orchestrator_full_execute",
    "run_core_wake_orchestrator_test_execute",
]
