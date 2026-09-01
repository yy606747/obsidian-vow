"""PySide6 transparent, click-through, one-shot Presence player."""

from __future__ import annotations

import ctypes
import logging
import os
import time
from ctypes import wintypes
from pathlib import Path
from queue import Queue
from typing import Any, Callable

from PySide6.QtCore import QPoint, QRectF, Qt, QTimer, Signal, Slot
from PySide6.QtGui import QGuiApplication, QImage, QPainter
from PySide6.QtWidgets import QWidget

from presence_protocol import (
    AckJournal,
    PresenceContractError,
    analyze_trajectory_geometry,
    playback_interruption_reason,
    playback_tick_interval_ms,
    rendered_trajectory_values,
    trajectory_intervals,
    validate_trajectory,
)


log = logging.getLogger("pc_agent")


def _active_screen():
    if os.name == "nt":
        try:
            user32 = ctypes.windll.user32
            user32.GetForegroundWindow.restype = wintypes.HWND
            user32.GetWindowRect.argtypes = [
                wintypes.HWND,
                ctypes.POINTER(wintypes.RECT),
            ]
            user32.GetWindowRect.restype = wintypes.BOOL
            hwnd = user32.GetForegroundWindow()
            rect = wintypes.RECT()
            if hwnd and user32.GetWindowRect(hwnd, ctypes.byref(rect)):
                point = QPoint((rect.left + rect.right) // 2, (rect.top + rect.bottom) // 2)
                selected = QGuiApplication.screenAt(point)
                if selected is not None:
                    return selected
        except Exception:
            pass
    return QGuiApplication.primaryScreen()


def _select_screen(target: str):
    return _active_screen() if target == "active" else QGuiApplication.primaryScreen()


def _axis_point(name: str, extent: float) -> float:
    if name in {"left", "top"}:
        return 0.0
    if name in {"right", "bottom"}:
        return extent
    return extent / 2.0


def _anchor_point(anchor: str, width: float, height: float) -> tuple[float, float]:
    vertical, horizontal = ("center", "center") if anchor == "center" else anchor.split("_", 1)
    return _axis_point(horizontal, width), _axis_point(vertical, height)


def _origin_point(origin: str, width: float, height: float) -> tuple[float, float]:
    if origin == "center":
        return width / 2.0, height / 2.0
    return width / 2.0, 0.0 if origin == "top_center" else height


class PresenceWindow(QWidget):
    finished = Signal(object)

    def __init__(
        self,
        request: dict[str, Any],
        screen,
        lock_checker: Callable[[], bool | None],
    ):
        super().__init__(None)
        self.request = request
        self.trajectory = validate_trajectory(request["trajectory"])
        self.event_id = str(request["event_id"])
        self.image = QImage(str(request["sprite_path"]))
        if self.image.isNull():
            raise PresenceContractError("sprite:image_load_failed")
        self.base_height = float(request["base_height_dip"])
        self._lock_checker = lock_checker
        geometry = screen.availableGeometry()
        self._work_width = float(geometry.width())
        self._work_height = float(geometry.height())
        geometry_analysis = analyze_trajectory_geometry(
            self.trajectory,
            work_width=self._work_width,
            work_height=self._work_height,
            image_width_px=self.image.width(),
            image_height_px=self.image.height(),
            base_height_dip=self.base_height,
        )
        left, top, right, bottom = geometry_analysis.bounds
        self._local_idle_enabled = geometry_analysis.local_idle_enabled
        self._intervals = trajectory_intervals(self.trajectory)
        self._window_left = float(left)
        self._window_top = float(top)
        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.Tool
            | Qt.WindowType.WindowTransparentForInput
            | Qt.WindowType.WindowDoesNotAcceptFocus
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, True)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self.setGeometry(
            geometry.x() + left,
            geometry.y() + top,
            right - left,
            bottom - top,
        )
        self._started_at = 0.0
        self._play_deadline = 0.0
        self._last_tick_at = 0.0
        self._terminal_sent = False
        self._timer = QTimer(self)
        self._timer.setTimerType(Qt.TimerType.PreciseTimer)
        self._timer.setInterval(
            playback_tick_interval_ms(
                self.trajectory,
                0.0,
                intervals=self._intervals,
            )
        )
        self._timer.timeout.connect(self._tick)

    def begin(self) -> None:
        self.show()
        self._apply_windows_flags()
        self._started_at = time.monotonic()
        self._play_deadline = self._started_at + self.trajectory["duration_ms"] / 1000.0
        self._last_tick_at = self._started_at
        self._timer.start()
        self.update()

    def _apply_windows_flags(self) -> None:
        if os.name != "nt":
            return
        try:
            user32 = ctypes.windll.user32
            hwnd = wintypes.HWND(int(self.winId()))
            get_style = getattr(user32, "GetWindowLongPtrW", None)
            set_style = getattr(user32, "SetWindowLongPtrW", None)
            if get_style is None or set_style is None:
                get_style = user32.GetWindowLongW
                set_style = user32.SetWindowLongW
            get_style.argtypes = [wintypes.HWND, ctypes.c_int]
            get_style.restype = ctypes.c_ssize_t
            set_style.argtypes = [wintypes.HWND, ctypes.c_int, ctypes.c_ssize_t]
            set_style.restype = ctypes.c_ssize_t
            user32.SetWindowPos.argtypes = [
                wintypes.HWND,
                wintypes.HWND,
                ctypes.c_int,
                ctypes.c_int,
                ctypes.c_int,
                ctypes.c_int,
                ctypes.c_uint,
            ]
            user32.SetWindowPos.restype = wintypes.BOOL
            ex_style = get_style(hwnd, -20)
            set_style(hwnd, -20, ex_style | 0x20 | 0x08000000 | 0x80 | 0x80000)
            user32.SetWindowPos(
                hwnd,
                wintypes.HWND(-1),
                0,
                0,
                0,
                0,
                0x0001 | 0x0002 | 0x0010,
            )
        except Exception as exc:
            log.warning(
                "Presence native window flags failed: %s: %s",
                type(exc).__name__,
                exc,
            )

    def _tick(self) -> None:
        now = time.monotonic()
        try:
            locked = self._lock_checker()
        except Exception:
            locked = None
        interruption = playback_interruption_reason(
            locked=locked,
            last_tick_at=self._last_tick_at,
            now=now,
        )
        self._last_tick_at = now
        if interruption:
            self._finish("expired", reason=interruption)
            return
        if now >= self._play_deadline:
            elapsed_ms = max(0, int(round((now - self._started_at) * 1000)))
            self._finish("played", actual_playback_ms=elapsed_ms)
            return
        elapsed_ms = (now - self._started_at) * 1000.0
        tick_interval_ms = playback_tick_interval_ms(
            self.trajectory,
            elapsed_ms,
            intervals=self._intervals,
        )
        if self._timer.interval() != tick_interval_ms:
            self._timer.setInterval(tick_interval_ms)
        self.update()

    def paintEvent(self, _event) -> None:
        if not self._started_at:
            return
        elapsed_ms = (time.monotonic() - self._started_at) * 1000.0
        values = rendered_trajectory_values(
            self.trajectory,
            elapsed_ms,
            local_idle_enabled=self._local_idle_enabled,
            intervals=self._intervals,
        )
        image_height = self.base_height
        image_width = image_height * self.image.width() / self.image.height()
        screen_anchor = _anchor_point(
            self.trajectory["anchor"], self._work_width, self._work_height
        )
        sprite_anchor = _anchor_point(self.trajectory["anchor"], image_width, image_height)
        origin = _origin_point(self.trajectory["transform_origin"], image_width, image_height)

        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)
        painter.setOpacity(values["opacity"])
        painter.translate(
            screen_anchor[0] + values["x"] - sprite_anchor[0] - self._window_left,
            screen_anchor[1] + values["y"] - sprite_anchor[1] - self._window_top,
        )
        painter.translate(origin[0], origin[1])
        painter.rotate(values["rotation"])
        painter.scale(values["scale"], values["scale"])
        painter.translate(-origin[0], -origin[1])
        painter.drawImage(QRectF(0.0, 0.0, image_width, image_height), self.image)
        painter.end()

    def closeEvent(self, event) -> None:
        if not self._terminal_sent:
            self._terminal_sent = True
            self._timer.stop()
            self.finished.emit(
                {
                    "event_id": self.event_id,
                    "status": "rejected",
                    "reason": "window_closed",
                    "actual_playback_ms": None,
                }
            )
        event.accept()

    def _finish(
        self,
        status: str,
        *,
        reason: str = "",
        actual_playback_ms: int | None = None,
    ) -> None:
        if self._terminal_sent:
            return
        self._terminal_sent = True
        self._timer.stop()
        self.finished.emit(
            {
                "event_id": self.event_id,
                "status": status,
                "reason": reason,
                "actual_playback_ms": actual_playback_ms,
            }
        )
        self.close()


