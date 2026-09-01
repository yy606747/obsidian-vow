import asyncio
from contextlib import asynccontextmanager
from datetime import datetime
import json

import aiosqlite
import pytest

import app.memory_v3.repository as repositories
import app.memory_v3.timeline as timeline_mod
from app.memory_v3.config import normalize_memory_v3_config
from app.memory_v3.provenance import source_hash_for_messages
from app.memory_v3.schema import init_memory_v3_tables


async def _create_db(path) -> None:
    async with aiosqlite.connect(path) as db:
        await db.execute("""
            CREATE TABLE messages (
                id TEXT PRIMARY KEY,
                conv_id TEXT NOT NULL,
                role TEXT NOT NULL,
                content TEXT NOT NULL,
                created_at REAL NOT NULL
            )
        """)
        await db.execute("""
            CREATE TABLE memory_chunks (
                id TEXT PRIMARY KEY,
                conv_id TEXT NOT NULL,
                message_ids_json TEXT NOT NULL DEFAULT '[]',
                content TEXT NOT NULL,
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL,
                embedding BLOB,
                keywords_json TEXT NOT NULL DEFAULT '[]',
                metadata_json TEXT NOT NULL DEFAULT '{}'
            )
        """)
        await db.execute("""
            CREATE TABLE memory_items (
                id TEXT PRIMARY KEY,
                legacy_memory_id TEXT,
                metadata_json TEXT NOT NULL DEFAULT '{}'
            )
        """)
        await init_memory_v3_tables(db)
        await db.commit()


def _get_db_factory(path):
    @asynccontextmanager
    async def factory():
        async with aiosqlite.connect(path) as db:
            yield db

    return factory


def _config(**updates):
    return normalize_memory_v3_config({
        "timeline_enabled": True,
        "timeline_hours": 72,
        "timeline_max_chars": 800,
        "timeline_generation_min_interval_sec": 0,
        "timeline_generation_timeout_sec": 5,
        "timeline_generation_attempts": 1,
        **updates,
    })


def _sources():
    return [
        {
            "id": "u1",
            "conv_id": "conv",
            "role": "user",
            "content": "明天要去复诊，我其实有点紧张。",
            "created_at": 900.0,
        },
        {
            "id": "a1",
            "conv_id": "conv",
            "role": "assistant",
            "content": "我记得，明天出门前可以再陪你确认一次要带的东西，也可以把问题先列下来。",
            "created_at": 901.0,
        },
    ]


def _ts(year, month, day, hour=0, minute=0):
    return datetime(
        year,
        month,
        day,
        hour,
        minute,
        tzinfo=timeline_mod.TIMELINE_TZ,
    ).timestamp()


def test_timeline_contract_and_renderer_are_bounded_and_source_aware():
    sources = _sources()
    entries, diagnostics = timeline_mod.validate_timeline_payload(
        {
            "entries": [
                {"text": "复诊安排已经定下，她对此有点紧张。", "source_message_ids": ["u1"]},
                {"text": "出门前还可以确认一次要带的东西。", "source_message_ids": ["a1"]},
            ]
        },
        source_messages=sources,
    )
    assert len(entries) == 2
    assert diagnostics == {"volatile_time_dropped": 0}

    rendered = timeline_mod.build_timeline_prompt_block(
        {"entries_json": json.dumps(entries, ensure_ascii=False)},
        visible_message_ids=["u1"],
        source_created_at={"u1": 900.0, "a1": 901.0},
        reference_ts=1000.0,
        max_chars=800,
    )
    assert rendered["enabled"] is True
    assert rendered["deduped_indices"] == [0]
    assert "出门前" in rendered["content"]
    assert "复诊安排已经定下" not in rendered["content"]
    assert len(rendered["content"]) <= 800

    with pytest.raises(timeline_mod.TimelineContractError) as exc:
        timeline_mod.validate_timeline_payload(
            {"entries": [{"text": "来源不明", "source_message_ids": ["outside"]}]},
            source_messages=sources,
        )
    assert exc.value.code == "bad_source"

    copied = sources[1]["content"][:30]
    with pytest.raises(timeline_mod.TimelineContractError) as exc:
        timeline_mod.validate_timeline_payload(
            {"entries": [{"text": copied, "source_message_ids": ["a1"]}]},
            source_messages=sources,
        )
    assert exc.value.code == "assistant_excerpt"


