import asyncio
from collections import deque
from contextlib import asynccontextmanager
from copy import deepcopy
from functools import wraps
import io
import json

import aiosqlite
from PIL import Image
import pytest

from app.together.config import TogetherConfig
from app.together.context import Identity, bounded_history, resolve_entry
from app.together.media import MediaStore
from app.together.protocol import TogetherOutput
from app.together.repository import TogetherRepository
from app.together.schema import init_together_tables
from app.together.service import TogetherConflict, TogetherDeviceMismatch, TogetherService


def _async_test(fn):
    @wraps(fn)
    def run(*args, **kwargs):
        return asyncio.run(fn(*args, **kwargs))
    return run


class Clock:
    value = 10000.0

    def __call__(self):
        return self.value

    def advance(self, seconds):
        self.value += seconds


class Models:
    def __init__(self, *outputs):
        self.outputs = deque(outputs)
        self.calls = []
        self.hook = None

    async def stream(self, model_key, messages, meta):
        self.calls.append(deepcopy(messages))
        meta.update(prompt_tokens=100, completion_tokens=20, reasoning_tokens=8)
        if self.hook:
            async for chunk in self.hook(messages):
                yield chunk
            return
        output = self.outputs.popleft() if self.outputs else "在这里。"
        for char in output:
            yield char

    async def transcribe(self, data, **kwargs):
        return "转写草稿"


@asynccontextmanager
async def harness(tmp_path, *, models=None, entry=None, config_overrides=None):
    @asynccontextmanager
    async def db():
        async with aiosqlite.connect(tmp_path / "together.db") as connection:
            yield connection
    async with db() as connection:
        await init_together_tables(connection)
        await connection.execute("CREATE TABLE IF NOT EXISTS conversations(id TEXT PRIMARY KEY,model TEXT,updated_at REAL DEFAULT 0)")
        await connection.execute("CREATE TABLE IF NOT EXISTS messages(id TEXT PRIMARY KEY,conv_id TEXT,role TEXT,content TEXT,created_at REAL)")
        await connection.commit()
    clock = Clock()
    repository = TogetherRepository(db, now=clock)
    cfg = TogetherConfig(tmp_path/"config.json")
    cfg.save({"enabled": True, **(config_overrides or {})})
    async def identity():
        return Identity("小栀", "阿澈", "PERSONA", "OWNER_PERSONA", "CURRENT_UNDERSTANDING")
    async def entry_loader(conv_id, count):
        return conv_id, "vision-model", (entry or [])[-count:] if count else []
    async def device_check(device_id):
        if device_id != "android_phone":
            raise ValueError("together_mobile_device_missing")
    async def activity_check():
        return False
    service = TogetherService(repository=repository, config=cfg, models=models or Models(), now=clock,
        media=MediaStore(tmp_path/"media"), identity_loader=identity, names_loader=lambda: ("小栀", "阿澈"),
        entry_loader=entry_loader, image_check=lambda model: model == "vision-model", model_check=lambda _: True,
        device_check=device_check, activity_check=activity_check)
    try:
        yield service, clock
    finally:
        await service.close()


async def start(service, **kwargs):
    return await service.start(request_id="request1", conv_id="conv1", device_id="android_phone", **kwargs)


async def settle(service):
    for _ in range(20):
        tasks = list(service.workers.values())
        if not tasks:
            return
        await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True), timeout=3)
        await asyncio.sleep(0)
    raise AssertionError("workers did not settle")


async def ready(service, session_id, *, busy=False):
    row = await service.repo.session(session_id)
    return await service.heartbeat(session_id, device_id="android_phone", source_version=row["source_version"], capture_state="ready", input_busy=busy)


@pytest.mark.parametrize("table,status,occupied", [
    (None, None, False),
    ("scene_sessions", "ended", False),
    ("look_at_me_requests", "queued", False),
    ("scene_sessions", "active", True),
    ("look_at_me_requests", "active", True),
])
@_async_test
async def test_entry_with_independently_installed_activity_modules(tmp_path, table, status, occupied):
    async with harness(tmp_path) as (svc, _):
        if table:
            await svc.repo.execute(f"CREATE TABLE {table}(id TEXT PRIMARY KEY,status TEXT,expires_at REAL DEFAULT 0,choice_deadline REAL)")
            await svc.repo.execute(f"INSERT INTO {table}(id,status) VALUES ('other',?)", (status,))
        svc.activity_check = svc._other_activity
        if occupied:
            with pytest.raises(TogetherConflict, match="other_activity"):
                await start(svc)
            assert await svc.repo.current() is None
        else:
            session = await start(svc)
            await settle(svc)
            assert (await svc.repo.session(session["id"]))["status"] == "active"


@_async_test
async def test_entry_configuration_uses_live_names_without_starting_a_session(tmp_path):
    from routes.together import configuration
    async with harness(tmp_path) as (svc, _):
        first = await configuration(service=svc)
        assert (first["user_name"], first["ai_name"]) == ("小栀", "阿澈")
        svc.names_loader = lambda: ("青禾", "临川")
        renamed = await configuration(service=svc)
        assert (renamed["user_name"], renamed["ai_name"]) == ("青禾", "临川")
        assert await svc.repo.current() is None
        assert svc.models.calls == []


def test_recent_companionship_is_dated_bounded_and_expires_without_inventing_notes():
    from app.together.recent import render_recent, TTL_SECONDS
    now = 200000
    rows = [{"status": "ended", "ended_at": now-offset, "active_seconds": 60+offset}
            for offset in (1, 2, 3, 4)]
    rows.extend([{"status": "ended", "ended_at": now-TTL_SECONDS, "active_seconds": 1000, "note": "已过期"},
                 {"status": "active", "ended_at": now, "active_seconds": 1000, "note": "未结束"},
                 {"status": "ended", "ended_at": now+1, "active_seconds": 1000, "note": "未来"}])
    text = render_recent(rows, user_name="小栀", ai_name="阿澈", now=now, timezone="Asia/Shanghai")
    assert text.count("阿澈陪小栀待了") == 3
    assert "+08:00" in text and "以上相处均已结束" in text
    assert all(word not in text for word in ("笔记", "已过期", "未结束", "未来"))
    assert render_recent(rows[:4], user_name="小栀", ai_name="阿澈", now=now+TTL_SECONDS) == ""


