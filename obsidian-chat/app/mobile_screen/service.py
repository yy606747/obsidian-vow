"""Per-device mobile screen capture state machine.

One pending request per target device, routed by ``target_device_id``. Mirrors
the PC screen gating (vision/rate-limit/duplicate/timeout/TTL) but each gate is
scoped per device, and rate-limit is per device rather than global.

TODO(dedup): file save + TTL cleanup + vision gate are copied from pc_screen
with light edits; extract a shared base once mobile is stable (plan §9.2).
"""

from __future__ import annotations

import asyncio
import shutil
import time
import uuid
from dataclasses import dataclass
from typing import Awaitable, Callable

from config import SCREENSHOTS_DIR, SETTINGS, UPLOADS_DIR, save_settings
# 共享 PC 的请求超时常量，避免两套状态机漂移（plan §9.2）。
from app.pc_screen.service import REQUEST_TIMEOUT_SEC

from .schemas import (
    MOBILE_REJECT_REASONS,
    NONTERMINAL_STATUSES,
    STATUS_APPROVED,
    STATUS_COMPLETED,
    STATUS_PENDING,
    STATUS_REJECTED,
    MobileScreenCheckRequest,
)

SCREEN_AGENT_ONLINE_SEC = 90
RATE_LIMIT_SEC = 300
SCREENSHOT_TTL_SEC = 900
MOBILE_TMP_DIR = SCREENSHOTS_DIR / "mobile_tmp"


@dataclass(frozen=True)
class TargetDeviceInfo:
    exists: bool = False
    name: str = ""
    device_type: str = ""
    online: bool = False
    screen_online: bool = False
    has_capability: bool = False


async def _default_device_resolver(device_id: str) -> TargetDeviceInfo:
    from app.devices import DeviceStatus, device_service

    driver = device_service.get_driver("android_mobile")
    if driver is None:
        return TargetDeviceInfo()
    for device in await driver.list_devices():
        if device.device_id != device_id:
            continue
        return TargetDeviceInfo(
            exists=True,
            name=device.name,
            device_type=str(device.metadata.get("device_type") or ""),
            online=device.status is DeviceStatus.ONLINE,
            screen_online=bool(device.metadata.get("screen_agent_online")),
            has_capability="screen.capture" in device.capabilities,
        )
    return TargetDeviceInfo()


def _default_poll_marker(device_id: str, now: float) -> bool:
    from app.devices import device_service

    driver = device_service.get_driver("android_mobile")
    if driver is None:
        return False
    return driver.mark_screen_poll(device_id, now)


def _default_vision_checker(model_key: str) -> bool:
    from app.pc_screen.service import model_supports_vision

    return model_supports_vision(model_key)


def _default_enabled_reader() -> bool:
    return bool(SETTINGS.get("mobile_screen_capture_enabled", False))


