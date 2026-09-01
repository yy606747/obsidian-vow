"""Non-activating desktop summon button with an off-thread HTTP boundary."""

from __future__ import annotations

import ctypes
import json
import logging
import math
import os
import threading
import time
import uuid
from concurrent.futures import Future, ThreadPoolExecutor
from pathlib import Path
from typing import Any, Callable


BUTTON_SIZE_DIP = 28
DEFAULT_MARGIN_DIP = 24
DRAG_THRESHOLD_DIP = 4.0
CLICK_DEBOUNCE_SEC = 1.5
HOTKEY_ID = 0x4F56
WM_HOTKEY = 0x0312

# Appearance. The widget footprint stays 28 DIP because position clamping and
# the saved state depend on it; the cat is smaller and the rest is glow room.
#
# The mark and its whole visual language come from the product's own launcher
# icon (AionApp/.../ic_launcher_foreground.png): a dark silhouette with a warm
# rim light and an outward bloom, not a shaded solid. Colours are the web UI's
# tokens (aion-chat/static/common.css:3-17) — --accent #d4943a for the light,
# --accent-glow for the bloom, and a body just above --bg #1a1714. The launcher
# icon's own pair of cats turns to mush below ~40 px, so this is one cat drawn
# for this size rather than that artwork scaled down.
GLOW_PASSES = 6
GLOW_MAX_WIDTH_DIP = 7.0
RIM_WIDTH_DIP = 1.0
BODY_COLOR = (20, 16, 14)
ACCENT_COLOR = (212, 148, 58)
BREATH_PERIOD_SEC = 6.0
ANIMATION_INTERVAL_MS = 100
IDLE_OPACITY = 0.70
HOVER_OPACITY = 1.0

log = logging.getLogger("pc_agent")


class SummonRequestDispatcher:
    """Debounce locally and perform every accepted POST outside the caller."""

    def __init__(
        self,
        *,
        endpoint: str,
        token: str,
        post_json: Callable[..., dict[str, Any] | None],
        executor: ThreadPoolExecutor | None = None,
        now: Callable[[], float] = time.monotonic,
        id_factory: Callable[[], Any] = uuid.uuid4,
    ):
        self.endpoint = str(endpoint or "").strip()
        self.token = str(token or "").strip()
        self.post_json = post_json
        self.executor = executor or ThreadPoolExecutor(
            max_workers=2,
            thread_name_prefix="presence-summon",
        )
        self._owns_executor = executor is None
        self._now = now
        self._id_factory = id_factory
        self._last_accepted_at: float | None = None
        self._lock = threading.Lock()

    def dispatch(self, callback: Callable[[dict[str, Any]], None]) -> bool:
        current = float(self._now())
        with self._lock:
            if (
                self._last_accepted_at is not None
                and current - self._last_accepted_at < CLICK_DEBOUNCE_SEC
            ):
                return False
            self._last_accepted_at = current
        summon_id = str(self._id_factory())
        future = self.executor.submit(self._post, summon_id)
        future.add_done_callback(
            lambda completed: callback(self._completed(completed, summon_id))
        )
        return True

    def _post(self, summon_id: str) -> dict[str, Any] | None:
        return self.post_json(
            self.endpoint,
            {"summon_id": summon_id, "device_id": "pc"},
            token=self.token,
            timeout=10,
        )

    @staticmethod
    def _completed(future: Future, summon_id: str) -> dict[str, Any]:
        try:
            response = future.result()
            accepted = bool(
                isinstance(response, dict)
                and response.get("accepted") is True
                and str(response.get("summon_id") or "") == summon_id
            )
            if not accepted:
                return {
                    "ok": False,
                    "summon_id": summon_id,
                    "error": "summon_response_invalid",
                }
            return {"ok": True, "summon_id": summon_id}
        except Exception as exc:
            return {
                "ok": False,
                "summon_id": summon_id,
                "error": f"{type(exc).__name__}:{exc}"[:240],
            }

    def shutdown(self) -> None:
        if self._owns_executor:
            self.executor.shutdown(wait=False, cancel_futures=False)


