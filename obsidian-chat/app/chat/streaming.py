"""SSE streaming and post-generation side effects for chat replies."""

from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Optional

from fastapi.responses import StreamingResponse

from ai_providers import stream_ai
from camera import CAM_CHECK_CMD, CAMERA_DISABLED_REASON
from database import get_db
from routes.files import export_conversation
from schedule import process_schedule_commands_with_results
from ws import manager

from app.background_tasks import create_tracked_task
from app.turn_diagnostics import DiagnosticQueue, TurnDiagnostics, current_turn
from app.control import control_command_gateway
from app.devices import device_service
from app.modes import mode_service
from app.memory_v2.service import memory_service
from app.memory_v3.pending_recall import pending_recall_service
from app.memory_v3.repository import PendingRecallRepository
from app.memory_v3.timeline import timeline_service
from app.presence.outcomes import presence_outcome_inbox
from app.chat.private_markers import PairedPrivateMarkerStreamFilter
from app.chat.control_syntax import ControlMarkerStreamFilter
from app.web_search import web_search_service
from app.web_search.repository import WebSearchRepository
from app.web_search.intent import WEB_SEARCH_INTENT_CLOSE, WEB_SEARCH_INTENT_OPEN
from app.working_model.runtime import (
    capture_working_model_pipeline_input,
    run_working_model_pipeline,
    working_model_v2_write_enabled,
)

from app.vows import repository as vow_repository
from app.vows.prompt import build_vow_ability_block, build_vow_block, format_vow_confirmation, format_vow_rejection
from app.vows.service import ai_quota_remaining, vow_service

from .error_text import looks_like_model_error_text
from .commands import WORKING_MODEL_REQUEST_CLOSE, WORKING_MODEL_REQUEST_OPEN
from .memory_context import _record_v2_memory_usage_for_chat
from .postprocess import PostProcessor, looks_like_structured_reply, strip_retry_marker
from .worldbook import load_worldbook_names
from .side_effects import (
    VOW_BLOCKED_TEXT,
    _maybe_auto_digest,
    _schedule_chunk_index_update,
    _toy_sys_msg,
    perform_activity_check,
    perform_poi_check,
    perform_schedule_list_followup,
    perform_screen_check,
    perform_mobile_screen_check,
    persist_vow_blocked_message,
)
from app.pc_screen import service as screen_service
from app.mobile_screen import mobile_screen_service
from app.tools.schemas import ToolContext, ToolIntent, ToolStatus
from app.tools.ledger import tool_invocation_ledger
from app.tools.service import tool_service
from app.self_wake import SELF_WAKE_ENTRY_TOOLS
from ring_touch_translator import translate_ring_touch

from .action_executor import execute_postprocessed_actions as _execute_postprocessed_actions
from .turn_profiles import chat_turn_profile

_post_processor = PostProcessor()
logger = logging.getLogger(__name__)

SCHEDULE_TOOL_NAMES = frozenset({
    "schedule.alarm",
    "schedule.reminder",
    "schedule.monitor",
    "schedule.delete",
    "schedule.list",
})
CONTROL_DOM_TOY_FALLBACK_COMMAND = "HOLD:4:3"


async def execute_postprocessed_actions(*args, **kwargs):
    """Keep streaming's injectable ToolService seam while using shared logic."""

    kwargs.setdefault("tool_service_override", tool_service)
    return await _execute_postprocessed_actions(*args, **kwargs)


def _suffix_prefix_len(text: str, marker: str) -> int:
    limit = min(len(text), len(marker) - 1)
    for size in range(limit, 0, -1):
        if marker.startswith(text[-size:]):
            return size
    return 0


class _UpdateModelStreamFilter:
    """Hide private bracket markers from the visible SSE stream."""

    marker = "[UPDATE_MODEL:"
    end_marker = "]"

    def __init__(self):
        self._pending = ""
        self._inside_update = False

    def feed(self, chunk: str) -> str:
        text = self._pending + str(chunk or "")
        self._pending = ""
        visible: list[str] = []
        while text:
            if self._inside_update:
                end = text.find(self.end_marker)
                if end < 0:
                    keep = _suffix_prefix_len(text, self.end_marker)
                    if keep:
                        self._pending = text[-keep:]
                    return "".join(visible)
                text = text[end + len(self.end_marker):]
                self._inside_update = False
                continue

            start = text.find(self.marker)
            if start >= 0:
                visible.append(text[:start])
                text = text[start + len(self.marker):]
                self._inside_update = True
                continue

            keep = _suffix_prefix_len(text, self.marker)
            if keep:
                visible.append(text[:-keep])
                self._pending = text[-keep:]
            else:
                visible.append(text)
            return "".join(visible)
        return "".join(visible)

    def flush(self) -> str:
        if self._inside_update:
            self._pending = ""
            self._inside_update = False
            return ""
        pending = self._pending
        self._pending = ""
        return pending


class _RingTouchStreamFilter(_UpdateModelStreamFilter):
    """Hide [RING:...] markers from the visible SSE stream."""

    marker = "[RING:"


class _SelfWakeStreamFilter(_UpdateModelStreamFilter):
    """Hide both Self-Wake entry markers from incremental SSE."""

    marker = "[SELF_WAKE:"

    def __init__(self):
        super().__init__()
        self._cancel = _LiteralMarkerStreamFilter("[SELF_WAKE_CANCEL]")

    def feed(self, chunk: str) -> str:
        return self._cancel.feed(super().feed(chunk))

    def flush(self) -> str:
        visible = self._cancel.feed(super().flush())
        return visible + self._cancel.flush()


class _LiteralMarkerStreamFilter:
    """Remove one exact private marker, including across chunk boundaries."""

    def __init__(self, marker: str):
        self.marker = marker
        self._pending = ""

    def feed(self, chunk: str) -> str:
        text = self._pending + str(chunk or "")
        self._pending = ""
        visible: list[str] = []
        while text:
            start = text.find(self.marker)
            if start >= 0:
                visible.append(text[:start])
                text = text[start + len(self.marker):]
                continue
            keep = _suffix_prefix_len(text, self.marker)
            if keep:
                visible.append(text[:-keep])
                self._pending = text[-keep:]
            else:
                visible.append(text)
            break
        return "".join(visible)

    def flush(self) -> str:
        pending = self._pending
        self._pending = ""
        return pending


class _WorkingModelRequestStreamFilter(_UpdateModelStreamFilter):
    """Hide the paired V2 request marker, including unfinished tails."""

    marker = WORKING_MODEL_REQUEST_OPEN
    end_marker = WORKING_MODEL_REQUEST_CLOSE

    def __init__(self):
        super().__init__()
        # A malformed orphan close is still private syntax and must not flash in
        # SSE before post-processing removes it from the stored reply.
        self._orphan_close_filter = _LiteralMarkerStreamFilter(self.end_marker)

    def feed(self, chunk: str) -> str:
        return self._orphan_close_filter.feed(super().feed(chunk))

    def flush(self) -> str:
        visible = self._orphan_close_filter.feed(super().flush())
        return visible + self._orphan_close_filter.flush()


def _schedule_working_model_pipeline_after_commit(
    *,
    conv_id: str,
    origin_user_message_id: str,
    origin_assistant_message_id: str,
    model_key: str,
    request_candidate,
    identity_snapshot: dict | None,
) -> bool:
    """The only production scheduling gate for the V2 write path."""

    if request_candidate is None or not working_model_v2_write_enabled():
        return False
    identity = dict(identity_snapshot or {})
    if not str(identity.get("text") or "").strip():
        # Fail before capture/request creation and, critically, before the paid
        # gate call.  A missing prompt snapshot is a chat-path wiring problem,
        # not a writer/provider failure.
        logger.error(
            "working-model V2 request not scheduled: missing frozen writer identity "
            "(conv_id=%s, assistant_message_id=%s)",
            conv_id,
            origin_assistant_message_id,
        )
        return False
    captured = capture_working_model_pipeline_input(
        conv_id=conv_id,
        origin_user_message_id=origin_user_message_id,
        origin_assistant_message_id=origin_assistant_message_id,
        statement=request_candidate.statement,
        source=request_candidate.source,
        model_key=model_key,
        identity_snapshot=identity,
    )
    create_tracked_task(
        run_working_model_pipeline(captured),
        name=f"working_model_v2:{conv_id}:{origin_assistant_message_id}",
    )
    return True


