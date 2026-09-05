from __future__ import annotations

import json
import sys
import threading
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
PC_AGENT = ROOT / "pc_agent"
sys.path.insert(0, str(PC_AGENT))

from summon_button import (  # noqa: E402
    BUTTON_SIZE_DIP,
    CLICK_DEBOUNCE_SEC,
    DRAG_THRESHOLD_DIP,
    SummonRequestDispatcher,
    clamp_position,
    default_position,
    load_button_state,
    parse_hotkey,
    save_button_state,
)


def test_dispatcher_debounces_and_posts_uuid_off_the_calling_thread():
    clock = [100.0]
    identifiers = iter(("summon-1", "summon-2"))
    calls = []
    results = []
    finished = threading.Event()
    caller_thread = threading.get_ident()

    def post(url, payload, **kwargs):
        calls.append((threading.get_ident(), url, payload, kwargs))
        return {"accepted": True, "summon_id": payload["summon_id"]}

    def completed(result):
        results.append(result)
        finished.set()

    dispatcher = SummonRequestDispatcher(
        endpoint="https://example.test/api/presence/summon",
        token="secret",
        post_json=post,
        now=lambda: clock[0],
        id_factory=lambda: next(identifiers),
    )
    try:
        assert dispatcher.dispatch(completed) is True
        assert dispatcher.dispatch(completed) is False
        assert finished.wait(2)
        assert calls[0][0] != caller_thread
        assert calls[0][1] == "https://example.test/api/presence/summon"
        assert calls[0][2] == {"summon_id": "summon-1", "device_id": "pc"}
        assert calls[0][3] == {"token": "secret", "timeout": 10}
        assert results == [{"ok": True, "summon_id": "summon-1"}]

        finished.clear()
        clock[0] += CLICK_DEBOUNCE_SEC
        assert dispatcher.dispatch(completed) is True
        assert finished.wait(2)
        assert len(calls) == 2
    finally:
        dispatcher.shutdown()


def test_dispatcher_reports_transport_and_protocol_failures_only_as_failures():
    results = []
    done = threading.Event()

    def invalid_response(*_args, **_kwargs):
        return {"accepted": True, "summon_id": "wrong"}

    dispatcher = SummonRequestDispatcher(
        endpoint="https://example.test/api/presence/summon",
        token="secret",
        post_json=invalid_response,
        id_factory=lambda: "expected",
    )
    try:
        assert dispatcher.dispatch(lambda result: (results.append(result), done.set()))
        assert done.wait(2)
        assert results == [{
            "ok": False,
            "summon_id": "expected",
            "error": "summon_response_invalid",
        }]
    finally:
        dispatcher.shutdown()


def test_position_state_is_separate_clamped_and_uses_logical_coordinates(tmp_path):
    state_path = tmp_path / "summon_button_state.json"
    save_button_state(
        state_path,
        {"screen_name": "Display-2", "x": 2500, "y": -20},
    )

    assert load_button_state(state_path) == {
        "screen_name": "Display-2",
        "x": 2500,
        "y": -20,
    }
    assert json.loads(state_path.read_text(encoding="utf-8"))["screen_name"] == "Display-2"
    assert clamp_position(2500, -20, (1920, 0, 1280, 720)) == (2500, 0)
    assert clamp_position(9999, 9999, (1920, 0, 1280, 720)) == (3172, 692)
    assert default_position((1920, 0, 1280, 720)) == (3148, 668)


def test_hotkey_parser_and_qt_non_focus_contract_are_explicit():
    assert parse_hotkey("Ctrl+Alt+S") == (0x4000 | 0x0002 | 0x0001, ord("S"))
    assert parse_hotkey("Win+Shift+F12") == (
        0x4000 | 0x0008 | 0x0004,
        0x7B,
    )
    source = (PC_AGENT / "summon_button.py").read_text(encoding="utf-8")

    assert BUTTON_SIZE_DIP == 28
    assert DRAG_THRESHOLD_DIP == 4.0
    assert "WindowDoesNotAcceptFocus" in source
    assert "WA_ShowWithoutActivating" in source
    assert "ThreadPoolExecutor(" in source
    assert "max_workers=2" in source
    assert '"没送出去"' in source
    assert "SP_MessageBoxWarning" in source
    assert "QIcon()" not in source
    assert 'if bool(result.get("ok")):\n                return' in source
    assert "summon_button_state.json" not in (PC_AGENT / "config.example.json").read_text(
        encoding="utf-8"
    )