@_async_test
async def test_recent_companionship_survives_transcript_cleanup_without_extra_calls(tmp_path):
    from app.together.recent import load_recent, TTL_SECONDS
    async with harness(tmp_path, models=Models("在这里。<remember>一起听雨。" + "安静"*100 + "</remember>")) as (svc, clock):
        sid = (await start(svc))["id"]
        await settle(svc)
        args = dict(user_name="小栀", ai_name="阿澈", repository=svc.repo, timezone="UTC")
        assert await load_recent(now=clock(), **args) == ""
        clock.advance(120)
        await svc.action(sid, device_id="android_phone", action="end")
        before = await load_recent(now=clock(), **args)
        await svc.repo.execute("UPDATE together_sessions SET event_message_id='persisted-event' WHERE id=?", (sid,))
        assert await svc.repo.cleanup_transcript(sid)
        assert await svc.repo.read("SELECT * FROM together_messages", many=True) == []
        assert await load_recent(now=clock(), **args) == before
        note_line = next(line for line in before.splitlines() if "主动留下的笔记" in line)
        assert note_line.endswith("一起听雨。" + "安静"*47 + "安")
        assert len(note_line.split("：", 1)[1]) == 100
        clock.advance(TTL_SECONDS)
        assert await load_recent(now=clock(), **args) == ""
        assert (await svc.repo.session(sid))["event_message_id"] == "persisted-event"
        assert len(svc.models.calls) == 1


@_async_test
async def test_active_companionship_blocks_send_and_regenerate_before_any_chat_write(tmp_path, monkeypatch):
    from routes import chat
    from app.chat.models import MsgCreate
    from app.together.chat_gate import ordinary_busy
    async with harness(tmp_path) as (svc, clock):
        sid = (await start(svc))["id"]
        await settle(svc)
        await svc.repo.execute("INSERT INTO messages VALUES ('old','conv1','assistant','已有回复',?)", (clock(),))
        monkeypatch.setattr(chat, "get_db", svc.repo.db)
        # A lost capture lease must not reopen ordinary chat behind an active room.
        clock.advance(3600)
        send = await chat.send_message("conv1", MsgCreate(content="这条不应写入"))
        regen = await chat.regenerate_message("conv1", replaced_message_id="old")
        for response in (send, regen):
            assert response.status_code == 409
            assert json.loads(response.body)["session_id"] == sid
        assert not ordinary_busy()
        rows = await svc.repo.read("SELECT id,content FROM messages", many=True)
        assert rows == [{"id": "old", "content": "已有回复"}]


@pytest.mark.parametrize("finish", ["complete", "cancel", "error"])
@_async_test
async def test_ordinary_generation_reservation_outlives_http_response(tmp_path, monkeypatch, finish):
    from app.together.chat_gate import ordinary_turn, bind_generation, ordinary_busy
    async with harness(tmp_path) as (svc, _):
        monkeypatch.setitem(globals(), "get_db", svc.repo.db)
        proceed = asyncio.Event()
        async def generate():
            await proceed.wait()
            if finish == "error":
                raise ValueError("mock model failed")
        @ordinary_turn
        async def route():
            assert ordinary_busy()
            with pytest.raises(TogetherConflict, match="together_chat_busy"):
                await start(svc)
            task = asyncio.create_task(generate())
            bind_generation(task)
            return task
        task = await route()
        assert ordinary_busy()
        with pytest.raises(TogetherConflict, match="together_chat_busy"):
            await start(svc)
        if finish == "cancel":
            task.cancel()
        else:
            proceed.set()
        await asyncio.gather(task, return_exceptions=True)
        await asyncio.sleep(0)
        assert not ordinary_busy()
        assert (await start(svc))["status"] == "active"


@_async_test
async def test_ordinary_prompt_failure_releases_entry_reservation(tmp_path, monkeypatch):
    from app.together.chat_gate import ordinary_turn, ordinary_busy
    async with harness(tmp_path) as (svc, _):
        monkeypatch.setitem(globals(), "get_db", svc.repo.db)
        @ordinary_turn
        async def route():
            assert ordinary_busy()
            raise ValueError("prompt failure")
        with pytest.raises(ValueError, match="prompt failure"):
            await route()
        assert not ordinary_busy()
        assert (await start(svc))["status"] == "active"


async def say(service, sid, text, client_id="m1", capture_id=None):
    return await service.message(sid, device_id="android_phone", client_id=client_id, text=text, capture_id=capture_id)


def jpeg():
    buffer = io.BytesIO()
    Image.new("RGB", (50, 40), "green").save(buffer, "JPEG")
    return buffer.getvalue()


@_async_test
async def test_ordinary_wake_hold_preserves_expiry_and_never_consumes_quota(tmp_path, monkeypatch):
    from app.self_wake.schema import init_self_wake_tables
    from app.self_wake import repository as global_wakes
    from app.together import coexist
    async with harness(tmp_path) as (svc, clock):
        monkeypatch.setattr(coexist, "together_config", svc.config)
        monkeypatch.setattr(global_wakes, "get_db", svc.repo.db)
        async with svc.repo.db() as db:
            await init_self_wake_tables(db)
            await db.execute("INSERT INTO conversations VALUES ('conv1','model',0)")
            await db.commit()
        wake = (await global_wakes.self_wake_repository.schedule_or_replace(
            wake_at=clock()+120, intent="回来看看", requested_capabilities=[], origin="relationship", origin_ref="conv1",
            source="chat", conv_id="conv1", source_turn_id="real_turn", owner_timezone="UTC", now=clock()))["wake"]
        session = await start(svc)
        await settle(svc)
        clock.advance(4000)
        # Simulate heartbeats without invoking the per-second scanner.
        await svc.repo.update(session["id"], heartbeat_at=clock())
        assert await global_wakes.claim_due_batch(now=clock(), timezone_name="UTC") == []
        held = await global_wakes.self_wake_repository.get(wake["id"])
        assert held["state"] == "pending" and held["consumed_at"] is None
        await svc.action(session["id"], device_id="android_phone", action="end")
        claims = await global_wakes.claim_due_batch(now=clock(), timezone_name="UTC")
        assert len(claims) == 1 and claims[0]["id"] == wake["id"]
        assert claims[0]["expires_at"] == wake["expires_at"]+4000