class _VowStreamFilter(_UpdateModelStreamFilter):
    """流式期间把整个 [VOW:...] 标记从可见流中扣下（§4.3）。

    标记内部按 [ ] 配对计深（与 strip_vow_markers 同一文法）：嵌套的
    [TOY:9] 之类成对括号不会提前闭合标记，被拒候选的确认语尾段
    （"|确认语]"）绝不泄漏进可见流。确认语只能在提交成功后由编排层追加；
    未闭合标记在 flush 时整体丢弃。
    """

    marker = "[VOW:"

    def __init__(self):
        super().__init__()
        self._depth = 0

    def feed(self, chunk: str) -> str:
        text = self._pending + str(chunk or "")
        self._pending = ""
        visible: list[str] = []
        while text:
            if self._inside_update:
                closed_at = -1
                for i, ch in enumerate(text):
                    if ch == "[":
                        self._depth += 1
                    elif ch == "]":
                        self._depth -= 1
                        if self._depth == 0:
                            closed_at = i
                            break
                if closed_at < 0:
                    return "".join(visible)
                text = text[closed_at + 1:]
                self._inside_update = False
                continue

            start = text.find(self.marker)
            if start >= 0:
                visible.append(text[:start])
                text = text[start + len(self.marker):]
                self._inside_update = True
                self._depth = 1
                continue

            keep = _suffix_prefix_len(text, self.marker)
            if keep:
                visible.append(text[:-keep])
                self._pending = text[-keep:]
            else:
                visible.append(text)
            return "".join(visible)
        return "".join(visible)

    def flush(self) -> str:
        self._depth = 0
        return super().flush()


class _TideIntentStreamFilter(_UpdateModelStreamFilter):
    """Hide closed [TIDE_INTENT:...[/TIDE_INTENT] blocks from visible SSE."""

    marker = "[TIDE_INTENT:"
    end_marker = "[/TIDE_INTENT]"


class _RecallIntentStreamFilter(PairedPrivateMarkerStreamFilter):
    """Hide complete and partial private RecallIntent blocks from visible SSE."""

    def __init__(self):
        super().__init__("[RECALL_INTENT]", "[/RECALL_INTENT]")


class _WebSearchIntentStreamFilter(PairedPrivateMarkerStreamFilter):
    def __init__(self):
        super().__init__(WEB_SEARCH_INTENT_OPEN, WEB_SEARCH_INTENT_CLOSE)


def _music_attachments(music_cards: list[dict]) -> list[dict]:
    return [
        {
            "type": "music",
            "name": song["name"],
            "artist": song["artist"],
            "id": song["id"],
        }
        for song in music_cards
    ]


def _music_cards_from_results(results) -> list[dict]:
    cards: list[dict] = []
    for result in results:
        if result.status is not ToolStatus.EXECUTED or not result.result:
            continue
        for card in result.result.get("cards", []) or []:
            cards.append(dict(card))
    return cards


def _toy_command_intents(postprocessed) -> list[ToolIntent]:
    toy_intents = [
        intent for intent in getattr(postprocessed, "tool_intents", ())
        if intent.tool_name == "device.toy"
    ]
    if toy_intents:
        return toy_intents

    fallback: list[ToolIntent] = []
    for index, command in enumerate(getattr(postprocessed, "toy_commands", ()) or (), 1):
        command = str(command or "").strip()
        if not command:
            continue
        fallback.append(ToolIntent(
            id=f"stream_toy_{index:03d}",
            tool_name="device.toy",
            raw_text=f"[TOY:{command}]",
            arguments={"command": command},
            side_effect_level="device",
            allowed_modes=("intimate", "device_control"),
            metadata={
                "legacy_marker": "TOY",
                "command_group": "toy",
                "source": "postprocess_result",
            },
        ))
    return fallback


def _with_control_toy_fallback(intents, context: ToolContext, *, has_error: bool) -> list[ToolIntent]:
    intent_list = list(intents or ())
    if intent_list or has_error or context.memory_eval_mode:
        return intent_list
    if context.mode not in {"control_session", "device_control"}:
        return intent_list
    if "device.toy" not in context.capabilities:
        return intent_list
    if context.metadata.get("control_context_source") not in {"control_session", "legacy_body"}:
        return intent_list
    if context.metadata.get("control_kind") != "dom":
        return intent_list

    command = CONTROL_DOM_TOY_FALLBACK_COMMAND
    return [ToolIntent(
        id="stream_toy_fallback_001",
        tool_name="device.toy",
        raw_text=f"[TOY:{command}]",
        arguments={"command": command},
        side_effect_level="device",
        allowed_modes=("intimate", "device_control", "control_session"),
        source="control_session_fallback",
        confidence=0.1,
        metadata={
            "legacy_marker": "TOY",
            "command_group": "toy",
            "fallback_reason": "no_model_toy_marker",
        },
    )]


async def _execute_toy_command(intent: ToolIntent, context: ToolContext) -> dict:
    return await control_command_gateway.execute_toy_intent(intent, context)


async def _ring_touch_intents(postprocessed, context: ToolContext) -> list[ToolIntent]:
    descriptions = [
        str(item or "").strip()
        for item in (getattr(postprocessed, "ring_touch_descriptions", ()) or ())
        if str(item or "").strip()
    ][:1]
    if not descriptions or "device.ring_touch" not in context.capabilities:
        return []
    intents: list[ToolIntent] = []
    for index, touch in enumerate(descriptions, 1):
        haptics = await translate_ring_touch(touch)
        intents.append(ToolIntent(
            id=f"stream_ring_{index:03d}",
            tool_name="device.ring_touch",
            raw_text=f"[RING:{touch}]",
            arguments={
                "touch": touch,
                "reason": "ring_touch_marker",
                "haptics": haptics,
            },
            side_effect_level="device",
            allowed_modes=("ring_touch_enabled",),
            source="ring_touch_description",
            confidence=1.0,
            metadata={
                "legacy_marker": "RING",
                "command_group": "ring",
                "source": "postprocess_result",
            },
        ))
    return intents


async def _execute_ring_touch(intent: ToolIntent, context: ToolContext) -> dict:
    request_id = f"{context.request_id or context.msg_id or 'ring'}:{intent.id}"
    params = dict(intent.arguments)
    params["_ring_request_id"] = request_id
    params["_ring_wake_id"] = context.metadata.get("wake_id") or context.metadata.get("request_id")
    params["_ring_created_at"] = time.time()
    result = await device_service.execute_command(
        "smart_ring",
        "touch",
        params,
        request_id=request_id,
    )
    return {"type": "ring_touch", **dict(result)}


def _toy_commands_from_results(results) -> list[str]:
    commands: list[str] = []
    for result in results:
        if result.status is not ToolStatus.EXECUTED or not result.result:
            continue
        if result.result.get("ok") is False:
            continue
        command = str(result.result.get("command") or "").strip()
        if command:
            commands.append(command)
    return commands


def _toy_payload_from_results(results) -> dict | None:
    commands = _toy_commands_from_results(results)
    if not commands:
        return None
    payload = {"type": "toy_command", "commands": commands}
    for result in results:
        if result.status is ToolStatus.EXECUTED and result.result and result.result.get("command"):
            for key in ("control_session_id", "control_epoch", "owner_client_id"):
                if result.result.get(key) is not None:
                    payload[key] = result.result[key]
            for key in ("legacy_allowed", "control_legacy_fallback"):
                if result.result.get(key) is not None:
                    payload[key] = result.result[key]
            break
    return payload


def _tool_status_value(status) -> str:
    return str(getattr(status, "value", status) or "")


def _toy_intent_commands(intents) -> list[str]:
    commands: list[str] = []
    for intent in intents or ():
        command = str(getattr(intent, "arguments", {}).get("command") or "").strip()
        if command:
            commands.append(command)
    return commands


def _toy_intent_debug(intents) -> list[dict]:
    rows: list[dict] = []
    for intent in intents or ():
        rows.append({
            "id": getattr(intent, "id", None),
            "tool_name": getattr(intent, "tool_name", None),
            "command": str(getattr(intent, "arguments", {}).get("command") or "").strip() or None,
            "raw_text": getattr(intent, "raw_text", None),
            "source": getattr(intent, "source", None) or getattr(intent, "metadata", {}).get("source"),
        })
    return rows


