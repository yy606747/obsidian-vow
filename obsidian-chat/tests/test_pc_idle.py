from app.pc_context import service


def test_active_idle_locked_unknown_states_are_preserved():
    service._reset_state_for_tests()
    for index, state in enumerate(["active", "idle", "locked", "unknown"]):
        entry = service.ingest_report({
            "timestamp": 1000.0 + index,
            "app": "Code.exe",
            "title": "Project",
            "active_state": state,
            "last_input_age_sec": 10,
        }, now=1000.0 + index)
        assert entry["active_state"] == state
    service._reset_state_for_tests()


def test_unknown_state_does_not_keep_input_age():
    service._reset_state_for_tests()
    entry = service.ingest_report({
        "timestamp": 1000.0,
        "app": "Code.exe",
        "title": "Project",
        "active_state": "unknown",
        "last_input_age_sec": 10,
    }, now=1000.0)

    assert entry["last_input_age_sec"] is None
    service._reset_state_for_tests()
