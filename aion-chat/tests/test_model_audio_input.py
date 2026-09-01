import asyncio
import base64
import json
from contextlib import asynccontextmanager
from pathlib import Path

import ai_providers
import config
from app.chat import audio_input, history as history_mod
from app.chat.models import MsgCreate
from routes import chat as chat_routes, settings as settings_routes


ROOT = Path(__file__).resolve().parents[1]


def _voice_attachment(*, transcript="听见了吗", duration_ms=1200):
    return {
        "type": "voice",
        "url": "/uploads/voice.wav",
        "mime_type": "audio/wav",
        "name": "voice.wav",
        "duration_ms": duration_ms,
        "transcript": transcript,
    }


def test_model_audio_capability_is_explicit_and_custom_defaults_closed(monkeypatch):
    assert config.model_supports_audio_input("gemini-3-flash") is True
    assert config.model_supports_audio_input("claude-sonnet-4-6") is False

    monkeypatch.setattr(config, "SETTINGS", {
        "endpoints": [{
            "id": "custom",
            "name": "Custom",
            "type": "openai",
            "base_url": "https://example.invalid/v1",
            "api_key": "test",
        }],
        "user_models": {
            "declared": {"endpoint": "custom", "model": "audio-model", "audio_input": True},
            "omitted": {"endpoint": "custom", "model": "unknown-model"},
            "truthy-not-bool": {"endpoint": "custom", "model": "unknown-model", "audio_input": 1},
        },
    })

    assert config.model_supports_audio_input("declared") is True
    assert config.model_supports_audio_input("omitted") is False
    assert config.model_supports_audio_input("truthy-not-bool") is False


def test_models_api_exposes_audio_capability_to_frontend():
    models = asyncio.run(settings_routes.list_models())
    by_key = {item["key"]: item for item in models}

    assert by_key["gemini-3-flash"]["audio_input"] is True
    assert by_key["claude-sonnet-4-6"]["audio_input"] is False
    assert all(isinstance(item["audio_input"], bool) for item in models)


def test_voice_attachment_normalization_keeps_original_audio_metadata():
    normalized = audio_input.normalize_chat_attachments([_voice_attachment(duration_ms=1234.4)])

    assert normalized == [{
        "type": "voice",
        "url": "/uploads/voice.wav",
        "mime_type": "audio/wav",
        "duration_ms": 1234,
        "transcript": "听见了吗",
        "name": "voice.wav",
    }]


def test_openai_compatible_payload_includes_base64_input_audio_only_when_enabled(
    monkeypatch, tmp_path
):
    raw_audio = b"RIFF-test-voice"
    (tmp_path / "voice.wav").write_bytes(raw_audio)
    monkeypatch.setattr(ai_providers, "UPLOADS_DIR", tmp_path)
    history = [{
        "role": "user",
        "content": "",
        "attachments": [_voice_attachment()],
    }]

    disabled = ai_providers.build_multimodal_messages(history, include_audio=False)
    enabled = ai_providers.build_multimodal_messages(history, include_audio=True)

    assert disabled == [{"role": "user", "content": ""}]
    assert enabled == [{
        "role": "user",
        "content": [{
            "type": "input_audio",
            "input_audio": {
                "data": base64.b64encode(raw_audio).decode(),
                "format": "wav",
            },
        }],
    }]


def test_gemini_payload_includes_inline_audio_only_when_enabled(monkeypatch, tmp_path):
    raw_audio = b"RIFF-gemini-voice"
    (tmp_path / "voice.wav").write_bytes(raw_audio)
    monkeypatch.setattr(ai_providers, "UPLOADS_DIR", tmp_path)
    history = [{
        "role": "user",
        "content": "",
        "attachments": [_voice_attachment()],
    }]

    disabled = ai_providers.build_gemini_contents(history, include_audio=False)
    enabled = ai_providers.build_gemini_contents(history, include_audio=True)

    assert disabled == [{"role": "user", "parts": [{"text": ""}]}]
    assert enabled == [{
        "role": "user",
        "parts": [{
            "inline_data": {
                "mime_type": "audio/wav",
                "data": base64.b64encode(raw_audio).decode(),
            },
        }],
    }]


def test_stream_ai_refuses_audio_instead_of_using_transcript_fallback(monkeypatch):
    monkeypatch.setattr(ai_providers, "resolve_core_model", lambda _key: {
        "_kind": "preset",
        "provider": "aipro",
        "model": "text-only",
        "audio_input": False,
    })

    async def collect():
        return [chunk async for chunk in ai_providers.stream_ai(
            [{
                "role": "user",
                "content": "<meta>语音自动转写（可能有误）：你好</meta>",
                "attachments": [_voice_attachment(transcript="你好")],
            }],
            "text-only",
        )]

    chunks = asyncio.run(collect())
    assert chunks == [f"[错误] {audio_input.AUDIO_INPUT_UNAVAILABLE_MESSAGE}"]


def test_stream_ai_passes_audio_capability_to_native_gemini_transport(monkeypatch):
    calls = []
    monkeypatch.setattr(ai_providers, "resolve_core_model", lambda _key: {
        "_kind": "preset",
        "provider": "gemini",
        "model": "gemini-audio",
        "audio_input": True,
    })

    async def fake_call_gemini(
        messages, model, meta=None, temperature=None, *, include_audio=False
    ):
        calls.append((messages, model, include_audio))
        yield "ok"

    monkeypatch.setattr(ai_providers, "call_gemini", fake_call_gemini)

    async def collect():
        return [chunk async for chunk in ai_providers.stream_ai(
            [{"role": "user", "content": "", "attachments": [_voice_attachment()]}],
            "gemini-audio",
        )]

    assert asyncio.run(collect()) == ["ok"]
    assert calls[0][1:] == ("gemini-audio", True)
    assert calls[0][0][0]["attachments"][0]["type"] == "voice"


