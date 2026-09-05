"""Persistent, fail-open ledger for model tool invocations.

The ledger is deliberately write-only from the chat runtime.  Nothing in the
reply, prompt, or execution path reads it, so deleting the table or disabling
the writes cannot change conversational behaviour.
"""

from __future__ import annotations

import asyncio
import difflib
import json
import logging
import re
import time
import uuid
from collections import Counter
from collections.abc import Callable, Collection, Iterable, Mapping
from dataclasses import dataclass
from typing import Any

from app.vows.service import find_vow_markers

from .schemas import (
    ToolContext,
    ToolIntent,
    ToolResult,
    ToolStatus,
    get_tool_definition,
)


logger = logging.getLogger(__name__)

HISTORY_TRACE_VERSION = 0
LEDGER_WRITE_TIMEOUT_SECONDS = 1.0
LEDGER_SQLITE_BUSY_TIMEOUT_SECONDS = 0.5
PENDING_TERMINAL_MAX_CORRELATIONS = 512
PENDING_TERMINAL_MAX_EVENTS_PER_CORRELATION = 16
DEFAULT_LEDGER_SNAPSHOT_MAX_BYTES = 64 * 1024
MIN_LEDGER_SNAPSHOT_MAX_BYTES = 1024
_MIDDLE_OMISSION_MARKER = "\n...[middle omitted]...\n"

_LEDGER_COLUMNS = (
    "id",
    "event_key",
    "turn_id",
    "invocation_id",
    "source_chain",
    "correlation_id",
    "conv_id",
    "assistant_message_id",
    "intent_id",
    "tool_name",
    "stage",
    "status",
    "outcome",
    "turn_outcome",
    "side_effect_level",
    "source",
    "arguments_json",
    "raw_text",
    "events_json",
    "error",
    "result_summary",
    "metadata_json",
    "model_key",
    "prompt_source",
    "mode",
    "advertised_tools_json",
    "request_snapshot_json",
    "raw_output",
    "cleaned_content",
    "truncated",
    "snapshot_original_bytes",
    "history_trace_version",
    "created_at",
    "updated_at",
)


@dataclass(frozen=True)
class MarkerCandidate:
    stage: str
    raw_text: str
    start: int
    end: int
    tool_name: str
    command_group: str
    marker_name: str
    normalized_name: str


_MARKER_TO_TOOL = {
    "MUSIC": ("music.search", "music"),
    "TOY": ("device.toy", "toy"),
    "CAMCHECK": ("monitor.camera", "cam"),
    "查看动态": ("activity.summary", "activity"),
    "SCREENCHECK": ("pc.screen_check", "screen"),
    "MOBILESCREENCHECK": ("mobile.screen_check", "mobile_screen"),
    "POISEARCH": ("location.poi_search", "poi"),
    "ALARM": ("schedule.alarm", "schedule"),
    "REMINDER": ("schedule.reminder", "schedule"),
    "MONITOR": ("schedule.monitor", "schedule"),
    "SCHEDULEDEL": ("schedule.delete", "schedule"),
    "SCHEDULELIST": ("schedule.list", "schedule"),
    "HEART": ("heart.whisper", "heart"),
    "REMEMBER": ("memory.remember", "remember"),
    "RING": ("device.ring_touch", "ring"),
    "PRESENCEDRAW": ("desktop.presence.draw", "presence_draw"),
    "PRESENCESHOW": ("desktop.presence.show", "presence_show"),
    "SELFWAKE": ("self_wake.schedule", "self_wake"),
    "SELFWAKECANCEL": ("self_wake.cancel", "self_wake"),
}

_IGNORED_MARKERS = frozenset(
    {
        "VOW",
        "TIDEINTENT",
        "RECALLINTENT",
        "WORKINGMODELREQUEST",
        "UPDATEMODEL",
        "OPPORTUNITYNONE",
        "OPPORTUNITYREFLECT",
        "VISIBLE_REPLY",
        "META",
        "THINK",
        "THINKING",
        "THOUGHT",
        "ANALYSIS",
        "REASONING",
    }
)

_BRACKET_MARKER_PATTERN = re.compile(
    r"(?P<open>\[|【)\s*"
    r"(?P<name>/?[^:：|｜\]】\r\n]{1,60}?)"
    r"(?:\s*(?P<separator>[:：|｜])(?P<body>[^\]】\r\n]{0,500}))?"
    r"\s*(?P<close>\]|】)",
    re.IGNORECASE,
)
_VALID_RING_PATTERN = re.compile(r"\[RING:([^\]]+)\]")
_PRIVATE_BLOCK_PATTERNS = (
    re.compile(r"<meta\b[^>]*>.*?</meta>", re.IGNORECASE | re.DOTALL),
    re.compile(
        r"<(think|thinking|thought|analysis|reasoning)\b[^>]*>.*?</\1>",
        re.IGNORECASE | re.DOTALL,
    ),
    re.compile(
        r"```(?:think|thinking|thought|analysis|reasoning)\b[\s\S]*?```",
        re.IGNORECASE,
    ),
    re.compile(
        r"<(?:meta|think|thinking|thought|analysis|reasoning)\b[^>]*>[\s\S]*$",
        re.IGNORECASE,
    ),
    re.compile(
        r"\[WORKING_MODEL_REQUEST\][\s\S]*?\[/WORKING_MODEL_REQUEST\]",
        re.IGNORECASE,
    ),
    re.compile(r"\[WORKING_MODEL_REQUEST\][\s\S]*$", re.IGNORECASE),
)


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)


