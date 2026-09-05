"""
WebSocket 连接管理器
"""

from __future__ import annotations

import json, logging
from collections.abc import Callable
from fastapi import WebSocket

log = logging.getLogger("ws")


def _load_notification_ai_name() -> str:
    from app.chat.worldbook import load_worldbook_names

    return load_worldbook_names()[1]


class ConnectionManager:
    def __init__(self, ai_name_loader: Callable[[], str] | None = None):
        self.active: list[WebSocket] = []
        self._device_ws: dict[str, WebSocket] = {}
        self._ai_name_loader = ai_name_loader or _load_notification_ai_name

    async def connect(self, ws: WebSocket):
        await ws.accept()
        self.active.append(ws)
        log.info("WS connected, total=%d", len(self.active))

    def disconnect(self, ws: WebSocket):
        if ws in self.active:
            self.active.remove(ws)
        self.unregister_device_ws(ws)
        log.info("WS disconnected, total=%d", len(self.active))

    def register_device_ws(self, device_type: str | None, ws: WebSocket):
        key = str(device_type or "").strip()
        if key:
            self._device_ws[key] = ws

    def unregister_device_ws(self, ws: WebSocket):
        stale = [key for key, value in self._device_ws.items() if value is ws]
        for key in stale:
            self._device_ws.pop(key, None)

    async def send_to_device(self, device_type: str, data: dict) -> bool:
        key = str(device_type or "").strip()
        ws = self._device_ws.get(key)
        if ws is None or ws not in self.active:
            self._device_ws.pop(key, None)
            return False
        try:
            await ws.send_text(json.dumps(data, ensure_ascii=False))
            return True
        except Exception as e:
            log.warning("WS device send failed type=%s: %s", key, e)
            if ws in self.active:
                self.active.remove(ws)
            self.unregister_device_ws(ws)
            return False

    async def broadcast(self, data: dict, exclude: WebSocket = None):
        wire_data = self._with_notification_identity(data)
        msg = json.dumps(wire_data, ensure_ascii=False)
        msg_type = wire_data.get("type", "unknown")
        targets = [ws for ws in self.active.copy() if ws is not exclude]
        sent = 0
        failed = 0
        for ws in targets:
            try:
                await ws.send_text(msg)
                sent += 1
            except Exception as e:
                log.warning("WS send failed: %s", e)
                if ws in self.active:
                    self.active.remove(ws)
                failed += 1
        log.info("broadcast type=%s sent=%d failed=%d total_clients=%d",
                 msg_type, sent, failed, len(self.active))

    def _with_notification_identity(self, event: dict) -> dict:
        """Attach the runtime companion name to assistant-message wire events."""

        payload = event.get("data")
        if event.get("type") != "msg_created" or not isinstance(payload, dict):
            return event
        if payload.get("role") != "assistant":
            return event
        try:
            ai_name = str(self._ai_name_loader() or "").strip()
        except Exception:
            log.warning("failed to resolve ai_name for msg_created", exc_info=True)
            return event
        if not ai_name:
            return event
        wire_event = dict(event)
        wire_event["data"] = {**payload, "ai_name": ai_name}
        return wire_event


manager = ConnectionManager()