@_async_test
async def test_claim_to_call_race_refunds_unstarted_wake(tmp_path, monkeypatch):
    from app.self_wake.schema import init_self_wake_tables
    from app.self_wake import repository as wakes
    from app.together import coexist
    async with harness(tmp_path) as (svc, clock):
        monkeypatch.setattr(coexist, "together_config", svc.config)
        monkeypatch.setattr(wakes, "get_db", svc.repo.db)
        async with svc.repo.db() as db:
            await init_self_wake_tables(db)
            await db.execute("INSERT INTO conversations VALUES ('conv1','model',0)")
            await db.commit()
        wake = (await wakes.self_wake_repository.schedule_or_replace(
            wake_at=clock()+120, intent="回来看看", requested_capabilities=[], origin="relationship", origin_ref="conv1",
            source="chat", conv_id="conv1", source_turn_id="real_turn", owner_timezone="UTC", now=clock()))["wake"]
        clock.advance(120)
        assert len(await wakes.claim_due_batch(now=clock(), timezone_name="UTC")) == 1
        await start(svc)
        await settle(svc)
        assert await wakes.self_wake_repository.defer_unstarted(wake["id"], now=clock())
        result = await wakes.self_wake_repository.get(wake["id"])
        assert result["state"] == "pending" and result["consumed_at"] is None


@_async_test
async def test_inflight_reply_is_durable_and_delivered_once_without_core(tmp_path, monkeypatch):
    from app.together import coexist, delivery
    async with harness(tmp_path) as (svc, clock):
        monkeypatch.setattr(coexist, "together_config", svc.config)
        await svc.repo.execute("ALTER TABLE messages ADD COLUMN attachments TEXT DEFAULT '[]'")
        await svc.repo.execute("INSERT INTO conversations VALUES ('conv1','model',0)")
        session = await start(svc)
        await settle(svc)
        reply = {"id": "ordinary_reply", "conv_id": "conv1", "role": "assistant", "content": "刚刚想到的事", "attachments": [], "created_at": clock()}
        async with svc.repo.db() as db:
            await db.execute("BEGIN IMMEDIATE")
            assert await delivery.defer_in_tx(db, [reply], now=clock())
            assert await delivery.defer_in_tx(db, [reply], now=clock())
            await db.commit()
        assert await svc.repo.read("SELECT id FROM messages") is None
        assert await delivery.drain(db_factory=svc.repo.db, now=clock()) == 0
        calls = len(svc.models.calls)
        await svc.action(session["id"], device_id="android_phone", action="end")
        broadcasts = []
        async def broadcast(event): broadcasts.append(event)
        assert await delivery.drain(db_factory=svc.repo.db, broadcast=broadcast, now=clock()) == 1
        assert await delivery.drain(db_factory=svc.repo.db, broadcast=broadcast, now=clock()) == 0
        assert len(broadcasts) == 1 and len(svc.models.calls) == calls
        assert (await svc.repo.read("SELECT payload FROM together_deliveries"))["payload"] == {}


@_async_test
async def test_invitation_hold_restores_remaining_choice_time(tmp_path, monkeypatch):
    from app.look_at_me.repository import init_tables, LookAtMeRepository
    from app.together import coexist
    async with harness(tmp_path) as (svc, clock):
        monkeypatch.setattr(coexist, "together_config", svc.config)
        async with svc.repo.db() as db:
            await init_tables(db)
            await db.commit()
        invitations = LookAtMeRepository(svc.repo.db, now=clock)
        invite = await invitations.create(conv_id="conv1", origin_conv_id="conv1", model_key="model", device_id="android_phone",
            source_turn="ordinary", source_intent="invite", expression="待会儿", duration_seconds=120)
        session = await start(svc)
        await settle(svc)
        clock.advance(500)
        await svc.repo.update(session["id"], heartbeat_at=clock())
        assert await invitations.pending("android_phone") is None
        await svc.action(session["id"], device_id="android_phone", action="end")
        pending = await invitations.pending("android_phone")
        assert pending["id"] == invite["id"] and pending["expires_at"] == invite["expires_at"]+500


def test_protocol_streaming_hides_all_controls_and_supports_silence():
    parser = TogetherOutput()
    output = '好。<remember>记着这件事。</remember><self_awake>{"after_seconds":1,"intent":"回来","needs_image":true}</self_awake><look/>'
    shown = "".join(parser.feed(c) for c in output)
    result = parser.finish()
    assert shown == "好。"
    assert result["notes"] == ["记着这件事。"]
    assert result["look"] and result["awake"]["needs_image"]
    quiet = TogetherOutput()
    assert quiet.feed('<self_awake>{"after_seconds":60}</self_awake>') == ""
    assert quiet.finish()["visible"] == ""


def test_literal_comparisons_and_heart_are_not_control_tags():
    parser = TogetherOutput()
    text = "<3 今天的温度 < 昨天。<b>这段是原样的文字</b>"
    assert "".join(parser.feed(char) for char in text) == text
    assert parser.finish()["notes"] == []


@_async_test
async def test_can_look_then_stay_silent_without_scheduling_another_wake(tmp_path):
    models = Models('<self_awake>{"after_seconds":30,"needs_image":true}</self_awake>', '<silent/>')
    async with harness(tmp_path, models=models) as (svc, clock):
        session = await start(svc)
        await settle(svc)
        await ready(svc, session["id"])
        clock.advance(30)
        await svc.tick()
        capture = None
        for _ in range(100):
            captures = await svc.repo.captures(session["id"])
            if captures:
                capture = captures[0]
                break
            await asyncio.sleep(.01)
        assert capture is not None
        await svc.receive_capture(session["id"], capture["id"], device_id="android_phone", source_version=1,
                                  captured_at=clock(), data=jpeg())
        await settle(svc)
        assert len(models.calls) == 2
        assert (await svc.repo.session(session["id"]))["wake_at"] is None
        assert await svc.repo.messages(session["id"]) == []
        assert not list(svc.media.root().glob("*.jpg"))


