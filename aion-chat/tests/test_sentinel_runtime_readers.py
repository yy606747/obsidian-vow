import asyncio
import json
import sqlite3
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest

import sentinel_runtime_readers
from app.sentinel import SENTINEL_RUNTIME_CONTEXT_SCHEMA_VERSION
from sentinel_runtime_readers import (
    DEFAULT_CORE_WAKE_EXECUTION_CONTEXT_MESSAGE_LIMIT,
    collect_sentinel_runtime_context_payload,
    read_core_wake_execution_context,
    read_sentinel_runtime_context,
)


class _AsyncCursor:
    def __init__(self, cursor):
        self._cursor = cursor

    async def fetchone(self):
        return self._cursor.fetchone()

    async def fetchall(self):
        return self._cursor.fetchall()


class _AsyncSqliteConn:
    def __init__(self, path):
        self._conn = sqlite3.connect(path)
        self._conn.row_factory = sqlite3.Row

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        self._conn.close()
        return False

    @property
    def row_factory(self):
        return self._conn.row_factory

    @row_factory.setter
    def row_factory(self, value):
        self._conn.row_factory = value

    async def execute(self, sql, params=()):
        return _AsyncCursor(self._conn.execute(sql, params))


class _FakeToyDevices:
    def __init__(self, session, *, status="online"):
        self.session = session
        self.status = status

    async def get_device(self, device_id):
        return {
            "ok": True,
            "device": {
                "device_id": device_id,
                "status": self.status,
                "capabilities": ["status.read", "notify.pulse", "toy.legacy_command"],
                "metadata": {
                    "control_session_id": self.session.session_id,
                    "control_kind": self.session.kind,
                    "control_epoch": self.session.control_epoch,
                    "owner_client_id": self.session.owner_client_id,
                },
            },
        }


def _init_db(path, *, now: float):
    conn = sqlite3.connect(path)
    try:
        conn.execute("""
            CREATE TABLE conversations (
                id TEXT PRIMARY KEY,
                title TEXT NOT NULL,
                model TEXT NOT NULL,
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL
            )
        """)
        conn.execute("""
            CREATE TABLE messages (
                id TEXT PRIMARY KEY,
                conv_id TEXT NOT NULL,
                role TEXT NOT NULL,
                content TEXT NOT NULL,
                created_at REAL NOT NULL,
                attachments TEXT DEFAULT ''
            )
        """)
        conn.execute(
            "INSERT INTO conversations (id, title, model, created_at, updated_at) VALUES (?,?,?,?,?)",
            ("conv_old", "Old", "mock", now - 500, now - 400),
        )
        conn.execute(
            "INSERT INTO conversations (id, title, model, created_at, updated_at) VALUES (?,?,?,?,?)",
            ("conv_new", "New", "mock", now - 300, now - 10),
        )
        rows = [
            ("m1", "conv_new", "user", "我先刷一会。", now - 120, "[]"),
            ("m2", "conv_new", "assistant", "好。", now - 100, "[]"),
            ("m3", "conv_old", "user", "旧对话不要进最近上下文。", now - 50, "[]"),
        ]
        conn.executemany(
            "INSERT INTO messages (id, conv_id, role, content, created_at, attachments) VALUES (?,?,?,?,?,?)",
            rows,
        )
        conn.commit()
    finally:
        conn.close()


def _db_factory(path):
    @asynccontextmanager
    async def factory():
        async with _AsyncSqliteConn(path) as db:
            yield db

    return factory


def _write_logs(log_dir, *, now: float):
    log_dir.mkdir()
    entries = [
        {
            "timestamp": now - 30000,
            "time": "19:00:00",
            "source": "sentinel",
            "monitoringlog": "太旧，不应进入。",
            "score": 1,
            "call_core": False,
            "status": "decided",
        },
        {
            "timestamp": now - 600,
            "time": "21:50:00",
            "source": "sentinel",
            "monitoringlog": "信号不足。",
            "score": 4,
            "call_core": False,
            "status": "decided",
        },
        {
            "timestamp": now - 300,
            "time": "21:55:00",
            "source": "sentinel",
            "monitoringlog": "可能是好时机。",
            "score": 7,
            "call_core": True,
            "status": "core_wake_requested",
        },
        {
            "timestamp": now - 100,
            "time": "21:58:00",
            "source": "camera",
            "monitoringlog": "旧摄像头日志不应进入。",
            "score": 9,
            "call_core": True,
        },
    ]
    content = "\n".join(json.dumps(item, ensure_ascii=False) for item in entries)
    (log_dir / "2026-05-14.jsonl").write_text(content + "\n", encoding="utf-8")