def test_history_keeps_transcript_but_only_latest_audio_attachment():
    older = history_mod._normalize_history_row({
        "role": "user",
        "content": "",
        "attachments": json.dumps([_voice_attachment(transcript="第一条")]),
        "created_at": 0,
    })
    latest = history_mod._normalize_history_row({
        "role": "user",
        "content": "",
        "attachments": json.dumps([_voice_attachment(transcript="第二条")]),
        "created_at": 0,
    })
    history = [older, {"role": "assistant", "content": "收到", "attachments": []}, latest]

    history_mod._strip_history_attachments(history, "last_message")

    assert history[0]["attachments"] == []
    assert "第一条" in history[0]["content"]
    assert history[-1]["attachments"][0]["type"] == "voice"
    assert "第二条" in history[-1]["content"]


class _Cursor:
    def __init__(self, row):
        self.row = row

    async def fetchone(self):
        return self.row


class _SendGateDb:
    def __init__(self):
        self.statements = []

    async def execute(self, sql, params=()):
        self.statements.append((sql, params))
        if sql.startswith("SELECT model"):
            return _Cursor(("text-only",))
        raise AssertionError("audio gate must run before any write")

    async def commit(self):
        raise AssertionError("audio gate must run before commit")


def test_send_audio_gate_rejects_before_message_is_persisted(monkeypatch):
    fake_db = _SendGateDb()

    @asynccontextmanager
    async def fake_get_db():
        yield fake_db

    monkeypatch.setattr(chat_routes, "get_db", fake_get_db)
    monkeypatch.setattr(chat_routes, "model_supports_audio_input", lambda _key: False)

    response = asyncio.run(chat_routes.send_message(
        "conv",
        MsgCreate(content="", attachments=[_voice_attachment()]),
    ))

    assert response.status_code == 422
    assert json.loads(response.body)["code"] == audio_input.AUDIO_INPUT_UNAVAILABLE_CODE
    assert all("INSERT INTO messages" not in sql for sql, _params in fake_db.statements)


def test_audio_gate_does_not_silently_drop_a_missing_recording(monkeypatch, tmp_path):
    monkeypatch.setattr(chat_routes, "model_supports_audio_input", lambda _key: True)
    monkeypatch.setattr(audio_input, "UPLOADS_DIR", tmp_path)

    response = chat_routes._audio_preflight_response(
        "audio-model",
        [_voice_attachment()],
    )

    assert response.status_code == 422
    assert json.loads(response.body)["code"] == audio_input.AUDIO_ATTACHMENT_UNAVAILABLE_CODE


class _RegenerateGateDb:
    def __init__(self):
        self.select_count = 0

    async def execute(self, sql, params=()):
        self.select_count += 1
        if sql.startswith("SELECT model"):
            return _Cursor(("text-only",))
        if sql.startswith("SELECT attachments"):
            return _Cursor((json.dumps([_voice_attachment()]),))
        raise AssertionError(sql)


def test_regenerate_audio_gate_rejects_before_old_answer_is_replaced(monkeypatch):
    fake_db = _RegenerateGateDb()

    @asynccontextmanager
    async def fake_get_db():
        yield fake_db

    async def should_not_replace(*_args, **_kwargs):
        raise AssertionError("old answer was replaced before the audio gate")

    monkeypatch.setattr(chat_routes, "get_db", fake_get_db)
    monkeypatch.setattr(chat_routes, "model_supports_audio_input", lambda _key: False)
    monkeypatch.setattr(chat_routes, "replace_message_and_freeze_vow_context", should_not_replace)

    response = asyncio.run(chat_routes.regenerate_message(
        "conv",
        replaced_message_id="assistant-old",
    ))

    assert response.status_code == 422
    assert json.loads(response.body)["code"] == audio_input.AUDIO_INPUT_UNAVAILABLE_CODE
    assert fake_db.select_count == 2


def test_frontend_voice_message_contract_is_lazy_and_fail_closed():
    html = (ROOT / "static/chat.html").read_text(encoding="utf-8")
    ui = (ROOT / "static/js/chat/ui.js").read_text(encoding="utf-8")
    send = (ROOT / "static/js/chat/send.js").read_text(encoding="utf-8")
    regenerate = (ROOT / "static/js/chat/messages.js").read_text(encoding="utf-8")
    recorder = (ROOT / "static/js/chat/voice_message.js").read_text(encoding="utf-8")
    bridge = (
        ROOT.parent
        / "AionApp/app/src/main/java/app/obsidianvow/core/AudioBridge.java"
    ).read_text(encoding="utf-8")

    assert 'id="voiceHoldBtn"' in html
    assert '/static/js/chat/voice_message.js' not in html
    assert 'script.src = VOICE_MESSAGE_SCRIPT' in ui
    assert "currentModelSupportsAudioInput()" in send
    assert send.index("hasVoiceAttachments(pendingAttachments)") < send.index("sending = true")
    assert regenerate.index("hasVoiceAttachments(latestUserMessage?.attachments)") < regenerate.index("sending = true")
    assert 'fetch("/api/upload"' in recorder
    assert 'fetch("/api/voice/remote-asr"' in recorder
    assert "navigator.mediaDevices.getUserMedia" in recorder
    assert "window.AionAudio.start()" in recorder
    assert "await send()" in recorder
    assert "_voiceNativeOnChunk" in bridge