def _positive_snapshot_limit(value: Any) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = DEFAULT_LEDGER_SNAPSHOT_MAX_BYTES
    return max(MIN_LEDGER_SNAPSHOT_MAX_BYTES, parsed)


def _decode_utf8_slice(data: bytes, size: int, *, tail: bool = False) -> str:
    if size <= 0:
        return ""
    selected = data[-size:] if tail else data[:size]
    return selected.decode("utf-8", errors="ignore")


def _truncate_middle_text(value: str, max_bytes: int) -> tuple[str, bool, int]:
    text = str(value or "")
    encoded = text.encode("utf-8")
    original_bytes = len(encoded)
    if original_bytes <= max_bytes:
        return text, False, original_bytes

    marker = _MIDDLE_OMISSION_MARKER.encode("utf-8")
    available = max(0, max_bytes - len(marker))
    head_budget = available // 2
    tail_budget = available - head_budget
    head = _decode_utf8_slice(encoded, head_budget)
    tail = _decode_utf8_slice(encoded, tail_budget, tail=True)
    truncated = head + _MIDDLE_OMISSION_MARKER + tail
    while len(truncated.encode("utf-8")) > max_bytes and tail:
        tail = tail[1:]
        truncated = head + _MIDDLE_OMISSION_MARKER + tail
    return truncated, True, original_bytes