@pytest.mark.parametrize(
    "text",
    (
        "用户对此有点紧张。",
        "assistant答应会陪着她。",
        "AI答应会陪着她。",
        "对方答应会陪着她。",
        "TA答应会陪着她。",
        "你答应会陪着她。",
    ),
)
def test_timeline_contract_rejects_generic_relationship_labels(text):
    with pytest.raises(timeline_mod.TimelineContractError) as exc:
        timeline_mod.validate_timeline_payload(
            {"entries": [{"text": text, "source_message_ids": ["u1"]}]},
            source_messages=_sources(),
            user_name="小栀",
            ai_name="阿澈",
        )

    assert exc.value.code == "generic_relationship_label"


def test_renderer_drops_generic_labels_from_an_existing_timeline_cache():
    reference_ts = _ts(2026, 8, 11, 14)
    texts = [
        "用户对此有点紧张。",
        "assistant答应会陪着她。",
        "AI答应会陪着她。",
        "对方答应会陪着她。",
        "TA答应会陪着她。",
        "你答应会陪着她。",
        "小栀和阿澈已经说定了。",
    ]
    entries = [
        {"text": text, "source_message_ids": [f"m{index}"]}
        for index, text in enumerate(texts)
    ]
    source_created_at = {
        f"m{index}": _ts(2026, 8, 11, 8, index)
        for index in range(len(texts))
    }

    rendered = timeline_mod.build_timeline_prompt_block(
        {"entries_json": json.dumps(entries, ensure_ascii=False)},
        visible_message_ids=[],
        source_created_at=source_created_at,
        reference_ts=reference_ts,
        max_chars=1600,
        user_name="小栀",
        ai_name="阿澈",
    )

    assert rendered["enabled"] is True
    assert rendered["generic_relationship_label_indices"] == [0, 1, 2, 3, 4, 5]
    assert [entry["index"] for entry in rendered["entries"]] == [6]
    assert "小栀和阿澈已经说定了。" in rendered["content"]


def test_volatile_time_is_dropped_without_rejecting_the_payload():
    entries, diagnostics = timeline_mod.validate_timeline_payload(
        {
            "entries": [
                {"text": "昨天她说复诊安排变了。", "source_message_ids": ["u1"]},
                {"text": "复诊安排改到了 8月12日。", "source_message_ids": ["u1"]},
            ]
        },
        source_messages=_sources(),
    )

    assert entries == [
        {"text": "复诊安排改到了 8月12日。", "source_message_ids": ["u1"]}
    ]
    assert diagnostics == {"volatile_time_dropped": 1}


def test_volatile_time_detection_avoids_known_lexical_false_positives():
    entries, diagnostics = timeline_mod.validate_timeline_payload(
        {
            "entries": [
                {"text": "她说这是后天形成的习惯。", "source_message_ids": ["u1"]},
                {"text": "这个尺寸刚刚好合适。", "source_message_ids": ["u1"]},
                {"text": "上周期的数据已经整理完。", "source_message_ids": ["u1"]},
                {"text": "后天要去医院。", "source_message_ids": ["u1"]},
                {"text": "刚刚到家。", "source_message_ids": ["u1"]},
                {"text": "上周见过医生。", "source_message_ids": ["u1"]},
            ]
        },
        source_messages=_sources(),
    )

    assert [entry["text"] for entry in entries] == [
        "她说这是后天形成的习惯。",
        "这个尺寸刚刚好合适。",
        "上周期的数据已经整理完。",
    ]
    assert diagnostics == {"volatile_time_dropped": 3}


