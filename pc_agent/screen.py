"""Local screen confirmation and capture helpers."""

from __future__ import annotations

import ctypes
import io
import logging
import os
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from PIL import Image, ImageGrab

try:
    from PySide6.QtCore import QObject, QTimer, Qt, Signal, Slot
    from PySide6.QtGui import QCursor, QFont, QGuiApplication
    from PySide6.QtWidgets import (
        QDialog,
        QFrame,
        QHBoxLayout,
        QLabel,
        QPushButton,
        QVBoxLayout,
        QWidget,
    )
except ImportError:  # Headless protocol tests do not install the Windows GUI stack.
    QObject = None


CONFIRM_WAIT_GRACE_SEC = 10
log = logging.getLogger("pc_agent")


SCREEN_CAPTURE_HARD_BLOCKLIST = frozenset({
    "1password.exe", "bitwarden.exe", "keepass.exe", "keepassxc.exe",
    "lastpass.exe", "lockapp.exe", "logonui.exe", "credentialproviderhost.exe",
    "alipay.exe",
})


def local_reject_reason() -> str:
    if _is_locked() is True:
        return "locked"
    if _foreground_process().lower() in SCREEN_CAPTURE_HARD_BLOCKLIST:
        return "hard_blocked"
    return ""


@dataclass(frozen=True)
class ScreenConfirmState:
    done: bool
    allowed: bool
    timed_out: bool
    remaining: int


class ScreenConfirmRequest:
    """Thread-safe handoff state shared by the poll worker and Qt thread."""

    def __init__(self, reason: str, timeout: int, ai_name: str):
        self.reason = " ".join(str(reason or "").split())[:72]
        self.ai_name = " ".join(str(ai_name or "AI").split())[:24] or "AI"
        self.timeout = max(1, int(timeout))
        self.event = threading.Event()
        self._lock = threading.Lock()
        self._done = False
        self._allowed = False
        self._timed_out = False
        self._remaining = self.timeout

    def snapshot(self) -> ScreenConfirmState:
        with self._lock:
            return self._snapshot_locked()

    def finish(self, allowed: bool, *, timed_out: bool = False) -> bool:
        """Set the single terminal result; late clicks can never overwrite it."""
        with self._lock:
            if self._done:
                return False
            self._done = True
            self._allowed = bool(allowed)
            self._timed_out = bool(timed_out)
        self.event.set()
        return True

    def advance_countdown(self) -> ScreenConfirmState:
        notify = False
        with self._lock:
            if not self._done:
                self._remaining = max(0, self._remaining - 1)
                if self._remaining == 0:
                    self._done = True
                    self._allowed = False
                    self._timed_out = True
                    notify = True
            state = self._snapshot_locked()
        if notify:
            self.event.set()
        return state

    def _snapshot_locked(self) -> ScreenConfirmState:
        return ScreenConfirmState(
            done=self._done,
            allowed=self._allowed,
            timed_out=self._timed_out,
            remaining=self._remaining,
        )


_confirm_controller: Any = None


def install_confirm_controller(controller: Any) -> None:
    global _confirm_controller
    if controller is None or not callable(getattr(controller, "submit", None)):
        raise TypeError("screen confirm controller must provide submit(request)")
    _confirm_controller = controller


def show_confirm_dialog(reason: str, timeout: int = 30, ai_name: str = "AI") -> bool:
    """Ask on the Qt main thread while synchronously serving the poll worker."""
    show_confirm_dialog.timed_out = False
    controller = _confirm_controller
    if controller is None:
        raise RuntimeError("screen_confirm_controller_unavailable")
    request = ScreenConfirmRequest(reason, timeout, ai_name)
    controller.submit(request)
    if not request.event.wait(request.timeout + CONFIRM_WAIT_GRACE_SEC):
        if request.finish(False, timed_out=True):
            log.warning(
                "screen confirm main-thread wait exceeded %ss; rejecting",
                request.timeout + CONFIRM_WAIT_GRACE_SEC,
            )
    state = request.snapshot()
    show_confirm_dialog.timed_out = state.timed_out
    return state.allowed


