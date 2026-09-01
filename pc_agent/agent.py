"""Windows PC agent host: Qt on the main thread, polling in workers."""

from __future__ import annotations

import json
import logging
import os
import queue
import sys
import threading
import time
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any

# These remain direct imports because their byte-identical copies are an
# acceptance boundary shared with the backend.
import app_map  # noqa: F401
import privacy  # noqa: F401
from activity_worker import (
    is_locked as _is_locked,
    run_activity_loop as _activity_loop,
    snapshot_payload as _snapshot_payload,
)
from screen_worker import run_screen_poll_loop as _screen_poll_loop
from transport import (
    get_json as _get_json,
    post_file as _post_file,
    post_json as _post_json,
    ssl_context as _ssl_context,
)


DEFAULT_SAMPLE_INTERVAL_SEC = 60
DEFAULT_IDLE_THRESHOLD_SEC = 180
DEFAULT_SCREEN_POLL_TIMEOUT_SEC = 30
DEFAULT_LOG_FILENAME = "pc_agent.log"
MIN_SAMPLE_INTERVAL_SEC = 30
MAX_SAMPLE_INTERVAL_SEC = 120
WORKER_RESTART_INITIAL_SEC = 2.0
WORKER_RESTART_MAX_SEC = 30.0
log = logging.getLogger("pc_agent")


def main() -> None:
    base_dir = _base_dir()
    if not os.environ.get("SSL_CERT_FILE"):
        bundled_ca = base_dir / "cacert.pem"
        if bundled_ca.exists():
            os.environ["SSL_CERT_FILE"] = str(bundled_ca)
    log_path = _configure_logging(base_dir)
    config = _load_config(base_dir)
    interval = _sample_interval(config)
    idle_threshold = int(config.get("idle_threshold_sec") or DEFAULT_IDLE_THRESHOLD_SEC)
    server_url = str(config.get("server_url") or "").rstrip("/")
    token = str(config.get("token") or os.environ.get("AION_AUTH_TOKEN") or "").strip()
    if not server_url:
        raise SystemExit("server_url is required")
    if not token:
        raise SystemExit("token is required; set config token or AION_AUTH_TOKEN")
    try:
        from PySide6.QtWidgets import QApplication
        from presence_player import PresenceController
        from presence_protocol import AckJournal
        from presence_worker import run_presence_loop
        from screen import ScreenConfirmController, install_confirm_controller
    except ImportError as exc:
        raise SystemExit("PySide6 is required; install requirements-windows.txt") from exc

    application = QApplication.instance() or QApplication(sys.argv)
    application.setQuitOnLastWindowClosed(False)
    ack_queue: queue.Queue = queue.Queue()
    presence_journal = AckJournal(base_dir / "presence_ack_journal.json")
    controller = PresenceController(ack_queue, _is_locked, presence_journal)
    screen_confirm_controller = ScreenConfirmController()
    install_confirm_controller(screen_confirm_controller)
    if bool(config.get("summon_button_enabled", False)):
        from summon_button import SummonButtonController
        summon_controller = SummonButtonController(
            server_url=server_url, token=token,
            state_path=base_dir / "summon_button_state.json", post_json=_post_json,
            hotkey=str(config.get("summon_hotkey") or ""))
        summon_controller.start(application)
        log.info("Presence summon button started")
    activity_endpoint = f"{server_url}/api/activity/report"
    _start_worker(
        "pc-activity", _activity_loop, activity_endpoint, token, interval, idle_threshold
    )
    if bool(config.get("screen_poll_enabled", False)):
        screen_timeout = int(config.get("screen_poll_timeout_sec") or DEFAULT_SCREEN_POLL_TIMEOUT_SEC)
        _start_worker(
            "pc-screen-poll",
            _screen_poll_loop,
            server_url,
            token,
            screen_timeout,
        )
        log.info("PC screen poll started; timeout=%ss", screen_timeout)
    if bool(config.get("presence_enabled", True)):
        presence_args = (
            server_url, token, controller, ack_queue, base_dir, _is_locked, presence_journal
        )
        _start_worker("pc-presence-poll", run_presence_loop, *presence_args)
        log.info("Desktop Presence poll and sprite sync started")

    log.info("PC agent started; interval=%ss endpoint=%s", interval, activity_endpoint)
    if log_path:
        log.info("PC agent log file=%s", log_path)
    raise SystemExit(application.exec())


def _start_worker(name: str, target, *args) -> threading.Thread:
    worker = threading.Thread(
        target=_supervise_worker, args=(name, target, args), name=name, daemon=True
    )
    worker.start()
    return worker


def _supervise_worker(name: str, target, args: tuple[Any, ...]) -> None:
    restarts = 0
    while True:
        try:
            target(*args)
        except Exception:
            log.exception("worker %s exited with an exception", name)
        else:
            log.error("worker %s returned unexpectedly", name)
        restarts += 1
        delay = min(WORKER_RESTART_MAX_SEC, WORKER_RESTART_INITIAL_SEC * restarts)
        log.warning("worker %s restarting in %.1fs", name, delay)
        time.sleep(delay)


def _base_dir() -> Path:
    return Path(sys.executable).parent if getattr(sys, "frozen", False) else Path(__file__).parent


def _configure_logging(base_dir: Path) -> Path | None:
    handlers: list[logging.Handler] = []
    if getattr(sys, "stderr", None) is not None:
        handlers.append(logging.StreamHandler())
    log_path = base_dir / DEFAULT_LOG_FILENAME
    try:
        handlers.append(
            RotatingFileHandler(log_path, maxBytes=1_048_576, backupCount=3, encoding="utf-8")
        )
    except Exception:
        log_path = None
    if not handlers:
        handlers.append(logging.NullHandler())
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", handlers=handlers, force=True
    )
    return log_path


def _load_config(base_dir: Path | None = None) -> dict[str, Any]:
    base_dir = base_dir or _base_dir()
    config_path = base_dir / "config.json"
    if not config_path.exists():
        config_path = base_dir / "config.example.json"
    if not config_path.exists():
        return {}
    # Windows PowerShell 5 writes a UTF-8 BOM by default.  ``utf-8-sig``
    # accepts those files while remaining byte-for-byte compatible with the
    # ordinary BOM-free config produced by editors and our example file.
    return json.loads(config_path.read_text(encoding="utf-8-sig"))


def _sample_interval(config: dict[str, Any]) -> int:
    try:
        value = int(config.get("sample_interval_sec") or DEFAULT_SAMPLE_INTERVAL_SEC)
    except (TypeError, ValueError):
        value = DEFAULT_SAMPLE_INTERVAL_SEC
    return min(MAX_SAMPLE_INTERVAL_SEC, max(MIN_SAMPLE_INTERVAL_SEC, value))


if __name__ == "__main__":
    main()
