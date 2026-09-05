from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
import json
from pathlib import Path
import sqlite3

import aiosqlite
import pytest

import ai_providers
from app.chat import memory_prompt, prompt_builder
from app.desire import repository as desire_repository
from app.desire.schema import init_desire_tables
from app.desire.service import ensure_desire_root_in_tx
from app.working_model import repository as wm_repository
from app.working_model import service as wm_service
from app.working_model.schema import init_working_model_tables
from routes import settings as settings_route
from scripts import wm_cp5_switch


async def _with_heartbeat(awaitable):
    async def heartbeat():
        while True:
            await asyncio.sleep(0.001)

    task = asyncio.create_task(heartbeat())
    try:
        return await awaitable
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


def _run(awaitable):
    return asyncio.run(_with_heartbeat(awaitable))


def test_v2_prompt_injects_exact_heads_and_omits_empty_desire(monkeypatch):
    async def heads_with_desire():
        return ({"content": "她会把复杂问题拆开。"}, {"content": "我想成为她能放心说真话的人。"})

    monkeypatch.setattr(memory_prompt, "working_model_v2_injection_enabled", lambda: True)
    monkeypatch.setattr(memory_prompt.working_model_service, "load_v2_prompt_heads", heads_with_desire)
    monkeypatch.setattr(
        memory_prompt,
        "load_working_model",
        lambda: pytest.fail("legacy file must not be read while V2 injection is enabled"),
    )
    history = [{"role": "system", "content": "identity"}]
    offset = _run(memory_prompt.inject_working_model_prompt(history, cap_idx=0, inject_offset=0))

    assert offset == 4
    assert history[0]["content"] == "[你对她的当前认识]\n她会把复杂问题拆开。"
    assert history[2]["content"] == (
        "[你此刻想以怎样的姿态与她相处]\n"
        "我想成为她能放心说真话的人。"
    )

    async def heads_without_desire():
        return ({"content": "她会把复杂问题拆开。"}, {"content": ""})

    monkeypatch.setattr(memory_prompt.working_model_service, "load_v2_prompt_heads", heads_without_desire)
    history = [{"role": "system", "content": "identity"}]
    offset = _run(memory_prompt.inject_working_model_prompt(history, cap_idx=0, inject_offset=0))
    assert offset == 2
    assert all("姿态与她相处" not in item["content"] for item in history)


def test_injection_flag_off_restores_legacy_block_without_v2_read(monkeypatch):
    async def forbidden_heads():
        pytest.fail("V2 heads must not be read while rollback injection is disabled")

    monkeypatch.setattr(memory_prompt, "working_model_v2_injection_enabled", lambda: False)
    monkeypatch.setattr(memory_prompt.working_model_service, "load_v2_prompt_heads", forbidden_heads)
    monkeypatch.setattr(
        memory_prompt,
        "load_working_model",
        lambda: {"content": "冻结的旧认识", "updated_at": 1},
    )
    history = [{"role": "system", "content": "identity"}]
    offset = _run(memory_prompt.inject_working_model_prompt(history, cap_idx=0, inject_offset=0))
    assert offset == 2
    assert history[0]["content"] == "[你对她的当前认识]\n冻结的旧认识"


def test_v2_prompt_builders_reject_silent_clipping():
    assert prompt_builder.build_v2_working_model_block({"content": "认" * 1200}).endswith(
        "认" * 1200
    )
    assert prompt_builder.build_desire_block({"content": "想" * 200}).endswith("想" * 200)
    with pytest.raises(ValueError, match="1200"):
        prompt_builder.build_v2_working_model_block({"content": "认" * 1201})
    with pytest.raises(ValueError, match="200"):
        prompt_builder.build_desire_block({"content": "想" * 201})


def _healthy_settings() -> dict:
    endpoint = {
        "id": "sf",
        "name": "SiliconFlow",
        "type": "openai",
        "base_url": "https://example.invalid/v1",
        "api_key": "secret",
    }
    return {
        "endpoints": [endpoint],
        "slots": {
            name: {"endpoint": "sf", "model": f"model-{name}"}
            for name in (
                "sentinel",
                "memory_digest",
                "relational_card_generation",
                "asr",
                "working_model_gate",
            )
        },
    }