show_confirm_dialog.timed_out = False


if QObject is not None:

    class _ScreenConfirmDialog(QDialog):
        completed = Signal(object)

        def __init__(self, request: ScreenConfirmRequest):
            super().__init__(None)
            self.request = request
            self._completion_emitted = False
            self._timer = QTimer(self)
            self._timer.setInterval(1_000)
            self._timer.timeout.connect(self._tick)
            self._timer_label: QLabel
            self._build_ui()

        def _build_ui(self) -> None:
            self.setWindowTitle("Obsidian Vow")
            self.setWindowFlags(
                Qt.WindowType.FramelessWindowHint
                | Qt.WindowType.WindowStaysOnTopHint
                | Qt.WindowType.Tool
            )
            self.setWindowModality(Qt.WindowModality.NonModal)
            self.setFixedSize(440, 290)
            self.setStyleSheet("QDialog { background: #2a2a40; }")

            outer = QVBoxLayout(self)
            outer.setContentsMargins(1, 1, 1, 1)
            outer.setSpacing(0)
            panel = QWidget(self)
            panel.setStyleSheet("background: #0f0f1a;")
            outer.addWidget(panel)

            layout = QVBoxLayout(panel)
            layout.setContentsMargins(0, 0, 0, 0)
            layout.setSpacing(0)
            layout.addSpacing(14)

            brand = QLabel("◆ Obsidian Vow", panel)
            brand.setContentsMargins(22, 0, 0, 0)
            brand.setFont(QFont("Segoe UI", 9))
            brand.setStyleSheet("color: #8888a8;")
            layout.addWidget(brand)
            layout.addSpacing(10)

            separator = QFrame(panel)
            separator.setFixedHeight(1)
            separator.setStyleSheet("background: #2a2a40;")
            layout.addWidget(separator)
            layout.addSpacing(24)

            title = QLabel(f"{self.request.ai_name} 请求查看你的屏幕", panel)
            title_font = QFont("Segoe UI", 16)
            title_font.setBold(True)
            title.setFont(title_font)
            title.setAlignment(Qt.AlignmentFlag.AlignCenter)
            title.setStyleSheet("color: #e8e8f0;")
            layout.addWidget(title)
            layout.addSpacing(10)

            reason = QLabel(self.request.reason, panel)
            reason.setFixedHeight(44)
            reason.setWordWrap(True)
            reason.setAlignment(Qt.AlignmentFlag.AlignCenter)
            reason.setFont(QFont("Segoe UI", 10))
            reason.setStyleSheet("color: #8888a8;")
            reason.setContentsMargins(28, 0, 28, 0)
            layout.addWidget(reason)
            layout.addSpacing(12)

            self._timer_label = QLabel(
                f"⏱  {self.request.timeout}s",
                panel,
            )
            self._timer_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
            self._timer_label.setFont(QFont("Segoe UI", 10))
            self._set_timer_color(self.request.timeout)
            layout.addWidget(self._timer_label)
            layout.addSpacing(18)

            buttons = QHBoxLayout()
            buttons.setContentsMargins(54, 0, 54, 0)
            buttons.setSpacing(16)
            buttons.addWidget(
                self._button(
                    "允许查看",
                    "#6c5ce7",
                    "#7d6cff",
                    "#ffffff",
                    lambda: self._finish(True),
                )
            )
            buttons.addWidget(
                self._button(
                    "拒绝",
                    "#2d2d44",
                    "#3a3a55",
                    "#c0c0d0",
                    lambda: self._finish(False),
                )
            )
            layout.addLayout(buttons)
            layout.addStretch(1)

        def _button(
            self,
            text: str,
            background: str,
            hover: str,
            foreground: str,
            callback,
        ) -> QPushButton:
            button = QPushButton(text, self)
            button.setFixedSize(150, 38)
            button.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
            button.setFont(QFont("Segoe UI", 11))
            button.setStyleSheet(
                "QPushButton {"
                f"background: {background}; color: {foreground};"
                "border: 0; border-radius: 10px;"
                "}"
                f"QPushButton:hover {{ background: {hover}; }}"
            )
            button.clicked.connect(callback)
            return button

        def begin(self) -> None:
            active_screen = QGuiApplication.screenAt(QCursor.pos())
            active_screen = active_screen or QGuiApplication.primaryScreen()
            if active_screen is not None:
                geometry = active_screen.availableGeometry()
                self.move(
                    geometry.x() + (geometry.width() - self.width()) // 2,
                    geometry.y() + (geometry.height() - self.height()) // 2,
                )
            self.show()
            self.raise_()
            self.activateWindow()
            self._timer.start()

        def _tick(self) -> None:
            state = self.request.advance_countdown()
            if state.done:
                self.close()
                return
            self._timer_label.setText(f"⏱  {state.remaining}s")
            self._set_timer_color(state.remaining)

        def _set_timer_color(self, remaining: int) -> None:
            color = "#e8715a" if remaining <= 8 else "#8888a8"
            self._timer_label.setStyleSheet(f"color: {color};")

        def _finish(self, allowed: bool) -> None:
            self.request.finish(allowed)
            self.close()

        def keyPressEvent(self, event) -> None:
            if event.key() == Qt.Key.Key_Escape:
                self._finish(False)
                return
            super().keyPressEvent(event)

        def closeEvent(self, event) -> None:
            self._timer.stop()
            self.request.finish(False)
            if not self._completion_emitted:
                self._completion_emitted = True
                self.completed.emit(self)
            event.accept()


    class ScreenConfirmController(QObject):
        requested = Signal(object)

        def __init__(self):
            super().__init__(None)
            self._active: _ScreenConfirmDialog | None = None
            self.requested.connect(
                self._show_requested,
                Qt.ConnectionType.QueuedConnection,
            )

        def submit(self, request: ScreenConfirmRequest) -> None:
            self.requested.emit(request)

        @Slot(object)
        def _show_requested(self, request: ScreenConfirmRequest) -> None:
            if request.snapshot().done:
                return
            if self._active is not None:
                request.finish(False)
                log.warning("screen confirm rejected because another dialog is active")
                return
            dialog = _ScreenConfirmDialog(request)
            dialog.completed.connect(self._dialog_completed)
            self._active = dialog
            dialog.begin()

        @Slot(object)
        def _dialog_completed(self, dialog: _ScreenConfirmDialog) -> None:
            if self._active is dialog:
                self._active = None
            dialog.deleteLater()