def _truncate_json_snapshot(value: Any, max_bytes: int) -> tuple[str, bool, int]:
    serialized = _json(value)
    encoded = serialized.encode("utf-8")
    original_bytes = len(encoded)
    if original_bytes <= max_bytes:
        return serialized, False, original_bytes

    def build(head_budget: int, tail_budget: int) -> str:
        head = _decode_utf8_slice(encoded, head_budget)
        tail = _decode_utf8_slice(encoded, tail_budget, tail=True)
        kept_bytes = len(head.encode("utf-8")) + len(tail.encode("utf-8"))
        return _json({
            "truncated": True,
            "original_bytes": original_bytes,
            "omitted_middle_bytes": max(0, original_bytes - kept_bytes),
            "head": head,
            "tail": tail,
        })

    empty_wrapper = build(0, 0)
    available = max(0, max_bytes - len(empty_wrapper.encode("utf-8")))
    head_budget = available // 2
    tail_budget = available - head_budget
    while True:
        payload = build(head_budget, tail_budget)
        payload_size = len(payload.encode("utf-8"))
        if payload_size <= max_bytes:
            return payload, True, original_bytes
        overflow = max(1, payload_size - max_bytes)
        total_budget = head_budget + tail_budget
        if total_budget <= 0:
            return empty_wrapper, True, original_bytes
        reduction = min(total_budget, overflow)
        head_reduction = min(head_budget, (reduction + 1) // 2)
        tail_reduction = min(tail_budget, reduction - head_reduction)
        remainder = reduction - head_reduction - tail_reduction
        if remainder:
            extra = min(head_budget - head_reduction, remainder)
            head_reduction += extra
            remainder -= extra
        if remainder:
            tail_reduction += min(tail_budget - tail_reduction, remainder)
        head_budget -= head_reduction
        tail_budget -= tail_reduction


def _turn_id(context: ToolContext) -> str:
    return str(context.metadata.get("turn_id") or context.request_id or context.msg_id or "").strip()


def _invocation_id(context: ToolContext) -> str:
    return str(context.metadata.get("invocation_id") or "").strip()


def _source_chain(context: ToolContext) -> str:
    source = str(
        context.metadata.get("source_chain")
        or context.metadata.get("source")
        or "unknown"
    ).strip()
    if source in {"send", "regenerate"}:
        return "main"
    return source or "unknown"


def _advertised_tools(context: ToolContext) -> list[str]:
    """Return the frozen prompt exposure set carried by this invocation."""

    values = context.metadata.get("advertised_tools")
    if values is None:
        values = context.capabilities
    if isinstance(values, str):
        values = (values,)
    try:
        return sorted({str(item) for item in values if str(item)})
    except TypeError:
        return []


def _find_correlation_id(value: Any) -> str:
    """Find the transport request id without assuming one adapter shape."""

    if isinstance(value, Mapping):
        for key in ("correlation_id", "request_id"):
            candidate = str(value.get(key) or "").strip()
            if candidate:
                return candidate
        for nested in value.values():
            candidate = _find_correlation_id(nested)
            if candidate:
                return candidate
    elif isinstance(value, (list, tuple)):
        for nested in value:
            candidate = _find_correlation_id(nested)
            if candidate:
                return candidate
    return ""


def _event_id(event_key: str) -> str:
    return "tool_evt_" + uuid.uuid5(uuid.NAMESPACE_URL, event_key).hex


def _normalize_marker_name(value: str) -> str:
    raw = str(value or "").strip().lstrip("/").upper()
    return re.sub(r"[^A-Z0-9\u4e00-\u9fff]", "", raw)


def _resolve_marker(value: str) -> tuple[str, str, str] | None:
    normalized = _normalize_marker_name(value)
    if not normalized or normalized in _IGNORED_MARKERS:
        return None
    exact = _MARKER_TO_TOOL.get(normalized)
    if exact:
        return exact[0], exact[1], normalized
    if len(normalized) < 4:
        return None
    nearest = difflib.get_close_matches(
        normalized,
        tuple(_MARKER_TO_TOOL),
        n=1,
        cutoff=0.78,
    )
    if not nearest:
        return None
    tool_name, command_group = _MARKER_TO_TOOL[nearest[0]]
    return tool_name, command_group, normalized


def _mask_span(chars: list[str], start: int, end: int) -> None:
    chars[start:end] = " " * max(0, end - start)


def _mask_inert_regions(text: str) -> str:
    """Preserve offsets while hiding private and VOW-owned marker syntax."""

    chars = list(text)
    for pattern in _PRIVATE_BLOCK_PATTERNS:
        for match in pattern.finditer(text):
            _mask_span(chars, *match.span())
    try:
        vow_spans, unclosed_start = find_vow_markers(text)
    except Exception:
        vow_spans, unclosed_start = [], None
    for start, end, _payload in vow_spans:
        _mask_span(chars, start, end)
    if unclosed_start is not None:
        _mask_span(chars, unclosed_start, len(text))
    return "".join(chars)


def detect_unparsed_marker_candidates(
    raw_output: str,
    *,
    parsed_intents: Iterable[ToolIntent] = (),
    enabled_commands: Collection[str] | None = None,
) -> list[MarkerCandidate]:
    """Find likely tool markers that the current parser did not accept.

    A syntactically valid marker that belongs to a command group excluded by
    the turn profile is ``not_enabled``.  Tolerant/fuzzy shapes which even the
    all-tools parser cannot accept are ``parse_failed``.
    """

    source = str(raw_output or "")
    if not source:
        return []
    # Lazy import keeps parser -> schedule/sentinel -> ledger acyclic during
    # application startup; the parser is only needed while recording output.
    from .parser import ALL_COMMAND_GROUPS, parse_tool_intents

    masked = _mask_inert_regions(source)
    parsed_raw = Counter(str(intent.raw_text or "") for intent in parsed_intents)
    enabled = (
        set(ALL_COMMAND_GROUPS)
        if enabled_commands is None
        else {str(item) for item in enabled_commands}
    )
    candidates: list[MarkerCandidate] = []

    for match in _BRACKET_MARKER_PATTERN.finditer(masked):
        start, end = match.span()
        raw_text = source[start:end]
        marker_name = str(match.group("name") or "").strip()
        resolved = _resolve_marker(marker_name)
        if resolved is None:
            continue
        tool_name, command_group, normalized = resolved

        if parsed_raw[raw_text] > 0:
            parsed_raw[raw_text] -= 1
            continue

        if tool_name == "device.ring_touch":
            valid_ring = bool(_VALID_RING_PATTERN.fullmatch(raw_text))
            if valid_ring and "ring" in enabled:
                continue
            stage = "not_enabled" if valid_ring else "parse_failed"
        else:
            valid_anywhere = parse_tool_intents(
                raw_text,
                enabled_commands=ALL_COMMAND_GROUPS,
            )
            stage = (
                "not_enabled"
                if valid_anywhere and command_group not in enabled
                else "parse_failed"
            )

        candidates.append(
            MarkerCandidate(
                stage=stage,
                raw_text=raw_text,
                start=start,
                end=end,
                tool_name=tool_name,
                command_group=command_group,
                marker_name=marker_name,
                normalized_name=normalized,
            )
        )
    return candidates


def execution_outcome(result: ToolResult) -> str:
    """Map transport status to honest semantic outcome."""

    if result.status is ToolStatus.FAILED:
        return "failed"
    if result.status is ToolStatus.PENDING:
        return "pending"
    if result.status is ToolStatus.SKIPPED:
        return "rejected"

    payload = dict(result.result or {})
    event_type = str(payload.get("type") or "").lower()
    payload_status = str(payload.get("status") or "").lower()
    if payload_status == "queued":
        return "dispatched"
    if payload_status in {
        "succeeded",
        "failed",
        "rejected",
        "dispatched",
        "pending",
        "unknown",
    }:
        return payload_status
    if payload_status in {"executed", "completed"}:
        return "succeeded"
    if payload_status in {"failed", "timeout"}:
        return "failed"
    if (
        payload.get("ok") is False
        or payload.get("accepted") is False
        or payload.get("stored") is False
        or payload.get("reject_reason")
        or "rejected" in event_type
        or "disabled" in event_type
        or payload_status in {"rejected", "denied", "disabled"}
    ):
        return "rejected"
    if result.tool_name == "memory.remember":
        return "succeeded" if payload.get("stored") is True else "rejected"
    if result.tool_name in {"activity.summary", "location.poi_search"}:
        return "dispatched"
    if result.tool_name in {"pc.screen_check", "mobile.screen_check"}:
        if "pending" in event_type:
            return "dispatched"
        if "rejected" in event_type:
            return "rejected"
        return "unknown"
    if not payload:
        return "unknown"
    return "succeeded"


def _result_summary(result: Mapping[str, Any] | None) -> str:
    payload = dict(result or {})
    if not payload:
        return ""
    summary: dict[str, Any] = {}
    preferred = (
        "type",
        "ok",
        "status",
        "reason",
        "reject_reason",
        "message",
        "request_id",
        "query",
        "stored",
        "command",
        "tool_name",
    )
    for key in preferred:
        value = payload.get(key)
        if value is None:
            continue
        summary[key] = str(value)[:240] if isinstance(value, str) else value
    for key, value in payload.items():
        if key in summary or len(summary) >= 14:
            continue
        if isinstance(value, (str, int, float, bool)) or value is None:
            summary[key] = str(value)[:240] if isinstance(value, str) else value
        elif isinstance(value, (list, tuple)):
            summary[f"{key}_count"] = len(value)
        elif isinstance(value, Mapping):
            summary[f"{key}_keys"] = sorted(str(item) for item in value)[:20]
    return _json(summary)


class ToolInvocationLedger:
    """Write-only ledger facade.  All public writes are fail-open."""

    def __init__(
        self,
        *,
        db_factory: Callable[[], Any] | None = None,
        write_timeout_seconds: float = LEDGER_WRITE_TIMEOUT_SECONDS,
        snapshot_max_bytes: int | None = None,
    ):
        self._db_factory = db_factory
        self._write_timeout_seconds = max(0.01, float(write_timeout_seconds))
        self._snapshot_max_bytes_override = (
            _positive_snapshot_limit(snapshot_max_bytes)
            if snapshot_max_bytes is not None
            else None
        )
        self.write_failures = 0
        # Some adapters start their background work before the execution row is
        # materialized.  Keep an in-process, bounded handoff so an immediate
        # terminal event can still be appended to that same row once it exists.
        self._pending_terminal_events: dict[str, list[dict[str, Any]]] = {}

    def _connection(self):
        if self._db_factory is not None:
            return self._db_factory()
        # Lazy import keeps database.init_db -> ledger_schema acyclic.
        from database import get_db

        return get_db(timeout=LEDGER_SQLITE_BUSY_TIMEOUT_SECONDS)

    def _record_failure(self, operation: str) -> None:
        self.write_failures += 1
        logger.warning("tool invocation ledger %s failed", operation, exc_info=True)

    def _snapshot_max_bytes(self) -> int:
        if self._snapshot_max_bytes_override is not None:
            return self._snapshot_max_bytes_override
        try:
            from config import load_ai_behavior

            configured = load_ai_behavior().get(
                "tool_ledger_snapshot_max_bytes",
                DEFAULT_LEDGER_SNAPSHOT_MAX_BYTES,
            )
        except Exception:
            configured = DEFAULT_LEDGER_SNAPSHOT_MAX_BYTES
        return _positive_snapshot_limit(configured)

    @staticmethod
    def new_invocation_id(prefix: str = "model") -> str:
        safe_prefix = re.sub(r"[^a-zA-Z0-9_-]+", "_", str(prefix or "model"))
        return f"{safe_prefix}_{uuid.uuid4().hex}"

    async def _insert_materialized_rows(
        self,
        materialized: list[dict[str, Any]],
    ) -> int:
        """Perform one ledger transaction; the caller supplies the deadline."""

        placeholders = ",".join("?" for _ in _LEDGER_COLUMNS)
        sql = (
            "INSERT OR IGNORE INTO tool_invocation_events "
            f"({','.join(_LEDGER_COLUMNS)}) VALUES ({placeholders})"
        )
        values = [
            tuple(row.get(column) for column in _LEDGER_COLUMNS)
            for row in materialized
        ]
        async with self._connection() as db:
            await db.executemany(sql, values)
            await db.commit()
        return len(materialized)

    async def _insert_rows(self, rows: Iterable[Mapping[str, Any]]) -> int:
        materialized = [dict(row) for row in rows]
        if not materialized:
            return 0
        return await asyncio.wait_for(
            self._insert_materialized_rows(materialized),
            timeout=self._write_timeout_seconds,
        )

    @staticmethod
    def _base_row(
        *,
        event_key: str,
        context: ToolContext,
        stage: str,
        intent_id: str | None = None,
        tool_name: str | None = None,
        created_at: float | None = None,
    ) -> dict[str, Any]:
        turn_id = _turn_id(context)
        return {
            "id": _event_id(event_key),
            "event_key": event_key,
            "turn_id": turn_id,
            "invocation_id": _invocation_id(context) or None,
            "source_chain": _source_chain(context),
            "correlation_id": str(
                context.metadata.get("correlation_id") or ""
            ).strip() or None,
            "conv_id": context.conv_id,
            "assistant_message_id": context.msg_id,
            "intent_id": intent_id,
            "tool_name": tool_name,
            "stage": stage,
            "status": None,
            "outcome": "not_executed",
            "turn_outcome": None,
            "side_effect_level": None,
            "source": str(context.metadata.get("source") or "model_output"),
            "arguments_json": "{}",
            "raw_text": "",
            "events_json": "[]",
            "error": "",
            "result_summary": "",
            "metadata_json": "{}",
            "model_key": context.model_key,
            "prompt_source": str(context.metadata.get("source") or ""),
            "mode": context.mode,
            "advertised_tools_json": _json(_advertised_tools(context)),
            "request_snapshot_json": "",
            "raw_output": "",
            "cleaned_content": "",
            "truncated": 0,
            "snapshot_original_bytes": 0,
            "history_trace_version": HISTORY_TRACE_VERSION,
            "created_at": time.time() if created_at is None else created_at,
            "updated_at": time.time() if created_at is None else created_at,
        }

    async def record_model_request(
        self,
        context: ToolContext,
        *,
        invocation_id: str,
        request_snapshot: Any,
        advertised_tools: Collection[str] = (),
        metadata: Mapping[str, Any] | None = None,
    ) -> int:
        try:
            turn_id = _turn_id(context)
            invocation_id = str(invocation_id or "").strip()
            if not turn_id or not invocation_id:
                return 0
            snapshot_json, truncated, original_bytes = _truncate_json_snapshot(
                request_snapshot,
                self._snapshot_max_bytes(),
            )
            row = self._base_row(
                event_key=f"model_request:{invocation_id}",
                context=context,
                stage="model_request",
            )
            row.update({
                "invocation_id": invocation_id,
                "outcome": "pending",
                "request_snapshot_json": snapshot_json,
                "truncated": int(truncated),
                "snapshot_original_bytes": original_bytes,
                "advertised_tools_json": _json(
                    sorted({str(item) for item in advertised_tools if str(item)})
                ),
                "metadata_json": _json(dict(metadata or {})),
            })
            return await self._insert_rows([row])
        except Exception:
            self._record_failure("model request write")
            return 0

    async def record_model_output(
        self,
        context: ToolContext,
        *,
        invocation_id: str,
        raw_output: Any,
        outcome: str,
        error: str = "",
        metadata: Mapping[str, Any] | None = None,
    ) -> int:
        try:
            turn_id = _turn_id(context)
            invocation_id = str(invocation_id or "").strip()
            if not turn_id or not invocation_id:
                return 0
            raw_text = (
                raw_output
                if isinstance(raw_output, str)
                else _json(raw_output)
            )
            clipped_output, truncated, original_bytes = _truncate_middle_text(
                raw_text,
                self._snapshot_max_bytes(),
            )
            row = self._base_row(
                event_key=f"model_output:{invocation_id}",
                context=context,
                stage="model_output",
            )
            row.update({
                "invocation_id": invocation_id,
                "outcome": str(outcome or "unknown"),
                "raw_output": clipped_output,
                "truncated": int(truncated),
                "snapshot_original_bytes": original_bytes,
                "error": str(error or "")[:4000],
                "metadata_json": _json(dict(metadata or {})),
            })
            return await self._insert_rows([row])
        except Exception:
            self._record_failure("model output write")
            return 0

    async def record_visible_message(
        self,
        context: ToolContext,
        *,
        invocation_id: str,
        cleaned_content: str,
        message_id: str | None = None,
        metadata: Mapping[str, Any] | None = None,
    ) -> int:
        try:
            turn_id = _turn_id(context)
            invocation_id = str(invocation_id or "").strip()
            if not turn_id or not invocation_id:
                return 0
            visible_id = str(message_id or context.msg_id or "visible").strip()
            row = self._base_row(
                event_key=f"visible_message:{invocation_id}:{visible_id}",
                context=context,
                stage="visible_message",
            )
            row.update({
                "invocation_id": invocation_id,
                "assistant_message_id": message_id or context.msg_id,
                "outcome": "succeeded",
                "cleaned_content": str(cleaned_content or ""),
                "metadata_json": _json(dict(metadata or {})),
            })
            return await self._insert_rows([row])
        except Exception:
            self._record_failure("visible message write")
            return 0

    async def record_marker(
        self,
        context: ToolContext,
        *,
        invocation_id: str,
        marker_name: str,
        raw_text: str,
        normalized: Mapping[str, Any] | None = None,
        marker_index: int = 1,
    ) -> int:
        try:
            turn_id = _turn_id(context)
            invocation_id = str(invocation_id or "").strip()
            if not turn_id or not invocation_id:
                return 0
            normalized_name = _normalize_marker_name(marker_name) or "MARKER"
            row = self._base_row(
                event_key=(
                    f"marker:{invocation_id}:{normalized_name}:{int(marker_index)}"
                ),
                context=context,
                stage="marker",
            )
            row.update({
                "invocation_id": invocation_id,
                "source": str(marker_name or "marker"),
                "raw_text": str(raw_text or ""),
                "outcome": "succeeded",
                "result_summary": _json(dict(normalized or {})),
                "metadata_json": _json({
                    "marker_name": str(marker_name or ""),
                    "normalized_name": normalized_name,
                }),
            })
            return await self._insert_rows([row])
        except Exception:
            self._record_failure("marker write")
            return 0

    async def record_renderer_frame(
        self,
        context: ToolContext,
        *,
        invocation_id: str,
        frame: Mapping[str, Any] | None,
        outcome: str,
        metadata: Mapping[str, Any] | None = None,
    ) -> int:
        try:
            turn_id = _turn_id(context)
            invocation_id = str(invocation_id or "").strip()
            if not turn_id or not invocation_id:
                return 0
            row = self._base_row(
                event_key=f"renderer_frame:{invocation_id}",
                context=context,
                stage="renderer_frame",
                tool_name="device.toy",
            )
            row.update({
                "invocation_id": invocation_id,
                "status": str(outcome or "unknown"),
                "outcome": str(outcome or "unknown"),
                "result_summary": _result_summary(frame),
                "metadata_json": _json(dict(metadata or {})),
            })
            return await self._insert_rows([row])
        except Exception:
            self._record_failure("renderer frame write")
            return 0

    async def record_gateway_result(
        self,
        context: ToolContext,
        *,
        intent: ToolIntent,
        result: Mapping[str, Any],
    ) -> int:
        try:
            turn_id = _turn_id(context)
            if not turn_id:
                return 0
            payload = dict(result or {})
            correlation_id = _find_correlation_id(payload)
            outcome = "succeeded" if payload.get("ok") is True else "rejected"
            row = self._base_row(
                event_key=f"control_gateway:{turn_id}:{intent.id}",
                context=context,
                stage="control_gateway",
                intent_id=intent.id,
                tool_name=intent.tool_name,
            )
            row.update({
                "correlation_id": correlation_id or None,
                "status": str(payload.get("status") or "executed"),
                "outcome": outcome,
                "side_effect_level": intent.side_effect_level.value,
                "source": "control.gateway",
                "arguments_json": _json(dict(intent.arguments)),
                "raw_text": intent.raw_text,
                "result_summary": _result_summary(payload),
                "metadata_json": _json({
                    "intent": dict(intent.metadata),
                    "gateway": True,
                }),
            })
            return await self._insert_rows([row])
        except Exception:
            self._record_failure("control gateway write")
            return 0

    async def _update_terminal_materialized(
        self,
        *,
        correlation_id: str,
        outcome: str,
        event_type: str,
        error: str,
        result: Mapping[str, Any] | None,
    ) -> int:
        async with self._connection() as db:
            cursor = await db.execute(
                "SELECT id, events_json FROM tool_invocation_events "
                "WHERE stage='execution' AND correlation_id=? "
                "ORDER BY created_at DESC LIMIT 1",
                (correlation_id,),
            )
            row = await cursor.fetchone()
            if row is None:
                return 0
            try:
                events = json.loads(row[1] or "[]")
                if not isinstance(events, list):
                    events = []
            except Exception:
                events = []
            events.append({
                "event_type": str(event_type or "terminal"),
                "correlation_id": correlation_id,
                "outcome": outcome,
                "result": dict(result or {}),
                "error": str(error or ""),
                "created_at": time.time(),
            })
            await db.execute(
                "UPDATE tool_invocation_events SET outcome=?, events_json=?, "
                "error=?, result_summary=?, updated_at=? WHERE id=?",
                (
                    outcome,
                    _json(events),
                    str(error or "")[:4000],
                    _result_summary(result),
                    time.time(),
                    row[0],
                ),
            )
            await db.commit()
            return 1

    def _queue_terminal_event(
        self,
        *,
        correlation_id: str,
        outcome: str,
        event_type: str,
        error: str,
        result: Mapping[str, Any] | None,
    ) -> None:
        if (
            correlation_id not in self._pending_terminal_events
            and len(self._pending_terminal_events)
            >= PENDING_TERMINAL_MAX_CORRELATIONS
        ):
            oldest = next(iter(self._pending_terminal_events), None)
            if oldest is not None:
                self._pending_terminal_events.pop(oldest, None)
        pending = self._pending_terminal_events.setdefault(correlation_id, [])
        pending.append({
            "outcome": outcome,
            "event_type": event_type,
            "error": error,
            "result": dict(result or {}),
        })
        del pending[:-PENDING_TERMINAL_MAX_EVENTS_PER_CORRELATION]

    async def _flush_pending_terminal_events(self, correlation_id: str) -> int:
        pending = self._pending_terminal_events.pop(correlation_id, [])
        if not pending:
            return 0
        updated = 0
        for index, event in enumerate(pending):
            try:
                applied = await asyncio.wait_for(
                    self._update_terminal_materialized(
                        correlation_id=correlation_id,
                        outcome=event["outcome"],
                        event_type=event["event_type"],
                        error=event["error"],
                        result=event["result"],
                    ),
                    timeout=self._write_timeout_seconds,
                )
            except Exception:
                self._record_failure("pending terminal outcome update")
                for remaining in pending[index:]:
                    self._queue_terminal_event(
                        correlation_id=correlation_id,
                        **remaining,
                    )
                return updated
            if not applied:
                for remaining in pending[index:]:
                    self._queue_terminal_event(
                        correlation_id=correlation_id,
                        **remaining,
                    )
                return updated
            updated += applied
        return updated

    async def record_terminal_outcome(
        self,
        *,
        correlation_id: str,
        outcome: str,
        event_type: str,
        error: str = "",
        result: Mapping[str, Any] | None = None,
    ) -> int:
        """Append an async terminal event to its original execution row."""

        try:
            correlation_id = str(correlation_id or "").strip()
            if not correlation_id:
                return 0
            if outcome not in {
                "succeeded",
                "failed",
                "rejected",
                "dispatched",
                "pending",
                "unknown",
            }:
                outcome = "unknown"
            updated = await asyncio.wait_for(
                self._update_terminal_materialized(
                    correlation_id=correlation_id,
                    outcome=outcome,
                    event_type=event_type,
                    error=error,
                    result=result,
                ),
                timeout=self._write_timeout_seconds,
            )
            if not updated:
                self._queue_terminal_event(
                    correlation_id=correlation_id,
                    outcome=outcome,
                    event_type=event_type,
                    error=error,
                    result=result,
                )
            return updated
        except Exception:
            self._record_failure("terminal outcome update")
            if correlation_id:
                self._queue_terminal_event(
                    correlation_id=correlation_id,
                    outcome=outcome,
                    event_type=event_type,
                    error=error,
                    result=result,
                )
            return 0

    async def record_diagnostic(
        self, context: ToolContext, *, phase: str, outcome: str,
        metadata: Mapping[str, Any] | None = None,
    ) -> int:
        try:
            if not _turn_id(context):
                return 0
            row = self._base_row(
                event_key=f"diagnostic:{_turn_id(context)}:{uuid.uuid4().hex}",
                context=context, stage="diagnostic",
            )
            row.update({"outcome": outcome, "metadata_json": _json({"phase": phase, **dict(metadata or {})})})
            return await self._insert_rows([row])
        except Exception:
            self._record_failure("diagnostic write")
            return 0

    async def record_turn(
        self,
        context: ToolContext,
        *,
        prompt_source: str,
        advertised_tools: Collection[str],
        turn_outcome: str,
        history_trace_version: int = HISTORY_TRACE_VERSION,
        metadata: Mapping[str, Any] | None = None,
    ) -> int:
        try:
            turn_id = _turn_id(context)
            if not turn_id:
                return 0
            event_key = f"turn:{turn_id}"
            row = self._base_row(
                event_key=event_key,
                context=context,
                stage="turn",
            )
            row.update(
                {
                    "turn_outcome": str(turn_outcome or "unknown"),
                    "prompt_source": str(prompt_source or ""),
                    "advertised_tools_json": _json(
                        sorted({str(item) for item in advertised_tools if str(item)})
                    ),
                    "history_trace_version": int(history_trace_version),
                    "metadata_json": _json(
                        {
                            "context": dict(context.metadata),
                            **dict(metadata or {}),
                        }
                    ),
                }
            )
            return await self._insert_rows([row])
        except Exception:
            self._record_failure("turn write")
            return 0

    async def record_postprocess(
        self,
        context: ToolContext,
        *,
        raw_output: str,
        intents: Iterable[ToolIntent],
        plan_results: Iterable[ToolResult],
        ring_touch_descriptions: Iterable[str] = (),
        enabled_commands: Collection[str] | None = None,
    ) -> int:
        try:
            turn_id = _turn_id(context)
            if not turn_id:
                return 0
            intent_list = list(intents)
            result_by_intent = {
                str(result.intent_id): result
                for result in plan_results
                if result.intent_id
            }
            rows: list[dict[str, Any]] = []
            for intent in intent_list:
                event_key = f"parsed:{turn_id}:{intent.id}"
                row = self._base_row(
                    event_key=event_key,
                    context=context,
                    stage="parsed",
                    intent_id=intent.id,
                    tool_name=intent.tool_name,
                )
                plan_result = result_by_intent.get(intent.id)
                row.update(
                    {
                        "side_effect_level": intent.side_effect_level.value,
                        "source": intent.source,
                        "arguments_json": _json(dict(intent.arguments)),
                        "raw_text": intent.raw_text,
                        "events_json": _json(
                            [event.to_dict() for event in plan_result.events]
                            if plan_result
                            else []
                        ),
                        "metadata_json": _json(dict(intent.metadata)),
                    }
                )
                rows.append(row)

            for index, description in enumerate(ring_touch_descriptions, 1):
                touch = str(description or "").strip()
                if not touch:
                    continue
                intent_id = f"stream_ring_{index:03d}"
                event_key = f"parsed:{turn_id}:{intent_id}"
                definition = get_tool_definition("device.ring_touch")
                parsed_at = time.time()
                row = self._base_row(
                    event_key=event_key,
                    context=context,
                    stage="parsed",
                    intent_id=intent_id,
                    tool_name="device.ring_touch",
                )
                row.update(
                    {
                        "side_effect_level": (
                            definition.side_effect_level.value if definition else "device"
                        ),
                        "source": "ring_touch_description",
                        "arguments_json": _json({"touch": touch}),
                        "raw_text": f"[RING:{touch}]",
                        "events_json": _json(
                            [
                                {
                                    "event_type": "intent_parsed",
                                    "tool_name": "device.ring_touch",
                                    "intent_id": intent_id,
                                    "message": "ring_touch_parsed",
                                    "payload": {"command_group": "ring"},
                                    "created_at": parsed_at,
                                }
                            ]
                        ),
                        "metadata_json": _json({"command_group": "ring"}),
                    }
                )
                rows.append(row)

            candidates = detect_unparsed_marker_candidates(
                raw_output,
                parsed_intents=intent_list,
                enabled_commands=enabled_commands,
            )
            for candidate in candidates:
                span = f"{candidate.start}-{candidate.end}"
                event_key = f"candidate:{turn_id}:{span}"
                definition = get_tool_definition(candidate.tool_name)
                row = self._base_row(
                    event_key=event_key,
                    context=context,
                    stage=candidate.stage,
                    tool_name=candidate.tool_name,
                )
                row.update(
                    {
                        "side_effect_level": (
                            definition.side_effect_level.value if definition else None
                        ),
                        "source": "model_output_candidate",
                        "raw_text": candidate.raw_text,
                        "metadata_json": _json(
                            {
                                "span": [candidate.start, candidate.end],
                                "marker_name": candidate.marker_name,
                                "normalized_name": candidate.normalized_name,
                                "command_group": candidate.command_group,
                                "command_group_enabled": (
                                    enabled_commands is None
                                    or candidate.command_group in set(enabled_commands)
                                ),
                            }
                        ),
                    }
                )
                rows.append(row)
            return await self._insert_rows(rows)
        except Exception:
            self._record_failure("postprocess write")
            return 0

    async def record_execution(
        self,
        context: ToolContext,
        *,
        results: Iterable[ToolResult],
        intents_by_id: Mapping[str, ToolIntent] | None = None,
    ) -> int:
        try:
            turn_id = _turn_id(context)
            if not turn_id:
                return 0
            intent_map = dict(intents_by_id or {})
            rows: list[dict[str, Any]] = []
            correlation_ids: set[str] = set()
            for index, result in enumerate(results, 1):
                intent_id = str(result.intent_id or f"{result.tool_name}:{index}")
                intent = intent_map.get(intent_id)
                event_key = f"execution:{turn_id}:{intent_id}"
                row = self._base_row(
                    event_key=event_key,
                    context=context,
                    stage="execution",
                    intent_id=intent_id,
                    tool_name=result.tool_name,
                )
                correlation_id = _find_correlation_id(result.result)
                if correlation_id:
                    correlation_ids.add(correlation_id)
                row.update(
                    {
                        "status": result.status.value,
                        "outcome": execution_outcome(result),
                        "correlation_id": correlation_id or None,
                        "side_effect_level": (
                            intent.side_effect_level.value if intent else None
                        ),
                        "source": (
                            intent.source
                            if intent
                            else str(context.metadata.get("source") or "execution")
                        ),
                        "arguments_json": _json(
                            dict(intent.arguments) if intent else {}
                        ),
                        "raw_text": intent.raw_text if intent else "",
                        "events_json": _json(
                            [event.to_dict() for event in result.events]
                        ),
                        "error": str(result.error or "")[:1000],
                        "result_summary": _result_summary(result.result),
                        "metadata_json": _json(
                            {
                                "result": dict(result.metadata),
                                "intent": dict(intent.metadata) if intent else {},
                            }
                        ),
                    }
                )
                rows.append(row)
            written = await self._insert_rows(rows)
            for correlation_id in correlation_ids:
                await self._flush_pending_terminal_events(correlation_id)
            return written
        except Exception:
            self._record_failure("execution write")
            return 0


tool_invocation_ledger = ToolInvocationLedger()


__all__ = [
    "HISTORY_TRACE_VERSION",
    "LEDGER_SQLITE_BUSY_TIMEOUT_SECONDS",
    "LEDGER_WRITE_TIMEOUT_SECONDS",
    "MarkerCandidate",
    "ToolInvocationLedger",
    "detect_unparsed_marker_candidates",
    "execution_outcome",
    "tool_invocation_ledger",
]