def test_health_requires_gate_only_while_write_path_is_enabled(monkeypatch):
    values = _healthy_settings()
    values["slots"].pop("working_model_gate")
    monkeypatch.setattr(settings_route, "SETTINGS", values)
    monkeypatch.setattr(settings_route, "get_key", lambda _name: "secret")
    monkeypatch.setattr(
        settings_route,
        "get_endpoint",
        lambda endpoint_id: values["endpoints"][0] if endpoint_id == "sf" else None,
    )
    monkeypatch.setattr(
        settings_route,
        "load_ai_behavior",
        lambda: {"working_model_v2_write_enabled": False},
    )
    assert _run(settings_route.settings_health())["ok"] is True

    monkeypatch.setattr(
        settings_route,
        "load_ai_behavior",
        lambda: {"working_model_v2_write_enabled": True},
    )
    enabled = _run(settings_route.settings_health())
    assert enabled["ok"] is False
    assert {issue["code"] for issue in enabled["issues"]} == {
        "slot_working_model_gate_missing"
    }


def test_health_rejects_empty_gate_model_and_missing_gate_key(monkeypatch):
    values = _healthy_settings()
    monkeypatch.setattr(settings_route, "SETTINGS", values)
    monkeypatch.setattr(settings_route, "get_key", lambda _name: "secret")
    monkeypatch.setattr(settings_route, "load_ai_behavior", lambda: {
        "working_model_v2_write_enabled": True,
    })
    monkeypatch.setattr(
        settings_route,
        "get_endpoint",
        lambda endpoint_id: values["endpoints"][0] if endpoint_id == "sf" else None,
    )

    values["slots"]["working_model_gate"]["model"] = ""
    result = _run(settings_route.settings_health())
    assert "slot_working_model_gate_missing_model" in {
        issue["code"] for issue in result["issues"]
    }

    values["slots"]["working_model_gate"]["model"] = "gate-model"
    values["endpoints"][0]["api_key"] = ""
    result = _run(settings_route.settings_health())
    assert "slot_working_model_gate_missing_key" in {
        issue["code"] for issue in result["issues"]
    }


def test_slot_chat_model_override_reaches_transport(monkeypatch):
    seen = {}
    monkeypatch.setattr(ai_providers, "get_slot", lambda _name: {
        "endpoint": {"id": "sf", "type": "openai"},
        "model": "new-live-model",
        "extras": {},
    })

    async def fake_transport(**kwargs):
        seen.update(kwargs)
        return "ok"

    monkeypatch.setattr(ai_providers, "_call_endpoint_chat_once", fake_transport)
    result = _run(ai_providers.call_slot_chat(
        "working_model_gate",
        messages=[{"role": "user", "content": "x"}],
        model_override="captured-model",
    ))
    assert result == "ok"
    assert seen["model"] == "captured-model"


def test_slot_chat_passes_configured_thinking_flag_to_transport(monkeypatch):
    seen = {}
    monkeypatch.setattr(ai_providers, "get_slot", lambda _name: {
        "endpoint": {"id": "sf", "type": "openai"},
        "model": "configured-model",
        "extras": {"enable_thinking": False},
    })

    async def fake_transport(**kwargs):
        seen.update(kwargs)
        return "ok"

    monkeypatch.setattr(ai_providers, "_call_endpoint_chat_once", fake_transport)
    result = _run(ai_providers.call_slot_chat(
        "ring_touch_translator",
        messages=[{"role": "user", "content": "x"}],
    ))

    assert result == "ok"
    assert seen["model"] == "configured-model"
    assert seen["enable_thinking"] is False


def _create_switch_db(path: Path, *, with_trigger: bool = False) -> None:
    with sqlite3.connect(path) as db:
        db.executescript(
            """
            CREATE TABLE working_model_versions (
                id TEXT PRIMARY KEY, previous_version_id TEXT,
                content TEXT NOT NULL, created_at REAL NOT NULL
            );
            CREATE TABLE desire_versions (
                id TEXT PRIMARY KEY, previous_version_id TEXT,
                content TEXT NOT NULL, created_at REAL NOT NULL
            );
            CREATE TABLE working_model_requests (id TEXT PRIMARY KEY);
            INSERT INTO working_model_versions VALUES ('wm-root', NULL, '认识', 1);
            INSERT INTO desire_versions VALUES ('desire-root', NULL, '', 1);
            """
        )
        if with_trigger:
            db.execute(
                "CREATE TRIGGER wm_cp4_natural_request_cap "
                "BEFORE INSERT ON working_model_requests "
                "BEGIN SELECT RAISE(ABORT, 'bounded shadow ended'); END"
            )
        db.commit()


