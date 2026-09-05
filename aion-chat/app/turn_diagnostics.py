"""单轮工程诊断；不参与提示词内容和聊天决策。"""

from __future__ import annotations

import asyncio
from contextvars import ContextVar
from functools import wraps
import time
import uuid


current_turn: ContextVar[TurnDiagnostics | None] = ContextVar("turn_diagnostics", default=None)


class TurnDiagnostics:
    def __init__(self, conv_id: str, source: str):
        self.turn_id = "turn_" + uuid.uuid4().hex
        self.conv_id = conv_id
        self.source = source
        self.model_key = ""
        self.assistant_message_id = ""
        self.started_at = time.time()
        self._started = time.perf_counter()
        self._phase_starts: dict[str, float] = {}
        self.timings: dict[str, float | None] = {
            key: None for key in (
                "history_ms", "retrieval_ms", "prepare_ms", "model_ms",
                "first_visible_ms", "tools_ms", "total_ms",
            )
        }

    def start(self, phase: str) -> None:
        self._phase_starts[phase] = time.perf_counter()

    def finish(self, phase: str) -> None:
        started = self._phase_starts.pop(phase, None)
        if started is not None:
            self.timings[f"{phase}_ms"] = round((time.perf_counter() - started) * 1000, 3)

    def visible(self) -> None:
        if self.timings["first_visible_ms"] is None:
            self.timings["first_visible_ms"] = round((time.perf_counter() - self._started) * 1000, 3)

    def snapshot(self, usage: dict | None = None, *, finished: bool = False) -> dict:
        if finished:
            self.timings["total_ms"] = round((time.perf_counter() - self._started) * 1000, 3)
        raw = usage or {}
        metrics_reported = raw.get("cache_metrics_reported")
        counters = {key: raw.get(key) for key in ("prompt_tokens", "completion_tokens", "total_tokens")}
        for key in ("cache_read_tokens", "cache_write_tokens", "cache_hit"):
            counters[key] = raw.get(key) if metrics_reported is True else None
        counters["cache_metrics_reported"] = metrics_reported
        return {"turn_id": self.turn_id, "started_at": self.started_at,
                "timings": dict(self.timings), "usage": counters}

    async def record(self, phase: str, *, outcome: str, metadata: dict | None = None) -> None:
        from app.tools.ledger import tool_invocation_ledger
        from app.tools.schemas import ToolContext
        context = ToolContext(
            conv_id=self.conv_id, msg_id=self.assistant_message_id or None,
            request_id=self.turn_id, model_key=self.model_key,
            metadata={"turn_id": self.turn_id, "source": self.source, "source_chain": "main"},
        )
        await tool_invocation_ledger.record_diagnostic(
            context, phase=phase, outcome=outcome, metadata=metadata or {},
        )


def mark_phase(phase: str, *, finished: bool = False) -> None:
    trace = current_turn.get()
    if trace:
        trace.finish(phase) if finished else trace.start(phase)


def trace_prompt(source: str):
    def decorate(function):
        @wraps(function)
        async def wrapped(*args, **kwargs):
            conv_id = str(args[0] if args else kwargs["conv_id"])
            trace = TurnDiagnostics(conv_id, source)
            token = current_turn.set(trace)
            trace.start("prepare")
            try:
                model_key, history, meta = await function(*args, **kwargs)
                trace.model_key = model_key
                trace.finish("prepare")
                meta["turn_id"] = trace.turn_id
                meta["_turn_trace"] = trace
                return model_key, history, meta
            except BaseException as exc:
                trace.finish("prepare")
                await trace.record(
                    "prepare", outcome="cancelled" if isinstance(exc, asyncio.CancelledError) else "failed",
                    metadata={**trace.snapshot(finished=True), "error_type": type(exc).__name__},
                )
                raise
            finally:
                current_turn.reset(token)
        return wrapped
    return decorate


def measure_phase(phase: str):
    def decorate(function):
        @wraps(function)
        async def wrapped(*args, **kwargs):
            trace = current_turn.get()
            started = time.perf_counter()
            try:
                return await function(*args, **kwargs)
            finally:
                if trace:
                    key = f"{phase}_ms"
                    trace.timings[key] = round(
                        (trace.timings.get(key) or 0) + (time.perf_counter() - started) * 1000, 3,
                    )
        return wrapped
    return decorate


class DiagnosticQueue(asyncio.Queue):
    def __init__(self, trace: TurnDiagnostics):
        super().__init__()
        self.trace = trace

    async def put(self, item):
        if item.get("type") == "chunk" and str(item.get("content") or "").strip():
            self.trace.visible()
        await super().put(item)
