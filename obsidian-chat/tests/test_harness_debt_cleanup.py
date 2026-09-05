from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _read(relative_path: str) -> str:
    return (ROOT / relative_path).read_text(encoding="utf-8")


def test_worldbook_persona_prefix_has_one_shared_implementation():
    sources = [
        *sorted((ROOT / "app").rglob("*.py")),
        ROOT / "sentinel_runtime.py",
    ]
    combined = "\n".join(path.read_text(encoding="utf-8") for path in sources)
    worldbook_source = _read("app/chat/worldbook.py")

    assert combined.count("def build_worldbook_prefix(") == 1
    assert worldbook_source.count("[关于你自己：") == 1
    assert worldbook_source.count("[关于{user_name}]") == 1

    callers = {
        "app/chat/history.py": 1,
        "app/chat/side_effects.py": 4,
        "app/chat/initiative_helpers.py": 1,
        "app/schedule/trigger.py": 1,
        "app/sentinel/core_wake_orchestrator.py": 1,
        "sentinel_runtime.py": 1,
    }
    for path, expected_calls in callers.items():
        assert _read(path).count("build_worldbook_prefix(") == expected_calls


def test_frontend_history_and_streams_share_one_control_marker_cleaner():
    core = _read("static/js/chat/core.js")
    send = _read("static/js/chat/send.js")
    render = _read("static/js/chat/render.js")
    html = _read("static/chat.html")

    assert core.count("function cleanAssistantContent(") == 1
    assert "function cleanAssistantContent(" not in send
    assert "CHAT_CONTROL_PATTERNS" not in send
    assert "cleanAssistantContent(m.content)" in render
    assert "formatMsg(m.content)" not in render

    for marker in (
        "MOBILE_SCREEN_CHECK",
        "TIDE_INTENT",
        "UPDATE_MODEL",
        "WORKING_MODEL_REQUEST",
        "RECALL_INTENT",
        "OPPORTUNITY_NONE",
        "OPPORTUNITY_REFLECT",
        "RING",
    ):
        assert marker in core

    assert html.index("/static/js/chat/core.js?v=20260905-brand") < html.index("/static/js/chat/render.js?v=20260905-brand")
    assert html.index("/static/js/chat/core.js?v=20260905-brand") < html.index("/static/js/chat/send.js?v=20260905-brand")