def test_runtime_readers_collect_material_and_build_normalized_context(tmp_path):
    now = 1_700_000_000.0
    db_path = tmp_path / "runtime.db"
    log_dir = tmp_path / "logs"
    _init_db(db_path, now=now)
    _write_logs(log_dir, now=now)
    payload = asyncio.run(collect_sentinel_runtime_context_payload(
        reference_time=now,
        db_factory=_db_factory(db_path),
        monitor_logs_dir=log_dir,
        worldbook_loader=lambda: {"user_name": "云", "ai_name": "Aion"},
        ai_behavior_loader=lambda: {"sentinel_call_core_criteria": "好时机可以主动出现。"},
        cam_config_loader=lambda: {
            "quiet_hours_enabled": True,
            "quiet_hours_start": "00:00",
            "quiet_hours_end": "23:59",
        },
    ))
    context = asyncio.run(read_sentinel_runtime_context(
        reference_time=now,
        db_factory=_db_factory(db_path),
        monitor_logs_dir=log_dir,
        worldbook_loader=lambda: {"user_name": "云", "ai_name": "Aion"},
        ai_behavior_loader=lambda: {"sentinel_call_core_criteria": "好时机可以主动出现。"},
        cam_config_loader=lambda: {
            "quiet_hours_enabled": True,
            "quiet_hours_start": "00:00",
            "quiet_hours_end": "23:59",
        },
    ))

    assert payload["user_name"] == "云"
    assert payload["ai_name"] == "Aion"
    assert payload["recent_chat"] == [
        {"role": "user", "content": "我先刷一会。"},
        {"role": "assistant", "content": "好。"},
    ]
    assert payload["last_user_message_age_sec"] == 50.0
    assert payload["recent_sentinel_logs"] == [
        {
            "time": "21:50:00",
            "status": "decided",
            "monitoringlog": "信号不足。",
            "score": 4,
            "call_core": False,
        },
        {
            "time": "21:55:00",
            "status": "core_wake_requested",
            "monitoringlog": "可能是好时机。",
            "score": 7,
            "call_core": True,
        },
    ]
    assert payload["last_wake_age_sec"] == 300.0
    assert payload["quiet_hours_active"] is True
    assert payload["clear_sleep"] is False
    assert payload["device_effect_requested"] is False
    assert payload["device_effect_allowed"] is False
    assert payload["urgent_risk"] is False
    assert "memories" not in payload
    assert context["schema_version"] == SENTINEL_RUNTIME_CONTEXT_SCHEMA_VERSION
    assert context["side_effects"] == []
    assert context["fallback_used"] is False
    assert context["judgment_context"]["recent_chat"] == ["云: 我先刷一会。", "Aion: 好。"]
    assert "memories" not in context["wake_context"]
    assert context["gate_context"]["last_user_message_age_sec"] == 50.0
    assert context["gate_context"]["last_wake_age_sec"] == 300.0
    assert context["gate_context"]["quiet_hours_active"] is True
    assert context["gate_context"]["clear_sleep"] is False
    assert context["gate_context"]["device_effect_requested"] is False
    assert context["gate_context"]["device_effect_allowed"] is False
    assert context["gate_context"]["urgent_risk"] is False


def test_core_wake_execution_context_reads_latest_conversation_material(tmp_path):
    now = 1_700_000_000.0
    db_path = tmp_path / "runtime.db"
    _init_db(db_path, now=now)

    context = asyncio.run(read_core_wake_execution_context(
        reference_time=now,
        db_factory=_db_factory(db_path),
        worldbook_loader=lambda: {"user_name": "云", "ai_name": "Aion"},
    ))

    assert DEFAULT_CORE_WAKE_EXECUTION_CONTEXT_MESSAGE_LIMIT == 20
    assert context == {
        "user_name": "云",
        "ai_name": "Aion",
        "recent_messages": [
            {"id": "m1", "role": "user", "content": "我先刷一会。"},
            {"id": "m2", "role": "assistant", "content": "好。"},
        ],
        "conv_id": "conv_new",
        "model_key": "mock",
        "toy_capability_allowed": False,
        "toy_capability_reason": "session_lookup_failed",
        "last_user_message_age_sec": 50.0,
    }


