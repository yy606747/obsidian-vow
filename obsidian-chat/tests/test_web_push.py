from __future__ import annotations

import asyncio
import base64
import os
import sqlite3
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace

import aiosqlite
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from pywebpush import webpush

from app.web_push import keys, repository, schema, service


def test_vapid_key_is_stable_and_application_server_key_is_uncompressed(tmp_path):
    private_key = tmp_path / "vapid_private.pem"

    first = keys.application_server_key(private_key)
    private_bytes = private_key.read_bytes()
    second = keys.application_server_key(private_key)

    assert first == second
    assert private_key.read_bytes() == private_bytes
    assert len(first) == 87  # 65-byte uncompressed P-256 point, base64url without padding
    assert keys.VAPID_SUBJECT.startswith("mailto:")


def test_generated_vapid_key_encrypts_and_signs_with_pinned_pywebpush(tmp_path):
    client_private_key = ec.generate_private_key(ec.SECP256R1())
    client_public_key = client_private_key.public_key().public_bytes(
        encoding=serialization.Encoding.X962,
        format=serialization.PublicFormat.UncompressedPoint,
    )

    def base64url(value: bytes) -> str:
        return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")

    curl = webpush(
        subscription_info={
            "endpoint": "https://push.example.test/subscription",
            "keys": {
                "p256dh": base64url(client_public_key),
                "auth": base64url(os.urandom(16)),
            },
        },
        data='{"type":"schedule_alarm"}',
        vapid_private_key=str(keys.ensure_vapid_private_key(tmp_path / "vapid.pem")),
        vapid_claims={"sub": keys.VAPID_SUBJECT},
        ttl=300,
        curl=True,
    )

    assert 'authorization: vapid ' in curl
    assert 'content-encoding: aes128gcm' in curl
    assert 'ttl: 300' in curl


def test_subscription_schema_contains_delivery_health_fields(tmp_path):
    db_path = tmp_path / "push.db"

    class AsyncSchemaConnection:
        def __init__(self, connection):
            self.connection = connection

        async def execute(self, sql, params=()):
            return self.connection.execute(sql, params)

    async def create_table():
        conn = sqlite3.connect(db_path)
        try:
            await schema.init_web_push_tables(AsyncSchemaConnection(conn))
            conn.commit()
        finally:
            conn.close()

    asyncio.run(create_table())
    conn = sqlite3.connect(db_path)
    try:
        columns = {
            row[1] for row in conn.execute("PRAGMA table_info(web_push_subscriptions)")
        }
    finally:
        conn.close()

    assert columns == {
        "endpoint",
        "p256dh",
        "auth",
        "created_at",
        "last_ok_at",
        "failure_count",
    }


def test_subscription_repository_lifecycle_uses_real_sqlite(monkeypatch, tmp_path):
    db_path = tmp_path / "push.db"

    @asynccontextmanager
    async def test_db():
        async with aiosqlite.connect(db_path) as db:
            yield db

    async def exercise_repository():
        async with aiosqlite.connect(db_path) as db:
            await schema.init_web_push_tables(db)
            await db.commit()

        monkeypatch.setattr(repository, "get_db", test_db)
        endpoint = "https://push.example.test/subscription"
        await repository.upsert_subscription(
            endpoint=endpoint, p256dh="public-key", auth="auth-secret"
        )
        rows = await repository.list_subscriptions()
        assert len(rows) == 1
        assert rows[0]["failure_count"] == 0

        await repository.mark_failure(endpoint)
        assert (await repository.list_subscriptions())[0]["failure_count"] == 1

        await repository.mark_success(endpoint)
        succeeded = (await repository.list_subscriptions())[0]
        assert succeeded["failure_count"] == 0
        assert succeeded["last_ok_at"] is not None

        await repository.delete_subscription(endpoint)
        assert await repository.list_subscriptions() == []

    asyncio.run(exercise_repository())