def test_cp5_switch_enables_both_flags_and_rollback_needs_no_database(tmp_path, monkeypatch):
    db_path = tmp_path / "chat.db"
    behavior_path = tmp_path / "ai_behavior.json"
    _create_switch_db(db_path)
    monkeypatch.setattr(wm_cp5_switch, "get_slot", lambda _name: {
        "model": wm_cp5_switch.DEFAULT_WORKING_MODEL_GATE_MODEL,
        "endpoint": {},
        "extras": {},
    })

    enabled = wm_cp5_switch.switch(
        enable=True,
        db_path=db_path,
        behavior_path=behavior_path,
    )
    assert enabled["mode"] == "enabled"
    stored = json.loads(behavior_path.read_text(encoding="utf-8"))
    assert stored[wm_cp5_switch.WRITE_FLAG] is True
    assert stored[wm_cp5_switch.INJECTION_FLAG] is True

    db_path.unlink()
    disabled = wm_cp5_switch.switch(
        enable=False,
        db_path=db_path,
        behavior_path=behavior_path,
    )
    assert disabled["mode"] == "disabled"
    stored = json.loads(behavior_path.read_text(encoding="utf-8"))
    assert stored[wm_cp5_switch.WRITE_FLAG] is False
    assert stored[wm_cp5_switch.INJECTION_FLAG] is False


def test_cp5_switch_refuses_residual_shadow_trigger(tmp_path, monkeypatch):
    db_path = tmp_path / "chat.db"
    behavior_path = tmp_path / "ai_behavior.json"
    _create_switch_db(db_path, with_trigger=True)
    monkeypatch.setattr(wm_cp5_switch, "get_slot", lambda _name: {
        "model": wm_cp5_switch.DEFAULT_WORKING_MODEL_GATE_MODEL,
        "endpoint": {},
        "extras": {},
    })
    with pytest.raises(wm_cp5_switch.CP5SwitchError, match="trigger"):
        wm_cp5_switch.switch(
            enable=True,
            db_path=db_path,
            behavior_path=behavior_path,
        )
    assert not behavior_path.exists()


def test_runtime_trigger_assertion_is_loud(tmp_path):
    db_path = tmp_path / "chat.db"
    _create_switch_db(db_path, with_trigger=True)

    async def check():
        async with aiosqlite.connect(db_path) as db:
            await wm_service.assert_v2_write_path_ready_in_tx(db)

    with pytest.raises(wm_service.WorkingModelActivationError, match="trigger"):
        _run(check())


def test_new_durable_heads_are_the_next_prompt_source(tmp_path, monkeypatch):
    db_path = tmp_path / "chat.db"

    @asynccontextmanager
    async def db_factory():
        async with aiosqlite.connect(db_path) as db:
            yield db

    async def prepare():
        async with aiosqlite.connect(db_path) as db:
            await init_working_model_tables(db)
            await init_desire_tables(db)
            await wm_repository.insert_version(
                db,
                version_id="wm-root",
                previous_version_id=None,
                content="旧认识",
                created_at=1.0,
                origin_conv_id=None,
                origin_message_id=None,
                origin_request_id=None,
                reason="",
                writer_model="unknown",
                prompt_version="legacy",
                diff_ratio=None,
                flagged=1,
            )
            await ensure_desire_root_in_tx(db, working_model_id="wm-root", created_at=1.0)
            await wm_repository.insert_version(
                db,
                version_id="wm-next",
                previous_version_id="wm-root",
                content="刚落库的新认识",
                created_at=2.0,
                origin_conv_id="conv",
                origin_message_id="assistant",
                origin_request_id="request",
                reason="申请和出处",
                writer_model="core",
                prompt_version="writer-v4",
                diff_ratio=0.5,
                flagged=0,
            )
            await desire_repository.insert_version(
                db,
                version_id="desire-next",
                previous_version_id="desire_root",
                content="刚落库的新姿态",
                change_note="随新认识调整",
                origin_request_id="request",
                working_model_id="wm-next",
                writer_model="core",
                prompt_version="writer-v4",
                created_at=2.0,
            )
            await db.commit()

    _run(prepare())

    load_heads = wm_service.load_v2_prompt_heads

    async def load_test_heads():
        return await load_heads(db_factory=db_factory)

    monkeypatch.setattr(memory_prompt, "working_model_v2_injection_enabled", lambda: True)
    monkeypatch.setattr(memory_prompt.working_model_service, "load_v2_prompt_heads", load_test_heads)
    history = [{"role": "system", "content": "identity"}]
    _run(memory_prompt.inject_working_model_prompt(history, cap_idx=0, inject_offset=0))
    contents = [item["content"] for item in history]
    assert "[你对她的当前认识]\n刚落库的新认识" in contents
    assert "[你此刻想以怎样的姿态与她相处]\n刚落库的新姿态" in contents
    assert all("旧认识" not in content for content in contents)