def test_runtime_readers_use_voice_transcript_and_skip_empty_attachment_messages(tmp_path):
    now = 1_700_000_000.0
    db_path = tmp_path / "runtime.db"
    _init_db(db_path, now=now)
    voice_attachment = json.dumps([{
        "type": "voice",
        "url": "/uploads/voice.wav",
        "mime_type": "audio/wav",
        "duration_ms": 3000,
        "transcript": "能听到我的声音吗？😔",
    }], ensure_ascii=False)
    image_attachment = json.dumps([{"type": "image", "url": "/uploads/photo.jpg"}])
    conn = sqlite3.connect(db_path)
    try:
        conn.executemany(
            "INSERT INTO messages (id, conv_id, role, content, created_at, attachments) "
            "VALUES (?,?,?,?,?,?)",
            [
                ("m_voice", "conv_new", "user", "", now - 80, voice_attachment),
                ("m_attachment_only", "conv_new", "assistant", "", now - 70, image_attachment),
            ],
        )
        conn.commit()
    finally:
        conn.close()

    recent_chat = asyncio.run(sentinel_runtime_readers._read_recent_chat(
        _db_factory(db_path),
        limit=10,
    ))
    recent_messages = asyncio.run(
        sentinel_runtime_readers._read_recent_messages_for_conversation(
            _db_factory(db_path),
            conv_id="conv_new",
            limit=20,
        )
    )

    assert recent_chat == [
        {"role": "user", "content": "我先刷一会。"},
        {"role": "assistant", "content": "好。"},
        {"role": "user", "content": "能听到我的声音吗？😔"},
    ]
    assert recent_messages[-1] == {
        "id": "m_voice",
        "role": "user",
        "content": "能听到我的声音吗？😔",
    }
    assert all(item.get("id") != "m_attachment_only" for item in recent_messages)


def test_runtime_readers_return_empty_context_when_no_conversation_or_logs(tmp_path):
    db_path = tmp_path / "empty.db"
    log_dir = tmp_path / "missing_logs"
    conn = sqlite3.connect(db_path)
    try:
        conn.execute("CREATE TABLE conversations (id TEXT PRIMARY KEY, title TEXT, model TEXT, created_at REAL, updated_at REAL)")
        conn.execute("CREATE TABLE messages (id TEXT PRIMARY KEY, conv_id TEXT, role TEXT, content TEXT, created_at REAL, attachments TEXT)")
        conn.commit()
    finally:
        conn.close()

    context = asyncio.run(read_sentinel_runtime_context(
        reference_time=1_700_000_000.0,
        db_factory=_db_factory(db_path),
        monitor_logs_dir=log_dir,
        worldbook_loader=lambda: {},
        ai_behavior_loader=lambda: {},
    ))

    assert context["judgment_context"]["recent_chat"] == []
    assert context["judgment_context"]["recent_sentinel_logs"] == []
    assert "memories" not in context["wake_context"]
    assert "last_user_message_age_sec" not in context["gate_context"]
    assert "last_wake_age_sec" not in context["gate_context"]
    assert context["gate_context"]["quiet_hours_active"] is False
    assert context["gate_context"]["clear_sleep"] is False
    assert context["gate_context"]["device_effect_requested"] is False
    assert context["gate_context"]["device_effect_allowed"] is False
    assert context["gate_context"]["urgent_risk"] is False

    core_context = asyncio.run(read_core_wake_execution_context(
        reference_time=1_700_000_000.0,
        db_factory=_db_factory(db_path),
        worldbook_loader=lambda: {},
    ))

    assert core_context == {
        "user_name": "她",
        "ai_name": "我",
        "recent_messages": [],
    }


def test_core_wake_execution_context_reads_persona_without_legacy_whisper_state(tmp_path):
    now = 1_700_000_000.0
    db_path = tmp_path / "runtime.db"
    _init_db(db_path, now=now)

    context = asyncio.run(read_core_wake_execution_context(
        reference_time=now,
        db_factory=_db_factory(db_path),
        worldbook_loader=lambda: {
            "user_name": "云",
            "ai_name": "Aion",
            "ai_persona": "Aion 是温柔但有掌控感的伴侣。",
            "user_persona": "用户最近压力偏大。",
        },
    ))

    assert context["ai_persona"] == "Aion 是温柔但有掌控感的伴侣。"
    assert context["user_persona"] == "用户最近压力偏大。"
    assert context["toy_capability_allowed"] is False
    assert "whisper_active" not in context
    assert context["conv_id"] == "conv_new"
    assert context["model_key"] == "mock"