class PresenceController(QWidget):
    requested = Signal(object)

    def __init__(
        self,
        ack_queue: Queue,
        lock_checker: Callable[[], bool | None],
        journal: AckJournal,
    ):
        super().__init__(None)
        self.ack_queue = ack_queue
        self.lock_checker = lock_checker
        self.journal = journal
        self._active: PresenceWindow | None = None
        self.requested.connect(self._play_requested, Qt.ConnectionType.QueuedConnection)

    def submit(self, request: dict[str, Any]) -> None:
        self.requested.emit(dict(request))

    @Slot(object)
    def _play_requested(self, request: dict[str, Any]) -> None:
        event_id = str(request.get("event_id") or "")
        if self._active is not None:
            self._queue_terminal(event_id, "rejected", "player_busy")
            return
        if time.monotonic() >= float(request.get("local_start_deadline") or 0):
            self._queue_terminal(event_id, "expired", "start_before_elapsed")
            return
        if self.lock_checker() is True:
            self._queue_terminal(event_id, "expired", "locked")
            return
        try:
            screen = _select_screen(str(request["trajectory"].get("target_screen") or ""))
            if screen is None:
                raise PresenceContractError("screen:unavailable")
            window = PresenceWindow(request, screen, self.lock_checker)
            window.finished.connect(self._on_finished)
            self._active = window
            window.begin()
        except Exception as exc:
            self._active = None
            self._queue_terminal(
                event_id, "rejected", str(exc)[:120] or "player_failed"
            )

    @Slot(object)
    def _on_finished(self, ack: dict[str, Any]) -> None:
        self._active = None
        self._persist_and_queue(ack)

    def _queue_terminal(self, event_id: str, status: str, reason: str) -> None:
        self._persist_and_queue(
            {"event_id": event_id, "status": status, "reason": reason, "actual_playback_ms": None}
        )

    def _persist_and_queue(self, ack: dict[str, Any]) -> None:
        payload = dict(ack)
        try:
            # This synchronous atomic replace completes on the GUI thread
            # before the completion callback returns.
            self.journal.record_ack(payload, now=time.time())
        except Exception as exc:
            log.error("Presence terminal ACK persistence failed: %s: %s", type(exc).__name__, exc)
        self.ack_queue.put(payload)


__all__ = ["PresenceController", "PresenceWindow"]