@pytest.mark.parametrize("recovery_offsets", [(), (1,), (1, 5), (1, 1000), (1000,), (1000, 1100)])
@_async_test
async def test_offline_end_receipt_uses_real_end_time_after_lease_expiry(tmp_path, recovery_offsets):
    async with harness(tmp_path) as (svc, clock):
        session = await start(svc)
        await settle(svc)
        started_at, ended_at = clock(), clock()+8
        for offset in recovery_offsets:
            clock.advance(started_at+offset-clock())
            await svc.repo.recover()
        clock.advance(started_at+2000-clock())
        await svc.tick()
        ended = await svc.action(session["id"], device_id="android_phone", action="end", ended_at=ended_at)
        assert ended["active_seconds"] == 8
        assert ended["ended_at"] == ended_at
        again = await svc.action(session["id"], device_id="android_phone", action="end", ended_at=ended_at)
        assert again["active_seconds"] == 8


@_async_test
async def test_existing_database_migrates_without_losing_session_or_end_time(tmp_path):
    async with harness(tmp_path) as (svc, clock):
        session = await start(svc)
        await settle(svc)
        async with svc.repo.db() as db:
            await db.execute("ALTER TABLE together_sessions DROP COLUMN recovery_heartbeat_at")
            await db.commit()
            await init_together_tables(db)
            await init_together_tables(db)
            await db.commit()
        assert (await svc.repo.session(session["id"]))["status"] == "active"
        assert await svc.repo.messages(session["id"])
        ended_at = clock()+8
        clock.advance(1000)
        await svc.repo.recover()
        ended = await svc.action(session["id"], device_id="android_phone", action="end", ended_at=ended_at)
        assert ended["active_seconds"] == 8


@_async_test
async def test_reconnection_cannot_overlap_a_new_scene(tmp_path):
    async with harness(tmp_path) as (svc, clock):
        session = await start(svc)
        await settle(svc)
        clock.advance(60)
        async def occupied(): return True
        svc.activity_check = occupied
        with pytest.raises(TogetherConflict, match="other_activity"):
            await ready(svc, session["id"])
        assert not svc.online(await svc.repo.session(session["id"]))
        assert (await svc.repo.session(session["id"]))["status"] == "active"


@_async_test
async def test_asr_uses_configured_slot_outside_the_event_loop(monkeypatch):
    import threading
    import wave
    import config
    import ai_providers
    from app.together.models import TogetherModels
    from app.provider_privacy import private_provider_scope
    main_thread = threading.get_ident()
    monkeypatch.setattr(config, "get_slot", lambda name: {"model": "owner-selected-asr"} if name == "asr" else None)
    def asr(data, **kwargs):
        assert threading.get_ident() != main_thread
        assert private_provider_scope.get() == "together:asr"
        assert kwargs["timeout"] == 60
        return "这是可以修改的转写。"
    monkeypatch.setattr(ai_providers, "call_slot_asr", asr)
    audio = io.BytesIO()
    with wave.open(audio, "wb") as recording:
        recording.setnchannels(1)
        recording.setsampwidth(2)
        recording.setframerate(16000)
        recording.writeframes(b"\x00\x00"*160)
    assert await TogetherModels().transcribe(audio.getvalue()) == "这是可以修改的转写。"


@_async_test
async def test_permission_handoff_waits_for_device_before_first_reply_and_accounting(tmp_path):
    async with harness(tmp_path, models=Models("一起来待会儿。<look/>")) as (svc, clock):
        session = await start(svc, wait_for_device=True)
        await settle(svc)
        assert not session["online"] and not svc.models.calls
        clock.advance(120)
        await svc.tick()
        await ready(svc, session["id"])
        await settle(svc)
        assert len(svc.models.calls) == 1
        assert (await svc.repo.session(session["id"]))["active_seconds"] == 0
        assert await svc.repo.captures(session["id"]) == []


@pytest.mark.parametrize("text", ['<remember>秘密', '<self_awake>{"after_seconds":true}</self_awake>',
    '<self_awake>{"after_seconds":NaN}</self_awake>', '<understand>{"statement":"没有依据"}</understand>'])
def test_invalid_controls_never_commit(text):
    parser = TogetherOutput()
    with pytest.raises((ValueError, TypeError)):
        parser.feed(text)
        parser.finish()


def test_bounded_history_drops_oldest_and_all_attachments():
    rows = bounded_history([{"role": "user", "content": "old-old"}], [
        {"role": "assistant", "content": "abcdefghij", "attachments": ["PRIVATE_IMAGE"]},
        {"role": "user", "content": "latest"}], max_characters=10)
    assert rows == [{"role": "assistant", "content": "ghij"}, {"role": "user", "content": "latest"}]


@_async_test
async def test_entry_zero_and_exact_conversation_without_inheritance(tmp_path, monkeypatch):
    from app.daily_chat import service as daily
    monkeypatch.setattr(daily, "resolve_conversation_id", lambda value: value)
    async with harness(tmp_path) as (service, _):
        async with service.repo.db() as db:
            await db.execute("INSERT INTO conversations(id,model) VALUES ('conv1','vision-model')")
            await db.executemany("INSERT INTO messages VALUES (?,?,?,?,?)", [
                ("1", "conv1", "user", "first", 1), ("2", "conv1", "assistant", "second", 2),
                ("3", "conv1", "system", "EVENT", 3), ("4", "another-day", "user", "OTHER_DAY", 4)])
            await db.commit()
        assert (await resolve_entry("conv1", 0, db_factory=service.repo.db))[2] == []
        assert [x["content"] for x in (await resolve_entry("conv1", 1, db_factory=service.repo.db))[2]] == ["second"]
        assert len((await resolve_entry("conv1", 100, db_factory=service.repo.db))[2]) == 2


