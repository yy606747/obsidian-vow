from __future__ import annotations

import asyncio
import contextlib
import json
import time
from dataclasses import dataclass

from app.background_tasks import create_tracked_task
from app.control.schemas import ControlSession
from app.tools.ledger import tool_invocation_ledger
from app.tools.schemas import ToolContext
from database import get_db
from ws import manager

from .intent import tide_intent_service


RUNNER_STALE_AFTER_SECONDS = 45.0
KEEPALIVE_SECONDS = 1.0
RENDER_INTERVAL_SECONDS = 6.0
TICK_SECONDS = KEEPALIVE_SECONDS
TIDE_TTL_MS = 3000


@dataclass
class _RenderMemory:
    last_intent: str = ""
    quiet_ticks: int = 0
    vib_pattern: int = 0
    thrust_pattern: int = 0
    locked_frame: dict | None = None
    last_rendered_version: int = 0
    ledger_observed: bool = False
    last_ledger_intent_version: int | None = None
    last_ledger_intent: str = ""
    last_ledger_frame: dict | None = None


@dataclass
class _RendererModelObservation:
    context: ToolContext
    invocation_id: str
    request_snapshot: list[dict]
    provider_path: str
    raw_output: object = ""
    outcome: str = "unknown"
    error: str = ""


class TideRendererRegistry:
    def __init__(self):
        self._lock = asyncio.Lock()
        self._task: asyncio.Task | None = None
        self._session: ControlSession | None = None
        self._intent_events: dict[str, asyncio.Event] = {}
        self._last_model_observation: dict[
            str,
            list[_RendererModelObservation],
        ] = {}

    async def activate(self, session: ControlSession) -> None:
        async with self._lock:
            if self._session and self._session.session_id == session.session_id and self._task and not self._task.done():
                self._session = session
                return
            await self._cancel_locked(emit_stop=True, reason="switch")
            self._session = session
            await tide_intent_service.bind_active_session(session)
            self._intent_events[session.session_id] = asyncio.Event()
            self._task = create_tracked_task(self._run(session), name=f"tide_renderer:{session.session_id}")

    async def ensure_running(self, session: ControlSession) -> None:
        async with self._lock:
            if self._session and self._session.session_id == session.session_id and self._task and not self._task.done():
                self._session = session
                return
            await self._cancel_locked(emit_stop=True, reason="ensure_running")
            self._session = session
            self._intent_events[session.session_id] = asyncio.Event()
            self._task = create_tracked_task(self._run(session), name=f"tide_renderer:{session.session_id}")

    async def rebind_session(self, session: ControlSession) -> None:
        async with self._lock:
            if self._session and self._session.session_id == session.session_id:
                self._session = session
                self._intent_events.setdefault(session.session_id, asyncio.Event()).set()
                return
            if self._task and not self._task.done():
                return
            self._session = session
            self._intent_events[session.session_id] = asyncio.Event()
            self._task = create_tracked_task(self._run(session), name=f"tide_renderer:{session.session_id}")

    async def stop_session(self, session: ControlSession, *, emit_stop: bool, reason: str) -> None:
        async with self._lock:
            if self._session and self._session.session_id == session.session_id:
                if self._task is asyncio.current_task():
                    self._session = None
                    self._task = None
                    self._intent_events.pop(session.session_id, None)
                    if emit_stop:
                        await self._broadcast_stop(session, reason=reason)
                    return
                await self._cancel_locked(emit_stop=emit_stop, reason=reason)
                return
        if emit_stop:
            await self._broadcast_stop(session, reason=reason)

    def notify_intent(self, session_id: str) -> None:
        event = self._intent_events.get(str(session_id or ""))
        if event:
            event.set()

    async def _cancel_locked(self, *, emit_stop: bool, reason: str) -> None:
        session = self._session
        task = self._task
        self._session = None
        self._task = None
        if session:
            self._intent_events.pop(session.session_id, None)
        if task and not task.done():
            task.cancel()
        if task and not task.done():
            try:
                await task
            except asyncio.CancelledError:
                pass
        if emit_stop and session:
            await self._broadcast_stop(session, reason=reason)

    async def _run(self, session: ControlSession) -> None:
        memory = _RenderMemory()
        resource_id = session.control_resource_id or "toy:muse"
        wake = self._intent_events.setdefault(session.session_id, asyncio.Event())
        keepalive = asyncio.create_task(
            self._keepalive_loop(session.session_id, resource_id, memory),
            name=f"tide_keepalive:{session.session_id}",
        )
        render = asyncio.create_task(
            self._render_loop(session.session_id, resource_id, memory, wake),
            name=f"tide_render:{session.session_id}",
        )
        tasks = {keepalive, render}
        try:
            done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for item in pending:
                item.cancel()
            for item in pending:
                with contextlib.suppress(asyncio.CancelledError):
                    await item
            for item in done:
                item.result()
        except asyncio.CancelledError:
            for item in tasks:
                item.cancel()
            for item in tasks:
                with contextlib.suppress(asyncio.CancelledError):
                    await item
            raise
        await self._broadcast_stop(await self._latest_session(session.session_id) or session, reason="runner_exit")

    async def _keepalive_loop(self, session_id: str, resource_id: str, memory: _RenderMemory) -> None:
        while True:
            session = await self._active_tide_session(session_id)
            if not session:
                return
            if memory.locked_frame:
                await self._broadcast_frame(session, memory.locked_frame, reason="keepalive")
            await asyncio.sleep(KEEPALIVE_SECONDS)

    async def _render_loop(self, session_id: str, resource_id: str, memory: _RenderMemory, wake: asyncio.Event) -> None:
        last_render_at = 0.0
        while True:
            session = await self._active_tide_session(session_id)
            if not session:
                return
            state = await tide_intent_service.latest_state(control_resource_id=resource_id)
            if not state or state.get("control_session_id") != session_id:
                await self._wait_for_wake(wake, RENDER_INTERVAL_SECONDS)
                continue

            intent = str(state.get("intent_text") or "").strip()
            if not intent:
                await self._wait_for_wake(wake, None)
                continue

            intent_version = self._int_value(state.get("intent_version"), 0)
            now = time.time()
            if intent_version == memory.last_rendered_version and now - last_render_at < RENDER_INTERVAL_SECONDS:
                await self._wait_for_wake(wake, max(0.1, RENDER_INTERVAL_SECONDS - (now - last_render_at)))
                continue

            version_at_start = intent_version
            frame = await self._render_frame(
                session,
                intent,
                memory,
                intent_version=version_at_start,
            )
            latest_state = await tide_intent_service.latest_state(control_resource_id=resource_id)
            latest_version = self._int_value((latest_state or {}).get("intent_version"), 0)
            if latest_version != version_at_start:
                continue
            if frame is None:
                await self._wait_for_wake(wake, RENDER_INTERVAL_SECONDS)
                continue

            memory.locked_frame = frame
            memory.last_rendered_version = version_at_start
            last_render_at = time.time()
            await self._broadcast_frame(session, frame, reason="render")
            await tide_intent_service.store_frame(control_resource_id=resource_id, frame_json=json.dumps(frame, ensure_ascii=False))

    async def _wait_for_wake(self, wake: asyncio.Event, timeout: float | None) -> None:
        try:
            if timeout is None:
                await wake.wait()
            else:
                await asyncio.wait_for(wake.wait(), timeout=timeout)
        except asyncio.TimeoutError:
            return
        finally:
            wake.clear()

    async def _latest_session(self, session_id: str) -> ControlSession | None:
        session = self._session
        if session and session.session_id == session_id:
            return session
        from app.control import control_session_service
        return await control_session_service.get_session(session_id)

    async def _active_tide_session(self, session_id: str) -> ControlSession | None:
        from app.control import control_session_service

        current = await control_session_service.get_session(session_id)
        if (
            not current
            or current.kind != "tide"
            or current.status != "active"
            or time.time() - current.last_heartbeat_at > RUNNER_STALE_AFTER_SECONDS
        ):
            return None
        if self._session and self._session.session_id == session_id:
            self._session = current
        return current

    async def _session_still_active(self, session: ControlSession) -> bool:
        return await self._active_tide_session(session.session_id) is not None

    async def _render_frame(
        self,
        session: ControlSession,
        intent: str,
        memory: _RenderMemory,
        *,
        intent_version: int | None = None,
    ) -> dict | None:
        if intent != memory.last_intent:
            memory.last_intent = intent
            memory.quiet_ticks = 0
        else:
            memory.quiet_ticks += 1

        # A frame with no provider call must not inherit the previous frame's
        # invocation id; it gets its own frame-only observation below.
        self._last_model_observation.pop(session.session_id, None)
        raw = await self._call_renderer_model(session, intent, memory)
        frame = self._parse_frame(raw)
        used_fallback = frame is None
        if frame is None:
            frame = self._fallback_frame(memory)
        frame = self._nonzero_frame_or_fallback(frame, memory)
        observations = self._last_model_observation.get(session.session_id, [])
        if not isinstance(observations, list):
            observations = []
        final_observation = observations[-1] if observations else None
        if final_observation is None:
            frame_invocation_id = tool_invocation_ledger.new_invocation_id(
                "tide_frame"
            )
            frame_context = ToolContext(
                conv_id=session.conv_id,
                request_id=f"tide_frame:{session.session_id}:{frame_invocation_id}",
                capabilities=("device.toy",),
                metadata={
                    "source": "tide_renderer",
                    "source_chain": "tide",
                    "invocation_id": frame_invocation_id,
                    "advertised_tools": (),
                    "control_session_id": session.session_id,
                    "control_resource_id": session.control_resource_id,
                },
            )
        else:
            frame_context = final_observation.context
            frame_invocation_id = final_observation.invocation_id

        if intent_version is None:
            intent_changed_for_snapshot = (
                not memory.ledger_observed
                or intent != memory.last_ledger_intent
            )
        else:
            intent_changed_for_snapshot = (
                not memory.ledger_observed
                or intent_version != memory.last_ledger_intent_version
            )
        frame_changed_for_snapshot = (
            not memory.ledger_observed
            or frame != memory.last_ledger_frame
        )
        snapshot_reasons = []
        if intent_changed_for_snapshot:
            snapshot_reasons.append("intent_version_changed")
        if frame_changed_for_snapshot:
            snapshot_reasons.append("frame_changed")
        store_full_snapshot = bool(observations) and bool(snapshot_reasons)
        if store_full_snapshot:
            await self._record_full_model_observations(
                observations,
                intent_version=intent_version,
                snapshot_reasons=snapshot_reasons,
            )

        await tool_invocation_ledger.record_renderer_frame(
            frame_context,
            invocation_id=frame_invocation_id,
            frame=frame,
            outcome="succeeded" if frame is not None else "failed",
            metadata={
                "used_fallback": used_fallback,
                "model_called": bool(observations),
                "intent": intent,
                "intent_version": intent_version,
                "quiet_ticks": memory.quiet_ticks,
                "snapshot_stored": store_full_snapshot,
                "snapshot_reasons": snapshot_reasons,
                "provider_path": (
                    final_observation.provider_path
                    if final_observation is not None
                    else ""
                ),
                "model_outcome": (
                    final_observation.outcome
                    if final_observation is not None
                    else "not_called"
                ),
                "model_error": (
                    final_observation.error
                    if final_observation is not None
                    else ""
                ),
                "attempted_invocation_ids": [
                    item.invocation_id for item in observations
                ],
                "model_attempts": [
                    {
                        "invocation_id": item.invocation_id,
                        "provider_path": item.provider_path,
                        "outcome": item.outcome,
                        "error": item.error[:1000],
                    }
                    for item in observations
                ],
            },
        )
        memory.ledger_observed = True
        memory.last_ledger_intent_version = intent_version
        memory.last_ledger_intent = intent
        memory.last_ledger_frame = dict(frame) if frame is not None else None
        if frame is None:
            return None
        memory.vib_pattern = frame["vib_pattern"]
        memory.thrust_pattern = frame["thrust_pattern"]
        return frame

    async def _record_full_model_observations(
        self,
        observations: list[_RendererModelObservation],
        *,
        intent_version: int | None,
        snapshot_reasons: list[str],
    ) -> None:
        for observation in observations:
            metadata = {
                "provider_path": observation.provider_path,
                "intent_version": intent_version,
                "snapshot_reasons": list(snapshot_reasons),
            }
            await tool_invocation_ledger.record_model_request(
                observation.context,
                invocation_id=observation.invocation_id,
                request_snapshot=observation.request_snapshot,
                advertised_tools=(),
                metadata=metadata,
            )
            await tool_invocation_ledger.record_model_output(
                observation.context,
                invocation_id=observation.invocation_id,
                raw_output=observation.raw_output,
                outcome=observation.outcome,
                error=observation.error,
                metadata=metadata,
            )
            await tool_invocation_ledger.record_turn(
                observation.context,
                prompt_source="tide_renderer",
                advertised_tools=(),
                turn_outcome=(
                    "failed"
                    if observation.error
                    else (
                        "succeeded"
                        if str(observation.raw_output or "").strip()
                        else "invalid_output"
                    )
                ),
                metadata=metadata,
            )

    async def _call_renderer_model(self, session: ControlSession, intent: str, memory: _RenderMemory) -> str:
        self._last_model_observation.pop(session.session_id, None)
        latest_user = await self._latest_user_text(session.conv_id)
        prompt = [
            {
                "role": "system",
                "content": (
                    "你是 Muse 潮汐触碰渲染器。你只把主脑的自然语言意图翻译成下一拍玩具输出，"
                    "不要写解释，只返回 JSON 对象。字段固定为 vib_pattern、thrust_pattern、ttl_ms。"
                    "vib_pattern/thrust_pattern 取 0..9，0 表示该通道局部停止；ttl_ms 固定不超过 3000。"
                    "pattern 语义：1/2/3 是持续强度递增；4/5/6 是低强度带较长停顿；7/8/9 是更高强度带较短停顿。"
                    "正常变化要克制，可以让单个通道短暂停顿制造呼吸感，但不要输出双通道全 0；"
                    "主脑没有新意图时默认维持当前触碰，不要因为沉默自动收尾。"
                    "振动是第一路，伸缩是第二路。"
                ),
            },
            {
                "role": "user",
                "content": json.dumps(
                    {
                        "intent": intent,
                        "latest_user_message": latest_user,
                        "quiet_ticks_without_new_intent": memory.quiet_ticks,
                        "current_frame": {
                            "vib_pattern": memory.vib_pattern,
                            "thrust_pattern": memory.thrust_pattern,
                            "ttl_ms": TIDE_TTL_MS,
                        },
                    },
                    ensure_ascii=False,
                ),
            },
        ]
        try:
            from ai_providers import call_slot_chat
            from config import get_slot

            if get_slot("tide_renderer"):
                invocation_id = tool_invocation_ledger.new_invocation_id(
                    "tide_renderer_slot"
                )
                context = self._renderer_context(
                    session,
                    invocation_id=invocation_id,
                    model_key="slot:tide_renderer",
                )
                observation = _RendererModelObservation(
                    context=context,
                    invocation_id=invocation_id,
                    request_snapshot=prompt,
                    provider_path="slot",
                )
                self._last_model_observation.setdefault(
                    session.session_id,
                    [],
                ).append(observation)
                try:
                    observation.raw_output = await call_slot_chat(
                        "tide_renderer",
                        prompt,
                        expect_json=True,
                        timeout=12.0,
                        temperature=0.35,
                        scope="tide_renderer",
                    )
                    observation.outcome = (
                        "succeeded"
                        if str(observation.raw_output or "").strip()
                        else "unknown"
                    )
                    return observation.raw_output
                except Exception as exc:
                    observation.error = str(exc)
                    observation.outcome = "failed"
                    raise
        except Exception as exc:
            print(f"[TideRenderer] slot renderer failed: {type(exc).__name__}: {exc}")

        model_key = await self._conversation_model_key(session.conv_id)
        if not model_key:
            return ""
        try:
            from ai_providers import stream_ai

            invocation_id = tool_invocation_ledger.new_invocation_id(
                "tide_renderer_core"
            )
            context = self._renderer_context(
                session,
                invocation_id=invocation_id,
                model_key=model_key,
            )
            observation = _RendererModelObservation(
                context=context,
                invocation_id=invocation_id,
                request_snapshot=prompt,
                provider_path="conversation",
            )
            self._last_model_observation.setdefault(
                session.session_id,
                [],
            ).append(observation)
            chunks: list[str] = []
            try:
                async for chunk in stream_ai(prompt, model_key, {}, temperature=0.35):
                    if isinstance(chunk, dict):
                        continue
                    chunks.append(str(chunk))
                    if sum(len(item) for item in chunks) > 1600:
                        break
                observation.raw_output = "".join(chunks)
                observation.outcome = (
                    "succeeded" if observation.raw_output.strip() else "unknown"
                )
                return observation.raw_output
            except Exception as exc:
                observation.raw_output = "".join(chunks)
                observation.error = str(exc)
                observation.outcome = "failed"
                raise
        except Exception as exc:
            print(f"[TideRenderer] conversation renderer failed: {type(exc).__name__}: {exc}")
            return ""

    def _renderer_context(
        self,
        session: ControlSession,
        *,
        invocation_id: str,
        model_key: str,
    ) -> ToolContext:
        return ToolContext(
            conv_id=session.conv_id,
            request_id=f"tide:{session.session_id}:{invocation_id}",
            model_key=model_key,
            capabilities=("device.toy",),
            metadata={
                "source": "tide_renderer",
                "source_chain": "tide",
                "invocation_id": invocation_id,
                "advertised_tools": (),
                "control_session_id": session.session_id,
                "control_resource_id": session.control_resource_id,
            },
        )

    async def _conversation_model_key(self, conv_id: str) -> str:
        async with get_db() as db:
            cur = await db.execute("SELECT model FROM conversations WHERE id=?", (conv_id,))
            row = await cur.fetchone()
            return str(row[0] or "") if row else ""

    async def _latest_user_text(self, conv_id: str) -> str:
        async with get_db() as db:
            cur = await db.execute(
                "SELECT content FROM messages WHERE conv_id=? AND role='user' ORDER BY created_at DESC LIMIT 1",
                (conv_id,),
            )
            row = await cur.fetchone()
            return str(row[0] or "")[:500] if row else ""

    def _parse_frame(self, raw: str) -> dict | None:
        text = str(raw or "").strip()
        if not text:
            return None
        if text.startswith("```"):
            text = text.strip("`")
            if text.lower().startswith("json"):
                text = text[4:].strip()
        start = text.find("{")
        end = text.rfind("}")
        if start < 0 or end <= start:
            return None
        try:
            data = json.loads(text[start:end + 1])
        except Exception:
            return None
        if not isinstance(data, dict):
            return None
        vib = self._clamp_int(data.get("vib_pattern"), 0, 9)
        thrust = self._clamp_int(data.get("thrust_pattern"), 0, 9)
        ttl = self._clamp_int(data.get("ttl_ms"), 500, TIDE_TTL_MS)
        return {"vib_pattern": vib, "thrust_pattern": thrust, "ttl_ms": ttl}

    def _fallback_frame(self, memory: _RenderMemory) -> dict | None:
        if memory.locked_frame:
            return dict(memory.locked_frame)
        if memory.vib_pattern > 0 or memory.thrust_pattern > 0:
            return {
                "vib_pattern": memory.vib_pattern,
                "thrust_pattern": memory.thrust_pattern,
                "ttl_ms": TIDE_TTL_MS,
            }
        return None

    def _nonzero_frame_or_fallback(self, frame: dict | None, memory: _RenderMemory) -> dict | None:
        if frame is None:
            return None
        if frame["vib_pattern"] != 0 or frame["thrust_pattern"] != 0:
            return frame
        if memory.locked_frame:
            return dict(memory.locked_frame)
        if memory.vib_pattern > 0 or memory.thrust_pattern > 0:
            return {
                "vib_pattern": memory.vib_pattern,
                "thrust_pattern": memory.thrust_pattern,
                "ttl_ms": TIDE_TTL_MS,
            }
        return None

    def _clamp_int(self, value, low: int, high: int) -> int:
        try:
            number = int(value)
        except (TypeError, ValueError):
            number = low
        return max(low, min(high, number))

    def _int_value(self, value, fallback: int) -> int:
        try:
            return int(value)
        except (TypeError, ValueError):
            return fallback

    async def _broadcast_frame(self, session: ControlSession, frame: dict, *, reason: str) -> None:
        payload = {
            "type": "tide_toy_frame",
            "control_session_id": session.session_id,
            "owner_client_id": session.owner_client_id,
            "control_resource_id": session.control_resource_id or "toy:muse",
            "conv_id": session.conv_id,
            "device_id": session.device_id or "muse",
            "reason": reason,
            "created_at": time.time(),
            **frame,
        }
        await manager.broadcast({"type": "tide_toy_frame", "data": payload})

    async def _broadcast_stop(self, session: ControlSession, *, reason: str) -> None:
        await self._broadcast_frame(
            session,
            {"vib_pattern": 0, "thrust_pattern": 0, "ttl_ms": TIDE_TTL_MS, "global_stop": True},
            reason=reason,
        )
        await manager.broadcast({
            "type": "control_stop_request",
            "data": {
                "kind": "tide",
                "control_session_id": session.session_id,
                "owner_client_id": session.owner_client_id,
                "control_resource_id": session.control_resource_id or "toy:muse",
                "conv_id": session.conv_id,
                "reason": reason,
            },
        })


tide_renderer_registry = TideRendererRegistry()