class MobileScreenService:
    def __init__(
        self,
        *,
        now: Callable[[], float] | None = None,
        device_resolver: Callable[[str], Awaitable[TargetDeviceInfo]] | None = None,
        poll_marker: Callable[[str, float], bool] | None = None,
        vision_checker: Callable[[str], bool] | None = None,
        enabled_reader: Callable[[], bool] | None = None,
        audit: Callable[[str, MobileScreenCheckRequest], Awaitable[None]] | None = None,
        request_timeout_sec: float = REQUEST_TIMEOUT_SEC,
        rate_limit_sec: float = RATE_LIMIT_SEC,
        screenshot_ttl_sec: float = SCREENSHOT_TTL_SEC,
    ):
        self._now = now or time.time
        self._resolve_device = device_resolver or _default_device_resolver
        self._mark_poll = poll_marker or _default_poll_marker
        self._supports_vision = vision_checker or _default_vision_checker
        self._enabled = enabled_reader or _default_enabled_reader
        self._audit = audit or self._default_audit
        self._request_timeout_sec = request_timeout_sec
        self._rate_limit_sec = rate_limit_sec
        self._screenshot_ttl_sec = screenshot_ttl_sec
        # per-device routing state
        self._requests: dict[str, MobileScreenCheckRequest] = {}
        self._events: dict[str, asyncio.Event] = {}
        self._last_completed: dict[str, float] = {}
        self._by_id: dict[str, str] = {}

    # ── config ──────────────────────────────────────────────
    def is_enabled(self) -> bool:
        return self._enabled()

    @staticmethod
    def set_enabled(enabled: bool) -> None:
        SETTINGS["mobile_screen_capture_enabled"] = bool(enabled)
        save_settings(SETTINGS)

    # ── request creation ────────────────────────────────────
    async def create_request(
        self,
        *,
        conv_id: str,
        msg_id: str,
        model_key: str,
        target_device_id: str,
        reason: str,
    ) -> MobileScreenCheckRequest:
        """Create a routed screenshot request.

        Unlike pc_screen (which returns None when disabled), this always returns
        a request so the AI gets an explicit reject_reason to relay.
        """
        self.cleanup_expired_files()
        reason = " ".join(str(reason or "").split())[:200] or "想确认你当前在做什么"
        info = await self._resolve_device(target_device_id)
        request = MobileScreenCheckRequest(
            request_id=str(uuid.uuid4()),
            conv_id=conv_id,
            msg_id=msg_id,
            model_key=model_key,
            reason=reason,
            target_device_id=target_device_id,
            target_device_name=info.name,
            target_device_type=info.device_type,
        )
        reject_reason = self._gate(model_key, target_device_id, info)
        if reject_reason:
            self._reject(request, reject_reason)
            await self._audit("rejected", request)
            return request
        self._requests[target_device_id] = request
        self._by_id[request.request_id] = target_device_id
        self._event(target_device_id).set()
        await self._audit("requested", request)
        return request

    def build_rejected_request(
        self,
        *,
        conv_id: str,
        msg_id: str,
        model_key: str,
        target_label: str,
        reason: str,
        reject_reason: str,
    ) -> MobileScreenCheckRequest:
        """An already-rejected request for failures that happen *before* a device
        is resolved (no device / ambiguous target). Lets the same follow-up runner
        produce a natural-language reply instead of the marker silently vanishing."""
        reason = " ".join(str(reason or "").split())[:200] or "想确认你当前在做什么"
        request = MobileScreenCheckRequest(
            request_id=str(uuid.uuid4()),
            conv_id=conv_id,
            msg_id=msg_id,
            model_key=model_key,
            reason=reason,
            target_device_id="",
            target_device_name=str(target_label or "").strip(),
        )
        self._reject(request, reject_reason)
        return request

    def _gate(self, model_key: str, device_id: str, info: TargetDeviceInfo) -> str:
        now = self._now()
        if not self.is_enabled():
            return "disabled"
        if not self._supports_vision(model_key):
            return "model_no_vision"
        if not info.exists or not info.online or not info.screen_online:
            return "offline"
        if not info.has_capability:
            return "hard_blocked"
        active = self._requests.get(device_id)
        if active and active.status in NONTERMINAL_STATUSES:
            return "duplicate_pending"
        last = self._last_completed.get(device_id)
        if last and now - last < self._rate_limit_sec:
            return "rate_limited"
        return ""

    # ── android poll loop ───────────────────────────────────
    async def wait_pending(self, device_id: str, timeout: float) -> MobileScreenCheckRequest | None:
        self._mark_poll(device_id, self._now())
        active = self._requests.get(device_id)
        if active and active.status == STATUS_PENDING:
            return active
        event = self._event(device_id)
        event.clear()
        try:
            await asyncio.wait_for(event.wait(), timeout=max(1.0, min(float(timeout), 30.0)))
        except asyncio.TimeoutError:
            return None
        active = self._requests.get(device_id)
        return active if active and active.status == STATUS_PENDING else None

    async def mark_decision(
        self, request_id: str, decision: str, reject_reason: str = ""
    ) -> MobileScreenCheckRequest | None:
        request = self._find(request_id)
        if not request:
            return None
        if decision == "approved" and request.status == STATUS_PENDING:
            request.status = STATUS_APPROVED
        elif decision == "rejected" and request.status in NONTERMINAL_STATUSES:
            self._reject(request, reject_reason or "denied")
            await self._audit("rejected", request)
        return request

    async def save_uploaded_screenshot(
        self, request_id: str, data: bytes
    ) -> MobileScreenCheckRequest | None:
        request = self._find(request_id)
        if not request or request.status != STATUS_APPROVED:
            return request
        try:
            MOBILE_TMP_DIR.mkdir(parents=True, exist_ok=True)
            UPLOADS_DIR.mkdir(parents=True, exist_ok=True)
            filename = f"{request.request_id}.jpg"
            tmp_path = MOBILE_TMP_DIR / filename
            tmp_path.write_bytes(data)
            shutil.copyfile(tmp_path, UPLOADS_DIR / filename)
            request.image_path = filename
            self._complete(request)
        except Exception:
            self._reject(request, "upload_failed")
            await self._audit("rejected", request)
        return request

    # ── lifecycle helpers ───────────────────────────────────
    def expire_request(self, request: MobileScreenCheckRequest) -> MobileScreenCheckRequest:
        if request.status in NONTERMINAL_STATUSES:
            self._reject(request, "request_expired")
        return request

    def release_request(self, request_id: str) -> None:
        device_id = self._by_id.pop(request_id, None)
        if device_id and self._requests.get(device_id) and self._requests[device_id].request_id == request_id:
            self._requests.pop(device_id, None)

    def delete_request_files(self, request: MobileScreenCheckRequest) -> None:
        if not request.image_path:
            return
        for directory in (UPLOADS_DIR, MOBILE_TMP_DIR):
            try:
                (directory / request.image_path).unlink(missing_ok=True)
            except Exception:
                pass

    async def audit_screen_event(self, event: str, request: MobileScreenCheckRequest) -> None:
        """Public alias so the shared follow-up runner can audit uniformly."""
        await self._audit(event, request)

    def request_timeout_sec(self) -> float:
        return self._request_timeout_sec

    def status_payload(self, now: float | None = None) -> dict:
        reference = self._now() if now is None else float(now)
        devices = []
        for device_id, request in self._requests.items():
            if request.status in NONTERMINAL_STATUSES:
                devices.append(request.to_public())
        return {
            "mobile_screen_capture_enabled": self.is_enabled(),
            "pending_requests": devices,
            "reference_time": reference,
        }

    def cleanup_expired_files(self, now: float | None = None) -> int:
        if not MOBILE_TMP_DIR.exists():
            return 0
        reference = self._now() if now is None else float(now)
        deleted = 0
        for path in MOBILE_TMP_DIR.glob("*.jpg"):
            try:
                if reference - path.stat().st_mtime > self._screenshot_ttl_sec:
                    (UPLOADS_DIR / path.name).unlink(missing_ok=True)
                    path.unlink()
                    deleted += 1
            except Exception:
                pass
        return deleted

    async def _default_audit(self, event: str, request: MobileScreenCheckRequest) -> None:
        try:
            from sentinel_runtime import append_and_broadcast_monitor_log

            now = self._now()
            await append_and_broadcast_monitor_log({
                "timestamp": now, "time": time.strftime("%H:%M:%S", time.localtime(now)),
                "type": "mobile_screen", "event": event, "request_id": request.request_id,
                "conv_id": request.conv_id, "target_device_id": request.target_device_id,
                "target_device_name": request.target_device_name,
                "reason": request.reason, "status": request.status,
                "reject_reason": request.reject_reason,
                "monitoringlog": f"📱 移动端截图 {event}: {request.target_device_name or request.target_device_id} {request.status}",
            })
        except Exception:
            pass

    def _event(self, device_id: str) -> asyncio.Event:
        event = self._events.get(device_id)
        if event is None:
            event = asyncio.Event()
            self._events[device_id] = event
        return event

    def _find(self, request_id: str) -> MobileScreenCheckRequest | None:
        device_id = self._by_id.get(request_id)
        if not device_id:
            return None
        request = self._requests.get(device_id)
        return request if request and request.request_id == request_id else None

    def _reject(self, request: MobileScreenCheckRequest, reject_reason: str) -> None:
        if reject_reason not in MOBILE_REJECT_REASONS:
            raise ValueError(f"invalid mobile screen reject_reason: {reject_reason}")
        request.status = STATUS_REJECTED
        request.reject_reason = reject_reason
        request.completed_at = self._now()
        request._done_event.set()

    def _complete(self, request: MobileScreenCheckRequest) -> None:
        request.status = STATUS_COMPLETED
        request.completed_at = self._now()
        self._last_completed[request.target_device_id] = request.completed_at
        request._done_event.set()

    def _reset_state_for_tests(self) -> None:
        self._requests.clear()
        self._events.clear()
        self._last_completed.clear()
        self._by_id.clear()


mobile_screen_service = MobileScreenService()


__all__ = [
    "MobileScreenService",
    "TargetDeviceInfo",
    "mobile_screen_service",
    "REQUEST_TIMEOUT_SEC",
    "RATE_LIMIT_SEC",
    "SCREENSHOT_TTL_SEC",
]