@_async_test
async def test_opening_is_text_only_with_identity_and_frozen_entry(tmp_path):
    history = [{"role": "user", "content": "ENTRY_SNAPSHOT"}]
    async with harness(tmp_path, entry=history) as (service, _):
        row = await start(service, history_count=1)
        history.append({"role": "user", "content": "LATER_MAIN_CHAT"})
        await settle(service)
        assert len(service.models.calls) == 1
        body = str(service.models.calls[0])
        for value in ("PERSONA", "OWNER_PERSONA", "CURRENT_UNDERSTANDING", "ENTRY_SNAPSHOT"):
            assert value in body
        assert "LATER_MAIN_CHAT" not in body
        assert not any(m.get("attachments") for m in service.models.calls[0])
        assert not await service.repo.captures(row["id"])
        assert (await start(service))["id"] == row["id"]
        await settle(service)
        assert len(service.models.calls) == 1


@_async_test
async def test_message_idempotency_and_device_binding(tmp_path):
    async with harness(tmp_path) as (service, _):
        row = await start(service)
        await settle(service)
        first = await say(service, row["id"], "hello")
        repeated = await say(service, row["id"], "hello")
        assert first["id"] == repeated["id"]
        with pytest.raises(ValueError, match="id_reused"):
            await say(service, row["id"], "changed")
        with pytest.raises(TogetherDeviceMismatch):
            await service.get(row["id"], "other-phone")
        await settle(service)
        assert len(service.models.calls) == 2


@_async_test
async def test_local_wake_silent_and_no_polling_model_or_global_quota(tmp_path):
    models = Models('<self_awake>{"after_seconds":1,"intent":"回来"}</self_awake>', '<self_awake>{"after_seconds":60}</self_awake>')
    async with harness(tmp_path, models=models) as (service, clock):
        row = await start(service)
        await settle(service)
        assert (await service.repo.session(row["id"]))["wake_at"] == clock()+30
        for _ in range(3):
            await service.tick()
            await settle(service)
        assert len(models.calls) == 1
        clock.advance(30)
        await service.tick()
        await settle(service)
        assert len(models.calls) == 2
        assert await service.repo.messages(row["id"]) == []
        assert (await service.repo.session(row["id"]))["wake_at"] == clock()+60


@_async_test
async def test_offline_and_input_busy_defer_wake_without_ending(tmp_path):
    models = Models('<self_awake>{"after_seconds":60}</self_awake>', "回来了。")
    async with harness(tmp_path, models=models) as (service, clock):
        row = await start(service)
        await settle(service)
        clock.advance(100)
        await service.tick()
        assert (await service.repo.session(row["id"]))["status"] == "active"
        assert len(models.calls) == 1
        await ready(service, row["id"], busy=True)
        await settle(service)
        assert len(models.calls) == 1
        await ready(service, row["id"])
        await settle(service)
        assert len(models.calls) == 2


@_async_test
async def test_owner_interrupts_active_autonomous_turn_and_drops_controls(tmp_path):
    models = Models('<self_awake>{"after_seconds":30}</self_awake>')
    async with harness(tmp_path, models=models) as (service, clock):
        row = await start(service)
        await settle(service)
        started = asyncio.Event()
        async def blocked(messages):
            if len(models.calls) == 2:
                started.set()
                yield "半句话"
                await asyncio.Event().wait()
                yield "<remember>不应该保存</remember>"
            else:
                yield "收到新消息。"
        models.hook = blocked
        clock.advance(30)
        await service.tick()
        await asyncio.wait_for(started.wait(), 2)
        await say(service, row["id"], "现在聊这个")
        await settle(service)
        assert len(models.calls) == 3
        assert not await service.repo.read("SELECT * FROM together_writebacks", many=True)
        messages = await service.repo.messages(row["id"])
        assert any(m["status"] == "interrupted" for m in messages)
        assert messages[-1]["content"] == "收到新消息。"


@_async_test
async def test_owner_turn_serializes_later_messages_and_replaces_wake(tmp_path):
    async with harness(tmp_path, config_overrides={"context_messages": 5}) as (service, clock):
        row = await start(service)
        await settle(service)
        await ready(service, row["id"])
        cap = await service.request_capture(row["id"], device_id="android_phone")
        await service.receive_capture(row["id"], cap["id"], device_id="android_phone", source_version=1, captured_at=clock(), data=jpeg())
        started, release = asyncio.Event(), asyncio.Event()
        async def blocked(messages):
            if len(service.models.calls) == 2:
                started.set()
                await release.wait()
                yield '<self_awake>{"after_seconds":30,"intent":"过时安排"}</self_awake>第一条回复'
            elif len(service.models.calls) == 3:
                yield "第二条回复"
            else:
                yield "第三条回复"
        service.models.hook = blocked
        await say(service, row["id"], "第一条", "m1")
        await asyncio.wait_for(started.wait(), 2)
        await say(service, row["id"], "第二条", "m2", capture_id=cap["id"])
        await say(service, row["id"], "第三条", "m3")
        release.set()
        await settle(service)
        assert len(service.models.calls) == 4
        assert "第二条" not in str(service.models.calls[1])
        second = service.models.calls[2]
        assert [(m["role"], m["content"].split("\n", 1)[0]) for m in second[1:]] == [
            ("assistant", "在这里。"), ("user", "第一条"), ("assistant", "第一条回复"), ("user", "第二条")]
        assert "第三条" not in str(second)
        assert len(second[-1]["attachments"]) == 1
        assert all(not m.get("attachments") for m in second[:-1])
        third = service.models.calls[3]
        assert [(m["role"], m["content"].split("\n", 1)[0]) for m in third[1:]] == [
            ("user", "第一条"), ("assistant", "第一条回复"), ("user", "第二条"),
            ("assistant", "第二条回复"), ("user", "第三条")]
        assert all(not m.get("attachments") for m in third)
        assert (await service.repo.session(row["id"]))["wake_at"] is None
        assert [m["content"] for m in await service.repo.messages(row["id"]) if m["role"] == "assistant"][-3:] == ["第一条回复", "第二条回复", "第三条回复"]


