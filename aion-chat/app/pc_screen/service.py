"""In-memory PC screen check state machine."""

from __future__ import annotations

import asyncio
import shutil
import time
import uuid

from config import SCREENSHOTS_DIR, SETTINGS, UPLOADS_DIR, resolve_core_model, save_settings
from .schemas import NONTERMINAL_STATUSES, REJECT_REASONS, STATUS_APPROVED, STATUS_COMPLETED, STATUS_PENDING, STATUS_REJECTED, ScreenCheckRequest

SCREEN_AGENT_ONLINE_SEC = 90
REQUEST_TIMEOUT_SEC = 120
RATE_LIMIT_SEC = 300
SCREENSHOT_TTL_SEC = 900
SCREEN_TMP_DIR = SCREENSHOTS_DIR / "tmp"
current_request: ScreenCheckRequest | None = None
last_completed_at: float | None = None
last_screen_poll_at: float | None = None
_pending_event: asyncio.Event | None = None

def _event() -> asyncio.Event:
    global _pending_event
    if _pending_event is None:
        _pending_event = asyncio.Event()
    return _pending_event

def is_screen_capture_enabled() -> bool:
    return bool(SETTINGS.get("screen_capture_enabled", False))

def set_screen_capture_enabled(enabled: bool) -> None:
    SETTINGS["screen_capture_enabled"] = bool(enabled)
    save_settings(SETTINGS)

def screen_agent_online(now: float | None = None) -> bool:
    reference = time.time() if now is None else float(now)
    return last_screen_poll_at is not None and reference - last_screen_poll_at <= SCREEN_AGENT_ONLINE_SEC

def model_supports_vision(model_key: str) -> bool:
    cfg = resolve_core_model(model_key)
    if not cfg:
        return False
    provider = str(cfg.get("provider") or cfg.get("endpoint", {}).get("type") or "").lower()
    model = str(cfg.get("model") or model_key or "").lower()
    key = str(model_key or "").lower()
    if provider == "gemini" or "gemini" in model or "gemini" in key:
        return True
    markers = ("claude", "gpt-4o", "gpt-4.1", "gpt-5", "vision", "qwen-vl", "qwen2-vl", "vl-")
    return any(marker in model or marker in key for marker in markers)
async def create_screen_request(*, conv_id: str, msg_id: str, model_key: str, reason: str) -> ScreenCheckRequest | None:
    if not is_screen_capture_enabled():
        return None
    cleanup_expired_files()
    reason = " ".join(str(reason or "").split())[:200] or "想确认你当前在做什么"
    request = _new_request(conv_id, msg_id, model_key, reason)
    reject_reason = _gateway_reject_reason(model_key)
    if reject_reason:
        _reject(request, reject_reason)
        await audit_screen_event("rejected", request)
        return request
    global current_request
    current_request = request
    _event().set()
    await audit_screen_event("requested", request)
    return request

def _gateway_reject_reason(model_key: str) -> str:
    now = time.time()
    if not model_supports_vision(model_key):
        return "model_no_vision"
    if not screen_agent_online(now):
        return "offline"
    if current_request and current_request.status in NONTERMINAL_STATUSES:
        return "duplicate_pending"
    if last_completed_at and now - last_completed_at < RATE_LIMIT_SEC:
        return "rate_limited"
    return ""
def _new_request(conv_id: str, msg_id: str, model_key: str, reason: str) -> ScreenCheckRequest:
    return ScreenCheckRequest(str(uuid.uuid4()), conv_id, msg_id, model_key, reason)
async def wait_pending_request(timeout: float) -> ScreenCheckRequest | None:
    global last_screen_poll_at
    last_screen_poll_at = time.time()
    if current_request and current_request.status == STATUS_PENDING:
        return current_request
    event = _event()
    event.clear()
    try:
        await asyncio.wait_for(event.wait(), timeout=max(1.0, min(float(timeout), 30.0)))
    except asyncio.TimeoutError:
        return None
    return current_request if current_request and current_request.status == STATUS_PENDING else None
