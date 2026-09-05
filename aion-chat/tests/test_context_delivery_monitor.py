import asyncio
from pathlib import Path

from routes import settings


def test_monitor_context_endpoint_uses_shared_renderer(monkeypatch):
    rendered = "[设备与环境上下文]\n直接观测：\n- 10:26 定位服务报告设备大概在校园。"
    monkeypatch.setattr(settings, "_render_current_context_status", lambda: rendered)
    monkeypatch.setattr(settings.time, "time", lambda: 1234.5)

    result = asyncio.run(settings.get_current_context_delivery_api())

    assert result == {
        "status": rendered,
        "generated_at": 1234.5,
        "source": "context_delivery_projection.v2",
    }


def test_monitor_shared_renderer_receives_configured_relationship_name(monkeypatch):
    import context_delivery_runtime_readers as runtime_readers

    captured = {}
    monkeypatch.setattr(
        settings,
        "load_worldbook",
        lambda: {"user_name": "阿玖", "ai_name": "Aion"},
    )
    monkeypatch.setattr(
        runtime_readers,
        "render_current_context_delivery",
        lambda **kwargs: captured.update(kwargs) or "共享上下文",
    )

    assert settings._render_current_context_status() == "共享上下文"
    assert captured["user_name"] == "阿玖"


def test_monitor_page_no_longer_renders_legacy_chat_status_payload():
    page = Path(__file__).resolve().parents[1] / "static" / "monitor-logs.html"
    text = page.read_text(encoding="utf-8")

    assert 'api("GET", "/api/context_delivery/current")' in text
    assert 'api("GET", "/api/chat_status")' not in text
    assert "updateChatStatus(msg.data" not in text
    assert "🧭 当前上下文：" in text


def test_monitor_page_shows_matched_shadow_labels_without_interrupting_owner():
    page = Path(__file__).resolve().parents[1] / "static" / "monitor-logs.html"
    text = page.read_text(encoding="utf-8")

    assert "/api/sentinel/context-trigger-shadow?limit=50" in text
    assert "/api/sentinel/context-trigger-shadow/${encodeURIComponent(evaluationId)}/label" in text
    assert "如果当时醒来，合适吗？" in text
    assert 'alert("如果当时醒来' not in text  # 页面只供回看，不弹出候选询问。
    assert "显示未命中/缺数据" in text
    assert '["right", "wrong", "indifferent"]' in text
    assert 'entry.evaluation_status === "matched"' in text