def test_cross_midnight_entry_uses_earliest_source_and_explicit_shanghai_day():
    reference_ts = _ts(2026, 8, 11, 14, 20)
    rendered = timeline_mod.build_timeline_prompt_block(
        {
            "entries_json": json.dumps(
                [
                    {
                        "text": "聊到很晚才结束这段对话。",
                        "source_message_ids": ["late", "after_midnight"],
                    }
                ],
                ensure_ascii=False,
            )
        },
        visible_message_ids=[],
        source_created_at={
            "late": _ts(2026, 8, 10, 23, 50),
            "after_midnight": _ts(2026, 8, 11, 2, 10),
        },
        reference_ts=reference_ts,
        max_chars=800,
    )

    assert rendered["enabled"] is True
    assert "昨天（8月10日" in rendered["content"]
    assert "· 23:50 聊到很晚才结束这段对话。" in rendered["content"]
    assert "\n今天\n" not in rendered["content"]


def test_renderer_keeps_newest_before_displaying_chronologically():
    entries = []
    source_created_at = {}
    for index, timestamp in enumerate(
        (
            _ts(2026, 8, 9, 9),
            _ts(2026, 8, 10, 9),
            _ts(2026, 8, 11, 9),
        )
    ):
        message_id = f"m{index}"
        text = f"条目{index}" + ("甲" * 177)
        entries.append({"text": text, "source_message_ids": [message_id]})
        source_created_at[message_id] = timestamp

    rendered = timeline_mod.build_timeline_prompt_block(
        {"entries_json": json.dumps(entries, ensure_ascii=False)},
        visible_message_ids=[],
        source_created_at=source_created_at,
        reference_ts=_ts(2026, 8, 11, 14),
        max_chars=350,
    )

    assert rendered["enabled"] is True
    assert [entry["index"] for entry in rendered["entries"]] == [2]
    assert rendered["budget_dropped_indices"] == [0, 1]
    assert "条目2" in rendered["content"]
    assert "条目0" not in rendered["content"]


def test_empty_day_buckets_are_not_rendered():
    rendered = timeline_mod.build_timeline_prompt_block(
        {
            "entries_json": json.dumps(
                [{"text": "上午确认了复诊安排。", "source_message_ids": ["m1"]}],
                ensure_ascii=False,
            )
        },
        visible_message_ids=[],
        source_created_at={"m1": _ts(2026, 8, 11, 9, 30)},
        reference_ts=_ts(2026, 8, 11, 14),
        max_chars=800,
    )

    assert "\n今天\n" in rendered["content"]
    assert "前天（" not in rendered["content"]
    assert "昨天（" not in rendered["content"]


def test_validator_keeps_six_full_entries_and_renderer_owns_total_budget():
    source_messages = []
    entries_payload = []
    source_created_at = {}
    for index in range(6):
        message_id = f"u{index}"
        timestamp = _ts(2026, 8, 11, 8 + index)
        prefix = f"条目{index}"
        text = prefix + ("甲" * (timeline_mod.MAX_ENTRY_CHARS - len(prefix)))
        source_messages.append({
            "id": message_id,
            "role": "user",
            "content": f"来源{index}",
            "created_at": timestamp,
        })
        entries_payload.append({"text": text, "source_message_ids": [message_id]})
        source_created_at[message_id] = timestamp

    entries, diagnostics = timeline_mod.validate_timeline_payload(
        {"entries": entries_payload},
        source_messages=source_messages,
    )
    assert len(entries) == 6
    assert sum(len(entry["text"]) for entry in entries) == 6 * timeline_mod.MAX_ENTRY_CHARS
    assert diagnostics == {"volatile_time_dropped": 0}

    rendered = timeline_mod.build_timeline_prompt_block(
        {"entries_json": json.dumps(entries, ensure_ascii=False)},
        visible_message_ids=[],
        source_created_at=source_created_at,
        reference_ts=_ts(2026, 8, 11, 15),
        max_chars=800,
    )
    rendered_indices = [entry["index"] for entry in rendered["entries"]]
    assert len(rendered_indices) < 6
    assert 5 in rendered_indices
    assert 0 not in rendered_indices
    assert 0 in rendered["budget_dropped_indices"]