@_async_test
async def test_end_cancels_generation_and_leaves_only_event_pending(tmp_path):
    models = Models()
    entered = asyncio.Event()
    async def blocked(messages):
        entered.set()
        yield "未完"
        await asyncio.Event().wait()
    models.hook = blocked
    async with harness(tmp_path, models=models) as (service, clock):
        row = await start(service)
        await asyncio.wait_for(entered.wait(), 2)
        clock.advance(12)
        result = await service.action(row["id"], device_id="android_phone", action="end")
        await settle(service)
        assert result["status"] == "ended" and result["messages"] == []
        jobs = await service.repo.read("SELECT * FROM together_writebacks", many=True)
        assert len(jobs) == 1 and jobs[0]["kind"] == "event"
        assert jobs[0]["payload"]["content"] == "阿澈陪小栀待了12 秒。"
        assert not await service.repo.messages(row["id"])
        with pytest.raises(TogetherConflict):
            await say(service, row["id"], "late")


@_async_test
async def test_manual_photo_and_caption_sent_once_then_deleted(tmp_path):
    async with harness(tmp_path) as (service, clock):
        row = await start(service)
        await settle(service)
        await ready(service, row["id"])
        cap = await service.request_capture(row["id"], device_id="android_phone")
        await service.receive_capture(row["id"], cap["id"], device_id="android_phone", source_version=1, captured_at=clock(), data=jpeg())
        await say(service, row["id"], "看这个", capture_id=cap["id"])
        await settle(service)
        assert sum(len(m.get("attachments", [])) for m in service.models.calls[-1]) == 1
        assert "看这个" in str(service.models.calls[-1])
        assert not list(service.media.root().glob("*.jpg"))
        await say(service, row["id"], "接着聊", "m2")
        await settle(service)
        assert not any(m.get("attachments") for m in service.models.calls[-1])


@pytest.mark.parametrize("action", ["pause_capture", "switch_source", "retry"])
@_async_test
async def test_photo_survives_requeued_and_manually_retried_turns(tmp_path, action):
    async with harness(tmp_path) as (service, clock):
        row = await start(service)
        await settle(service)
        await ready(service, row["id"])
        cap = await service.request_capture(row["id"], device_id="android_phone")
        await service.receive_capture(row["id"], cap["id"], device_id="android_phone", source_version=1, captured_at=clock(), data=jpeg())
        first_started, retried, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
        async def interrupted(messages):
            if len(service.models.calls) == 2:
                first_started.set()
                if action == "retry":
                    raise RuntimeError("model failed")
                await asyncio.Event().wait()
            else:
                retried.set()
                await release.wait()
                yield "看到了。"
        service.models.hook = interrupted
        message = await say(service, row["id"], "这张图", capture_id=cap["id"])
        await asyncio.wait_for(first_started.wait(), 2)
        if action == "retry":
            await settle(service)
            assert (await service.repo.message(message["id"]))["status"] == "failed"
            await service.cleanup_media()
            assert (await service.repo.capture(cap["id"]))["status"] == "attached"
        await service.action(row["id"], device_id="android_phone", action=action, source="camera", message_id=message["id"])
        await asyncio.wait_for(retried.wait(), 2)
        await service.cleanup_media()
        capture = await service.repo.capture(cap["id"])
        assert capture["status"] == "attached" and service.media.path(capture["filename"]).is_file()
        original, retry = service.models.calls[1:]
        assert retry[-1]["content"].startswith("这张图\n")
        assert retry[-1]["attachments"] == original[-1]["attachments"]
        assert "together_draft_unavailable" not in str(retry)
        release.set()
        await settle(service)
        assert (await service.repo.message(message["id"]))["status"] == "complete"
        assert (await service.repo.capture(cap["id"]))["status"] == "consumed"
        assert not list(service.media.root().glob("*.jpg"))


@pytest.mark.parametrize("cleanup_before_retry", [False, True])
@_async_test
async def test_failed_photo_expires_and_is_not_reused_on_late_retry(tmp_path, cleanup_before_retry):
    async with harness(tmp_path) as (service, clock):
        row = await start(service)
        await settle(service)
        await ready(service, row["id"])
        cap = await service.request_capture(row["id"], device_id="android_phone")
        await service.receive_capture(row["id"], cap["id"], device_id="android_phone", source_version=1, captured_at=clock(), data=jpeg())
        async def failure(messages):
            raise RuntimeError("model failed")
            yield ""
        service.models.hook = failure
        message = await say(service, row["id"], "这张图", capture_id=cap["id"])
        await settle(service)
        await service.cleanup_media()
        assert (await service.repo.capture(cap["id"]))["status"] == "attached"
        assert list(service.media.root().glob("*.jpg"))
        clock.advance(service.config.load()["draft_seconds"])
        if cleanup_before_retry:
            await service.cleanup_media()
            assert not list(service.media.root().glob("*.jpg"))
        service.models.hook = None
        await ready(service, row["id"])
        await service.action(row["id"], device_id="android_phone", action="retry", message_id=message["id"])
        await settle(service)
        assert all(not m.get("attachments") for m in service.models.calls[-1])
        assert "together_draft_unavailable" in str(service.models.calls[-1])
        await service.cleanup_media()
        assert not list(service.media.root().glob("*.jpg"))


@_async_test
async def test_ending_session_cleans_retained_failed_photo(tmp_path):
    async with harness(tmp_path) as (service, clock):
        row = await start(service)
        await settle(service)
        await ready(service, row["id"])
        cap = await service.request_capture(row["id"], device_id="android_phone")
        await service.receive_capture(row["id"], cap["id"], device_id="android_phone", source_version=1, captured_at=clock(), data=jpeg())
        async def failure(messages):
            raise RuntimeError("model failed")
            yield ""
        service.models.hook = failure
        await say(service, row["id"], "这张图", capture_id=cap["id"])
        await settle(service)
        assert list(service.media.root().glob("*.jpg"))
        await service.action(row["id"], device_id="android_phone", action="end")
        await service.cleanup_media()
        assert not list(service.media.root().glob("*.jpg"))


@_async_test
async def test_source_change_keeps_frozen_manual_draft_and_rejects_late_frame(tmp_path):
    async with harness(tmp_path) as (service, clock):
        row = await start(service)
        await settle(service)
        await ready(service, row["id"])
        draft = await service.request_capture(row["id"], device_id="android_phone")
        await service.receive_capture(row["id"], draft["id"], device_id="android_phone", source_version=1, captured_at=clock(), data=jpeg())
        late = await service.request_capture(row["id"], device_id="android_phone")
        await service.action(row["id"], device_id="android_phone", action="switch_source", source="camera")
        with pytest.raises(TogetherConflict, match="source_changed"):
            await service.receive_capture(row["id"], late["id"], device_id="android_phone", source_version=1, captured_at=clock(), data=jpeg())
        await say(service, row["id"], "刚才屏幕上这个", capture_id=draft["id"])
        await settle(service)
        assert "来源为手机屏幕" in str(service.models.calls[-1])