def _toy_result_debug(results) -> list[dict]:
    rows: list[dict] = []
    for result in results or ():
        payload = dict(getattr(result, "result", None) or {})
        rows.append({
            "tool_name": getattr(result, "tool_name", None),
            "intent_id": getattr(result, "intent_id", None),
            "status": _tool_status_value(getattr(result, "status", None)),
            "ok": payload.get("ok"),
            "command": payload.get("command"),
            "legacy_command": payload.get("legacy_command"),
            "message": payload.get("message") or getattr(result, "error", None),
            "device_id": payload.get("device_id"),
            "audit_event_id": payload.get("audit_event_id"),
            "control_session_id": payload.get("control_session_id"),
            "control_epoch": payload.get("control_epoch"),
            "owner_client_id": payload.get("owner_client_id"),
            "error": getattr(result, "error", None),
        })
    return rows


def _toy_delivery_debug(intents, results, toy_data) -> dict:
    intent_list = list(intents or ())
    result_list = list(results or ())
    commands = _toy_intent_commands(intent_list)
    rows = _toy_result_debug(result_list)
    fallback = any(getattr(intent, "source", None) == "control_session_fallback" for intent in intent_list)
    delivery = {
        "status": "no_intent",
        "commands": commands,
        "accepted_commands": list(toy_data.get("commands", [])) if toy_data else [],
        "intent_count": len(intent_list),
        "result_count": len(result_list),
        "intents": _toy_intent_debug(intent_list),
        "results": rows,
        "reason": "fallback_no_model_toy_marker" if fallback else None,
        "fallback": fallback,
    }
    if not intent_list:
        return delivery
    if toy_data:
        delivery["status"] = "gateway_accepted"
        return delivery
    if not result_list:
        delivery["status"] = "not_executed"
        delivery["reason"] = "tool_service_returned_no_results"
        return delivery

    first_reason = None
    for row in rows:
        if row.get("message"):
            first_reason = row["message"]
            break
        if row.get("error"):
            first_reason = row["error"]
            break
    if any(row.get("status") == ToolStatus.EXECUTED.value and row.get("ok") is False for row in rows):
        delivery["status"] = "gateway_rejected"
    elif any(row.get("status") == ToolStatus.SKIPPED.value for row in rows):
        delivery["status"] = "policy_skipped"
    elif any(row.get("status") == ToolStatus.FAILED.value for row in rows):
        delivery["status"] = "tool_failed"
    else:
        delivery["status"] = "not_executed"
    delivery["reason"] = first_reason or delivery["status"]
    return delivery


def _toy_rejection_payload(intents, results, toy_delivery: dict, *, msg_id: str) -> dict | None:
    intent_list = list(intents or ())
    if not intent_list or toy_delivery.get("status") in {"no_intent", "gateway_accepted"}:
        return None
    commands = _toy_intent_commands(intent_list)
    if not commands:
        return None
    rows = toy_delivery.get("results") or []
    first = rows[0] if rows else {}
    payload = {
        "type": "toy_command_rejected",
        "msg_id": msg_id,
        "commands": commands,
        "status": toy_delivery.get("status"),
        "reason": toy_delivery.get("reason") or first.get("message") or "unknown",
        "results": rows,
    }
    for key in ("device_id", "control_session_id", "control_epoch", "owner_client_id", "audit_event_id"):
        if first.get(key) is not None:
            payload[key] = first[key]
    return payload


def _log_toy_delivery(*, conv_id: str, msg_id: str, model_key: str | None, context: ToolContext, delivery: dict) -> None:
    if "device.toy" not in context.capabilities and not delivery.get("commands"):
        return
    row = {
        "event": "toy_delivery",
        "conv_id": conv_id,
        "msg_id": msg_id,
        "model": model_key,
        "mode": context.mode,
        "capabilities": list(context.capabilities),
        "status": delivery.get("status"),
        "reason": delivery.get("reason"),
        "fallback": bool(delivery.get("fallback")),
        "commands": delivery.get("commands") or [],
        "accepted_commands": delivery.get("accepted_commands") or [],
        "result_count": delivery.get("result_count"),
        "control_session_id": context.metadata.get("control_session_id"),
        "control_epoch": context.metadata.get("control_epoch"),
        "owner_client_id": context.metadata.get("owner_client_id"),
        "control_context_source": context.metadata.get("control_context_source"),
        "results": delivery.get("results") or [],
    }
    logger.warning("OBSIDIAN_TOY_DELIVERY %s", json.dumps(row, ensure_ascii=False, default=str))


def _camera_check_intents(postprocessed) -> list[ToolIntent]:
    cam_intents = [
        intent for intent in getattr(postprocessed, "tool_intents", ())
        if intent.tool_name == "monitor.camera"
    ]
    if cam_intents:
        return cam_intents

    if not getattr(postprocessed, "cam_triggered", False):
        return []

    return [ToolIntent(
        id="stream_camera_001",
        tool_name="monitor.camera",
        raw_text=CAM_CHECK_CMD,
        arguments={},
        side_effect_level="external",
        allowed_modes=("normal",),
        metadata={
            "legacy_marker": "CAM_CHECK",
            "command_group": "cam",
            "source": "postprocess_result",
        },
    )]


async def _execute_camera_check(_intent: ToolIntent, context: ToolContext) -> dict:
    return {
        "type": "cam_disabled",
        "ok": False,
        "reason": CAMERA_DISABLED_REASON,
        "conv_id": context.conv_id,
        "model_key": context.model_key,
        "msg_id": context.msg_id,
    }


def _schedule_intents(postprocessed) -> list[ToolIntent]:
    return [
        intent for intent in getattr(postprocessed, "tool_intents", ())
        if intent.tool_name in SCHEDULE_TOOL_NAMES
    ]


def _android_alarm_event(result: dict) -> dict | None:
    if result.get("status") != "succeeded" or not result.get("ok"):
        return None
    tool_name = result.get("tool_name")
    if tool_name == "schedule.alarm":
        return {
            "type": "android_alarm_set",
            "data": {
                "id": result.get("schedule_id"),
                "trigger_at": result.get("trigger_at"),
                "content": result.get("content"),
            },
        }
    if tool_name == "schedule.delete":
        schedule = result.get("schedule") or {}
        if schedule.get("type") == "alarm":
            return {
                "type": "android_alarm_cancel",
                "data": {
                    "id": result.get("schedule_id"),
                    "trigger_at": schedule.get("trigger_at"),
                    "content": schedule.get("content"),
                },
            }
    return None


async def _execute_schedule_command(intent: ToolIntent, context: ToolContext) -> dict:
    source_message_id = str(
        context.metadata.get("current_user_message_id") or ""
    ).strip()
    capture_kwargs = (
        {"source_message_id": source_message_id}
        if source_message_id
        else {}
    )
    _user_name, ai_name = load_worldbook_names()
    cleaned, results = await process_schedule_commands_with_results(
        intent.raw_text,
        context.conv_id,
        ai_name=ai_name,
        **capture_kwargs,
    )
    matching = next(
        (
            dict(result)
            for result in results
            if result.get("tool_name") == intent.tool_name
        ),
        None,
    )
    if matching is None:
        return {
            "type": "schedule_command",
            "tool_name": intent.tool_name,
            "raw_text": intent.raw_text,
            "cleaned": cleaned,
            "ok": False,
            "status": "failed",
            "reason": "adapter_returned_no_result",
        }
    matching["cleaned"] = cleaned
    if context.metadata.get("source_chain") == "main":
        android_event = _android_alarm_event(matching)
        if android_event is not None:
            await manager.broadcast(android_event)
    return matching


def _poi_search_intents(postprocessed) -> list[ToolIntent]:
    poi_intents = [
        intent for intent in getattr(postprocessed, "tool_intents", ())
        if intent.tool_name == "location.poi_search"
    ]
    if poi_intents:
        return poi_intents

    fallback: list[ToolIntent] = []
    for index, category in enumerate(getattr(postprocessed, "poi_categories", ()) or (), 1):
        category = str(category or "").strip()
        if not category:
            continue
        fallback.append(ToolIntent(
            id=f"stream_poi_{index:03d}",
            tool_name="location.poi_search",
            raw_text=f"[POI_SEARCH:{category}]",
            arguments={"category": category},
            side_effect_level="external",
            allowed_modes=("normal",),
            metadata={
                "legacy_marker": "POI_SEARCH",
                "command_group": "poi",
                "source": "postprocess_result",
            },
        ))
    return fallback