def test_generation_prompt_contains_formatted_times_and_no_priority_ordering():
    now = _ts(2026, 8, 11, 14, 20)
    prompt = timeline_mod.build_generation_prompt(
        [{**_sources()[0], "created_at": _ts(2026, 8, 10, 23, 50)}],
        now=now,
        window_start=timeline_mod._natural_window_start(now),
        user_name="小栀",
        ai_name="阿澈",
    )

    assert timeline_mod.PROMPT_VERSION == "recent-timeline-v4"
    assert '"now": "2026-08-11 14:20"' in prompt
    assert '"window_start": "2026-08-09 00:00"' in prompt
    assert '"time": "08-10 23:50"' in prompt
    assert '"created_at":' not in prompt
    assert '"role":' not in prompt
    assert '"speaker_name": "小栀"' in prompt
    assert "user/assistant" not in prompt
    assert "按未来最值得先记得的顺序排列" not in prompt
    assert "按来源时间先后输出" in prompt


def test_timeline_hours_48_does_not_clip_the_natural_three_day_window(
    monkeypatch,
):
    now = _ts(2026, 8, 11, 12)
    captured_params = []

    class FakeCursor:
        async def fetchall(self):
            return [{
                "id": "u-old",
                "conv_id": "conv",
                "role": "user",
                "content": "仍在自然三日内。",
                "created_at": _ts(2026, 8, 9, 1),
            }]

    class FakeDb:
        row_factory = None

        async def execute(self, _sql, params):
            captured_params.append(params)
            return FakeCursor()

    @asynccontextmanager
    async def factory():
        yield FakeDb()

    monkeypatch.setattr(timeline_mod, "get_db", factory)

    rows, meta = asyncio.run(timeline_mod._recent_source_messages(now=now, hours=48))
    assert [row["id"] for row in rows] == ["u-old"]
    assert meta["requested_window_start_ts"] == _ts(2026, 8, 9)
    assert meta["configured_hours_ignored_for_window"] == 48
    assert captured_params == [(_ts(2026, 8, 9), now, timeline_mod.MAX_SOURCE_MESSAGES)]


def test_refresh_persists_soft_drop_diagnostics_without_renderer_budget(
    monkeypatch,
):
    now = _ts(2026, 8, 11, 12)
    window_start = _ts(2026, 8, 9)
    sources = [{
        "id": "u1",
        "conv_id": "conv",
        "role": "user",
        "content": "复诊改到 8月12日。",
        "created_at": _ts(2026, 8, 10, 9),
    }]
    captured = {}

    class FakeRepository:
        async def active(self):
            return None

        async def append_active(self, **kwargs):
            captured.update(kwargs)
            return {"id": "timeline-1", "created": True}

    async def fake_sources(*, now, hours):
        assert hours == 48
        return sources, {
            "requested_window_start_ts": window_start,
            "source_truncated": False,
        }

    async def fake_call(prompt, *, scope, timeout):
        assert '"now": "2026-08-11 12:00"' in prompt
        return {
            "entries": [
                {"text": "昨天复诊改期了。", "source_message_ids": ["u1"]},
                {"text": "复诊改到了 8月12日。", "source_message_ids": ["u1"]},
            ]
        }

    monkeypatch.setattr(timeline_mod.time, "time", lambda: now)
    monkeypatch.setattr(timeline_mod, "_recent_source_messages", fake_sources)
    monkeypatch.setattr(timeline_mod, "_call_flash_lite", fake_call)
    service = timeline_mod.TimelineService(FakeRepository())
    result = asyncio.run(service.refresh_now(config_snapshot=_config(timeline_hours=48)))

    assert result["status"] == "created"
    assert captured["window_start_ts"] == window_start
    assert captured["entries"] == [
        {"text": "复诊改到了 8月12日。", "source_message_ids": ["u1"]}
    ]
    assert captured["metadata"]["volatile_time_dropped"] == 1
    assert captured["metadata"]["configured_hours_affects_window"] is False