def test_core_wake_execution_context_includes_current_control_session(tmp_path):
    now = 1_700_000_000.0
    db_path = tmp_path / "runtime.db"
    _init_db(db_path, now=now)

    class FakeControlSessions:
        async def get_current(self, *, conv_id):
            assert conv_id == "conv_new"
            return SimpleNamespace(
                session_id="ctrl_reader",
                conv_id=conv_id,
                kind="whisper",
                status="active",
                owner_client_id="tab_reader",
                control_epoch=3,
                device_id="browser_toy_bridge",
            )

    session = asyncio.run(FakeControlSessions().get_current(conv_id="conv_new"))

    context = asyncio.run(read_core_wake_execution_context(
        reference_time=now,
        db_factory=_db_factory(db_path),
        worldbook_loader=lambda: {},
        control_session_service_obj=FakeControlSessions(),
        device_service_obj=_FakeToyDevices(session),
    ))

    assert context["toy_capability_allowed"] is True
    assert context["toy_capability_reason"] == "allowed"
    assert context["control_session_id"] == "ctrl_reader"
    assert context["control_kind"] == "whisper"
    assert context["control_status"] == "active"
    assert context["control_epoch"] == 3
    assert context["owner_client_id"] == "tab_reader"
    assert context["control_device_id"] == "browser_toy_bridge"


def test_core_wake_execution_context_fails_loud_on_bad_persona(tmp_path):
    now = 1_700_000_000.0
    db_path = tmp_path / "runtime.db"
    _init_db(db_path, now=now)

    with pytest.raises(ValueError, match="worldbook ai_persona must be text"):
        asyncio.run(read_core_wake_execution_context(
            reference_time=now,
            db_factory=_db_factory(db_path),
            worldbook_loader=lambda: {"ai_persona": 123},
        ))


def test_runtime_readers_last_wake_age_uses_full_lookback_not_prompt_log_limit(tmp_path):
    now = 1_700_000_000.0
    db_path = tmp_path / "runtime.db"
    log_dir = tmp_path / "logs"
    _init_db(db_path, now=now)
    log_dir.mkdir()
    entries = [
        {
            "timestamp": now - 500,
            "source": "sentinel",
            "monitoringlog": "这次叫醒过。",
            "score": 8,
            "call_core": True,
        },
        {
            "timestamp": now - 60,
            "source": "sentinel",
            "monitoringlog": "最近一条只是观察。",
            "score": 2,
            "call_core": False,
        },
    ]
    (log_dir / "2026-05-14.jsonl").write_text(
        "\n".join(json.dumps(item, ensure_ascii=False) for item in entries) + "\n",
        encoding="utf-8",
    )

    payload = asyncio.run(collect_sentinel_runtime_context_payload(
        reference_time=now,
        db_factory=_db_factory(db_path),
        monitor_logs_dir=log_dir,
        sentinel_log_limit=1,
    ))

    assert payload["recent_sentinel_logs"] == [{
        "monitoringlog": "最近一条只是观察。",
        "score": 2,
        "call_core": False,
    }]
    assert payload["last_wake_age_sec"] == 500.0


def test_runtime_readers_fail_loud_on_malformed_monitor_log(tmp_path):
    now = 1_700_000_000.0
    db_path = tmp_path / "runtime.db"
    log_dir = tmp_path / "logs"
    _init_db(db_path, now=now)
    log_dir.mkdir()
    (log_dir / "2026-05-14.jsonl").write_text("{not json}\n", encoding="utf-8")

    with pytest.raises(ValueError, match="invalid JSON"):
        asyncio.run(read_sentinel_runtime_context(
            reference_time=now,
            db_factory=_db_factory(db_path),
            monitor_logs_dir=log_dir,
        ))


def test_runtime_readers_fail_loud_on_bad_gate_fact_sources(tmp_path):
    now = 1_700_000_000.0
    db_path = tmp_path / "runtime.db"
    log_dir = tmp_path / "logs"
    _init_db(db_path, now=now)
    log_dir.mkdir()
    (log_dir / "2026-05-14.jsonl").write_text(json.dumps({
        "timestamp": now - 60,
        "source": "sentinel",
        "monitoringlog": "坏日志。",
        "score": 7,
        "call_core": "yes",
    }, ensure_ascii=False) + "\n", encoding="utf-8")

    with pytest.raises(ValueError, match="call_core must be a boolean"):
        asyncio.run(read_sentinel_runtime_context(
            reference_time=now,
            db_factory=_db_factory(db_path),
            monitor_logs_dir=log_dir,
        ))

    with pytest.raises(ValueError, match="quiet_hours_start must be HH:MM text"):
        asyncio.run(read_sentinel_runtime_context(
            reference_time=now,
            db_factory=_db_factory(db_path),
            monitor_logs_dir=tmp_path / "missing_logs",
            cam_config_loader=lambda: {
                "quiet_hours_enabled": True,
                "quiet_hours_start": "bad",
                "quiet_hours_end": "09:00",
            },
        ))