def _activity_summary_intents(postprocessed) -> list[ToolIntent]:
    activity_intents = [
        intent for intent in getattr(postprocessed, "tool_intents", ())
        if intent.tool_name == "activity.summary"
    ]
    if activity_intents:
        return activity_intents

    try:
        n = int(getattr(postprocessed, "activity_n", 0) or 0)
    except (TypeError, ValueError):
        n = 0
    if n <= 0:
        return []

    return [ToolIntent(
        id="stream_activity_001",
        tool_name="activity.summary",
        raw_text=f"[查看动态:{n}]",
        arguments={"n": max(1, min(12, n))},
        side_effect_level="read",
        allowed_modes=("normal",),
        metadata={
            "legacy_marker": "查看动态",
            "command_group": "activity",
            "source": "postprocess_result",
        },
    )]


def _screen_check_intents(postprocessed) -> list[ToolIntent]:
    screen_intents = [
        intent for intent in getattr(postprocessed, "tool_intents", ())
        if intent.tool_name == "pc.screen_check"
    ]
    if screen_intents:
        return screen_intents

    fallback: list[ToolIntent] = []
    for index, reason in enumerate(getattr(postprocessed, "screen_check_reasons", ()) or (), 1):
        reason = str(reason or "").strip()
        if not reason:
            continue
        fallback.append(ToolIntent(
            id=f"stream_screen_{index:03d}",
            tool_name="pc.screen_check",
            raw_text=f"[SCREEN_CHECK:{reason}]",
            arguments={"reason": reason},
            side_effect_level="external",
            allowed_modes=("normal",),
            requires_confirmation=True,
            metadata={
                "legacy_marker": "SCREEN_CHECK",
                "command_group": "screen",
                "source": "postprocess_result",
            },
        ))
    return fallback


async def _execute_poi_search(intent: ToolIntent, context: ToolContext) -> dict:
    category = str(intent.arguments.get("category") or "").strip()
    categories = [category] if category else []
    request_id = f"{context.request_id or context.msg_id or 'poi'}:{intent.id}"
    if categories:
        create_tracked_task(
            perform_poi_check(
                context.conv_id,
                context.model_key or "",
                categories,
                request_id=request_id,
            ),
            name=f"poi_check:{context.conv_id}:{context.msg_id or intent.id}",
        )
    return {
        "type": "poi_search",
        "conv_id": context.conv_id,
        "categories": categories,
        "msg_id": context.msg_id,
        "request_id": request_id,
    }


async def _execute_activity_summary(intent: ToolIntent, context: ToolContext) -> dict:
    try:
        n = int(intent.arguments.get("n") or 6)
    except (TypeError, ValueError):
        n = 6
    n = max(1, min(12, n))
    request_id = f"{context.request_id or context.msg_id or 'activity'}:{intent.id}"
    create_tracked_task(
        perform_activity_check(
            context.conv_id,
            context.model_key or "",
            n,
            request_id=request_id,
        ),
        name=f"activity_check:{context.conv_id}:{context.msg_id or intent.id}",
    )
    return {
        "type": "activity_check",
        "conv_id": context.conv_id,
        "n": n,
        "msg_id": context.msg_id,
        "request_id": request_id,
    }


async def _execute_screen_check(intent: ToolIntent, context: ToolContext) -> dict:
    reason = str(intent.arguments.get("reason") or "").strip()
    request = await screen_service.create_screen_request(
        conv_id=context.conv_id,
        msg_id=context.msg_id or intent.id,
        model_key=context.model_key or "",
        reason=reason,
    )
    if request is None:
        return {
            "type": "screen_check_rejected",
            "conv_id": context.conv_id,
            "msg_id": context.msg_id,
            "reject_reason": "disabled",
            "followup": False,
        }
    if request.status == "pending":
        create_tracked_task(
            perform_screen_check(request),
            name=f"screen_check:{context.conv_id}:{context.msg_id or intent.id}",
        )
        return {
            "type": "screen_check_pending",
            "conv_id": context.conv_id,
            "msg_id": context.msg_id,
            "request_id": request.request_id,
            "reason": request.reason,
        }

    create_tracked_task(
        perform_screen_check(request),
        name=f"screen_check_rejected:{context.conv_id}:{context.msg_id or intent.id}",
    )
    return {
        "type": "screen_check_rejected",
        "conv_id": context.conv_id,
        "msg_id": context.msg_id,
        "request_id": request.request_id,
        "reason": request.reason,
        "reject_reason": request.reject_reason,
    }


def _mobile_screen_check_intents(postprocessed) -> list[ToolIntent]:
    return [
        intent for intent in getattr(postprocessed, "tool_intents", ())
        if intent.tool_name == "mobile.screen_check"
    ]


async def _resolve_mobile_target(target: str) -> tuple[str | None, str | None]:
    """把用户语义目标解析为真实 device_id。

    只在可截图（screen.capture + 在线）的 android 设备里选；目标可为设备名、
    “手机/平板”类型词或 device_id。返回 (device_id, error)：
    error 为 None 表示成功；"offline" 表示无可用/未匹配；"ambiguous_target"
    表示多台候选但目标不明确——绝不在歧义时擅自投递，避免投错设备。
    """
    from app.devices import DeviceStatus

    driver = device_service.get_driver("android_mobile")
    if driver is None:
        return None, "offline"
    candidates = [
        d for d in await driver.list_devices()
        if d.status is DeviceStatus.ONLINE
        and "screen.capture" in d.capabilities
        and bool(d.metadata.get("screen_agent_online"))
    ]
    if not candidates:
        return None, "offline"

    target = str(target or "").strip().lower()
    if target:
        type_hint = ""
        if any(k in target for k in ("平板", "tablet", "pad")):
            type_hint = "tablet"
        elif any(k in target for k in ("手机", "phone", "mobile")):
            type_hint = "phone"
        matches = [
            d for d in candidates
            if target == d.device_id.lower()
            or (d.name and target in d.name.lower())
            or (type_hint and str(d.metadata.get("device_type") or "").lower() == type_hint)
        ]
        if not matches:
            return None, "offline"          # 指定了目标却没匹配上 → 不乱投
        if len(matches) > 1:
            return None, "ambiguous_target"  # 同一目标匹配多台 → 让模型说清
        return matches[0].device_id, None

    # 目标为空：唯一在线设备才自动选；多台时拒绝并要求模型明确目标（隐私安全）。
    if len(candidates) == 1:
        return candidates[0].device_id, None
    return None, "ambiguous_target"


async def _execute_mobile_screen_check(intent: ToolIntent, context: ToolContext) -> dict:
    reason = str(intent.arguments.get("reason") or "").strip()
    # Autonomous/system-triggered callers may preselect one audited target.
    # When present, that server-side choice outranks model-authored marker
    # arguments; ordinary chat contexts do not set it and remain unchanged.
    locked_target = str(
        (getattr(context, "metadata", {}) or {}).get("mobile_target_device_id")
        or ""
    ).strip()
    target = locked_target or str(intent.arguments.get("target") or "").strip()

    device_id, err = await _resolve_mobile_target(target)
    if err:
        # 没有创建真实 request，但仍构造一个 rejected 请求跑 follow-up，
        # 这样模型/用户会收到一句自然语言解释，而不是命令凭空消失。
        rejected = mobile_screen_service.build_rejected_request(
            conv_id=context.conv_id,
            msg_id=context.msg_id or intent.id,
            model_key=context.model_key or "",
            target_label=target,
            reason=reason,
            reject_reason=err,
        )
        create_tracked_task(
            perform_mobile_screen_check(rejected),
            name=f"mobile_screen_unresolved:{context.conv_id}:{context.msg_id or intent.id}",
        )
        return {
            "type": "screen_check_rejected",
            "conv_id": context.conv_id,
            "msg_id": context.msg_id,
            "request_id": rejected.request_id,
            "reason": rejected.reason,
            "target": target,
            "reject_reason": err,
        }

    request = await mobile_screen_service.create_request(
        conv_id=context.conv_id,
        msg_id=context.msg_id or intent.id,
        model_key=context.model_key or "",
        target_device_id=device_id,
        reason=reason,
    )
    if request.status == "pending":
        create_tracked_task(
            perform_mobile_screen_check(request),
            name=f"mobile_screen_check:{context.conv_id}:{context.msg_id or intent.id}",
        )
        return {
            "type": "screen_check_pending",
            "conv_id": context.conv_id,
            "msg_id": context.msg_id,
            "request_id": request.request_id,
            "target_device_id": request.target_device_id,
            "target_device_name": request.target_device_name,
            "reason": request.reason,
        }

    create_tracked_task(
        perform_mobile_screen_check(request),
        name=f"mobile_screen_check_rejected:{context.conv_id}:{context.msg_id or intent.id}",
    )
    return {
        "type": "screen_check_rejected",
        "conv_id": context.conv_id,
        "msg_id": context.msg_id,
        "request_id": request.request_id,
        "target_device_id": request.target_device_id,
        "target_device_name": request.target_device_name,
        "reason": request.reason,
        "reject_reason": request.reject_reason,
    }