def test_prompt_context_and_renderer_share_the_earliest_source_cutoff(monkeypatch):
    current = _ts(2026, 8, 11, 12)
    source_rows = [
        {
            "id": "old",
            "conv_id": "conv",
            "role": "user",
            "content": "窗口外的开头。",
            "created_at": _ts(2026, 8, 8, 23, 50),
        },
        {
            "id": "new",
            "conv_id": "conv",
            "role": "assistant",
            "content": "窗口内的结尾。",
            "created_at": _ts(2026, 8, 10, 9),
        },
    ]
    active = {
        "id": "timeline-1",
        "version": 1,
        "window_end_ts": current,
        "entries_json": json.dumps(
            [{
                "text": "一段跨越自然窗口边界的互动。",
                "source_message_ids": ["old", "new"],
            }],
            ensure_ascii=False,
        ),
        "source_message_ids_json": json.dumps(["old", "new"]),
        "source_hash": source_hash_for_messages(source_rows),
        "prompt_version": timeline_mod.PROMPT_VERSION,
    }

    class FakeRepository:
        async def active(self):
            return active

    async def fake_messages(_message_ids):
        return source_rows

    monkeypatch.setattr(timeline_mod, "_messages_by_ids", fake_messages)
    result = asyncio.run(
        timeline_mod.TimelineService(FakeRepository()).prompt_context(
            visible_messages=[],
            config_snapshot=_config(),
            now=current,
        )
    )

    assert result["status"] == "all_visible_out_of_window_or_budget_dropped"
    assert result["stale_entry_indices"] == [0]
    assert result["out_of_window_indices"] == []
    assert result["entries"] == []


def test_timeline_refresh_versions_dedupes_and_keeps_active_on_failure(
    tmp_path, monkeypatch
):
    db_path = tmp_path / "timeline.db"
    asyncio.run(_create_db(db_path))
    factory = _get_db_factory(db_path)
    monkeypatch.setattr(repositories, "get_db", factory)
    monkeypatch.setattr(timeline_mod, "get_db", factory)
    monkeypatch.setattr(timeline_mod.time, "time", lambda: 1000.0)

    calls = []

    async def fake_call(prompt, *, scope, timeout):
        calls.append((prompt, scope, timeout))
        return {
            "entries": [
                {
                    "text": "复诊安排已经定下，她对此有点紧张。",
                    "source_message_ids": ["u1"],
                }
            ]
        }

    monkeypatch.setattr(timeline_mod, "_call_flash_lite", fake_call)

    async def scenario():
        async with factory() as db:
            await db.executemany(
                "INSERT INTO messages (id,conv_id,role,content,created_at) "
                "VALUES (?,?,?,?,?)",
                [
                    (row["id"], row["conv_id"], row["role"], row["content"], row["created_at"])
                    for row in _sources()
                ],
            )
            await db.commit()

        service = timeline_mod.TimelineService(repositories.TimelineRepository())
        first = await service.refresh_now(config_snapshot=_config())
        unchanged = await service.refresh_now(config_snapshot=_config())

        async with factory() as db:
            await db.execute(
                "INSERT INTO messages VALUES (?,?,?,?,?)",
                ("u2", "conv", "user", "复诊时间改到下午了。", 902.0),
            )
            await db.commit()
        second = await service.refresh_now(config_snapshot=_config())
        active_before_failure = await service.repository.active()

        async with factory() as db:
            await db.execute(
                "INSERT INTO messages VALUES (?,?,?,?,?)",
                ("a2", "conv", "assistant", "好，那上午不用赶。", 903.0),
            )
            await db.commit()

        async def bad_call(*_args, **_kwargs):
            return {"not_entries": []}

        monkeypatch.setattr(timeline_mod, "_call_flash_lite", bad_call)
        failed = await service.refresh_now(config_snapshot=_config())
        active_after_failure = await service.repository.active()
        latest_attempt = await service.repository.latest_attempt()
        return first, unchanged, second, failed, active_before_failure, active_after_failure, latest_attempt

    first, unchanged, second, failed, active_before, active_after, latest = asyncio.run(scenario())
    assert first["status"] == "created"
    assert unchanged["status"] == "unchanged"
    assert second["status"] == "created"
    assert len(calls) == 2
    assert failed["status"] == "failed"
    assert active_after["id"] == active_before["id"]
    assert latest["status"] == "invalid"
    assert latest["failure_reason"] == "bad_shape"


