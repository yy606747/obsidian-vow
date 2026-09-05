import ai_providers
from app.web_push import service as web_push_service


def test_preset_gemini_defaults_to_direct_connection(monkeypatch):
    for name in ("OBSIDIAN_GEMINI_PROXY", "OBSIDIAN_PROVIDER_PROXY"):
        monkeypatch.delenv(name, raising=False)

    assert (
        ai_providers._resolve_proxy("gemini", None, preset_gemini=True) is None
    )


def test_web_push_proxy_is_explicit_opt_in(monkeypatch):
    monkeypatch.delenv("OBSIDIAN_WEB_PUSH_PROXY", raising=False)
    assert web_push_service._push_proxy_url() == ""

    monkeypatch.setenv("OBSIDIAN_WEB_PUSH_PROXY", "http://127.0.0.1:7890")
    assert web_push_service._push_proxy_url() == "http://127.0.0.1:7890"
