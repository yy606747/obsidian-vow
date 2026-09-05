import asyncio
import base64
from contextlib import asynccontextmanager
import json
import sqlite3

import aiosqlite
import pytest

import ai_providers
from app.chat import history as history_mod
from app.chat import image_history
from routes import settings as settings_routes


_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aS2kAAAAASUVORK5CYII="
)


@pytest.fixture
def chat_images(monkeypatch, tmp_path):
    db_path = tmp_path / "history.db"
    with sqlite3.connect(db_path) as db:
        db.executescript(
            "CREATE TABLE conversations (id TEXT PRIMARY KEY, title TEXT, model TEXT);"
            "INSERT INTO conversations VALUES ('chat', '图片续聊', 'custom-vision-model');"
            "CREATE TABLE messages (id TEXT PRIMARY KEY, conv_id TEXT, role TEXT, content TEXT, attachments TEXT, created_at REAL);"
        )

    @asynccontextmanager
    async def get_db():
        async with aiosqlite.connect(db_path) as db:
            yield db

    monkeypatch.setattr(history_mod, "get_db", get_db)
    monkeypatch.setattr(history_mod, "load_worldbook", lambda: {"user_name": "小栀", "ai_name": "阿澈"})
    monkeypatch.setattr(image_history, "UPLOADS_DIR", tmp_path)
    monkeypatch.setattr(ai_providers, "UPLOADS_DIR", tmp_path)
    monkeypatch.setattr(image_history, "SETTINGS", {})

    class Chat:
        def image(self, name):
            (tmp_path / name).write_bytes(_PNG)
            return f"/uploads/{name}"

        def add(self, message_id, attachments=(), *, role="user", content="继续说", created_at=None):
            with sqlite3.connect(db_path) as db:
                now = db.execute("SELECT COUNT(*) FROM messages").fetchone()[0] + 1
                db.execute(
                    "INSERT INTO messages VALUES (?, 'chat', ?, ?, ?, ?)",
                    (message_id, role, content, json.dumps(list(attachments)), created_at or now),
                )

        def delete(self, message_id):
            with sqlite3.connect(db_path) as db:
                db.execute("DELETE FROM messages WHERE id=?", (message_id,))

        def prepare(self, policy="last_message", **kwargs):
            return asyncio.run(history_mod.prepare_chat_history(
                "chat", context_limit=200, attachment_policy=policy, **kwargs,
            ))

    return Chat()


def images_in_openai_payload(history):
    return [
        part["image_url"]["url"]
        for message in ai_providers.build_multimodal_messages(history)
        if isinstance(message["content"], list)
        for part in message["content"] if part.get("type") == "image_url"
    ]


@pytest.mark.parametrize("policy", ["last_message", "last_user"])
def test_image_then_text_followup_reaches_both_actual_provider_encoders(chat_images, policy):
    photo = chat_images.image("photo.png")
    chat_images.add("photo", [photo], content="")
    chat_images.add("answer", role="assistant")
    chat_images.add("followup", content="那张图左边是什么？")
    context = chat_images.prepare(policy)
    assert context.model_key == "custom-vision-model"
    assert images_in_openai_payload(context.history) == ["data:image/png;base64," + base64.b64encode(_PNG).decode()]
    native = ai_providers.build_gemini_contents(context.history)
    assert [
        part["inline_data"]["data"] for message in native
        for part in message["parts"] if "inline_data" in part
    ] == [base64.b64encode(_PNG).decode()]
    assert context.image_history["retained_images"] == 1


def test_real_user_window_ignores_triggers_and_keeps_message_positions(chat_images):
    chat_images.add("too-old", [chat_images.image("old.png")])
    chat_images.add("keep", [chat_images.image("keep.png")])
    for index in range(3):
        for event in range(3):
            chat_images.add(f"event-{index}-{event}", role="trigger")
        chat_images.add(f"user-{index}")
    context = chat_images.prepare()
    photos = [message for message in context.history if message.get("attachments")]
    assert len(photos) == 1
    assert photos[0]["attachments"] == ["/uploads/keep.png"]
    assert context.image_history["omitted"] == [{
        "message_id": "too-old", "image_number": 1, "reason": "outside_window",
    }]


