"""Runtime adapters for Sentinel Core wake execution ports.

The pure ``app.sentinel`` package owns planning and orchestration contracts.
This module is the production runtime edge used by the full Wake Orchestrator:
database writes, websocket broadcasts, prompt context, Core provider streaming,
monitor logs, clock and settings.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterable, Callable, Mapping, Sequence
from typing import Any

from app.sentinel.core_wake_ports import stored_message_to_dict
from app.memory_v3.timeline import timeline_service


async def load_sentinel_working_model_prompt_context() -> tuple[str, str]:
    """Load the two P1 relationship blocks behind their existing shared gate.

    The Working Model and desire heads are one durable snapshot for prompt
    assembly, so they are read together exactly once.  A disabled gate means
    neither block is exposed to Sentinel Core wake.
    """
    from app.chat.prompt_builder import build_desire_block, build_v2_working_model_block
    from app.working_model import service as working_model_service
    from app.working_model.runtime import working_model_v2_injection_enabled

    if not working_model_v2_injection_enabled():
        return "", ""

    working_model_head, desire_head = await working_model_service.load_v2_prompt_heads()
    return (
        build_v2_working_model_block(working_model_head),
        build_desire_block(desire_head),
    )


async def load_sentinel_timeline_prompt_context(
    *,
    visible_message_ids: Sequence[str] = (),
    now: float | None = None,
) -> dict[str, Any]:
    """Load the existing gated three-day Timeline block for a Core wake."""
    if not isinstance(visible_message_ids, Sequence) or isinstance(
        visible_message_ids,
        str | bytes,
    ):
        raise ValueError("sentinel timeline visible_message_ids must be a list")
    visible_messages = []
    for index, message_id in enumerate(visible_message_ids):
        if not isinstance(message_id, str) or not message_id.strip():
            raise ValueError(
                f"sentinel timeline visible_message_ids[{index}] must be non-empty text"
            )
        visible_messages.append({"id": message_id.strip()})
    result = await timeline_service.prompt_context(
        visible_messages=visible_messages,
        now=now,
    )
    if not isinstance(result, Mapping):
        raise ValueError("sentinel timeline prompt context must be an object")
    return dict(result)


async def record_sentinel_timeline_injection_usage(
    timeline_meta: Mapping[str, Any] | None,
    *,
    conv_id: str,
    assistant_message_id: str,
    response_text: str,
) -> dict[str, Any]:
    """Record Timeline use through the same service as ordinary chat."""
    result = await timeline_service.record_injection_usage(
        dict(timeline_meta) if isinstance(timeline_meta, Mapping) else None,
        conv_id=conv_id,
        assistant_message_id=assistant_message_id,
        response_text=response_text,
    )
    if not isinstance(result, Mapping):
        raise ValueError("sentinel timeline usage result must be an object")
    return dict(result)


class LegacyCoreWakePorts:
    """Adapter from Wake Orchestrator ports to the current legacy runtime APIs."""

    def __init__(
        self,
        *,
        db_factory: Callable[[], Any],
        broadcaster: Callable[[Mapping[str, Any]], Any],
        core_streamer: Callable[..., AsyncIterable[str]],
        monitor_log_writer: Callable[[Mapping[str, Any]], Any],
        clock: Callable[[], float] | None = None,
        default_temperature: Any = None,
        sleeper: Callable[[float], Any] | None = None,
        control_session_service_obj: Any | None = None,
        toy_tool_service: Any | None = None,
        toy_gateway_adapter: Callable | None = None,
        observation_conv_id: str = "",
        observation_request_id: str = "",
        linked_invocation_id: str = "",
    ) -> None:
        self._db_factory = _required_callable(db_factory, "db_factory")
        self._broadcaster = _required_callable(broadcaster, "broadcaster")
        self._core_streamer = _required_callable(core_streamer, "core_streamer")
        self._monitor_log_writer = _required_callable(monitor_log_writer, "monitor_log_writer")
        self._clock = _required_callable(clock or time.time, "clock")
        self._default_temperature = default_temperature
        self._sleeper = _required_callable(sleeper or asyncio.sleep, "sleeper")
        self._control_session_service = control_session_service_obj
        self._toy_tool_service = toy_tool_service
        self._toy_gateway_adapter = toy_gateway_adapter
        self._observation_conv_id = str(observation_conv_id or "").strip()
        self._observation_request_id = str(observation_request_id or "").strip()
        self._linked_invocation_id = str(linked_invocation_id or "").strip()
        self._last_core_observation: tuple[Any, str] | None = None

    def _action_invocation_id(self, conv_id: str) -> str:
        if self._last_core_observation is not None:
            parent_context, invocation_id = self._last_core_observation
            if str(parent_context.conv_id or "") == str(conv_id or ""):
                return str(invocation_id or "").strip()
        return self._linked_invocation_id

    def _action_observation(
        self,
        *,
        conv_id: str,
        msg_id: str,
        request_id: str,
        model_key: str,
        capability: str,
    ) -> tuple[Any, Any, str] | None:
        invocation_id = self._action_invocation_id(conv_id)
        if not invocation_id:
            return None
        from app.tools.ledger import tool_invocation_ledger
        from app.tools.schemas import ToolContext

        parent_capabilities: tuple[str, ...] = ()
        if self._last_core_observation is not None:
            parent_context, _parent_invocation_id = self._last_core_observation
            if str(parent_context.conv_id or "") == str(conv_id or ""):
                parent_capabilities = tuple(parent_context.capabilities)
        context = ToolContext(
            conv_id=conv_id,
            msg_id=msg_id,
            request_id=request_id,
            model_key=model_key,
            capabilities=parent_capabilities or (capability,),
            metadata={
                "source": "sentinel",
                "source_chain": "sentinel",
                "invocation_id": invocation_id,
                "advertised_tools": parent_capabilities or (capability,),
            },
        )
        return tool_invocation_ledger, context, invocation_id

    def now(self) -> float:
        value = self._clock()
        if isinstance(value, bool) or not isinstance(value, int | float):
            raise ValueError("core wake clock must return a numeric timestamp")
        return float(value)

    async def load_vow_prompt_context(self) -> tuple[str, str]:
        """誓约常驻注入（誓约设计 §5.1）：生产 ports 才提供；读取失败抛
        VowReadError，由 orchestrator 按"系统主动 → 跳过本次生成"处置。"""
        from app.vows.service import vow_service

        return await vow_service.load_vow_prompt_context()

    async def load_working_model_prompt_context(self) -> tuple[str, str]:
        """Return the gated Working Model and desire prompt blocks together."""
        return await load_sentinel_working_model_prompt_context()

    async def load_timeline_prompt_context(
        self,
        *,
        visible_message_ids: Sequence[str],
        now: float | None = None,
    ) -> dict[str, Any]:
        """Return the gated Timeline block, deduped against visible history."""
        return await load_sentinel_timeline_prompt_context(
            visible_message_ids=visible_message_ids,
            now=now,
        )

    async def record_timeline_injection_usage(
        self,
        timeline_meta: Mapping[str, Any] | None,
        *,
        conv_id: str,
        assistant_message_id: str,
        response_text: str,
    ) -> dict[str, Any]:
        return await record_sentinel_timeline_injection_usage(
            timeline_meta,
            conv_id=conv_id,
            assistant_message_id=assistant_message_id,
            response_text=response_text,
        )

    async def prepare_web_search_turn(
        self,
        *,
        conv_id: str,
        bound_turn_id: str,
    ) -> Mapping[str, Any]:
        from app.chat.prompt_builder import join_prompt_blocks
        from app.web_search import web_search_service
        from app.web_search.intent import web_search_ability_block

        prepared = await web_search_service.prepare_dialogue_turn(
            conv_id=conv_id,
            bound_turn_id=bound_turn_id,
        )
        if prepared.get("status") == "disabled":
            return prepared
        return {
            **prepared,
            "block": join_prompt_blocks(
                web_search_ability_block(),
                str(prepared.get("block") or ""),
            ),
        }

    async def finalize_web_search_turn(
        self,
        *,
        conv_id: str,
        bound_turn_id: str,
        assistant_message_id: str,
        intent_text: str,
    ) -> Mapping[str, Any]:
        from app.web_search import web_search_service

        return await web_search_service.finalize_independent(
            conv_id=conv_id,
            bound_turn_id=bound_turn_id,
            assistant_message_id=assistant_message_id,
            intent_text=intent_text,
            origin_source="sentinel_core",
            allow_new_intent=True,
            now=self.now(),
        )

    async def broadcast_monitor_alert(self, content: str) -> None:
        content = _required_text(content, "monitor alert content")
        await self._broadcaster({"type": "monitor_alert", "data": {"content": content}})

    async def insert_system_wake_notice(
        self,
        *,
        conv_id: str,
        content: str,
        created_at: float,
    ) -> Mapping[str, Any]:
        return await self._insert_message(
            conv_id=conv_id,
            role="system",
            content=content,
            created_at=created_at,
            suffix="_sentinel_sys",
        )

    async def stream_core(
        self,
        *,
        messages: Sequence[Mapping[str, str]],
        model_key: str,
        temperature: Any = None,
    ) -> str:
        model_key = _required_text(model_key, "core model_key")
        normalized_messages = _validated_core_messages(messages)
        selected_temperature = self._default_temperature if temperature is None else temperature
        observation = None
        if self._observation_conv_id:
            from app.tools.ledger import tool_invocation_ledger
            from app.tools.schemas import ToolContext

            invocation_id = tool_invocation_ledger.new_invocation_id(
                "sentinel_v2_core"
            )
            context = ToolContext(
                conv_id=self._observation_conv_id,
                request_id=(
                    self._observation_request_id
                    or f"sentinel_core:{invocation_id}"
                ),
                model_key=model_key,
                capabilities=_advertised_tools_from_messages(normalized_messages),
                metadata={
                    "source": "sentinel_core",
                    "source_chain": "sentinel",
                    "invocation_id": invocation_id,
                    "advertised_tools": _advertised_tools_from_messages(
                        normalized_messages
                    ),
                },
            )
            observation = (tool_invocation_ledger, context, invocation_id)
            await tool_invocation_ledger.record_model_request(
                context,
                invocation_id=invocation_id,
                request_snapshot=normalized_messages,
                advertised_tools=context.capabilities,
                metadata={"temperature": selected_temperature},
            )
        full_content: list[str] = []
        error = ""
        try:
            stream = self._core_streamer(
                normalized_messages,
                model_key,
                temperature=selected_temperature,
            )
            if not hasattr(stream, "__aiter__"):
                raise ValueError("core wake streamer must return an async iterable")
            async for chunk in stream:
                if not isinstance(chunk, str):
                    raise ValueError("core wake streamer yielded a non-text chunk")
                full_content.append(chunk)
            return "".join(full_content)
        except Exception as exc:
            error = str(exc)
            raise
        finally:
            if observation is not None:
                ledger, context, invocation_id = observation
                raw_output = "".join(full_content)
                await ledger.record_model_output(
                    context,
                    invocation_id=invocation_id,
                    raw_output=raw_output,
                    outcome="failed" if error else (
                        "succeeded" if raw_output.strip() else "unknown"
                    ),
                    error=error,
                )
                await ledger.record_turn(
                    context,
                    prompt_source="sentinel_core",
                    advertised_tools=context.capabilities,
                    turn_outcome="failed" if error else (
                        "succeeded" if raw_output.strip() else "invalid_output"
                    ),
                )
                self._last_core_observation = (context, invocation_id)

    async def insert_assistant_message(
        self,
        *,
        conv_id: str,
        content: str,
        created_at: float,
    ) -> Mapping[str, Any]:
        message = await self._insert_message(
            conv_id=conv_id,
            role="assistant",
            content=content,
            created_at=created_at,
            suffix="_sentinel",
        )
        if self._last_core_observation is not None:
            from app.tools.ledger import tool_invocation_ledger

            context, invocation_id = self._last_core_observation
            await tool_invocation_ledger.record_visible_message(
                context,
                invocation_id=invocation_id,
                cleaned_content=content,
                message_id=str(message.get("id") or ""),
            )
        timeline_service.start_background_refresh()
        return message

    async def update_conversation(self, *, conv_id: str, updated_at: float) -> None:
        conv_id = _required_text(conv_id, "conversation id")
        updated_at = _required_timestamp(updated_at, "conversation updated_at")
        async with self._db_factory() as db:
            await db.execute("UPDATE conversations SET updated_at=? WHERE id=?", (updated_at, conv_id))
            await db.commit()

    async def broadcast_msg_created(self, message: Mapping[str, Any]) -> None:
        payload = stored_message_to_dict(message, label="broadcast")
        await self._broadcaster({"type": "msg_created", "data": payload})

    async def broadcast_toy_command(
        self,
        *,
        commands: Sequence[str],
        msg_id: str,
        conv_id: str | None = None,
        toy_capability_allowed: bool = False,
        control_session_id: str | None = None,
        control_epoch: int | None = None,
        owner_client_id: str | None = None,
        control_device_id: str | None = None,
        request_id: str | None = None,
        wake_id: str | None = None,
    ) -> Mapping[str, Any]:
        commands = _validated_toy_commands(commands)
        msg_id = _required_text(msg_id, "toy command message id")
        conv_id = _required_text(conv_id, "toy command conversation id")
        toy_capability_allowed = _required_bool(toy_capability_allowed, "toy capability allowed")
        audit = _toy_audit_metadata(msg_id=msg_id, request_id=request_id, wake_id=wake_id)
        expected_session_id = str(control_session_id or "").strip()
        expected_owner = str(owner_client_id or "").strip()
        expected_device = str(control_device_id or "").strip()
        if not toy_capability_allowed:
            return _toy_delivery(
                "gateway_rejected",
                commands=commands,
                reason="capability_not_frozen",
                audit=audit,
            )
        if not expected_session_id or control_epoch is None or not expected_owner or not expected_device:
            return _toy_delivery(
                "gateway_rejected",
                commands=commands,
                reason="missing_control_metadata",
                audit=audit,
            )
        expected_session = await self._known_control_session(expected_session_id)
        if expected_session is not None and _session_status(expected_session) != "active":
            return _toy_delivery(
                "gateway_rejected",
                commands=commands,
                reason=f"session_{_session_status(expected_session)}",
                session=expected_session,
                audit=audit,
            )

        current_session = await self._current_control_session(conv_id)
        if current_session is not None:
            if _session_status(current_session) != "active":
                return _toy_delivery(
                    "gateway_rejected",
                    commands=commands,
                    reason=f"session_{_session_status(current_session)}",
                    session=current_session,
                    audit=audit,
                )
            if expected_session_id and expected_session_id != _session_id(current_session):
                return _toy_delivery(
                    "gateway_rejected",
                    commands=commands,
                    reason="session_mismatch",
                    session=current_session,
                    audit=audit,
                )
            return await self._broadcast_gateway_toy(
                commands=commands,
                msg_id=msg_id,
                conv_id=conv_id,
                session=current_session,
                control_epoch=control_epoch,
                owner_client_id=owner_client_id,
                control_device_id=control_device_id,
                audit=audit,
            )

        tombstone = await self._recent_safety_tombstone(conv_id)
        if tombstone is not None:
            return _toy_delivery(
                "gateway_rejected",
                commands=commands,
                reason="safety_tombstone",
                session=tombstone,
                audit=audit,
            )

        return _toy_delivery("gateway_rejected", commands=commands, reason="no_active_session", audit=audit)

    async def request_screen_check(
        self,
        *,
        conv_id: str,
        msg_id: str,
        model_key: str,
        reason: str,
        request_id: str | None = None,
        wake_id: str | None = None,
    ) -> Mapping[str, Any]:
        conv_id = _required_text(conv_id, "screen check conversation id")
        msg_id = _required_text(msg_id, "screen check message id")
        model_key = _required_text(model_key, "screen check model key")
        reason = " ".join(_required_text(reason, "screen check reason").split())[:200]

        from app.background_tasks import create_tracked_task
        from app.chat.side_effects import perform_screen_check
        from app.pc_screen import service as screen_service

        request = await screen_service.create_screen_request(
            conv_id=conv_id,
            msg_id=msg_id,
            model_key=model_key,
            reason=reason,
        )
        if request is None:
            payload = {
                "type": "screen_check_rejected",
                "status": "disabled",
                "reject_reason": "disabled",
                "conv_id": conv_id,
                "msg_id": msg_id,
                "reason": reason,
                "request_id": request_id or "",
                "wake_id": wake_id or request_id or "",
                "broadcast": False,
            }
            await self._broadcaster({"type": "screen_check_rejected", "data": payload})
            await _record_observed_tool_result(
                self._action_observation(
                    conv_id=conv_id,
                    msg_id=msg_id,
                    request_id=str(request_id or msg_id),
                    model_key=model_key,
                    capability="pc.screen_check",
                ),
                tool_name="pc.screen_check",
                raw_text=f"[SCREEN_CHECK:{reason}]",
                payload=payload,
            )
            return payload

        payload = {
            "type": "screen_check_pending" if request.status == "pending" else "screen_check_rejected",
            "status": request.status,
            "reject_reason": request.reject_reason,
            "conv_id": request.conv_id,
            "msg_id": request.msg_id,
            "reason": request.reason,
            "request_id": request.request_id,
            "wake_id": wake_id or request_id or request.request_id,
            "broadcast": True,
        }
        create_tracked_task(
            perform_screen_check(request),
            name=f"sentinel_screen_check:{conv_id}:{msg_id}:{request.request_id}",
        )
        if request.status == "pending":
            await self._broadcaster({"type": "screen_check_pending", "data": payload})
        await _record_observed_tool_result(
            self._action_observation(
                conv_id=conv_id,
                msg_id=msg_id,
                request_id=str(request_id or request.request_id),
                model_key=model_key,
                capability="pc.screen_check",
            ),
            tool_name="pc.screen_check",
            raw_text=f"[SCREEN_CHECK:{reason}]",
            payload=payload,
        )
        return payload

    async def request_mobile_screen_check(
        self,
        *,
        conv_id: str,
        msg_id: str,
        model_key: str,
        target: str,
        reason: str,
        request_id: str | None = None,
        wake_id: str | None = None,
    ) -> Mapping[str, Any]:
        conv_id = _required_text(conv_id, "mobile screen check conversation id")
        msg_id = _required_text(msg_id, "mobile screen check message id")
        model_key = _required_text(model_key, "mobile screen check model key")
        reason = " ".join(_required_text(reason, "mobile screen check reason").split())[:200]
        target = " ".join(str(target or "").split())[:80]

        from app.background_tasks import create_tracked_task
        from app.chat.side_effects import perform_mobile_screen_check
        from app.chat.streaming import _resolve_mobile_target
        from app.mobile_screen import mobile_screen_service

        device_id, err = await _resolve_mobile_target(target)
        if err:
            # 未解析到设备/歧义：构造 rejected 请求，仍跑 follow-up 给出自然语言解释。
            request = mobile_screen_service.build_rejected_request(
                conv_id=conv_id, msg_id=msg_id, model_key=model_key,
                target_label=target, reason=reason, reject_reason=err,
            )
        else:
            request = await mobile_screen_service.create_request(
                conv_id=conv_id, msg_id=msg_id, model_key=model_key,
                target_device_id=device_id, reason=reason,
            )
            if request.status == "pending":
                try:
                    from app.mobile_screen.autonomous import record_autonomous_mobile_screen_request

                    record_autonomous_mobile_screen_request(self.now())
                except Exception:
                    pass

        payload = {
            "type": "screen_check_pending" if request.status == "pending" else "screen_check_rejected",
            "status": request.status,
            "reject_reason": request.reject_reason,
            "conv_id": request.conv_id,
            "msg_id": request.msg_id,
            "reason": request.reason,
            "target": target,
            "target_device_id": request.target_device_id,
            "target_device_name": request.target_device_name,
            "request_id": request.request_id,
            "wake_id": wake_id or request_id or request.request_id,
            "broadcast": True,
        }
        create_tracked_task(
            perform_mobile_screen_check(request),
            name=f"sentinel_mobile_screen_check:{conv_id}:{msg_id}:{request.request_id}",
        )
        if request.status == "pending":
            await self._broadcaster({"type": "screen_check_pending", "data": payload})
        await _record_observed_tool_result(
            self._action_observation(
                conv_id=conv_id,
                msg_id=msg_id,
                request_id=str(request_id or request.request_id),
                model_key=model_key,
                capability="mobile.screen_check",
            ),
            tool_name="mobile.screen_check",
            raw_text=f"[MOBILE_SCREEN_CHECK:{target}|{reason}]",
            payload=payload,
        )
        return payload

    async def execute_ring_touch(
        self,
        *,
        touch_descriptions: Sequence[str],
        conv_id: str,
        msg_id: str,
        model_key: str,
        request_id: str | None = None,
        wake_id: str | None = None,
    ) -> Mapping[str, Any]:
        from app.devices import device_service
        from app.modes.service import ring_touch_enabled
        from app.tools.schemas import ToolContext, ToolIntent
        from app.tools.service import tool_service
        from ring_touch_translator import translate_ring_touch

        touches = [
            " ".join(str(item or "").split())[:120]
            for item in (touch_descriptions or ())
            if str(item or "").strip()
        ][:1]
        if not touches:
            return {"status": "skipped", "count": 0, "reason": "empty_touch_description"}
        if not ring_touch_enabled():
            return {"status": "capability_disabled", "count": len(touches), "results": []}

        context = ToolContext(
            conv_id=_required_text(conv_id, "ring touch conversation id"),
            msg_id=_required_text(msg_id, "ring touch message id"),
            request_id=request_id or msg_id,
            model_key=model_key,
            mode="normal",
            capabilities=("device.ring_touch",) if ring_touch_enabled() else (),
            metadata={
                "source": "sentinel",
                "source_chain": "sentinel",
                "invocation_id": self._action_invocation_id(conv_id),
                "wake_id": wake_id or request_id or msg_id,
            },
        )

        intents = []
        for index, touch in enumerate(touches, 1):
            haptics = await translate_ring_touch(touch)
            intents.append(ToolIntent(
                id=f"sentinel_ring_{index:03d}",
                tool_name="device.ring_touch",
                raw_text=f"[RING:{touch}]",
                arguments={
                    "touch": touch,
                    "reason": "sentinel_core_wake",
                    "haptics": haptics,
                },
                side_effect_level="device",
                allowed_modes=("ring_touch_enabled",),
                source="sentinel_ring_touch_description",
                metadata={
                    "command_group": "ring",
                    "source": "sentinel_core_wake",
                },
            ))

        async def adapter(intent, ctx):
            rid = f"{ctx.request_id}:{intent.id}"
            params = dict(intent.arguments)
            params["_ring_request_id"] = rid
            params["_ring_wake_id"] = ctx.metadata.get("wake_id")
            params["_ring_created_at"] = self.now()
            return await device_service.execute_command("smart_ring", "touch", params, request_id=rid)

        results = await tool_service.execute_async(
            intents,
            context=context,
            adapters={"device.ring_touch": adapter},
        )
        if self._action_invocation_id(conv_id):
            from app.tools.ledger import tool_invocation_ledger

            await tool_invocation_ledger.record_postprocess(
                context,
                raw_output="\n".join(intent.raw_text for intent in intents),
                intents=intents,
                plan_results=(),
                enabled_commands=("ring",),
            )
            await tool_invocation_ledger.record_execution(
                context,
                results=results,
                intents_by_id={intent.id: intent for intent in intents},
            )
        return {
            "status": "executed",
            "count": len(intents),
            "results": [result.to_dict() for result in results],
        }

    async def _broadcast_gateway_toy(
        self,
        *,
        commands: Sequence[str],
        msg_id: str,
        conv_id: str,
        session: Any,
        control_epoch: int | None,
        owner_client_id: str | None,
        control_device_id: str | None,
        audit: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        from app.tools.schemas import ToolContext

        metadata = {
            "source": "sentinel",
            "source_chain": "sentinel",
            "invocation_id": self._action_invocation_id(conv_id),
            "delivery_path": "core_wake",
            "request_id": audit["request_id"],
            "wake_id": audit["wake_id"],
            "control_session_id": _session_id(session),
            "control_epoch": control_epoch,
            "owner_client_id": owner_client_id,
            "device_id": control_device_id,
        }
        context = ToolContext(
            conv_id=conv_id,
            msg_id=msg_id,
            request_id=audit["request_id"],
            mode="device_control" if _session_kind(session) == "dom" else "intimate",
            capabilities=("device.toy",),
            metadata=metadata,
        )
        intents = _toy_intents(commands, audit=audit)
        results = await self._tool_service().execute_async(
            intents,
            context=context,
            adapters={"device.toy": self._gateway_adapter()},
        )
        if self._action_invocation_id(conv_id):
            from app.tools.ledger import tool_invocation_ledger

            await tool_invocation_ledger.record_postprocess(
                context,
                raw_output="\n".join(intent.raw_text for intent in intents),
                intents=intents,
                plan_results=(),
                enabled_commands=("toy",),
            )
            await tool_invocation_ledger.record_execution(
                context,
                results=results,
                intents_by_id={intent.id: intent for intent in intents},
            )
        payload = _toy_payload_from_results(results)
        if not payload:
            return _toy_delivery(
                "gateway_rejected",
                commands=commands,
                reason=_toy_reject_reason(results),
                session=session,
                audit=audit,
            )
        payload["msg_id"] = msg_id
        await self._broadcaster({"type": "toy_command", "data": payload})
        return _toy_delivery(
            "gateway_accepted",
            commands=payload["commands"],
            broadcast=True,
            session=session,
            audit=audit,
        )

    async def _current_control_session(self, conv_id: str) -> Any | None:
        return await self._control_sessions().get_current(conv_id=conv_id)

    async def _known_control_session(self, session_id: str) -> Any | None:
        if not session_id:
            return None
        return await self._control_sessions().get_session(session_id)

    async def _recent_safety_tombstone(self, conv_id: str) -> Any | None:
        return await self._control_sessions().recent_safety_tombstone(conv_id)

    def _control_sessions(self) -> Any:
        if self._control_session_service is None:
            from app.control import control_session_service

            self._control_session_service = control_session_service
        return self._control_session_service

    def _tool_service(self) -> Any:
        if self._toy_tool_service is None:
            from app.tools.service import tool_service

            self._toy_tool_service = tool_service
        return self._toy_tool_service

    def _gateway_adapter(self) -> Callable:
        if self._toy_gateway_adapter is None:
            from app.control import control_command_gateway

            self._toy_gateway_adapter = control_command_gateway.execute_toy_intent
        return self._toy_gateway_adapter

    async def sleep(self, seconds: float) -> None:
        seconds = _required_non_negative_number(seconds, "sleep seconds")
        await self._sleeper(seconds)

    async def write_monitor_log(self, entry: Mapping[str, Any]) -> None:
        if not isinstance(entry, Mapping):
            raise ValueError("core wake monitor log entry must be an object")
        payload = self._legacy_monitor_log_entry(entry)
        result = await self._monitor_log_writer(payload)
        if result is False:
            raise RuntimeError("core wake monitor log writer returned false")

    async def _insert_message(
        self,
        *,
        conv_id: str,
        role: str,
        content: str,
        created_at: float,
        suffix: str,
    ) -> dict[str, Any]:
        conv_id = _required_text(conv_id, "conversation id")
        content = _required_text(content, f"{role} message content")
        created_at = _required_timestamp(created_at, f"{role} message created_at")
        msg_id = f"msg_{int(created_at * 1000)}{suffix}"
        message = {
            "id": msg_id,
            "conv_id": conv_id,
            "role": role,
            "content": content,
            "created_at": created_at,
            "attachments": [],
        }
        async with self._db_factory() as db:
            await db.execute(
                "INSERT INTO messages (id, conv_id, role, content, created_at, attachments) VALUES (?,?,?,?,?,?)",
                (msg_id, conv_id, role, content, created_at, "[]"),
            )
            await db.commit()
        return message

    def _legacy_monitor_log_entry(self, entry: Mapping[str, Any]) -> dict[str, Any]:
        status = _required_text(entry.get("status"), "monitor log status")
        timestamp = self.now()
        local = time.localtime(timestamp)
        payload = {
            "timestamp": timestamp,
            "time": time.strftime("%H:%M:%S", local),
            "date": time.strftime("%Y-%m-%d", local),
            "monitoringlog": _monitoringlog_for_entry(entry),
            "summary": str(entry.get("summary") or ""),
            "score": None,
            "call_core": bool(entry.get("call_core", False)),
            "core_reason": str(entry.get("core_reason") or ""),
            "screenshot": "",
            "source": "sentinel",
            "status": status,
        }
        for key, value in entry.items():
            if key not in payload:
                payload[key] = value
        return payload


def build_legacy_core_wake_ports(
    *,
    observation_conv_id: str = "",
    observation_request_id: str = "",
    linked_invocation_id: str = "",
) -> LegacyCoreWakePorts:
    """Build the production full-wake ports using current runtime singletons."""
    from ai_providers import stream_ai
    from config import SETTINGS
    from database import get_db
    from ws import manager

    async def _write_monitor_log(entry: Mapping[str, Any]) -> bool:
        from sentinel_runtime import append_and_broadcast_monitor_log

        return await append_and_broadcast_monitor_log(dict(entry))

    return LegacyCoreWakePorts(
        db_factory=get_db,
        broadcaster=manager.broadcast,
        core_streamer=stream_ai,
        monitor_log_writer=_write_monitor_log,
        default_temperature=SETTINGS.get("temperature"),
        observation_conv_id=observation_conv_id,
        observation_request_id=observation_request_id,
        linked_invocation_id=linked_invocation_id,
    )


async def _record_observed_tool_result(
    observation: tuple[Any, Any, str] | None,
    *,
    tool_name: str,
    raw_text: str,
    payload: Mapping[str, Any],
) -> None:
    if observation is None:
        return
    ledger, context, _invocation_id = observation
    from app.tools.schemas import ToolIntent, ToolResult, ToolStatus

    intent = ToolIntent(
        id=ledger.new_invocation_id("sentinel_tool_intent"),
        tool_name=tool_name,
        raw_text=raw_text,
        arguments={
            key: value
            for key, value in dict(payload).items()
            if key in {"reason", "target", "target_device_id"}
        },
        side_effect_level="external",
        requires_confirmation=True,
        source="sentinel_core_wake",
    )
    result = ToolResult.from_intent(
        intent,
        status=ToolStatus.EXECUTED,
        result=dict(payload),
    )
    await ledger.record_postprocess(
        context,
        raw_output=raw_text,
        intents=(intent,),
        plan_results=(),
    )
    await ledger.record_execution(
        context,
        results=(result,),
        intents_by_id={intent.id: intent},
    )


def _advertised_tools_from_messages(
    messages: Sequence[Mapping[str, str]],
) -> tuple[str, ...]:
    # The Core capability block is in the final wake prompt.  Earlier chat
    # history may mention old marker syntax but is not an advertisement for this
    # invocation.
    text = str(messages[-1].get("content") or "") if messages else ""
    candidates = (
        ("device.toy", "[TOY:"),
        ("device.ring_touch", "[RING:"),
        ("pc.screen_check", "[SCREEN_CHECK:"),
        ("mobile.screen_check", "[MOBILE_SCREEN_CHECK:"),
    )
    return tuple(tool_name for tool_name, marker in candidates if marker in text)


def _required_callable(value: Any, label: str) -> Callable[..., Any]:
    if not callable(value):
        raise ValueError(f"core wake legacy adapter {label} must be callable")
    return value


def _required_text(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"core wake {label} must be non-empty text")
    return value.strip()


def _required_timestamp(value: Any, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise ValueError(f"core wake {label} must be a numeric timestamp")
    return float(value)


def _required_non_negative_number(value: Any, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise ValueError(f"core wake {label} must be a numeric value")
    if value < 0:
        raise ValueError(f"core wake {label} must be non-negative")
    return float(value)


def _validated_core_messages(messages: Sequence[Mapping[str, str]]) -> list[dict[str, str]]:
    if not isinstance(messages, Sequence) or isinstance(messages, str | bytes):
        raise ValueError("core wake messages must be a sequence")
    result = []
    for index, message in enumerate(messages):
        if not isinstance(message, Mapping):
            raise ValueError(f"core wake message {index} must be an object")
        role = message.get("role")
        content = message.get("content")
        if role not in {"assistant", "user"}:
            raise ValueError(f"core wake message {index} role must be user or assistant")
        result.append({"role": role, "content": _required_text(content, f"message {index} content")})
    return result


def _validated_toy_commands(commands: Sequence[str]) -> list[str]:
    if not isinstance(commands, Sequence) or isinstance(commands, str | bytes):
        raise ValueError("core wake toy commands must be a sequence")
    result = []
    for index, command in enumerate(commands):
        result.append(_required_text(command, f"toy command {index}"))
    return result


def _required_bool(value: Any, label: str) -> bool:
    if not isinstance(value, bool):
        raise ValueError(f"core wake {label} must be a boolean")
    return value


def _toy_audit_metadata(
    *,
    msg_id: str,
    request_id: str | None = None,
    wake_id: str | None = None,
) -> dict[str, Any]:
    normalized_request_id = str(request_id or "").strip() or msg_id
    normalized_wake_id = str(wake_id or "").strip() or normalized_request_id
    return {
        "source": "sentinel",
        "delivery_path": "core_wake",
        "request_id": normalized_request_id,
        "wake_id": normalized_wake_id,
    }


def _toy_intents(commands: Sequence[str], *, audit: Mapping[str, Any]) -> list[Any]:
    from app.tools.schemas import ToolIntent

    return [
        ToolIntent(
            id=f"sentinel_toy_{index:03d}",
            tool_name="device.toy",
            raw_text=f"[TOY:{command}]",
            arguments={"command": command},
            side_effect_level="device",
            allowed_modes=("intimate", "device_control"),
            metadata=dict(audit),
        )
        for index, command in enumerate(commands, 1)
    ]


def _toy_payload_from_results(results: Sequence[Any]) -> dict[str, Any] | None:
    commands: list[str] = []
    control_fields: dict[str, Any] = {}
    for result in results:
        status = getattr(getattr(result, "status", None), "value", getattr(result, "status", None))
        if status != "executed" or not getattr(result, "result", None):
            continue
        data = dict(result.result)
        if data.get("ok") is False:
            continue
        command = str(data.get("command") or "").strip()
        if not command:
            continue
        commands.append(command)
        if not control_fields:
            for key in ("control_session_id", "control_epoch", "owner_client_id"):
                if data.get(key) is not None:
                    control_fields[key] = data[key]
    if not commands:
        return None
    return {"type": "toy_command", "commands": commands, **control_fields}


def _toy_reject_reason(results: Sequence[Any]) -> str:
    for result in results:
        data = getattr(result, "result", None)
        if isinstance(data, Mapping) and data.get("message"):
            return str(data["message"])
        error = getattr(result, "error", None)
        if error:
            return str(error)
    return "gateway_rejected"


def _toy_delivery(
    status: str,
    *,
    commands: Sequence[str],
    broadcast: bool = False,
    reason: str = "",
    session: Any | None = None,
    audit: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "status": status,
        "broadcast": bool(broadcast),
        "commands": list(commands),
    }
    if reason:
        payload["reason"] = reason
    if session is not None:
        payload["control_session_id"] = _session_id(session)
        payload["control_epoch"] = _session_epoch(session)
        payload["owner_client_id"] = _session_owner(session)
    if audit:
        payload.update({
            "source": audit.get("source"),
            "delivery_path": audit.get("delivery_path"),
            "request_id": audit.get("request_id"),
            "wake_id": audit.get("wake_id"),
        })
    return payload


def _session_id(session: Any) -> str:
    return _required_text(getattr(session, "session_id", None), "control session id")


def _session_kind(session: Any) -> str:
    return _required_text(getattr(session, "kind", None), "control session kind")


def _session_owner(session: Any) -> str:
    return _required_text(getattr(session, "owner_client_id", None), "control session owner")


def _session_status(session: Any) -> str:
    return _required_text(getattr(session, "status", None), "control session status")


def _session_epoch(session: Any) -> int:
    epoch = getattr(session, "control_epoch", None)
    if isinstance(epoch, bool) or not isinstance(epoch, int):
        raise ValueError("core wake control session epoch must be an integer")
    return epoch


def _monitoringlog_for_entry(entry: Mapping[str, Any]) -> str:
    explicit = entry.get("monitoringlog")
    if isinstance(explicit, str) and explicit.strip():
        return explicit.strip()

    status = str(entry.get("status") or "")
    reason = str(entry.get("core_reason") or "").strip()
    if status == "core_succeeded":
        return "🧠 哨兵唤醒 Core 成功。"
    if status == "core_empty":
        return f"⚠️ 哨兵唤醒 Core 后返回空内容。原因：{reason or '未提供'}"
    if status == "core_failed":
        error_type = str(entry.get("error_type") or "RuntimeError")
        error = str(entry.get("error") or "").strip()
        detail = f": {error}" if error else ""
        return f"⚠️ 哨兵唤醒 Core 失败：{error_type}{detail}"
    return f"哨兵 Core wake 结果：{status or 'unknown'}"


__all__ = [
    "LegacyCoreWakePorts",
    "build_legacy_core_wake_ports",
]
