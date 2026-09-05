from __future__ import annotations

import time
from collections.abc import Callable

from app.devices import device_service
from app.tools.schemas import ToolContext, ToolIntent
from app.tools.ledger import tool_invocation_ledger

from .ledger import ControlLedger
from .service import ControlSessionService, control_session_service
from .toy_capability import (
    normalize_toy_device_id,
    resolve_toy_capability_snapshot,
)


class ControlCommandGateway:
    def __init__(
        self,
        *,
        session_service: ControlSessionService = control_session_service,
        ledger: ControlLedger | None = None,
        device_service_adapter=device_service,
        tool_ledger=None,
        now: Callable[[], float] = time.time,
    ):
        self._sessions = session_service
        self._ledger = ledger or ControlLedger(now=now)
        self._devices = device_service_adapter
        self._tool_ledger = tool_ledger

    async def execute_toy_intent(self, intent: ToolIntent, context: ToolContext) -> dict:
        command = str(intent.arguments.get("command") or "").strip()
        requested_device_id = str(intent.arguments.get("device_id") or context.metadata.get("device_id") or "").strip()
        current = await self._sessions.get_current(conv_id=context.conv_id)
        expected_session_id = str(context.metadata.get("control_session_id") or "").strip()
        source = self._source(intent, context)
        audit_metadata = self._audit_metadata(intent, context, source=source)
        if not current:
            known = await self._known_session(expected_session_id)
            device_id = self._device_id(requested_device_id, known)
            tombstone = await self._recent_safety_tombstone(context.conv_id)
            if tombstone:
                return await self._reject(intent, context, command, device_id, "safety_tombstone", session=tombstone, audit_metadata=audit_metadata)
            reason = "no_active_session" if not known else f"session_{known.status}"
            return await self._reject(intent, context, command, device_id, reason, session=known, audit_metadata=audit_metadata)
        device_id = self._device_id(requested_device_id, current)
        if expected_session_id and expected_session_id != current.session_id:
            return await self._reject(intent, context, command, device_id, "session_mismatch", session=current, audit_metadata=audit_metadata)
        if current.status != "active":
            return await self._reject(intent, context, command, device_id, f"session_{current.status}", session=current, audit_metadata=audit_metadata)
        expected_epoch = context.metadata.get("control_epoch")
        expected_owner = str(context.metadata.get("owner_client_id") or "").strip()
        if not expected_session_id or expected_epoch is None or not expected_owner:
            return await self._reject(intent, context, command, device_id, "missing_control_metadata", session=current, audit_metadata=audit_metadata)
        if expected_epoch is not None and not self._epoch_matches(expected_epoch, current.control_epoch):
            return await self._reject(intent, context, command, device_id, "epoch_mismatch", session=current, audit_metadata=audit_metadata)
        if expected_owner and expected_owner != current.owner_client_id:
            return await self._reject(intent, context, command, device_id, "owner_mismatch", session=current, audit_metadata=audit_metadata)

        live_capability = await resolve_toy_capability_snapshot(
            conv_id=context.conv_id,
            session_service=self._sessions,
            device_service_adapter=self._devices,
            expected_session_id=expected_session_id,
            expected_epoch=expected_epoch,
            expected_owner_client_id=expected_owner,
            expected_device_id=device_id,
        )
        if not live_capability.allowed:
            return await self._reject(
                intent,
                context,
                command,
                live_capability.control_device_id or device_id,
                live_capability.reason,
                session=live_capability.session or current,
                audit_metadata=audit_metadata,
            )
        current = live_capability.session
        device_id = live_capability.control_device_id

        await self._ledger.record(
            "control.action.proposed",
            conv_id=context.conv_id,
            session_id=current.session_id,
            metadata={
                **audit_metadata,
                "tool_intent_id": intent.id,
                "raw_command": command,
                "device_id": device_id,
            },
        )
        device_result = await self._devices.execute_command(
            device_id,
            "pulse",
            {
                "legacy_command": command,
                "source": source,
                "conv_id": context.conv_id,
                "msg_id": context.msg_id,
                "tool_intent_id": intent.id,
                "control_session_id": current.session_id,
                "control_epoch": current.control_epoch,
                "owner_client_id": current.owner_client_id,
            },
            request_id=context.request_id or context.msg_id or intent.id,
        )
        ok = bool(device_result.get("ok"))
        await self._ledger.record(
            "control.action.accepted" if ok else "control.action.rejected",
            conv_id=context.conv_id,
            session_id=current.session_id,
            metadata={
                **audit_metadata,
                "tool_intent_id": intent.id,
                "raw_command": command,
                "device_id": device_id,
                "decision": "accepted" if ok else "rejected",
                "reason": None if ok else device_result.get("message"),
                "device_audit_event_id": device_result.get("audit_event_id"),
            },
        )
        payload = self._payload(command if ok else "", command, device_id, device_result, session=current, reason=None if ok else device_result.get("message"))
        await self._mirror_tool_result(intent, context, payload)
        return payload

    async def _known_session(self, session_id: str):
        return await self._sessions.get_session(session_id) if session_id else None

    async def _recent_safety_tombstone(self, conv_id: str):
        return await self._sessions.recent_safety_tombstone(conv_id)

    def _epoch_matches(self, expected, current: int) -> bool:
        try:
            return int(expected) == int(current)
        except (TypeError, ValueError):
            return False

    def _device_id(self, requested: str, session) -> str:
        return normalize_toy_device_id(requested or (session.device_id if session else None))

    async def _reject(self, intent, context, command, device_id, reason, *, session=None, audit_metadata=None) -> dict:
        await self._ledger.record(
            "control.action.rejected",
            conv_id=context.conv_id,
            session_id=session.session_id if session else None,
            metadata={
                **dict(audit_metadata or {}),
                "tool_intent_id": intent.id,
                "raw_command": command,
                "device_id": device_id,
                "decision": "rejected",
                "reason": reason,
            },
        )
        payload = self._payload("", command, device_id, {"ok": False, "message": reason}, session=session, reason=reason)
        await self._mirror_tool_result(intent, context, payload)
        return payload

    async def _mirror_tool_result(self, intent, context, payload) -> None:
        if self._tool_ledger is None:
            return
        await self._tool_ledger.record_gateway_result(
            context,
            intent=intent,
            result=payload,
        )

    def _source(self, intent: ToolIntent, context: ToolContext) -> str:
        source = str(context.metadata.get("source") or intent.metadata.get("source") or "").strip()
        if not source and context.metadata.get("control_context_source") == "legacy_body":
            return "legacy_body"
        return source or "chat_tool"

    def _audit_metadata(self, intent: ToolIntent, context: ToolContext, *, source: str) -> dict:
        wake_id = context.metadata.get("wake_id") or intent.metadata.get("wake_id")
        delivery_path = context.metadata.get("delivery_path") or intent.metadata.get("delivery_path")
        return {
            "source": source,
            "request_id": context.request_id or context.msg_id or intent.id,
            "wake_id": str(wake_id or "").strip() or None,
            "delivery_path": str(delivery_path or "").strip() or None,
        }

    def _payload(self, command, legacy_command, device_id, device_result, *, session=None, reason=None) -> dict:
        payload = {
            "type": "toy_command",
            "command": command,
            "legacy_command": legacy_command,
            "ok": bool(device_result.get("ok")),
            "device_id": device_id,
            "device_command": "pulse",
            "device_result": device_result,
            "audit_event_id": device_result.get("audit_event_id"),
            "message": reason or device_result.get("message"),
            "control_session_id": session.session_id if session else None,
            "control_epoch": session.control_epoch if session else None,
            "owner_client_id": session.owner_client_id if session else None,
        }
        return payload


control_command_gateway = ControlCommandGateway(tool_ledger=tool_invocation_ledger)