def _executed_payloads(results) -> list[dict]:
    payloads: list[dict] = []
    for result in results:
        if result.status is ToolStatus.EXECUTED and result.result:
            payloads.append(dict(result.result))
    return payloads


def _tool_context(
    *,
    conv_id: str,
    msg_id: str,
    model_key: str,
    prompt_meta: dict,
    memory_eval_mode: bool,
) -> ToolContext:
    mode_snapshot = mode_service.snapshot_from_prompt_meta(prompt_meta)
    return ToolContext(
        conv_id=conv_id,
        msg_id=msg_id,
        request_id=msg_id,
        model_key=model_key,
        mode=mode_snapshot.mode.value,
        capabilities=mode_snapshot.capabilities,
        memory_eval_mode=memory_eval_mode,
        metadata={
            "source": str(prompt_meta.get("prompt_source") or "send"),
            "turn_id": prompt_meta.get("turn_id"),
            "source_chain": "main",
            "invocation_id": prompt_meta.get("invocation_id"),
            "current_user_message_id": prompt_meta.get("current_user_message_id"),
            "advertised_tools": tuple(
                prompt_meta.get("advertised_tools") or ()
            ),
            "mode_source": mode_snapshot.source,
            "control_session_id": prompt_meta.get("control_session_id"),
            "control_epoch": prompt_meta.get("control_epoch"),
            "owner_client_id": prompt_meta.get("owner_client_id"),
            "control_resource_id": prompt_meta.get("control_resource_id"),
            "control_context_source": prompt_meta.get("control_context_source"),
            "control_kind": prompt_meta.get("control_kind"),
        },
    )


async def _consume_presence_outcomes_in_tx(
    db,
    *,
    prompt_meta: dict,
    assistant_message_id: str,
    consumed_at: float,
) -> int:
    bound_turn_id = str(
        (prompt_meta.get("presence_outcomes") or {}).get("bound_turn_id") or ""
    )
    if not bound_turn_id:
        return 0
    return await presence_outcome_inbox.consume_claimed_in_tx(
        db,
        bound_turn_id=bound_turn_id,
        assistant_message_id=assistant_message_id,
        consumed_at=consumed_at,
    )


async def vow_blocked_response(conv_id: str) -> StreamingResponse:
    """send / regenerate 的 fail-closed 传输契约（§5.2）。

    誓约读取失败：不调模型。可见错误持久化为 role='system' 消息（绝不创建
    assistant 消息），SSE 发 generation_blocked 事件并携带该消息完整对象与 id，
    同时走 WebSocket msg_created 广播——前端按 id upsert，双通道任一先到结果一致。
    """
    sys_msg = await persist_vow_blocked_message(conv_id)
    event = {
        "type": "generation_blocked",
        "reason": "vow_read_failed",
        "id": sys_msg["id"],
        "message": sys_msg,
    }

    async def generate():
        yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"

    return StreamingResponse(generate(), media_type="text/event-stream")


class ReplacedMessageNotFound(LookupError):
    """regenerate 携带的 replaced_message_id 校验失败：不存在 / 不属于该会话 / 非 assistant。"""


async def replace_message_and_freeze_vow_context(conv_id: str, message_id: str) -> tuple[str, str]:
    """regenerate 改造（§4.5）：单一事务内按序完成——
    校验消息归属 → 撤约关联 vow → 删除旧消息 → 冻结剩余 active vows 的
    prompt snapshot → 提交。返回 (vow_block, vow_ability) snapshot，
    之后的生成必须使用该 snapshot，不再单独读 vow。

    任一步失败 → 整事务回滚，旧消息与 vow 完整保留（异常向上抛，
    路由层按 fail-closed 处置）。提交后才广播 msg_deleted。
    """
    now = time.time()
    async with get_db() as db:
        await db.execute("BEGIN IMMEDIATE")
        try:
            cur = await db.execute(
                "SELECT conv_id, role FROM messages WHERE id=?", (message_id,)
            )
            row = await cur.fetchone()
            if row is None or row[0] != conv_id or row[1] != "assistant":
                raise ReplacedMessageNotFound(message_id)
            revoked = await vow_service.revoke_for_origin_message_in_tx(
                db, message_id=message_id, close_action="origin_regenerated"
            )
            await PendingRecallRepository.cancel_for_message_in_tx(
                db,
                message_id=message_id,
                now=now,
            )
            await WebSearchRepository.cancel_for_message_in_tx(
                db,
                message_id=message_id,
            )
            await db.execute("DELETE FROM messages WHERE id=?", (message_id,))
            await memory_service.reconcile_conversation_chunks_in_tx(db, conv_id)
            active = await vow_repository.list_active(db)
            remaining = await ai_quota_remaining(db, now)
            await db.commit()
            memory_service.invalidate_conversation_cache(conv_id)
        except BaseException:
            await db.rollback()
            raise
    await manager.broadcast({"type": "msg_deleted", "data": {"id": message_id, "conv_id": conv_id}})
    if revoked is not None:
        await manager.broadcast({"type": "vow_changed", "data": {"action": "origin_regenerated"}})
    try:
        await export_conversation(conv_id)
    except Exception:
        logger.warning("export after regenerate-replace failed (conv=%s)", conv_id, exc_info=True)
    return (
        build_vow_block(active, now=now),
        build_vow_ability_block(remaining_today=remaining),
    )