async def mark_decision(request_id: str, decision: str, reject_reason: str = "") -> ScreenCheckRequest | None:
    request = _find_request(request_id)
    if not request:
        return None
    if decision == "approved" and request.status == STATUS_PENDING:
        request.status = STATUS_APPROVED
    elif decision == "rejected" and request.status in NONTERMINAL_STATUSES:
        _reject(request, reject_reason or "denied")
        await audit_screen_event("rejected", request)
    return request
async def save_uploaded_screenshot(request_id: str, data: bytes) -> ScreenCheckRequest | None:
    request = _find_request(request_id)
    if not request or request.status != STATUS_APPROVED:
        return request
    try:
        SCREEN_TMP_DIR.mkdir(parents=True, exist_ok=True)
        UPLOADS_DIR.mkdir(parents=True, exist_ok=True)
        filename = f"{request.request_id}.jpg"
        tmp_path = SCREEN_TMP_DIR / filename
        tmp_path.write_bytes(data)
        shutil.copyfile(tmp_path, UPLOADS_DIR / filename)
        request.image_path = filename
        _complete(request)
    except Exception:
        _reject(request, "upload_failed")
        await audit_screen_event("rejected", request)
    return request
def expire_request(request: ScreenCheckRequest) -> ScreenCheckRequest:
    if request.status in NONTERMINAL_STATUSES:
        _reject(request, "request_expired")
    return request
def release_request(request_id: str) -> None:
    global current_request
    if current_request and current_request.request_id == request_id:
        current_request = None
def delete_request_files(request: ScreenCheckRequest) -> None:
    if not request.image_path:
        return
    for directory in (UPLOADS_DIR, SCREEN_TMP_DIR):
        try:
            (directory / request.image_path).unlink(missing_ok=True)
        except Exception:
            pass

def cleanup_expired_files(now: float | None = None) -> int:
    if not SCREEN_TMP_DIR.exists():
        return 0
    reference = time.time() if now is None else float(now)
    deleted = 0
    for path in SCREEN_TMP_DIR.glob("*.jpg"):
        try:
            if reference - path.stat().st_mtime > SCREENSHOT_TTL_SEC:
                (UPLOADS_DIR / path.name).unlink(missing_ok=True)
                path.unlink()
                deleted += 1
        except Exception:
            pass
    return deleted
def status_payload(now: float | None = None) -> dict:
    from app.pc_context.service import get_pc_status_payload
    reference = time.time() if now is None else float(now)
    pc_status = get_pc_status_payload(reference)
    request = current_request if current_request and current_request.status in NONTERMINAL_STATUSES else None
    return {
        "screen_capture_enabled": is_screen_capture_enabled(),
        "pc_agent_online": pc_status.get("active_state") != "offline",
        "screen_agent_online": screen_agent_online(reference),
        "current_request": request.to_public() if request else None,
    }
async def audit_screen_event(event: str, request: ScreenCheckRequest) -> None:
    try:
        from sentinel_runtime import append_and_broadcast_monitor_log
        now = time.time()
        await append_and_broadcast_monitor_log({
            "timestamp": now, "time": time.strftime("%H:%M:%S", time.localtime(now)),
            "type": "pc_screen", "event": event, "request_id": request.request_id,
            "conv_id": request.conv_id, "reason": request.reason, "status": request.status,
            "reject_reason": request.reject_reason,
            "monitoringlog": f"🖥 PC截图 {event}: {request.status}",
        })
    except Exception:
        pass
def _find_request(request_id: str) -> ScreenCheckRequest | None:
    return current_request if current_request and current_request.request_id == request_id else None
def _reject(request: ScreenCheckRequest, reject_reason: str) -> None:
    if reject_reason not in REJECT_REASONS:
        raise ValueError(f"invalid screen reject_reason: {reject_reason}")
    request.status = STATUS_REJECTED
    request.reject_reason = reject_reason
    request.completed_at = time.time()
    request._done_event.set()
def _complete(request: ScreenCheckRequest) -> None:
    global last_completed_at
    request.status = STATUS_COMPLETED
    request.completed_at = time.time()
    last_completed_at = request.completed_at
    request._done_event.set()

def _reset_state_for_tests() -> None:
    global current_request, last_completed_at, last_screen_poll_at, _pending_event
    current_request = last_completed_at = last_screen_poll_at = _pending_event = None