@_async_test
async def test_server_restart_does_not_replay_started_model(tmp_path):
    async with harness(tmp_path) as (service, _):
        row = await start(service)
        await settle(service)
        service.closed = True
        message = await say(service, row["id"], "等待人工重试")
        turn = await service.repo.claim_turn(row["id"], lease_seconds=45)
        assert turn["message_id"] == message["id"]
        await service.recover()
        service.closed = False
        await ready(service, row["id"])
        await settle(service)
        assert len(service.models.calls) == 1
        assert (await service.repo.message(message["id"]))["status"] == "failed"
        await service.action(row["id"], device_id="android_phone", action="retry", message_id=message["id"])
        await settle(service)
        assert len(service.models.calls) == 2


@_async_test
async def test_writeback_evidence_survives_transcript_cleanup(tmp_path):
    models = Models("开始。", '好。<remember>一起聊过的事</remember><understand>{"statement":"新的认识","source":"这句话"}</understand>')
    async with harness(tmp_path, models=models) as (service, _):
        row = await start(service)
        await settle(service)
        await say(service, row["id"], "这句话")
        await settle(service)
        await service.action(row["id"], device_id="android_phone", action="end")
        assert not await service.repo.cleanup_transcript(row["id"])
        await service.repo.execute("UPDATE together_sessions SET event_message_id='durable-event' WHERE id=?", (row["id"],))
        assert await service.repo.cleanup_transcript(row["id"])
        jobs = await service.repo.read("SELECT * FROM together_writebacks WHERE kind='understanding'", many=True)
        assert jobs[0]["evidence"]["owner_messages"][-1]["content"] == "这句话"
        assert not await service.repo.messages(row["id"])


@_async_test
async def test_text_only_model_and_paused_capture_keep_chat_usable(tmp_path):
    async with harness(tmp_path) as (service, _):
        row = await start(service, model_key="text-model")
        await settle(service)
        await ready(service, row["id"])
        with pytest.raises(TogetherConflict, match="no_vision"):
            await service.request_capture(row["id"], device_id="android_phone")
        await service.action(row["id"], device_id="android_phone", action="pause_capture")
        await say(service, row["id"], "继续文字")
        await settle(service)
        assert len(service.models.calls) == 2
        assert (await service.repo.session(row["id"]))["status"] == "active"


@_async_test
async def test_failed_model_no_retry_loop_and_usage_counts_attempt(tmp_path):
    models = Models()
    async def failure(messages):
        raise RuntimeError("PROVIDER_PRIVATE_MESSAGE")
        yield ""
    models.hook = failure
    async with harness(tmp_path, models=models) as (service, _):
        row = await start(service)
        await settle(service)
        for _ in range(3):
            await service.tick()
            await settle(service)
        assert len(models.calls) == 1
        usage = await service.repo.usage(row["id"])
        assert usage[0]["calls"] == 1 and usage[0]["reasoning_tokens"] == 8
        assert "PROVIDER_PRIVATE_MESSAGE" not in str(await service.repo.events(row["id"]))


@_async_test
async def test_disconnected_time_is_not_counted_as_companionship(tmp_path):
    async with harness(tmp_path) as (service, clock):
        row = await start(service)
        await settle(service)
        clock.advance(1000)
        await service.tick()
        await ready(service, row["id"])
        clock.advance(10)
        ended = await service.action(row["id"], device_id="android_phone", action="end")
        assert ended["active_seconds"] == 55


@_async_test
async def test_ephemeral_provider_image_does_not_use_uploads(tmp_path, monkeypatch):
    import ai_providers
    from app.ephemeral_media import EphemeralImage
    monkeypatch.setattr(ai_providers, "UPLOADS_DIR", tmp_path)
    message = {"role": "user", "content": "caption", "attachments": [
        {"url": "not-a-public-upload.jpg", "mime_type": "image/jpeg", "_ephemeral_image": EphemeralImage(jpeg())}]}
    openai = ai_providers.build_multimodal_messages([message])
    gemini = ai_providers.build_gemini_contents([message])
    assert openai[0]["content"][1]["image_url"]["url"].startswith("data:image/jpeg;base64,")
    assert gemini[0]["parts"][1]["inline_data"]["mime_type"] == "image/jpeg"
    assert not list(tmp_path.iterdir())
    message["attachments"][0]["_ephemeral_image"] = {"data": "client JSON cannot forge the internal type"}
    assert ai_providers.build_multimodal_messages([message])[0]["content"] == [{"type": "text", "text": "caption"}]


@_async_test
async def test_http_api_binding_image_upload_and_asr_draft(tmp_path):
    import httpx
    from fastapi import FastAPI
    from routes.together import router
    from app.together.service import get_together_service
    async with harness(tmp_path) as (service, clock):
        app = FastAPI()
        app.include_router(router)
        app.dependency_overrides[get_together_service] = lambda: service
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            response = await client.post("/api/together/sessions", json={"request_id": "api1", "conv_id": "conv1", "device_id": "android_phone", "history_count": 0})
            assert response.status_code == 200
            sid = response.json()["id"]
            await settle(service)
            response = await client.post(f"/api/together/sessions/{sid}/messages", json={"device_id": "wrong", "client_id": "m1", "text": "bad"})
            assert response.status_code == 403
            await client.post(f"/api/together/sessions/{sid}/heartbeat", json={"device_id": "android_phone", "source_version": 1, "capture_state": "ready"})
            cap = (await client.post(f"/api/together/sessions/{sid}/captures", json={"device_id": "android_phone"})).json()
            response = await client.post(f"/api/together/sessions/{sid}/captures/{cap['id']}/result",
                data={"device_id": "android_phone", "source_version": "1", "captured_at": str(clock())}, files={"image": ("frame.jpg", jpeg(), "image/jpeg")})
            assert response.status_code == 200 and response.json()["status"] == "ready"
            count = len(service.models.calls)
            response = await client.post(f"/api/together/sessions/{sid}/asr", data={"device_id": "android_phone"}, files={"recording": ("voice.wav", b"fake", "audio/wav")})
            assert response.json() == {"text": "转写草稿"}
            assert len(service.models.calls) == count
            assert (await service.repo.capture(cap["id"]))["status"] == "ready"
            events = (await client.get(f"/api/together/sessions/{sid}/events", params={"device_id": "android_phone", "after": 0, "timeout": 0})).json()
            assert events["events"] and events["status"] == "active"