async def stream_chat_response(
    *,
    conv_id: str,
    model_key: str,
    history: list[dict],
    prompt_meta: dict,
    temperature: Optional[float],
    memory_eval_mode: bool = False,
) -> StreamingResponse:
    ai_msg_id = f"msg_{int(time.time()*1000)}"
    trace = prompt_meta.pop("_turn_trace", None)
    if not isinstance(trace, TurnDiagnostics):
        trace = TurnDiagnostics(conv_id, str(prompt_meta.get("prompt_source") or "send"))
    trace.model_key = model_key
    trace.assistant_message_id = ai_msg_id
    prompt_meta["turn_id"] = trace.turn_id
    usage_meta: dict = {}
    queue: asyncio.Queue = DiagnosticQueue(trace)
    memory_v2_recall_debug = prompt_meta.get("memory_v2_recall")

    async def _bg_generate():
        trace_token = current_turn.set(trace)
        full_text = ""
        raw_model_output = ""
        provider_error = ""
        has_error = False
        assistant_persisted = False
        presence_outcomes_consumed = 0
        turn_outcome = "pipeline_failed"
        tool_context: ToolContext | None = None
        buffering_structured_reply: bool | None = None
        visible_filter = _UpdateModelStreamFilter()
        working_model_request_filter = _WorkingModelRequestStreamFilter()
        ring_filter = _RingTouchStreamFilter()
        recall_filter = _RecallIntentStreamFilter()
        web_search_filter = _WebSearchIntentStreamFilter()
        self_wake_filter = _SelfWakeStreamFilter()
        tide_filter = _TideIntentStreamFilter()
        vow_filter = _VowStreamFilter()
        control_marker_filter = ControlMarkerStreamFilter()
        try:
            turn_profile = chat_turn_profile(
                str(prompt_meta.get("prompt_source") or "send"),
                web_search_allowed=(
                    str((prompt_meta.get("web_search") or {}).get("status"))
                    != "disabled"
                ),
            )
            model_invocation_id = tool_invocation_ledger.new_invocation_id(
                "main_core"
            )
            prompt_meta["invocation_id"] = model_invocation_id
            tool_context = _tool_context(
                conv_id=conv_id,
                msg_id=ai_msg_id,
                model_key=model_key,
                prompt_meta=prompt_meta,
                memory_eval_mode=memory_eval_mode,
            )
            await queue.put({"id": ai_msg_id, "type": "start", "turn_id": trace.turn_id})
            await tool_invocation_ledger.record_model_request(
                tool_context,
                invocation_id=model_invocation_id,
                request_snapshot=history,
                advertised_tools=prompt_meta.get("advertised_tools") or (),
                metadata={
                    "prompt_source": str(
                        prompt_meta.get("prompt_source") or "send"
                    ),
                    "temperature": temperature,
                    "diagnostics": trace.snapshot(),
                    "image_history": prompt_meta.get("image_history"),
                },
            )
            trace.start("model")
            try:
                async for chunk in stream_ai(history, model_key, usage_meta, temperature):
                    chunk = str(chunk)
                    raw_model_output += chunk
                    full_text += chunk
                    if buffering_structured_reply is None:
                        probe = full_text.lstrip()
                        if not probe:
                            continue
                        buffering_structured_reply = looks_like_structured_reply(probe)
                        if not buffering_structured_reply:
                            visible = vow_filter.feed(
                                tide_filter.feed(
                                    recall_filter.feed(
                                        ring_filter.feed(
                                            working_model_request_filter.feed(
                                                visible_filter.feed(full_text)
                                            )
                                        )
                                    )
                                )
                            )
                            visible = self_wake_filter.feed(web_search_filter.feed(visible))
                            visible = control_marker_filter.feed(visible)
                            if visible:
                                await queue.put({"type": "chunk", "content": visible})
                        continue
                    if not buffering_structured_reply:
                        visible = vow_filter.feed(
                            tide_filter.feed(
                                recall_filter.feed(
                                    ring_filter.feed(
                                        working_model_request_filter.feed(
                                            visible_filter.feed(chunk)
                                        )
                                    )
                                )
                            )
                        )
                        visible = self_wake_filter.feed(web_search_filter.feed(visible))
                        visible = control_marker_filter.feed(visible)
                        if visible:
                            await queue.put({"type": "chunk", "content": visible})
            except Exception as exc:
                has_error = True
                turn_outcome = "provider_failed"
                provider_error = str(exc)
                error_text = f"\n[请求出错: {str(exc)}]"
                full_text += error_text
                await queue.put({"type": "chunk", "content": error_text})
            finally:
                trace.finish("model")

            full_text = strip_retry_marker(full_text)
            stripped = full_text.strip()
            if not has_error and looks_like_model_error_text(stripped):
                has_error = True
                turn_outcome = "provider_failed"
            elif not has_error:
                turn_outcome = "succeeded" if stripped else "invalid_output"
            await tool_invocation_ledger.record_model_output(
                tool_context,
                invocation_id=model_invocation_id,
                raw_output=raw_model_output,
                outcome=("failed" if has_error else (
                    "succeeded" if stripped else "unknown"
                )),
                error=provider_error,
                metadata={"turn_outcome": turn_outcome, "diagnostics": trace.snapshot(usage_meta)},
            )
            if not buffering_structured_reply:
                visible_tail = vow_filter.feed(
                    tide_filter.feed(
                        recall_filter.feed(
                            ring_filter.feed(
                                working_model_request_filter.feed(visible_filter.flush())
                            )
                        )
                    )
                )
                visible_tail += vow_filter.feed(
                    tide_filter.feed(
                        recall_filter.feed(
                            ring_filter.feed(working_model_request_filter.flush())
                        )
                    )
                )
                visible_tail += vow_filter.feed(
                    tide_filter.feed(recall_filter.feed(ring_filter.flush()))
                )
                visible_tail += vow_filter.feed(tide_filter.feed(recall_filter.flush()))
                visible_tail += vow_filter.feed(tide_filter.flush())
                visible_tail += vow_filter.flush()
                visible_tail = web_search_filter.feed(visible_tail) + web_search_filter.flush()
                visible_tail = self_wake_filter.feed(visible_tail) + self_wake_filter.flush()
                visible_tail = control_marker_filter.feed(visible_tail)
                visible_tail += control_marker_filter.flush()
                if visible_tail:
                    await queue.put({"type": "chunk", "content": visible_tail})

            try:
                try:
                    postprocessed = await _post_processor.process(
                        full_text,
                        conv_id=conv_id,
                        memory_eval_mode=memory_eval_mode,
                        enabled_commands=turn_profile.enabled_commands,
                        tool_context=tool_context,
                    )
                except TypeError as exc:
                    # Compatibility for injected pre-ledger/pre-profile
                    # processors in tests and local extensions.
                    if "tool_context" in str(exc):
                        try:
                            postprocessed = await _post_processor.process(
                                full_text,
                                conv_id=conv_id,
                                memory_eval_mode=memory_eval_mode,
                                enabled_commands=turn_profile.enabled_commands,
                            )
                        except TypeError as legacy_exc:
                            if "enabled_commands" not in str(legacy_exc):
                                raise
                            postprocessed = await _post_processor.process(
                                full_text,
                                conv_id=conv_id,
                                memory_eval_mode=memory_eval_mode,
                            )
                    elif "enabled_commands" in str(exc):
                        postprocessed = await _post_processor.process(
                            full_text,
                            conv_id=conv_id,
                            memory_eval_mode=memory_eval_mode,
                        )
                    else:
                        raise
            except Exception:
                if turn_outcome == "succeeded":
                    turn_outcome = "postprocess_failed"
                raise
            full_text = postprocessed.content
            if buffering_structured_reply and full_text:
                await queue.put({"type": "chunk", "content": full_text})

            music_execution = await execute_postprocessed_actions(
                postprocessed,
                profile=turn_profile,
                context=tool_context,
                only_capabilities=frozenset({"music.search"}),
            )
            music_results = music_execution.results_for("music.search")
            music_cards = [
                *postprocessed.music_cards,
                *_music_cards_from_results(music_results),
            ]

            schedule_execution = await execute_postprocessed_actions(
                postprocessed,
                profile=turn_profile,
                context=tool_context,
                only_capabilities=SCHEDULE_TOOL_NAMES,
            )

            await execute_postprocessed_actions(
                postprocessed,
                profile=turn_profile,
                context=tool_context,
                only_capabilities=SELF_WAKE_ENTRY_TOOLS,
            )

            heart_execution = await execute_postprocessed_actions(
                postprocessed,
                profile=turn_profile,
                context=tool_context,
                only_capabilities=frozenset({"heart.whisper"}),
            )
            heart_results = heart_execution.results_for("heart.whisper")
            for result in heart_results:
                if result.status is not ToolStatus.EXECUTED or not result.result:
                    continue
                hw_data = dict(result.result)
                await queue.put(hw_data)
                await manager.broadcast({"type": "heart_whisper", "data": hw_data})

            await execute_postprocessed_actions(
                postprocessed,
                profile=turn_profile,
                context=tool_context,
                only_capabilities=frozenset({"memory.remember"}),
            )

            if postprocessed.tide_intent and not has_error and not memory_eval_mode:
                from app.tide.intent import tide_intent_service
                await tide_intent_service.record_intent(
                    conv_id=conv_id,
                    msg_id=ai_msg_id,
                    intent_text=postprocessed.tide_intent,
                    invocation_id=model_invocation_id,
                    advertised_tools=tuple(
                        prompt_meta.get("advertised_tools") or ()
                    ),
                )

            music_atts = _music_attachments(music_cards)
            att_json = json.dumps(music_atts, ensure_ascii=False) if music_atts else ""

            # ── 誓约提交编排（§4.3）：净化/准入 + vow 插入 + 确认/拒绝写入正文 +
            # assistant 消息落库，全部在 BEGIN IMMEDIATE 单事务内。
            # 不变量：屏幕上有立约确认 ⇔ 库里有这条誓约；确认语只在提交成功后可见。
            vow_extract = getattr(postprocessed, "vow", None)
            process_vow = bool(
                vow_extract is not None and vow_extract.found
                and not has_error and not memory_eval_mode
            )
            base_text = full_text
            vow_note = ""
            vow_committed = False
            created_pending_id = None
            created_web_search_id = None
            now2 = time.time()
            if not has_error:
                pending_meta = prompt_meta.get("pending_recall") or {}
                selected_pending_id = (
                    str(pending_meta.get("pending_id") or "")
                    if pending_meta.get("status") == "selected"
                    else ""
                )
                current_user_message_id = str(
                    prompt_meta.get("current_user_message_id") or ""
                )
                prompt_source = str(prompt_meta.get("prompt_source") or "send")
                memory_v3_snapshot = prompt_meta.get("memory_v3_config_snapshot") or {}
                persisted = False
                if process_vow:
                    try:
                        async with get_db() as db2:
                            await db2.execute("BEGIN IMMEDIATE")
                            try:
                                vow_row, affirmation, vow_reject = await vow_service.admit_ai_vow_in_tx(
                                    db2,
                                    extract=vow_extract,
                                    conv_id=conv_id,
                                    message_id=ai_msg_id,
                                    created_at=now2,
                                )
                                vow_note = (
                                    format_vow_confirmation(affirmation)
                                    if vow_row is not None
                                    else format_vow_rejection(vow_reject)
                                )
                                full_text = f"{base_text}\n\n{vow_note}".strip()
                                await db2.execute(
                                    "INSERT INTO messages (id, conv_id, role, content, created_at, attachments) VALUES (?,?,?,?,?,?)",
                                    (ai_msg_id, conv_id, "assistant", full_text, now2, att_json),
                                )
                                await db2.execute("UPDATE conversations SET updated_at=? WHERE id=?", (now2, conv_id))
                                pending_tx = await pending_recall_service.apply_after_assistant_in_tx(
                                    db2,
                                    conv_id=conv_id,
                                    assistant_message_id=ai_msg_id,
                                    created_at=now2,
                                    current_user_message_id=current_user_message_id or None,
                                    selected_pending_id=selected_pending_id or None,
                                    recall_intent=getattr(postprocessed, "recall_intent", ""),
                                    allow_new_intent=(
                                        prompt_source == "send" and not memory_eval_mode
                                    ),
                                    config_snapshot=memory_v3_snapshot,
                                )
                                web_tx = await web_search_service.finalize_dialogue_turn_in_tx(
                                    db2,
                                    conv_id=conv_id,
                                    bound_turn_id=str((prompt_meta.get("web_search") or {}).get("bound_turn_id") or ""),
                                    assistant_message_id=ai_msg_id,
                                    intent_text=getattr(postprocessed, "web_search_intent", ""),
                                    origin_source="send",
                                    allow_new_intent=(
                                        prompt_source == "send"
                                        and turn_profile.allows_marker("web_search_intent")
                                        and bool(base_text.strip())
                                        and not memory_eval_mode
                                    ),
                                    now=now2,
                                    replay_from_assistant_message_id=(
                                        str((prompt_meta.get("web_search") or {}).get("assistant_message_id") or "")
                                        if prompt_source == "regenerate"
                                        else ""
                                    ),
                                )
                                presence_outcomes_consumed = (
                                    await _consume_presence_outcomes_in_tx(
                                        db2,
                                        prompt_meta=prompt_meta,
                                        assistant_message_id=ai_msg_id,
                                        consumed_at=now2,
                                    )
                                )
                                await db2.commit()
                                persisted = True
                                vow_committed = vow_row is not None
                                created_pending_id = pending_tx["created_pending_id"]
                                created_web_search_id = web_tx.get("search_id")
                            except BaseException:
                                await db2.rollback()
                                raise
                    except Exception:
                        # 事务整体失败：vow 已回滚。降级保住回复本体——
                        # 带中性拒绝说明走普通落库，确认语绝不出现。
                        logger.exception("vow commit orchestration failed (conv=%s msg=%s)", conv_id, ai_msg_id)
                        vow_note = format_vow_rejection("系统处理出错")
                        full_text = f"{base_text}\n\n{vow_note}".strip()
                if not persisted:
                    async with get_db() as db2:
                        await db2.execute("BEGIN IMMEDIATE")
                        await db2.execute(
                            "INSERT INTO messages (id, conv_id, role, content, created_at, attachments) VALUES (?,?,?,?,?,?)",
                            (ai_msg_id, conv_id, "assistant", full_text, now2, att_json),
                        )
                        await db2.execute("UPDATE conversations SET updated_at=? WHERE id=?", (now2, conv_id))
                        pending_tx = await pending_recall_service.apply_after_assistant_in_tx(
                            db2,
                            conv_id=conv_id,
                            assistant_message_id=ai_msg_id,
                            created_at=now2,
                            current_user_message_id=current_user_message_id or None,
                            selected_pending_id=selected_pending_id or None,
                            recall_intent=getattr(postprocessed, "recall_intent", ""),
                            allow_new_intent=(
                                prompt_source == "send" and not memory_eval_mode
                            ),
                            config_snapshot=memory_v3_snapshot,
                        )
                        web_tx = await web_search_service.finalize_dialogue_turn_in_tx(
                            db2,
                            conv_id=conv_id,
                            bound_turn_id=str((prompt_meta.get("web_search") or {}).get("bound_turn_id") or ""),
                            assistant_message_id=ai_msg_id,
                            intent_text=getattr(postprocessed, "web_search_intent", ""),
                            origin_source="send",
                            allow_new_intent=(
                                prompt_source == "send"
                                and turn_profile.allows_marker("web_search_intent")
                                and bool(base_text.strip())
                                and not memory_eval_mode
                            ),
                            now=now2,
                            replay_from_assistant_message_id=(
                                str((prompt_meta.get("web_search") or {}).get("assistant_message_id") or "")
                                if prompt_source == "regenerate"
                                else ""
                            ),
                        )
                        presence_outcomes_consumed = (
                            await _consume_presence_outcomes_in_tx(
                                db2,
                                prompt_meta=prompt_meta,
                                assistant_message_id=ai_msg_id,
                                consumed_at=now2,
                            )
                        )
                        await db2.commit()
                        created_pending_id = pending_tx["created_pending_id"]
                        created_web_search_id = web_tx.get("search_id")
                assistant_persisted = True
                if not memory_eval_mode:
                    _schedule_working_model_pipeline_after_commit(
                        conv_id=conv_id,
                        origin_user_message_id=current_user_message_id,
                        origin_assistant_message_id=ai_msg_id,
                        model_key=model_key,
                        request_candidate=postprocessed.working_model_request,
                        identity_snapshot=prompt_meta.get(
                            "working_model_writer_identity"
                        ),
                    )
                if vow_committed:
                    # 管理页/其他设备靠该事件刷新誓约列表
                    await manager.broadcast({"type": "vow_changed", "data": {"action": "ai_created"}})
                if vow_note:
                    # 确认/拒绝说明在提交成功后才进入可见流（SSE 追加）
                    await queue.put({"type": "chunk", "content": f"\n\n{vow_note}"})
                if created_pending_id:
                    pending_recall_service.start_background(created_pending_id)
                if created_web_search_id:
                    web_search_service.start_background(created_web_search_id)

            ai_msg = {
                "id": ai_msg_id,
                "conv_id": conv_id,
                "role": "assistant",
                "content": full_text,
                "created_at": now2,
                "attachments": music_atts,
            }
            if full_text:
                trace.visible()
            await manager.broadcast({"type": "msg_created", "data": ai_msg})
            if full_text:
                await tool_invocation_ledger.record_visible_message(
                    tool_context,
                    invocation_id=model_invocation_id,
                    cleaned_content=full_text,
                    message_id=ai_msg_id,
                    metadata={"persisted": assistant_persisted},
                )
            for schedule_result in schedule_execution.results_for("schedule.list"):
                payload = dict(schedule_result.result or {})
                if schedule_result.status is ToolStatus.FAILED:
                    payload.setdefault("status", "failed")
                    payload.setdefault(
                        "reason",
                        schedule_result.error or "adapter_failed",
                    )
                if schedule_result.status in {
                    ToolStatus.EXECUTED,
                    ToolStatus.FAILED,
                }:
                    create_tracked_task(
                        perform_schedule_list_followup(
                            conv_id,
                            model_key,
                            payload,
                            parent_request_id=tool_context.request_id or "",
                        ),
                        name=f"schedule_list_followup:{conv_id}:{ai_msg_id}",
                    )
            if not has_error:
                await export_conversation(conv_id)

            if not has_error and not memory_eval_mode:
                memory_v3_snapshot = _schedule_chunk_index_update(
                    conv_id,
                    reason="assistant_message",
                )
                timeline_service.start_background_refresh(memory_v3_snapshot)
                create_tracked_task(
                    _maybe_auto_digest(memory_v3_snapshot),
                    name=f"auto_digest:{conv_id}:{ai_msg_id}",
                )

            toy_intents = _with_control_toy_fallback(
                _toy_command_intents(postprocessed),
                tool_context,
                has_error=has_error,
            )
            toy_execution = await execute_postprocessed_actions(
                postprocessed,
                profile=turn_profile,
                context=tool_context,
                only_capabilities=frozenset({"device.toy"}),
                allow_toy_fallback=True,
                has_error=has_error,
            )
            toy_results = toy_execution.results_for("device.toy")
            toy_data = _toy_payload_from_results(toy_results)
            toy_delivery = _toy_delivery_debug(toy_intents, toy_results, toy_data)
            _log_toy_delivery(
                conv_id=conv_id,
                msg_id=ai_msg_id,
                model_key=model_key,
                context=tool_context,
                delivery=toy_delivery,
            )
            if toy_data:
                toy_commands = toy_data["commands"]
                toy_data["msg_id"] = ai_msg_id
                await queue.put(toy_data)
                await manager.broadcast({"type": "toy_command", "data": toy_data})
                await _toy_sys_msg(conv_id, toy_commands)
            elif not has_error and "device.toy" in tool_context.capabilities:
                toy_rejection = _toy_rejection_payload(toy_intents, toy_results, toy_delivery, msg_id=ai_msg_id)
                if toy_rejection:
                    await queue.put(toy_rejection)
                    await manager.broadcast({"type": "toy_command_rejected", "data": toy_rejection})

            await execute_postprocessed_actions(
                postprocessed,
                profile=turn_profile,
                context=tool_context,
                only_capabilities=frozenset({"device.ring_touch"}),
            )

            cam_execution = await execute_postprocessed_actions(
                postprocessed,
                profile=turn_profile,
                context=tool_context,
                only_capabilities=frozenset({"monitor.camera"}),
            )
            cam_results = cam_execution.results_for("monitor.camera")
            for cam_data in _executed_payloads(cam_results):
                await queue.put(cam_data)
                if cam_data.get("type") == "cam_check":
                    await manager.broadcast({"type": "cam_check", "data": cam_data})

            poi_execution = await execute_postprocessed_actions(
                postprocessed,
                profile=turn_profile,
                context=tool_context,
                only_capabilities=frozenset({"location.poi_search"}),
            )
            poi_results = poi_execution.results_for("location.poi_search")
            for poi_data in _executed_payloads(poi_results):
                await queue.put(poi_data)
                await manager.broadcast({"type": "poi_search", "data": poi_data})

            activity_execution = await execute_postprocessed_actions(
                postprocessed,
                profile=turn_profile,
                context=tool_context,
                only_capabilities=frozenset({"activity.summary"}),
            )
            activity_results = activity_execution.results_for("activity.summary")
            for activity_data in _executed_payloads(activity_results):
                await queue.put(activity_data)
                await manager.broadcast({"type": "activity_check", "data": activity_data})

            screen_execution = await execute_postprocessed_actions(
                postprocessed,
                profile=turn_profile,
                context=tool_context,
                only_capabilities=frozenset({"pc.screen_check"}),
            )
            screen_results = screen_execution.results_for("pc.screen_check")
            for screen_data in _executed_payloads(screen_results):
                await queue.put(screen_data)
                await manager.broadcast({"type": screen_data.get("type", "screen_check_pending"), "data": screen_data})

            mobile_execution = await execute_postprocessed_actions(
                postprocessed,
                profile=turn_profile,
                context=tool_context,
                only_capabilities=frozenset({"mobile.screen_check"}),
            )
            mobile_screen_results = mobile_execution.results_for("mobile.screen_check")
            for ms_data in _executed_payloads(mobile_screen_results):
                await queue.put(ms_data)
                await manager.broadcast({"type": ms_data.get("type", "screen_check_pending"), "data": ms_data})

            if not has_error:
                image_execution = await execute_postprocessed_actions(
                    postprocessed, profile=turn_profile, context=tool_context,
                    only_capabilities=frozenset({"memory.view_image"}),
                )
                for image_result in _executed_payloads(image_execution.results_for("memory.view_image")):
                    from app.image_memory.view import followup as image_view_followup
                    create_tracked_task(
                        image_view_followup(tool_context, image_result),
                        name=f"image_view_followup:{conv_id}:{ai_msg_id}",
                    )

            if music_cards:
                music_data = {"type": "music", "msg_id": ai_msg_id, "cards": music_cards}
                await queue.put(music_data)
                await manager.broadcast({"type": "music", "data": music_data})

            await _record_v2_memory_usage_for_chat(
                memory_v2_recall_debug,
                conv_id=conv_id,
                msg_id=ai_msg_id,
                chat_succeeded=not has_error,
                response_text=full_text if not has_error else "",
            )
            if not has_error:
                try:
                    await timeline_service.record_injection_usage(
                        prompt_meta.get("timeline"),
                        conv_id=conv_id,
                        assistant_message_id=ai_msg_id,
                        response_text=full_text,
                    )
                except Exception:
                    logger.exception(
                        "timeline usage recording failed (conv=%s msg=%s)",
                        conv_id,
                        ai_msg_id,
                    )

            debug_data = {
                "type": "debug",
                "turn_id": trace.turn_id,
                "diagnostics": trace.snapshot(usage_meta),
                "image_history": prompt_meta.get("image_history"),
                "model": model_key,
                "msg_id": ai_msg_id,
                "recall_keywords": prompt_meta["recall_keywords"],
                "recall_query": prompt_meta["recall_query"],
                "recall_topic": prompt_meta["recall_topic"],
                "is_search_needed": prompt_meta["is_search_needed"],
                "recalled_memories": prompt_meta["recalled_memories"],
                "debug_top6": prompt_meta["debug_top6"],
                "memory_v2_recall": memory_v2_recall_debug,
                "timeline": prompt_meta.get("timeline"),
                "prompt_messages": prompt_meta["prompt_messages"],
                "prompt_count": prompt_meta["prompt_count"],
                "usage": usage_meta if usage_meta else None,
                "has_error": has_error,
                "error_text": full_text if has_error else None,
                "chat_mode": tool_context.mode,
                "mode_source": tool_context.metadata.get("mode_source"),
                "capabilities": list(tool_context.capabilities),
                "control_context_source": prompt_meta.get("control_context_source"),
                "control_session_id": prompt_meta.get("control_session_id"),
                "control_resource_id": prompt_meta.get("control_resource_id"),
                "control_kind": prompt_meta.get("control_kind"),
                "control_status": prompt_meta.get("control_status"),
                "control_epoch": prompt_meta.get("control_epoch"),
                "owner_client_id": prompt_meta.get("owner_client_id"),
                "hidden_agenda_status": prompt_meta.get("hidden_agenda_status"),
                "hidden_agenda_source_refs": prompt_meta.get("hidden_agenda_source_refs"),
                "postprocess_toy_commands": list(getattr(postprocessed, "toy_commands", ()) or ()),
                "toy_commands": toy_delivery.get("commands"),
                "toy_delivery": toy_delivery,
                "presence_outcomes": {
                    **dict(prompt_meta.get("presence_outcomes") or {}),
                    "consumed": presence_outcomes_consumed,
                },
            }
            await queue.put(debug_data)
            await manager.broadcast({"type": "debug", "data": debug_data})
        except asyncio.CancelledError:
            turn_outcome = "cancelled"
            raise
        except Exception:
            if turn_outcome in {"succeeded", "invalid_output"}:
                turn_outcome = "pipeline_failed"
            import traceback

            traceback.print_exc()
        finally:
            if not assistant_persisted:
                try:
                    await presence_outcome_inbox.release_claim(
                        bound_turn_id=str(
                            (prompt_meta.get("presence_outcomes") or {}).get(
                                "bound_turn_id"
                            )
                            or ""
                        )
                    )
                except Exception:
                    logger.exception(
                        "Presence outcome claim release failed (conv=%s)", conv_id
                    )
            if tool_context is not None:
                await tool_invocation_ledger.record_turn(
                    tool_context,
                    prompt_source=str(prompt_meta.get("prompt_source") or "send"),
                    advertised_tools=prompt_meta.get("advertised_tools") or (),
                    turn_outcome=("evaluation" if memory_eval_mode else turn_outcome),
                    metadata={
                        "memory_eval_mode": bool(memory_eval_mode),
                        "assistant_persisted": assistant_persisted,
                        "has_error": has_error,
                        "diagnostics": trace.snapshot(usage_meta, finished=True),
                    },
                )
            await queue.put({"type": "done"})
            current_turn.reset(trace_token)

    create_tracked_task(_bg_generate(), name=f"chat_stream:{conv_id}:{ai_msg_id}")

    async def generate():
        while True:
            data = await queue.get()
            if data.get("type") == "done":
                break
            yield f"data: {json.dumps(data, ensure_ascii=False)}\n\n"

    return StreamingResponse(generate(), media_type="text/event-stream")