def test_send_one_uses_explicit_proxy_timeout_and_ttl(monkeypatch, tmp_path):
    captured = {}

    def fake_webpush(**kwargs):
        captured.update(kwargs)
        return SimpleNamespace(status_code=201)

    monkeypatch.setattr(service, "_invoke_webpush", fake_webpush)
    monkeypatch.setenv("OBSIDIAN_WEB_PUSH_PROXY", "http://127.0.0.1:7890")
    attempt = service._send_one(
        {
            "endpoint": "https://push.example.test/subscription",
            "p256dh": "public",
            "auth": "auth",
        },
        '{"type":"schedule_alarm"}',
        str(tmp_path / "vapid.pem"),
    )

    assert attempt.succeeded
    assert captured["ttl"] == 300
    assert captured["timeout"] == 10.0
    assert captured["vapid_claims"] == {"sub": keys.VAPID_SUBJECT}
    assert captured["requests_session"].trust_env is False
    assert captured["requests_session"].proxies == {
        "http": "http://127.0.0.1:7890",
        "https": "http://127.0.0.1:7890",
    }


def test_broadcast_classifies_results_without_deleting_transient_failures(
    monkeypatch, tmp_path
):
    subscriptions = [
        {"endpoint": "https://push.test/ok", "p256dh": "p", "auth": "a"},
        {"endpoint": "https://push.test/gone", "p256dh": "p", "auth": "a"},
        {"endpoint": "https://push.test/timeout", "p256dh": "p", "auth": "a"},
    ]
    updates = {"success": [], "deleted": [], "failed": []}

    async def list_subscriptions():
        return subscriptions

    async def mark_success(endpoint):
        updates["success"].append(endpoint)

    async def delete_subscription(endpoint):
        updates["deleted"].append(endpoint)

    async def mark_failure(endpoint):
        updates["failed"].append(endpoint)

    def fake_send(subscription, _payload, _key):
        if subscription["endpoint"].endswith("/ok"):
            return service.PushAttempt(202)
        if subscription["endpoint"].endswith("/gone"):
            return service.PushAttempt(410, "WebPushException")
        return service.PushAttempt(None, "Timeout")

    async def direct_to_thread(func, *args):
        return func(*args)

    monkeypatch.setattr(service.repository, "list_subscriptions", list_subscriptions)
    monkeypatch.setattr(service.repository, "mark_success", mark_success)
    monkeypatch.setattr(service.repository, "delete_subscription", delete_subscription)
    monkeypatch.setattr(service.repository, "mark_failure", mark_failure)
    monkeypatch.setattr(service, "ensure_vapid_private_key", lambda: tmp_path / "vapid.pem")
    monkeypatch.setattr(service, "_send_one", fake_send)
    monkeypatch.setattr(service.asyncio, "to_thread", direct_to_thread)

    summary = asyncio.run(
        service.broadcast_alarm(
            {"id": "alarm-1", "ids": ["alarm-1"], "content": "起床"}
        )
    )

    assert summary == {"sent": 1, "deleted": 1, "failed": 1}
    assert updates == {
        "success": ["https://push.test/ok"],
        "deleted": ["https://push.test/gone"],
        "failed": ["https://push.test/timeout"],
    }


def test_service_worker_and_page_bootstrap_cover_delivery_contract():
    static_dir = Path(__file__).parents[1] / "static"
    worker = (static_dir / "sw.js").read_text(encoding="utf-8")
    bootstrap = (static_dir / "web_push.js").read_text(encoding="utf-8")
    home = (static_dir / "home.html").read_text(encoding="utf-8")
    common = (static_dir / "common.js").read_text(encoding="utf-8")
    chat_alarms = (static_dir / "js/chat/alarms.js").read_text(encoding="utf-8")

    assert "includeUncontrolled: true" in worker
    assert "client.focused" in worker
    assert "client.postMessage" in worker
    # activate, push, and notificationclick each keep their async work alive.
    assert worker.count("event.waitUntil") >= 3
    assert "notificationclick" in worker
    assert all(
        feature in bootstrap
        for feature in ("'serviceWorker' in navigator", "'PushManager' in window", "'Notification' in window")
    )
    assert "pushManager.subscribe" in bootstrap
    assert "Notification.requestPermission()" in bootstrap
    assert "showFallbackAlarm" in bootstrap
    assert '<script src="/static/web_push.js?v=20260905-brand"></script>' in home
    assert "Notification.requestPermission()" not in common
    assert "sendSystemNotification('⏰ 闹铃'" not in common
    assert "sendSystemNotification('⏰ 闹铃'" not in chat_alarms