def load_button_state(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8-sig"))
    except (OSError, ValueError, TypeError):
        return {}
    if not isinstance(value, dict):
        return {}
    try:
        return {
            "screen_name": str(value.get("screen_name") or "")[:200],
            "x": int(value["x"]),
            "y": int(value["y"]),
        }
    except (KeyError, TypeError, ValueError):
        return {}


def save_button_state(path: Path, state: dict[str, Any]) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "screen_name": str(state.get("screen_name") or "")[:200],
        "x": int(state["x"]),
        "y": int(state["y"]),
    }
    temporary = target.with_name(target.name + ".tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    temporary.replace(target)


def clamp_position(
    x: int,
    y: int,
    geometry: tuple[int, int, int, int],
    *,
    size: int = BUTTON_SIZE_DIP,
) -> tuple[int, int]:
    left, top, width, height = (int(value) for value in geometry)
    right = max(left, left + width - int(size))
    bottom = max(top, top + height - int(size))
    return min(right, max(left, int(x))), min(bottom, max(top, int(y)))


def default_position(
    geometry: tuple[int, int, int, int],
    *,
    size: int = BUTTON_SIZE_DIP,
    margin: int = DEFAULT_MARGIN_DIP,
) -> tuple[int, int]:
    left, top, width, height = (int(value) for value in geometry)
    return clamp_position(
        left + width - size - margin,
        top + height - size - margin,
        geometry,
        size=size,
    )


def parse_hotkey(value: str) -> tuple[int, int]:
    parts = [part.strip().casefold() for part in str(value or "").split("+")]
    parts = [part for part in parts if part]
    if not parts:
        raise ValueError("summon_hotkey_empty")
    modifier_map = {
        "alt": 0x0001,
        "ctrl": 0x0002,
        "control": 0x0002,
        "shift": 0x0004,
        "win": 0x0008,
        "windows": 0x0008,
    }
    modifiers = 0
    key_name = ""
    for part in parts:
        if part in modifier_map:
            modifiers |= modifier_map[part]
        elif not key_name:
            key_name = part
        else:
            raise ValueError("summon_hotkey_multiple_keys")
    if not key_name:
        raise ValueError("summon_hotkey_key_required")
    if len(key_name) == 1 and key_name.isascii() and key_name.isalnum():
        virtual_key = ord(key_name.upper())
    elif key_name.startswith("f") and key_name[1:].isdigit():
        number = int(key_name[1:])
        if not 1 <= number <= 24:
            raise ValueError("summon_hotkey_function_key_invalid")
        virtual_key = 0x70 + number - 1
    else:
        raise ValueError("summon_hotkey_key_invalid")
    return modifiers | 0x4000, virtual_key  # MOD_NOREPEAT


try:
    from PySide6.QtCore import (
        QAbstractNativeEventFilter,
        QObject,
        QPoint,
        QPointF,
        QRectF,
        Qt,
        QTimer,
        Signal,
        Slot,
    )
    from PySide6.QtGui import (
        QColor,
        QGuiApplication,
        QPainter,
        QPainterPath,
        QPainterPathStroker,
        QPen,
        QPolygonF,
    )
    from PySide6.QtWidgets import QStyle, QSystemTrayIcon, QWidget

    PYSIDE_AVAILABLE = True
except ImportError:  # Linux/unit-test hosts do not install the Windows GUI.
    PYSIDE_AVAILABLE = False


if PYSIDE_AVAILABLE:
    class _Bridge(QObject):
        finished = Signal(object)
        hotkey_pressed = Signal()


    class SummonButton(QWidget):
        def __init__(self, controller: "SummonButtonController"):
            super().__init__(None)
            self.controller = controller
            self._pressed = False
            self._dragging = False
            self._press_global = QPoint()
            self._press_window = QPoint()
            self._phase = 0.0
            self._target_hover = False
            self._hover = 0.0
            self._press = 0.0
            self._pulse = 0.0
            self._applied_opacity = -1.0
            self.setFixedSize(BUTTON_SIZE_DIP, BUTTON_SIZE_DIP)
            self.setWindowFlags(
                Qt.WindowType.FramelessWindowHint
                | Qt.WindowType.WindowStaysOnTopHint
                | Qt.WindowType.Tool
                | Qt.WindowType.WindowDoesNotAcceptFocus
            )
            self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
            self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, True)
            self.setAttribute(Qt.WidgetAttribute.WA_Hover, True)
            self.setToolTip("")
            self.setWindowOpacity(IDLE_OPACITY)
            self._animation = QTimer(self)
            self._animation.setInterval(ANIMATION_INTERVAL_MS)
            self._animation.timeout.connect(self._advance)
            self._animation.start()

        def pulse(self) -> None:
            """Acknowledge a trigger that did not come from a mouse press."""

            self._pulse = 1.0

        def stop_animation(self) -> None:
            self._animation.stop()

        def enterEvent(self, event) -> None:
            self._target_hover = True
            super().enterEvent(event)

        def leaveEvent(self, event) -> None:
            self._target_hover = False
            super().leaveEvent(event)

        @Slot()
        def _advance(self) -> None:
            step = ANIMATION_INTERVAL_MS / 1000.0
            self._phase = (self._phase + step / BREATH_PERIOD_SEC) % 1.0
            hover_target = 1.0 if self._target_hover else 0.0
            self._hover += (hover_target - self._hover) * 0.35
            self._press += ((1.0 if self._pressed else 0.0) - self._press) * 0.45
            self._pulse = max(0.0, self._pulse - step / 0.45)
            lit = max(self._hover, self._press, self._pulse)
            opacity = IDLE_OPACITY + (HOVER_OPACITY - IDLE_OPACITY) * lit
            # setWindowOpacity is a window-manager call on every platform; only
            # pay for it when the value actually moved.
            if abs(opacity - self._applied_opacity) > 0.01:
                self._applied_opacity = opacity
                self.setWindowOpacity(opacity)
            self.update()

        def mousePressEvent(self, event) -> None:
            if event.button() != Qt.MouseButton.LeftButton:
                return
            self._pressed = True
            self._dragging = False
            self._press_global = event.globalPosition().toPoint()
            self._press_window = self.pos()
            self.update()
            event.accept()

        def mouseMoveEvent(self, event) -> None:
            if not self._pressed:
                return
            delta = event.globalPosition().toPoint() - self._press_global
            distance = (delta.x() ** 2 + delta.y() ** 2) ** 0.5
            if distance > DRAG_THRESHOLD_DIP:
                self._dragging = True
            if self._dragging:
                self.move(self._press_window + delta)
            event.accept()

        def mouseReleaseEvent(self, event) -> None:
            if event.button() != Qt.MouseButton.LeftButton or not self._pressed:
                return
            dragged = self._dragging
            self._pressed = False
            self._dragging = False
            self.update()
            if dragged:
                self.controller.clamp_and_save_position()
            else:
                self.controller.trigger()
            event.accept()

        def _silhouette(self) -> "QPainterPath":
            """One closed outline for the whole cat, built once in 28 DIP space."""

            cached = getattr(self, "_cached_path", None)
            if cached is not None:
                return cached

            def point(x: float, y: float) -> QPointF:
                return QPointF(x, y)

            def closed(points) -> QPainterPath:
                path = QPainterPath()
                path.addPolygon(QPolygonF(points))
                path.closeSubpath()
                return path

            haunch = QPainterPath()
            haunch.addEllipse(QRectF(point(7.6, 14.6), point(20.4, 24.8)))
            head = QPainterPath()
            head.addEllipse(QRectF(point(9.4, 4.9), point(18.6, 13.4)))
            torso = closed([
                point(10.6, 10.0), point(17.4, 10.0),
                point(19.6, 20.0), point(8.4, 20.0),
            ])
            left_ear = closed([point(10.5, 6.6), point(9.9, 2.6), point(13.3, 5.0)])
            right_ear = closed([point(17.5, 6.6), point(18.1, 2.6), point(14.7, 5.0)])

            tail = QPainterPath()
            tail.moveTo(point(19.4, 23.2))
            tail.quadTo(point(24.6, 22.0), point(23.4, 15.0))
            stroker = QPainterPathStroker()
            stroker.setWidth(1.8)
            stroker.setCapStyle(Qt.PenCapStyle.RoundCap)
            stroker.setJoinStyle(Qt.PenJoinStyle.RoundJoin)

            path = haunch
            for part in (torso, head, left_ear, right_ear, stroker.createStroke(tail)):
                path = path.united(part)
            self._cached_path = path
            return path

        def paintEvent(self, _event) -> None:
            painter = QPainter(self)
            painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
            breath = 0.5 - 0.5 * math.cos(2.0 * math.pi * self._phase)
            lit = max(self._hover, self._pulse)
            path = self._silhouette()

            painter.save()
            if self._press > 0.0:
                center = QPointF(14.0, 14.0)
                scale = 1.0 - 0.07 * self._press
                painter.translate(center)
                painter.scale(scale, scale)
                painter.translate(-center)

            # Bloom: repeated strokes of the silhouette, widest and faintest
            # first. The inner half is covered by the body fill below, so this
            # reads as an outward glow. QGraphicsEffect is avoided on purpose —
            # it is unreliable on a translucent frameless top-level window.
            glow_alpha = 30.0 + 38.0 * breath + 92.0 * lit + 58.0 * self._press
            width_scale = 0.72 + 0.28 * breath - 0.20 * lit
            painter.setBrush(Qt.BrushStyle.NoBrush)
            for index in range(GLOW_PASSES, 0, -1):
                width = GLOW_MAX_WIDTH_DIP * width_scale * index / GLOW_PASSES
                alpha = glow_alpha / (index * 1.55)
                pen = QPen(QColor(*ACCENT_COLOR, int(min(255.0, alpha))), width)
                pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
                painter.setPen(pen)
                painter.drawPath(path)

            painter.fillPath(path, QColor(*BODY_COLOR))

            rim = tuple(min(255, value + int(26 * lit)) for value in ACCENT_COLOR)
            painter.setPen(
                QPen(QColor(*rim, int(195 + 55 * lit)), RIM_WIDTH_DIP)
            )
            painter.drawPath(path)
            painter.restore()
            painter.end()


    class _WindowsHotkeyFilter(QAbstractNativeEventFilter):
        def __init__(self, callback: Callable[[], None]):
            super().__init__()
            self.callback = callback

        def nativeEventFilter(self, event_type, message):
            if os.name == "nt" and b"windows" in bytes(event_type):
                try:
                    from ctypes import wintypes

                    native = ctypes.cast(
                        int(message),
                        ctypes.POINTER(wintypes.MSG),
                    ).contents
                    if native.message == WM_HOTKEY and native.wParam == HOTKEY_ID:
                        self.callback()
                        return True, 0
                except Exception:
                    pass
            return False, 0


    class SummonButtonController(QObject):
        def __init__(
            self,
            *,
            server_url: str,
            token: str,
            state_path: Path,
            post_json: Callable[..., dict[str, Any] | None],
            hotkey: str = "",
        ):
            super().__init__()
            self.state_path = Path(state_path)
            self.hotkey = str(hotkey or "").strip()
            self.dispatcher = SummonRequestDispatcher(
                endpoint=f"{str(server_url).rstrip('/')}/api/presence/summon",
                token=token,
                post_json=post_json,
            )
            self.bridge = _Bridge()
            self.bridge.finished.connect(self._request_finished)
            self.bridge.hotkey_pressed.connect(self.trigger)
            self.button = SummonButton(self)
            self._tray: QSystemTrayIcon | None = None
            self._hotkey_filter: _WindowsHotkeyFilter | None = None
            self._hotkey_registered = False

        def start(self, application) -> None:
            self._restore_position()
            self.button.show()
            self.button.raise_()
            self._install_hotkey(application)
            application.aboutToQuit.connect(self.shutdown)

        @Slot()
        def trigger(self) -> None:
            if self.dispatcher.dispatch(self.bridge.finished.emit):
                # The mouse path already shows a press; the hotkey path would
                # otherwise fire with no sign that anything happened.
                self.button.pulse()

        @Slot(object)
        def _request_finished(self, result: dict[str, Any]) -> None:
            if bool(result.get("ok")):
                return
            log.warning("Presence summon POST failed: %s", result.get("error"))
            self._notify_failure()

        def _notify_failure(self) -> None:
            if self._tray is None:
                warning_icon = self.button.style().standardIcon(
                    QStyle.StandardPixmap.SP_MessageBoxWarning
                )
                self._tray = QSystemTrayIcon(warning_icon, self.button)
            self._tray.show()
            self._tray.showMessage(
                "Obsidian Vow",
                "没送出去",
                QSystemTrayIcon.MessageIcon.Warning,
                3500,
            )
            QTimer.singleShot(4000, self._tray.hide)

        def _screens(self):
            return list(QGuiApplication.screens())

        @staticmethod
        def _geometry(screen) -> tuple[int, int, int, int]:
            rect = screen.availableGeometry()
            return rect.x(), rect.y(), rect.width(), rect.height()

        def _restore_position(self) -> None:
            screens = self._screens()
            if not screens:
                return
            state = load_button_state(self.state_path)
            screen = next(
                (
                    item for item in screens
                    if item.name() == state.get("screen_name")
                ),
                QGuiApplication.primaryScreen() or screens[0],
            )
            geometry = self._geometry(screen)
            if state:
                position = clamp_position(state["x"], state["y"], geometry)
            else:
                position = default_position(geometry)
            self.button.move(*position)

        def clamp_and_save_position(self) -> None:
            screens = self._screens()
            if not screens:
                return
            center = self.button.frameGeometry().center()
            screen = QGuiApplication.screenAt(center)
            if screen is None:
                screen = QGuiApplication.primaryScreen() or screens[0]
            x, y = clamp_position(
                self.button.x(),
                self.button.y(),
                self._geometry(screen),
            )
            self.button.move(x, y)
            try:
                save_button_state(
                    self.state_path,
                    {"screen_name": screen.name(), "x": x, "y": y},
                )
            except OSError as exc:
                log.warning("summon button position save failed: %s", exc)

        def _install_hotkey(self, application) -> None:
            if not self.hotkey:
                return
            if os.name != "nt":
                log.info("summon hotkey unsupported on %s", os.name)
                return
            try:
                modifiers, virtual_key = parse_hotkey(self.hotkey)
                registered = bool(
                    ctypes.windll.user32.RegisterHotKey(
                        None,
                        HOTKEY_ID,
                        modifiers,
                        virtual_key,
                    )
                )
                if not registered:
                    log.warning("summon hotkey registration failed: %s", self.hotkey)
                    return
                self._hotkey_filter = _WindowsHotkeyFilter(
                    self.bridge.hotkey_pressed.emit
                )
                application.installNativeEventFilter(self._hotkey_filter)
                self._hotkey_registered = True
                log.info("summon hotkey registered: %s", self.hotkey)
            except Exception as exc:
                log.warning(
                    "summon hotkey registration failed: %s: %s",
                    type(exc).__name__,
                    exc,
                )

        @Slot()
        def shutdown(self) -> None:
            self.button.stop_animation()
            if self._hotkey_registered and os.name == "nt":
                try:
                    ctypes.windll.user32.UnregisterHotKey(None, HOTKEY_ID)
                except Exception:
                    pass
                self._hotkey_registered = False
            self.dispatcher.shutdown()


else:
    class SummonButtonController:  # pragma: no cover - Windows-only runtime guard
        def __init__(self, **_kwargs):
            raise RuntimeError("PySide6 is required for SummonButtonController")


__all__ = [
    "BUTTON_SIZE_DIP",
    "CLICK_DEBOUNCE_SEC",
    "DRAG_THRESHOLD_DIP",
    "SummonButtonController",
    "SummonRequestDispatcher",
    "clamp_position",
    "default_position",
    "load_button_state",
    "parse_hotkey",
    "save_button_state",
]
