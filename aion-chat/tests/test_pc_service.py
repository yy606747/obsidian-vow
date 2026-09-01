import pytest

from app.pc_context import service


@pytest.fixture(autouse=True)
def reset_pc_context():
    service._reset_state_for_tests()
    yield
    service._reset_state_for_tests()


def test_ingest_report_normalizes_and_caches_status():
    entry = service.ingest_report({
        "timestamp": 1000.0,
        "app": "Code.exe",
        "title": "ObsidianVow",
        "active_state": "active",
        "last_input_age_sec": 42,
    }, now=1001.0)

    assert entry["timestamp"] == 1000.0
    assert entry["device"] == "pc"
    assert entry["app"] == "VS Code"
    assert entry["title"] == "ObsidianVow"
    assert entry["active_state"] == "active"
    assert entry["last_input_age_sec"] == 42
    assert entry["time"]
    assert entry["date"]
    snapshot = service.get_pc_status(1002.0)
    assert snapshot.active_state == "active"
    assert snapshot.foreground_app == "VS Code"
    assert snapshot.foreground_title_sanitized == "ObsidianVow"


def test_status_goes_offline_without_reusing_last_foreground():
    service.ingest_report({
        "timestamp": 1000.0,
        "app": "Chrome.exe",
        "title": "Docs",
        "active_state": "idle",
        "last_input_age_sec": 200,
    }, now=1000.0)

    online = service.get_pc_status(1200.0)
    assert online.active_state == "idle"
    offline = service.get_pc_status(1301.0, offline_after_sec=300)
    assert offline.active_state == "offline"
    assert offline.foreground_app is None
    assert offline.foreground_title_sanitized is None
    assert offline.last_input_age_sec is None


def test_never_seen_status_is_offline_and_does_not_write_evidence():
    snapshot = service.get_pc_status(2000.0)

    assert snapshot.active_state == "offline"
    assert snapshot.foreground_app is None
    assert snapshot.last_input_age_sec is None


def test_invalid_or_agent_offline_state_becomes_unknown(caplog):
    entry = service.ingest_report({
        "timestamp": 1000.0,
        "app": "SomeRandomApp.exe",
        "title": "Safe title",
        "active_state": "offline",
        "last_input_age_sec": 12,
    }, now=1000.0)

    assert entry["active_state"] == "unknown"
    assert entry["app"] == "SomeRandomApp"
    assert entry["title"] == "Safe title"
    assert entry["last_input_age_sec"] is None
    assert "normalized to unknown" in caplog.text


def test_locked_lock_screen_process_removes_app_title():
    entry = service.ingest_report({
        "timestamp": 1000.0,
        "app": "LockApp.exe",
        "title": "Windows Default Lock Screen",
        "active_state": "locked",
        "last_input_age_sec": 500,
    }, now=1000.0)

    assert entry["active_state"] == "locked"
    assert entry["app"] is None
    assert entry["title"] is None


def test_defensive_title_cleanup():
    entry = service.ingest_report({
        "timestamp": 1000.0,
        "app": "Chrome",
        "title": "Docs https://example.com/path",
        "active_state": "active",
    }, now=1000.0)

    assert entry["title"] == "Docs"