else:

    class ScreenConfirmController:
        def __init__(self):
            raise RuntimeError("PySide6 is required for screen confirmation")


def capture_jpeg_bytes(max_edge: int = 1280, quality: int = 70) -> bytes:
    img = ImageGrab.grab().convert("RGB")
    img.thumbnail((max_edge, max_edge), Image.Resampling.LANCZOS)
    out = io.BytesIO()
    img.save(out, format="JPEG", quality=quality, optimize=True)
    return out.getvalue()

def post_confirm_delay() -> None:
    time.sleep(0.4)

def _foreground_process() -> str:
    if os.name != "nt":
        return ""
    try:
        user32 = ctypes.windll.user32
        hwnd = user32.GetForegroundWindow()
        if not hwnd:
            return ""
        pid = ctypes.c_ulong()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        return _process_name_from_pid(int(pid.value))
    except Exception:
        return ""

def _process_name_from_pid(pid: int) -> str:
    kernel32 = ctypes.windll.kernel32
    handle = kernel32.OpenProcess(0x1000, False, int(pid))
    if not handle:
        return ""
    try:
        buf = ctypes.create_unicode_buffer(1024)
        size = ctypes.c_uint(len(buf))
        if not kernel32.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size)):
            return ""
        return Path(buf.value).name
    finally:
        kernel32.CloseHandle(handle)

def _is_locked() -> bool | None:
    if os.name != "nt":
        return None
    user32 = ctypes.windll.user32
    desktop = user32.OpenInputDesktop(0, False, 0)
    if not desktop:
        return True
    user32.CloseDesktop(desktop)
    return False