@_async_test
async def test_event_idempotency_and_failed_optional_write_retains_only_evidence(tmp_path):
    from app.together.writeback import Writebacks
    models = Models("开始。", "收到。<remember>留下的笔记</remember>")
    calls, sent = [], []
    async def memory(content, kind, **kwargs):
        calls.append((content, kind, kwargs))
        if kind == "ai_note":
            raise RuntimeError("provider-private-error")
        return {"id": "memory-event"}
    async def broadcast(event):
        sent.append(event)
    async with harness(tmp_path, models=models) as (service, _):
        await service.repo.execute("INSERT INTO conversations(id,model) VALUES ('conv1','vision-model')")
        row = await start(service)
        await settle(service)
        await say(service, row["id"], "需要留下的实际依据")
        await settle(service)
        await service.action(row["id"], device_id="android_phone", action="end")
        writer = Writebacks(service.repo, memory_writer=memory, broadcast=broadcast)
        await writer.drain()
        await writer.drain()
        assert len(calls) == 2
        assert len(sent) == 1 and sent[0]["type"] == "msg_created"
        assert not await service.repo.messages(row["id"])
        assert (await service.repo.session(row["id"]))["entry_history"] == []
        messages = await service.repo.read("SELECT * FROM messages", many=True)
        assert len(messages) == 1 and messages[0]["role"] == "system"
        failed = await service.repo.read("SELECT * FROM together_writebacks WHERE status='failed'")
        assert failed["kind"] == "note" and failed["error"] == "RuntimeError"
        assert failed["evidence"]["owner_messages"][-1]["content"] == "需要留下的实际依据"


@pytest.mark.parametrize("with_owner", [False, True])
@_async_test
async def test_cognition_uses_real_together_evidence_and_existing_version_pipeline(tmp_path, with_owner):
    from app.desire.schema import init_desire_tables
    from app.desire.service import ensure_desire_root_in_tx
    from app.working_model.schema import init_working_model_tables
    from app.working_model.repository import insert_version
    from app.working_model.writer import build_writer_identity_snapshot
    from app.working_model.runtime import capture_together_working_model_pipeline_input, run_working_model_pipeline
    output = '明白。<understand>{"statement":"喜欢自己决定","source":"具体观察"}</understand>'
    models = Models("在这里。", output) if with_owner else Models(output)
    async with harness(tmp_path, models=models) as (service, _):
        row = await start(service)
        await settle(service)
        owner = None
        if with_owner:
            owner = await say(service, row["id"], "录音先转写到输入框，等我确认再发送。")
            await settle(service)
        async with service.repo.db() as db:
            await init_working_model_tables(db)
            await init_desire_tables(db)
            await insert_version(db, version_id="root", previous_version_id=None, content="旧认识", created_at=1,
                origin_conv_id=None, origin_message_id=None, origin_request_id=None, reason="root", writer_model=None, prompt_version=None, diff_ratio=None)
            await ensure_desire_root_in_tx(db, working_model_id="root", created_at=1)
            await db.commit()
        job = await service.repo.read("SELECT * FROM together_writebacks WHERE kind='understanding'")
        assert bool(job["evidence"]["owner_messages"]) == with_owner
        if owner:
            assert job["evidence"]["owner_messages"][-1]["id"] == owner["id"]
        value = capture_together_working_model_pipeline_input(conv_id="conv1", together_source_id=job["id"],
            statement="喜欢自己决定", source="CALLER_TEXT_MUST_BE_REPLACED", model_key="vision-model",
            identity_snapshot=build_writer_identity_snapshot(job["payload"]["identity"]))
        seen = []
        async def gate(messages):
            seen.append(messages)
            payload = json.loads(messages[-1]["content"])
            assert "CALLER_TEXT_MUST_BE_REPLACED" not in payload["source"]
            if owner:
                assert payload["latest_user_message"] == owner["content"]
            else:
                assert "本轮没有新发言" in payload["latest_user_message"]
            assert "具体观察" in payload["source"]
            return '{"route":"working_model","reason":"认识"}'
        async def writer(messages):
            seen.append(messages)
            payload = json.loads(messages[-1]["content"])
            if owner:
                assert payload["original_user_message"] == {
                    "message_id": owner["id"], "speaker": "小栀", "content": owner["content"],
                }
            else:
                assert "original_user_message" not in payload
            return json.dumps({"disposition": "integrated", "working_model": "小栀喜欢自己决定。", "desire": payload["current_desire"], "change_note": "本场形成的新认识"})
        result = await run_working_model_pipeline(value, db_factory=service.repo.db, gate_provider=gate, writer_provider=writer)
        assert result["request"]["status"] == "applied"
        assert result["request"]["origin_user_message_id"] is None
        again = await run_working_model_pipeline(value, db_factory=service.repo.db, gate_provider=gate, writer_provider=writer)
        assert again["deduplicated"] and len(seen) == 2
        assert not await service.repo.read("SELECT * FROM messages", many=True)
        version = await service.repo.read("SELECT * FROM working_model_versions WHERE id!='root'")
        assert json.loads(version["reason"])["together_source_id"] == job["id"]
        bad = capture_together_working_model_pipeline_input(conv_id="conv1", together_source_id="invented",
            statement="喜欢自己决定", source="fake", model_key="vision-model", identity_snapshot=value.identity_snapshot)
        rejected = await run_working_model_pipeline(bad, db_factory=service.repo.db, gate_provider=gate, writer_provider=writer)
        assert rejected["request"]["failure_code"] == "together_source_invalid" and len(seen) == 2