def test_budget_prefers_recent_images_and_exempts_current_images(chat_images):
    chat_images.add("older", [chat_images.image(f"old-{index}.png") for index in range(3)])
    chat_images.add("newer", [chat_images.image(f"new-{index}.png") for index in range(3)])
    current = [chat_images.image(f"current-{index}.png") for index in range(6)]
    chat_images.add("current", current)
    context = chat_images.prepare()
    actual = [message["attachments"] for message in context.history if message.get("attachments")]
    assert actual == [["/uploads/old-0.png"], [f"/uploads/new-{index}.png" for index in range(3)], current]
    assert context.image_history["retained_images"] == 4
    assert len(images_in_openai_payload(context.history)) == 10


def test_missing_and_oversize_images_have_named_context_and_diagnostics(chat_images, monkeypatch):
    monkeypatch.setitem(image_history.SETTINGS, "image_history_max_bytes", len(_PNG) - 1)
    chat_images.add("old", [chat_images.image("big.png"), "/uploads/missing.png"])
    chat_images.add("current")
    context = chat_images.prepare()
    assert {item["reason"] for item in context.image_history["omitted"]} == {"byte_limit", "missing_file"}
    assert not images_in_openai_payload(context.history)
    note = next(message["content"] for message in context.history if "[图片上下文]" in message["content"])
    assert "小栀" in note and "阿澈" in note
    assert "用户" not in note and "TA" not in note


def test_zero_limit_restores_legacy_attachment_policy(chat_images, monkeypatch):
    monkeypatch.setitem(image_history.SETTINGS, "image_history_max_images", 0)
    chat_images.add("old", [chat_images.image("old.png")])
    chat_images.add("current", [chat_images.image("current.png")])
    context = chat_images.prepare()
    assert context.image_history["enabled"] is False
    assert [message["attachments"] for message in context.history if message.get("attachments")] == [["/uploads/current.png"]]


def test_audio_and_video_are_not_reintroduced_from_history(chat_images):
    voice = {"type": "voice", "url": "/uploads/voice.wav", "transcript": "过去的语音"}
    chat_images.add("old", [chat_images.image("image.png"), voice, "/uploads/video.mp4"])
    chat_images.add("current", [voice, "/uploads/current-video.mp4"])
    context = chat_images.prepare()
    actual = [message["attachments"] for message in context.history if message.get("attachments")]
    assert actual == [["/uploads/image.png"], [voice, "/uploads/current-video.mp4"]]
    assert any("过去的语音" in message["content"] for message in context.history)


def test_deleted_or_retracted_source_cannot_reenter_image_payload(chat_images):
    chat_images.add("deleted", [chat_images.image("deleted.png")])
    chat_images.add("current")
    chat_images.delete("deleted")
    context = chat_images.prepare(retracted=True)
    assert not images_in_openai_payload(context.history)
    assert any("小栀刚刚偷偷撤回" in message["content"] for message in context.history)


def test_image_limits_are_exposed_and_saved_without_touching_other_settings(monkeypatch):
    values = {"unrelated": "keep"}
    monkeypatch.setattr(settings_routes, "SETTINGS", values)
    monkeypatch.setattr(settings_routes, "_ensure_endpoints_and_slots", lambda _data: False)
    monkeypatch.setattr(settings_routes, "save_settings", lambda _data: None)
    assert asyncio.run(settings_routes.get_settings())["image_history_max_images"] == 4
    asyncio.run(settings_routes.update_settings(settings_routes.SettingsUpdate(
        image_history_user_turns=3, image_history_max_images=0, image_history_max_bytes=100,
    )))
    assert values == {"unrelated": "keep", "image_history_user_turns": 3, "image_history_max_images": 0, "image_history_max_bytes": 100}