def test_timeline_prompt_rejects_source_drift_and_records_injection_usage(
    tmp_path, monkeypatch
):
    db_path = tmp_path / "timeline-drift.db"
    asyncio.run(_create_db(db_path))
    factory = _get_db_factory(db_path)
    monkeypatch.setattr(repositories, "get_db", factory)
    monkeypatch.setattr(timeline_mod, "get_db", factory)

    async def scenario():
        async with factory() as db:
            await db.executemany(
                "INSERT INTO messages (id,conv_id,role,content,created_at) VALUES (?,?,?,?,?)",
                [
                    (row["id"], row["conv_id"], row["role"], row["content"], row["created_at"])
                    for row in _sources()
                ],
            )
            await db.commit()
        service = timeline_mod.TimelineService(repositories.TimelineRepository())
        active = await service.repository.append_active(
            window_start_ts=800,
            window_end_ts=1000,
            entries=[{"text": "复诊安排已经定下。", "source_message_ids": ["u1"]}],
            source_message_ids=["u1", "a1"],
            source_hash=source_hash_for_messages(_sources()),
            prompt_version=timeline_mod.PROMPT_VERSION,
        )
        injected = await service.prompt_context(
            visible_messages=[],
            config_snapshot=_config(),
            now=1001,
        )
        recorded = await service.record_injection_usage(
            injected,
            conv_id="conv",
            assistant_message_id="reply1",
            response_text="我记得你的复诊安排已经定下。",
        )
        async with factory() as db:
            row = await (
                await db.execute(
                    "SELECT route, candidate_id FROM memory_injection_events"
                )
            ).fetchone()
            await db.execute("UPDATE messages SET content=? WHERE id='u1'", ("内容被编辑",))
            await db.commit()
        service.start_background_refresh = lambda *_args, **_kwargs: None
        drift = await service.prompt_context(
            visible_messages=[],
            config_snapshot=_config(),
            now=1002,
        )
        return active, injected, recorded, row, drift

    active, injected, recorded, row, drift = asyncio.run(scenario())
    assert injected["status"] == "injected"
    assert recorded["status"] == "recorded"
    assert row == ("timeline", f"{active['id']}:entry:0")
    assert drift["status"] == "source_changed"
    assert drift["block"] == ""


def test_empty_timeline_window_uses_no_provider_call(tmp_path, monkeypatch):
    db_path = tmp_path / "timeline-empty.db"
    asyncio.run(_create_db(db_path))
    factory = _get_db_factory(db_path)
    monkeypatch.setattr(repositories, "get_db", factory)
    monkeypatch.setattr(timeline_mod, "get_db", factory)
    monkeypatch.setattr(timeline_mod.time, "time", lambda: 1000.0)

    async def forbidden(*_args, **_kwargs):
        raise AssertionError("empty window must not call provider")

    monkeypatch.setattr(timeline_mod, "_call_flash_lite", forbidden)
    service = timeline_mod.TimelineService(repositories.TimelineRepository())
    result = asyncio.run(service.refresh_now(config_snapshot=_config()))
    assert result["status"] == "empty"
    assert result["provider_calls"] == 0


def test_disabling_timeline_cancels_a_sleeping_refresh():
    async def scenario():
        service = timeline_mod.TimelineService()
        blocker = asyncio.Event()
        task = asyncio.create_task(blocker.wait())
        service._task = task
        service.start_background_refresh(
            normalize_memory_v3_config({"timeline_enabled": False})
        )
        await asyncio.sleep(0)
        return task.cancelled()

    assert asyncio.run(scenario()) is True
