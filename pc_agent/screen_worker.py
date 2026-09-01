"""PC screen-consent poll worker."""

from __future__ import annotations

import logging
import time

import screen
from activity_worker import heartbeat_due, retry_delay, sleep_with_gap_log
from transport import get_json, post_file, post_json


DEFAULT_SCREEN_POLL_TIMEOUT_SEC = 30
SCREEN_POLL_RETRY_INITIAL_SEC = 5
SCREEN_POLL_RETRY_MAX_SEC = 15
log = logging.getLogger("pc_agent")


def run_screen_poll_loop(server_url: str, token: str, poll_timeout: int) -> None:
    failures = 0
    last_heartbeat_at: float | None = None
    poll_timeout = max(1, min(30, int(poll_timeout or DEFAULT_SCREEN_POLL_TIMEOUT_SEC)))
    while True:
        try:
            request = get_json(
                f"{server_url}/api/pc-screen/pending?timeout={poll_timeout}",
                token=token,
                timeout=poll_timeout + 10,
            )
            if failures:
                log.info("screen poll recovered after %s failed attempt(s)", failures)
            failures = 0
            now = time.monotonic()
            if heartbeat_due(last_heartbeat_at, now):
                log.info("screen poll heartbeat")
                last_heartbeat_at = now
            if request:
                handle_screen_request(server_url, token, request)
        except Exception as exc:
            failures += 1
            delay = retry_delay(
                failures, SCREEN_POLL_RETRY_INITIAL_SEC, SCREEN_POLL_RETRY_MAX_SEC
            )
            log.warning(
                "screen poll failed; attempt=%s retry_in=%.1fs error=%s: %s",
                failures, delay, type(exc).__name__, exc,
            )
            sleep_with_gap_log(delay, "screen poll retry")


def handle_screen_request(server_url: str, token: str, request: dict) -> None:
    request_id = str(request.get("request_id") or "")
    reason = str(request.get("reason") or "")
    ai_name = str(request.get("ai_name") or "AI").strip() or "AI"
    if not request_id:
        return
    reject_reason = screen.local_reject_reason()
    if reject_reason:
        screen_decision(server_url, request_id, token, "rejected", reject_reason)
        return
    try:
        allowed = screen.show_confirm_dialog(reason, timeout=30, ai_name=ai_name)
    except Exception as exc:
        log.warning("screen confirm failed: %s: %s", type(exc).__name__, exc)
        screen_decision(server_url, request_id, token, "rejected", "denied")
        return
    if not allowed:
        reason_code = (
            "confirm_timeout"
            if getattr(screen.show_confirm_dialog, "timed_out", False)
            else "denied"
        )
        screen_decision(server_url, request_id, token, "rejected", reason_code)
        return
    screen_decision(server_url, request_id, token, "approved")
    screen.post_confirm_delay()
    reject_reason = screen.local_reject_reason()
    if reject_reason:
        screen_decision(server_url, request_id, token, "rejected", reject_reason)
        return
    try:
        image_bytes = screen.capture_jpeg_bytes()
        post_file(
            f"{server_url}/api/pc-screen/{request_id}/upload",
            image_bytes,
            filename=f"{request_id}.jpg",
            token=token,
        )
    except Exception as exc:
        log.warning("screen upload failed: %s: %s", type(exc).__name__, exc)
        screen_decision(server_url, request_id, token, "rejected", "upload_failed")


def screen_decision(
    server_url: str,
    request_id: str,
    token: str,
    decision: str,
    reject_reason: str = "",
) -> None:
    payload = {"decision": decision}
    if reject_reason:
        payload["reject_reason"] = reject_reason
    post_json(
        f"{server_url}/api/pc-screen/{request_id}/decision", payload, token=token
    )


__all__ = ["run_screen_poll_loop"]
