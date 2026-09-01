"""Windows activity sampling worker; no GUI objects live here."""

from __future__ import annotations

import ctypes
import logging
import os
import random
import time
from pathlib import Path

import app_map
import privacy
from transport import post_json


REPORT_RETRY_INITIAL_SEC = 5
REPORT_RETRY_MAX_SEC = 30
SLEEP_GAP_LOG_THRESHOLD_SEC = 30
WORKER_HEARTBEAT_INTERVAL_SEC = 10 * 60
log = logging.getLogger("pc_agent")


def run_activity_loop(endpoint: str, token: str, interval: int, idle_threshold: int) -> None:
    failures = 0
    last_heartbeat_at: float | None = None
    while True:
        try:
            payload = snapshot_payload(idle_threshold)
            post_json(endpoint, payload, token=token)
            if failures:
                log.info("activity report recovered after %s failed attempt(s)", failures)
            failures = 0
            now = time.monotonic()
            if heartbeat_due(last_heartbeat_at, now):
                log.info("activity report heartbeat")
                last_heartbeat_at = now
            log.debug("reported %s", payload)
        except Exception as exc:
            failures += 1
            delay = retry_delay(failures, REPORT_RETRY_INITIAL_SEC, REPORT_RETRY_MAX_SEC)
            log.warning(
                "report failed; attempt=%s retry_in=%.1fs error=%s: %s",
                failures, delay, type(exc).__name__, exc,
            )
            sleep_with_gap_log(delay, "activity report retry")
            continue
        sleep_with_gap_log(interval, "activity report interval")


def snapshot_payload(idle_threshold_sec: int) -> dict:
    raw_process, raw_title = foreground_window()
    last_input_age = last_input_age_sec()
    locked = is_locked()
    if locked is True:
        active_state = "locked"
    elif last_input_age is None or locked is None:
        active_state = "unknown"
    elif last_input_age < idle_threshold_sec:
        active_state = "active"
    else:
        active_state = "idle"
    app_name = app_map.normalize_app_name(raw_process)
    title = privacy.sanitize_title(raw_process, raw_title)
    if active_state == "locked" and privacy.is_lock_screen_process(raw_process):
        app_name = None
        title = None
    return {
        "device": "pc",
        "app": app_name,
        "title": title,
        "timestamp": time.time(),
        "active_state": active_state,
        "last_input_age_sec": None if active_state == "unknown" else last_input_age,
    }


def foreground_window() -> tuple[str, str]:
    if os.name != "nt":
        return "Unknown", ""
    try:
        user32 = ctypes.windll.user32
        user32.GetForegroundWindow.restype = ctypes.c_void_p
        user32.GetWindowTextLengthW.argtypes = [ctypes.c_void_p]
        user32.GetWindowTextLengthW.restype = ctypes.c_int
        user32.GetWindowTextW.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p, ctypes.c_int]
        user32.GetWindowTextW.restype = ctypes.c_int
        user32.GetWindowThreadProcessId.argtypes = [
            ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_ulong),
        ]
        user32.GetWindowThreadProcessId.restype = ctypes.c_ulong
        hwnd = user32.GetForegroundWindow()
        if not hwnd:
            return "Unknown", ""
        length = max(1, user32.GetWindowTextLengthW(hwnd) + 1)
        title_buffer = ctypes.create_unicode_buffer(length)
        user32.GetWindowTextW(hwnd, title_buffer, length)
        pid = ctypes.c_ulong()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        return process_name_from_pid(int(pid.value)), title_buffer.value or ""
    except Exception as exc:
        log.debug("foreground collection failed: %s", exc)
        return "Unknown", ""


def process_name_from_pid(pid: int) -> str:
    if os.name != "nt":
        return "Unknown"
    kernel32 = ctypes.windll.kernel32
    kernel32.OpenProcess.argtypes = [ctypes.c_ulong, ctypes.c_int, ctypes.c_ulong]
    kernel32.OpenProcess.restype = ctypes.c_void_p
    kernel32.QueryFullProcessImageNameW.argtypes = [
        ctypes.c_void_p,
        ctypes.c_ulong,
        ctypes.c_wchar_p,
        ctypes.POINTER(ctypes.c_ulong),
    ]
    kernel32.QueryFullProcessImageNameW.restype = ctypes.c_int
    kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
    kernel32.CloseHandle.restype = ctypes.c_int
    handle = kernel32.OpenProcess(0x1000, False, int(pid))
    if not handle:
        return "Unknown"
    try:
        buffer = ctypes.create_unicode_buffer(1024)
        size = ctypes.c_uint(len(buffer))
        if not kernel32.QueryFullProcessImageNameW(handle, 0, buffer, ctypes.byref(size)):
            return "Unknown"
        return Path(buffer.value).name or "Unknown"
    finally:
        kernel32.CloseHandle(handle)


class _LastInputInfo(ctypes.Structure):
    _fields_ = [("cbSize", ctypes.c_uint), ("dwTime", ctypes.c_uint)]


def last_input_age_sec() -> int | None:
    if os.name != "nt":
        return None
    info = _LastInputInfo()
    info.cbSize = ctypes.sizeof(_LastInputInfo)
    if not ctypes.windll.user32.GetLastInputInfo(ctypes.byref(info)):
        return None
    tick = ctypes.windll.kernel32.GetTickCount()
    return max(0, int((tick - info.dwTime) / 1000))


def is_locked() -> bool | None:
    if os.name != "nt":
        return None
    user32 = ctypes.windll.user32
    desktop = user32.OpenInputDesktop(0, False, 0)
    if not desktop:
        return True
    user32.CloseDesktop(desktop)
    return False


def retry_delay(failures: int, initial: float, maximum: float) -> float:
    base = min(float(maximum), float(initial) * max(1, int(failures)))
    return base + random.uniform(0, min(1.0, base * 0.2))


def heartbeat_due(last_at: float | None, now: float) -> bool:
    return last_at is None or now - last_at >= WORKER_HEARTBEAT_INTERVAL_SEC


def sleep_with_gap_log(delay: float, label: str) -> None:
    started = time.time()
    time.sleep(delay)
    elapsed = time.time() - started
    if elapsed > delay + SLEEP_GAP_LOG_THRESHOLD_SEC:
        log.info("%s resumed after %.1fs sleep gap; scheduled=%.1fs", label, elapsed, delay)


__all__ = [
    "heartbeat_due",
    "is_locked",
    "run_activity_loop",
    "snapshot_payload",
]
