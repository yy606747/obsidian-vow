"""誓约层 Phase 2 测试：regenerate 事务、删除/编辑撤约、其余管道 strip+注入+失败策略
（设计 §10 之 5、13、14、16、17、18）。"""

import asyncio
import sqlite3
from contextlib import asynccontextmanager
from functools import partial
from types import SimpleNamespace

import aiosqlite
import pytest

from app.chat import crud_routes, initiative_helpers, side_effects, streaming
from app.chat.models import MsgUpdate
from app.chat.streaming import (
    ReplacedMessageNotFound,
    replace_message_and_freeze_vow_context,
)
from app.schedule import trigger
from app.tools.schemas import ToolContext
from app.vows.schema import init_vow_tables
from app.vows.service import VowReadError
from app.web_search.schema import init_web_search_tables
from app.memory_v3.schema import init_memory_v3_tables

import opportunity as opportunity_mod


@asynccontextmanager
async def _open_db(path):
    async with aiosqlite.connect(path) as db:
        yield db


def _init_chat_db(tmp_path, name="phase2.db"):
    db_path = str(tmp_path / name)

    async def _init():
        async with _open_db(db_path) as db:
            await db.execute(
                "CREATE TABLE conversations (id TEXT PRIMARY KEY, title TEXT, model TEXT, created_at REAL, updated_at REAL)"
            )
            await db.execute(
                "CREATE TABLE messages (id TEXT PRIMARY KEY, conv_id TEXT, role TEXT, content TEXT, created_at REAL, attachments TEXT)"
            )
            await init_vow_tables(db)
            await init_web_search_tables(db)
            await db.execute(
                "CREATE TABLE memory_chunks (id TEXT PRIMARY KEY, conv_id TEXT, "
                "message_ids_json TEXT NOT NULL DEFAULT '[]', content TEXT, "
                "created_at REAL, updated_at REAL, embedding BLOB, "
                "keywords_json TEXT NOT NULL DEFAULT '[]', metadata_json TEXT NOT NULL DEFAULT '{}')"
            )
            await db.execute(
                "CREATE TABLE memory_items (id TEXT PRIMARY KEY, legacy_memory_id TEXT, "
                "metadata_json TEXT NOT NULL DEFAULT '{}')"
            )
            await init_memory_v3_tables(db)
            await db.commit()

    asyncio.run(_init())
    return db_path


def _query(db_path, sql, params=()):
    conn = sqlite3.connect(db_path)
    try:
        conn.row_factory = sqlite3.Row
        return [dict(r) for r in conn.execute(sql, params).fetchall()]
    finally:
        conn.close()


def _exec(db_path, sql, params=()):
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(sql, params)
        conn.commit()
    finally:
        conn.close()


def _insert_vow(db_path, *, vow_id, content, status="active", origin_message_id=None,
                root_id=None, previous_version_id=None, origin_type="ai_marker",
                close_action=None):
    _exec(
        db_path,
        "INSERT INTO vows (id, root_id, previous_version_id, content, status, origin_type,"
        " origin_conv_id, origin_message_id, created_at, status_changed_at, close_action, closed_reason)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        (vow_id, root_id or vow_id, previous_version_id, content, status, origin_type,
         "conv1", origin_message_id, 1000.0, 1000.0, close_action, None),
    )


def _insert_msg(db_path, msg_id, role="assistant", conv_id="conv1", content="正文"):
    _exec(
        db_path,
        "INSERT INTO messages VALUES (?,?,?,?,?,?)",
        (msg_id, conv_id, role, content, 1000.0, "[]"),
    )


class _RaisingVowService:
    """load 即抛 VowReadError 的桩：验证各管道按 §5.2 三分类处置。"""

    def __init__(self):
        self.load_calls = 0

    async def load_vow_prompt_context(self):
        self.load_calls += 1
        raise VowReadError("vow store down")


class _FixedVowService:
    def __init__(self, block="【你们之间已经说定的事】\n- 每年今天一起看海（今天立下）", ability=""):
        self.block = block
        self.ability = ability

    async def load_vow_prompt_context(self):
        return self.block, self.ability


def _fake_broadcaster(sink):
    async def fake_broadcast(payload):
        sink.append(payload)
    return SimpleNamespace(broadcast=fake_broadcast)


async def _noop_async(*_args, **_kwargs):
    return None


# ── §10-13 regenerate：replaced_message_id 事务全流程 ──


def _patch_streaming_db(monkeypatch, db_path):
    broadcasts = []
    monkeypatch.setattr(streaming, "get_db", partial(_open_db, db_path))
    monkeypatch.setattr(streaming, "manager", _fake_broadcaster(broadcasts))
    monkeypatch.setattr(streaming, "export_conversation", _noop_async)
    return broadcasts


def test_replace_tx_revokes_deletes_and_freezes_snapshot(monkeypatch, tmp_path):
    db_path = _init_chat_db(tmp_path)
    _insert_msg(db_path, "m1", content="正文 🔏 记下了")
    _insert_vow(db_path, vow_id="v1", content="被撤的约", origin_message_id="m1")
    _insert_vow(db_path, vow_id="v2", content="留下的约")
    broadcasts = _patch_streaming_db(monkeypatch, db_path)

    vow_block, vow_ability = asyncio.run(
        replace_message_and_freeze_vow_context("conv1", "m1")
    )

    # 旧消息删除；关联 vow 退役且 close_action=origin_regenerated
    assert _query(db_path, "SELECT * FROM messages WHERE id='m1'") == []
    v1 = _query(db_path, "SELECT * FROM vows WHERE id='v1'")[0]
    assert v1["status"] == "retired" and v1["close_action"] == "origin_regenerated"
    v2 = _query(db_path, "SELECT * FROM vows WHERE id='v2'")[0]
    assert v2["status"] == "active"
    # snapshot 在事务内冻结：只含剩余 active，撤销的不在
    assert "留下的约" in vow_block
    assert "被撤的约" not in vow_block
    assert vow_ability  # 能力片段照常给出（regenerate 可立约）
    # 提交后才广播 msg_deleted
    assert any(b.get("type") == "msg_deleted" and b["data"]["id"] == "m1" for b in broadcasts)


def test_replace_tx_failure_preserves_message_and_vow(monkeypatch, tmp_path):
    db_path = _init_chat_db(tmp_path)
    _insert_msg(db_path, "m1")
    _insert_vow(db_path, vow_id="v1", content="被撤的约", origin_message_id="m1")
    broadcasts = _patch_streaming_db(monkeypatch, db_path)

    async def broken_list_active(_db):
        raise RuntimeError("read failed mid-tx")

    # 撤约+删消息之后、snapshot 冻结这一步失败 → 整事务回滚
    monkeypatch.setattr(streaming.vow_repository, "list_active", broken_list_active)
    with pytest.raises(RuntimeError):
        asyncio.run(replace_message_and_freeze_vow_context("conv1", "m1"))

    assert len(_query(db_path, "SELECT * FROM messages WHERE id='m1'")) == 1
    v1 = _query(db_path, "SELECT * FROM vows WHERE id='v1'")[0]
    assert v1["status"] == "active" and v1["close_action"] is None
    assert not any(b.get("type") == "msg_deleted" for b in broadcasts)


def test_replace_tx_validates_ownership(monkeypatch, tmp_path):
    db_path = _init_chat_db(tmp_path)
    _insert_msg(db_path, "m_user", role="user")
    _insert_msg(db_path, "m_other", conv_id="conv_other")
    _patch_streaming_db(monkeypatch, db_path)

    for conv_id, msg_id in (
        ("conv1", "missing"),       # 不存在
        ("conv1", "m_other"),       # 不属于该会话
        ("conv1", "m_user"),        # 非 assistant
    ):
        with pytest.raises(ReplacedMessageNotFound):
            asyncio.run(replace_message_and_freeze_vow_context(conv_id, msg_id))
    # 校验失败不删任何东西
    assert len(_query(db_path, "SELECT * FROM messages")) == 2


# ── §10-13/14 消息 DELETE 撤约与分支 ──


def _patch_crud_db(monkeypatch, db_path):
    broadcasts = []
    monkeypatch.setattr(crud_routes, "get_db", partial(_open_db, db_path))
    monkeypatch.setattr(crud_routes, "manager", _fake_broadcaster(broadcasts))
    monkeypatch.setattr(crud_routes, "export_conversation", _noop_async)
    return broadcasts


def test_delete_message_revokes_active_vow(monkeypatch, tmp_path):
    db_path = _init_chat_db(tmp_path)
    _insert_msg(db_path, "m1")
    _insert_vow(db_path, vow_id="v1", content="约", origin_message_id="m1")
    broadcasts = _patch_crud_db(monkeypatch, db_path)

    result = asyncio.run(crud_routes.delete_message("m1"))
    assert result == {"ok": True}
    assert _query(db_path, "SELECT * FROM messages WHERE id='m1'") == []
    v1 = _query(db_path, "SELECT * FROM vows WHERE id='v1'")[0]
    assert v1["status"] == "retired" and v1["close_action"] == "origin_deleted"
    assert any(b.get("type") == "msg_deleted" for b in broadcasts)


def test_delete_message_superseded_is_noop_and_successor_untouched(monkeypatch, tmp_path):
    # §10-14：原始行已被 UI 修订为 superseded → 撤约合法 no-op，消息照常删除；
    # 修订产生的 active 后继版本不被波及。
    db_path = _init_chat_db(tmp_path)
    _insert_msg(db_path, "m1")
    _insert_vow(db_path, vow_id="v1", content="旧版", status="superseded",
                origin_message_id="m1", close_action="revised")
    _insert_vow(db_path, vow_id="v2", content="修订版", root_id="v1",
                previous_version_id="v1", origin_type="user_ui")
    _patch_crud_db(monkeypatch, db_path)

    result = asyncio.run(crud_routes.delete_message("m1"))
    assert result == {"ok": True}
    assert _query(db_path, "SELECT * FROM messages WHERE id='m1'") == []
    assert _query(db_path, "SELECT status FROM vows WHERE id='v1'")[0]["status"] == "superseded"
    assert _query(db_path, "SELECT status FROM vows WHERE id='v2'")[0]["status"] == "active"


def test_delete_message_failure_rolls_back(monkeypatch, tmp_path):
    db_path = _init_chat_db(tmp_path)
    _insert_msg(db_path, "m1")
    _insert_vow(db_path, vow_id="v1", content="约", origin_message_id="m1")
    _patch_crud_db(monkeypatch, db_path)

    async def broken_revoke(*_args, **_kwargs):
        raise RuntimeError("revoke failed")

    monkeypatch.setattr(
        crud_routes, "vow_service",
        SimpleNamespace(revoke_for_origin_message_in_tx=broken_revoke),
    )
    result = asyncio.run(crud_routes.delete_message("m1"))
    assert result["ok"] is False
    # 任一步失败整体回滚：消息与 vow 都保留
    assert len(_query(db_path, "SELECT * FROM messages WHERE id='m1'")) == 1
    assert _query(db_path, "SELECT status FROM vows WHERE id='v1'")[0]["status"] == "active"


# ── §13-1 批量删除路径的撤约（会话删除 / 聊天文件覆盖导入）──


def test_delete_conversation_bulk_revokes_vows(monkeypatch, tmp_path):
    db_path = _init_chat_db(tmp_path)
    _insert_msg(db_path, "m1")
    _insert_msg(db_path, "m2")
    _insert_msg(db_path, "m_other", conv_id="conv_other")
    _insert_vow(db_path, vow_id="v1", content="约一", origin_message_id="m1")
    _insert_vow(db_path, vow_id="v2", content="约二", origin_message_id="m2")
    _insert_vow(db_path, vow_id="v3", content="别的会话的约", origin_message_id="m_other")
    # UI 修订产生的 active 后继（origin_message_id 为 NULL）不受波及
    _insert_vow(db_path, vow_id="v4", content="修订后继", origin_type="user_ui")
    broadcasts = _patch_crud_db(monkeypatch, db_path)
    monkeypatch.setattr("routes.files.delete_exported_file", lambda _cid: None)
    _exec(db_path, "INSERT INTO conversations VALUES (?,?,?,?,?)",
          ("conv1", "标题", "m", 1000.0, 1000.0))

    result = asyncio.run(crud_routes.delete_conversation("conv1"))
    assert result == {"ok": True}
    assert _query(db_path, "SELECT * FROM conversations WHERE id='conv1'") == []
    for vid in ("v1", "v2"):
        row = _query(db_path, f"SELECT * FROM vows WHERE id='{vid}'")[0]
        assert row["status"] == "retired" and row["close_action"] == "origin_deleted"
    assert _query(db_path, "SELECT status FROM vows WHERE id='v3'")[0]["status"] == "active"
    assert _query(db_path, "SELECT status FROM vows WHERE id='v4'")[0]["status"] == "active"
    assert any(b.get("type") == "vow_changed" for b in broadcasts)


def test_delete_conversation_without_vows_no_vow_broadcast(monkeypatch, tmp_path):
    db_path = _init_chat_db(tmp_path)
    _insert_msg(db_path, "m1")
    broadcasts = _patch_crud_db(monkeypatch, db_path)
    monkeypatch.setattr("routes.files.delete_exported_file", lambda _cid: None)
    _exec(db_path, "INSERT INTO conversations VALUES (?,?,?,?,?)",
          ("conv1", "标题", "m", 1000.0, 1000.0))

    result = asyncio.run(crud_routes.delete_conversation("conv1"))
    assert result == {"ok": True}
    assert not any(b.get("type") == "vow_changed" for b in broadcasts)


def test_save_chat_file_overwrite_revokes_vows(monkeypatch, tmp_path):
    from routes import files as files_routes

    db_path = _init_chat_db(tmp_path)
    _insert_msg(db_path, "m1")
    _insert_vow(db_path, vow_id="v1", content="约", origin_message_id="m1")
    broadcasts = []
    monkeypatch.setattr(files_routes, "get_db", partial(_open_db, db_path))
    monkeypatch.setattr(files_routes, "manager", _fake_broadcaster(broadcasts))
    monkeypatch.setattr(files_routes, "load_file_index", lambda: {})
    monkeypatch.setattr(files_routes, "save_file_index", lambda _idx: None)
    monkeypatch.setattr(files_routes, "load_worldbook", lambda: {})
    monkeypatch.setattr(files_routes, "CHATS_DIR", tmp_path)
    _exec(db_path, "INSERT INTO conversations VALUES (?,?,?,?,?)",
          ("conv1", "标题", "m", 1000.0, 1000.0))

    result = asyncio.run(files_routes.save_chat_file(
        "conv1", files_routes.FileContent(content="# 标题\n")
    ))
    assert result.get("ok", True) is not False
    assert _query(db_path, "SELECT * FROM messages WHERE conv_id='conv1'") == []
    v1 = _query(db_path, "SELECT * FROM vows WHERE id='v1'")[0]
    assert v1["status"] == "retired" and v1["close_action"] == "origin_deleted"
    assert any(b.get("type") == "vow_changed" for b in broadcasts)


# ── §10-18 消息编辑：PUT 只允许 user ──


def test_put_rejects_assistant_and_system_messages(monkeypatch, tmp_path):
    db_path = _init_chat_db(tmp_path)
    _insert_msg(db_path, "m_ai", role="assistant", content="正文 🔏 记下了")
    _insert_msg(db_path, "m_sys", role="system", content="系统提示")
    _patch_crud_db(monkeypatch, db_path)

    for msg_id in ("m_ai", "m_sys"):
        result = asyncio.run(crud_routes.update_message(msg_id, MsgUpdate(content="改掉")))
        assert result["ok"] is False
        assert result["error"] == "only_user_messages_editable"
    # 内容未被改动（vow 来源消息的确认语保持原样）
    assert _query(db_path, "SELECT content FROM messages WHERE id='m_ai'")[0]["content"] == "正文 🔏 记下了"


def test_put_allows_user_message(monkeypatch, tmp_path):
    db_path = _init_chat_db(tmp_path)
    _insert_msg(db_path, "m_u", role="user", content="原话")
    broadcasts = _patch_crud_db(monkeypatch, db_path)

    result = asyncio.run(crud_routes.update_message("m_u", MsgUpdate(content="改后")))
    assert result == {"ok": True}
    assert _query(db_path, "SELECT content FROM messages WHERE id='m_u'")[0]["content"] == "改后"
    assert any(b.get("type") == "msg_updated" for b in broadcasts)


def test_put_missing_message_not_found(monkeypatch, tmp_path):
    db_path = _init_chat_db(tmp_path)
    _patch_crud_db(monkeypatch, db_path)
    result = asyncio.run(crud_routes.update_message("nope", MsgUpdate(content="x")))
    assert result == {"ok": False, "error": "not_found"}


# ── §10-5 strip 先于工具解析：日程路径 ──


def test_schedule_postprocess_strips_vow_before_music_and_schedule(monkeypatch):
    searched = []
    schedule_inputs = []
    tool_context = ToolContext(conv_id="conv1", request_id="test_schedule_postprocess")

    monkeypatch.setattr(trigger, "search_songs", lambda kw, limit=5: searched.append(kw) or [])

    async def fake_schedule(text, _conv_id, **_kwargs):
        schedule_inputs.append(text)
        return text, []

    async def fake_record_postprocess(*_args, **_kwargs):
        return None

    monkeypatch.setattr(trigger, "process_schedule_commands_with_results", fake_schedule)
    monkeypatch.setattr(
        trigger.tool_invocation_ledger,
        "record_postprocess",
        fake_record_postprocess,
    )

    # 完整标记内的 MUSIC 指令是惰性文本
    text, cards = asyncio.run(trigger._postprocess_reply(
        "[VOW:今晚都听 [MUSIC:雨声|好] 正文在",
        "conv1",
        tool_context=tool_context,
        ai_name="Aion",
    ))
    assert searched == [] and cards == []
    assert "[VOW" not in text and "[MUSIC" not in text

    # 未闭合标记删到结尾：内部 ALARM 指令不进入 schedule 解析
    text2, _ = asyncio.run(trigger._postprocess_reply(
        "正文。[VOW:把 [ALARM:08:00 设上",
        "conv1",
        tool_context=tool_context,
        ai_name="Aion",
    ))
    assert text2 == "正文。"
    assert all("[ALARM" not in s and "[VOW" not in s for s in schedule_inputs)

    text3, _ = asyncio.run(trigger._postprocess_reply(
        "闹铃正文。[RECALL_INTENT]不应在定时路径显示[/RECALL_INTENT]",
        "conv1",
        tool_context=tool_context,
        ai_name="Aion",
    ))
    assert text3 == "闹铃正文。"
    assert all("RECALL_INTENT" not in s for s in schedule_inputs)


# ── §10-16 must-fire：闹铃降级模板；monitor 跳过 ──


def _patch_trigger_env(monkeypatch, db_path):
    broadcasts = []
    monitor_logs = []
    monkeypatch.setattr(trigger, "get_db", partial(_open_db, db_path))
    monkeypatch.setattr(trigger, "manager", _fake_broadcaster(broadcasts))
    monkeypatch.setattr(trigger, "export_conversation", _noop_async)
    monkeypatch.setattr(trigger, "load_worldbook", lambda: {"user_name": "她", "ai_name": "AI"})
    monkeypatch.setattr(trigger, "append_monitor_log", lambda entry: monitor_logs.append(entry))

    async def fake_latest_conversation():
        return {"id": "conv1", "model": "gemini-3-flash"}

    monkeypatch.setattr(trigger, "_latest_conversation", fake_latest_conversation)
    return broadcasts, monitor_logs


def test_alarm_must_fire_fallback_on_vow_read_error(monkeypatch, tmp_path):
    db_path = _init_chat_db(tmp_path)
    broadcasts, _ = _patch_trigger_env(monkeypatch, db_path)
    monkeypatch.setattr(trigger, "vow_service", _RaisingVowService())

    items = [{"id": "s1", "type": "alarm", "content": "去喝水", "trigger_at": "2026-06-12 10:00"}]
    asyncio.run(trigger._fire(items))

    msgs = _query(db_path, "SELECT * FROM messages")
    assert len(msgs) == 1
    # 降级固定模板：system 消息，不以人格开口，绝不是 assistant
    assert msgs[0]["role"] == "system"
    assert msgs[0]["content"] == "⏰ 到点了：去喝水"
    assert any(b.get("type") == "msg_created" for b in broadcasts)


def test_monitor_skips_on_vow_read_error(monkeypatch, tmp_path):
    db_path = _init_chat_db(tmp_path)
    _, monitor_logs = _patch_trigger_env(monkeypatch, db_path)
    monkeypatch.setattr(trigger, "vow_service", _RaisingVowService())

    items = [{"id": "s2", "type": "monitor", "content": "看看她在干嘛", "trigger_at": "2026-06-12 10:00"}]
    asyncio.run(trigger._fire(items))

    # 静默跳过：不落任何消息，但记录错误
    assert _query(db_path, "SELECT * FROM messages") == []
    assert any(entry.get("status") == "vow_read_failed" for entry in monitor_logs)


# ── §10-17 注入存在性：日程路径 vow block 先于 ability block ──


def test_schedule_build_messages_injects_vow_before_ability(monkeypatch):
    monkeypatch.setattr(trigger, "vow_service", _FixedVowService())
    monkeypatch.setattr(trigger, "store", SimpleNamespace(
        build_schedule_prompt=lambda _items: "SCHED",
        list_active=_noop_async,
    ))
    monkeypatch.setattr(trigger, "prompt", SimpleNamespace(
        build_abilities_block=lambda _u, _s: "ABILITY-BLOCK",
        build_alarm_trigger_prompt=lambda _i, _n, _u: "TRIGGER",
    ))
    async def missing_alarm_context(*_args, **_kwargs):
        return {"status": "missing", "block": "", "visible_messages": []}

    async def missing_timeline(**_kwargs):
        return {"status": "missing", "block": "", "entries": []}

    monkeypatch.setattr(
        trigger,
        "alarm_context",
        SimpleNamespace(load_prompt_context=missing_alarm_context),
    )
    monkeypatch.setattr(
        trigger,
        "timeline_service",
        SimpleNamespace(prompt_context=missing_timeline),
    )

    items = [{"id": "s1", "type": "alarm", "content": "喝水", "trigger_at": "t"}]
    messages, _, _, _ = asyncio.run(
        trigger._build_messages(items, {}, "conv1", "now", "她")
    )
    contents = [m["content"] for m in messages]
    vow_idx = next(i for i, c in enumerate(contents) if "你们之间已经说定的事" in c)
    ability_idx = next(i for i, c in enumerate(contents) if c == "ABILITY-BLOCK")
    assert vow_idx < ability_idx


# ── §10-16/17 截图 follow-up：fail-closed + 注入 ──


def _screen_request():
    event = asyncio.Event()
    return SimpleNamespace(
        _done_event=event, status="completed", conv_id="conv1", msg_id="m_src",
        model_key="gemini-3-flash", reason="看看她", reject_reason=None,
        image_path="/tmp/x.png", request_id="req1",
    )


def _screen_ops():
    released = []
    return SimpleNamespace(
        timeout=0.05,
        expire=lambda _r: None,
        audit=_noop_async,
        delete_files=lambda _r: None,
        release=lambda rid: released.append(rid),
    ), released


def test_screen_followup_fail_closed_persists_system_via_ws(monkeypatch, tmp_path):
    db_path = _init_chat_db(tmp_path)
    broadcasts = []
    monkeypatch.setattr(side_effects, "get_db", partial(_open_db, db_path))
    monkeypatch.setattr(side_effects, "manager", _fake_broadcaster(broadcasts))
    monkeypatch.setattr(side_effects, "vow_service", _RaisingVowService())

    request = _screen_request()
    request._done_event.set()
    ops, released = _screen_ops()

    asyncio.run(side_effects._run_screen_followup(
        request, ops=ops, screen_label="电脑", reject_text={},
    ))

    msgs = _query(db_path, "SELECT * FROM messages")
    # 持久化 system 消息（绝不创建 assistant），经 WebSocket msg_created 广播
    assert len(msgs) == 1
    assert msgs[0]["role"] == "system"
    assert msgs[0]["content"] == side_effects.VOW_BLOCKED_TEXT
    assert any(
        b.get("type") == "msg_created" and b["data"]["id"] == msgs[0]["id"]
        for b in broadcasts
    )
    assert released == ["req1"]  # finally 清理照常执行


def test_screen_followup_messages_inject_vow_block(monkeypatch, tmp_path):
    db_path = _init_chat_db(tmp_path)
    monkeypatch.setattr(side_effects, "get_db", partial(_open_db, db_path))
    request = _screen_request()
    messages = asyncio.run(side_effects._screen_followup_messages(
        request, {}, "她", screen_label="电脑", reject_text={},
        vow_block="【你们之间已经说定的事】\n- 看海",
    ))
    contents = [m["content"] for m in messages]
    vow_idx = next(i for i, c in enumerate(contents) if "你们之间已经说定的事" in c)
    # vow block 在最终指令 prompt 之前
    assert vow_idx < len(contents) - 1
    assert contents[vow_idx + 1] == "（嗯，这些一直都算数。）"


def test_poi_check_skips_on_vow_read_error(monkeypatch, tmp_path):
    db_path = _init_chat_db(tmp_path)
    monkeypatch.setattr(side_effects, "get_db", partial(_open_db, db_path))
    monkeypatch.setattr(side_effects, "vow_service", _RaisingVowService())
    # 让前置 location 检查与搜索通过，确保走到誓约读取这一步
    import location as location_mod
    monkeypatch.setattr(location_mod, "load_location_config",
                        lambda: {"amap_key": "k", "poi_types": {"咖啡": "code"}})
    monkeypatch.setattr(location_mod, "load_location_status", lambda: {"lng": 1.0, "lat": 1.0})

    async def fake_regeo(*_a, **_k):
        return None

    async def fake_poi_search(*_a, **_k):
        return [{"name": "X", "distance": 100}]

    monkeypatch.setattr(location_mod, "amap_regeo", fake_regeo)
    monkeypatch.setattr(location_mod, "amap_poi_search", fake_poi_search)
    monkeypatch.setattr(location_mod, "save_location_status", lambda _s: None)

    asyncio.run(side_effects.perform_poi_check("conv1", "gemini-3-flash", ["咖啡"]))
    # 静默跳过：不落任何消息
    assert _query(db_path, "SELECT * FROM messages") == []


# ── §10-16/17 initiative：跳过 + 注入 + 流过滤 ──


def _initiative_spec():
    return initiative_helpers.InitiativeSpec(
        kind="whisper", context_limit=10, msg_suffix="init",
        ability_block="ABILITY-BLOCK", event_block="EVENT-BLOCK",
        toy_enabled=False,
    )


async def _consume_initiative(response):
    events = []
    async for raw in response.body_iterator:
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8")
        import json as _json
        events.append(_json.loads(raw[len("data: "):].strip()))
    return events


def _run_initiative(monkeypatch, tmp_path, vow_service_obj, reply_text="你好。"):
    db_path = _init_chat_db(tmp_path)
    _exec(db_path, "INSERT INTO conversations VALUES ('conv1','t','gemini-3-flash',1,1)")
    monkeypatch.setattr(initiative_helpers, "vow_service", vow_service_obj)
    captured = {}

    async def fake_stream_ai(history, _model_key, usage_meta):
        captured["history"] = list(history)
        yield reply_text

    async def fake_broadcast(_payload):
        return None

    from app.chat.postprocess import PostProcessor

    async def flow():
        # 后台生成任务与 SSE 消费必须同一事件循环：在一个 run 里走完
        result = await initiative_helpers.stream_initiative_response(
            conv_id="conv1", spec=_initiative_spec(), worldbook={},
            get_db=partial(_open_db, db_path), stream_ai=fake_stream_ai,
            post_processor=PostProcessor(), broadcast=fake_broadcast,
            export_conversation=_noop_async, store_remember_notes=_noop_async,
            toy_sys_msg=_noop_async,
        )
        if isinstance(result, dict):
            return result, None
        return result, await _consume_initiative(result)

    result, events = asyncio.run(flow())
    return db_path, result, events, captured


def test_initiative_skips_on_vow_read_error(monkeypatch, tmp_path):
    service = _RaisingVowService()
    _, result, _, captured = _run_initiative(monkeypatch, tmp_path, service)
    assert result == {"error": "vow_read_failed"}
    assert "history" not in captured  # 模型从未被调用
    assert service.load_calls == 1


def test_initiative_injects_vow_before_ability_and_filters_stream(monkeypatch, tmp_path):
    db_path, _, events, captured = _run_initiative(
        monkeypatch, tmp_path, _FixedVowService(),
        reply_text=(
            "先说点什么 [RECALL_INTENT]主动路径不得执行[/RECALL_INTENT] "
            "[VOW:不该立的|x] 再说点什么"
        ),
    )
    contents = [m["content"] for m in captured["history"]]
    vow_idx = next(i for i, c in enumerate(contents) if "你们之间已经说定的事" in c)
    ability_idx = contents.index("ABILITY-BLOCK")
    assert vow_idx < ability_idx
    # 流过滤：标记不漏进可见流；本路径不允许立约，剥除即丢弃
    chunks = [e["content"] for e in events if e.get("type") == "chunk"]
    assert all("[VOW" not in c for c in chunks)
    assert all("RECALL_INTENT" not in c for c in chunks)
    msgs = _query(db_path, "SELECT * FROM messages WHERE role='assistant'")
    assert len(msgs) == 1
    assert "[VOW" not in msgs[0]["content"]
    assert "RECALL_INTENT" not in msgs[0]["content"]
    vows = _query(db_path, "SELECT * FROM vows")
    assert vows == []


# ── §10-5/16/17 opportunity ──


def test_opportunity_prompt_injects_vow_before_ability_and_heads(monkeypatch):
    async def fake_history(*_args, **_kwargs):
        return SimpleNamespace(
            history=[],
            cap_idx=0,
            wb={
                "ai_name": "AI",
                "user_name": "她",
                "ai_persona": "有主见且诚实。",
                "user_persona": "重视选择空间。",
            },
        )

    async def fake_heads():
        return (
            {"id": "wm-head", "content": "她在重要决定上希望被正面回答。"},
            {"id": "desire-head", "content": "我想诚实地陪着她。"},
        )

    async def no_mobile(**_kwargs):
        return None

    async def capabilities(**_kwargs):
        return frozenset({"heart.whisper"})

    monkeypatch.setattr(opportunity_mod, "prepare_chat_history", fake_history)
    monkeypatch.setattr(
        opportunity_mod,
        "vow_service",
        _FixedVowService(block="【你们之间已经说定的事】\n- 看海"),
    )
    monkeypatch.setattr(
        opportunity_mod.working_model_service,
        "load_v2_prompt_heads",
        fake_heads,
    )
    monkeypatch.setattr(opportunity_mod, "load_ai_behavior", lambda: {})
    monkeypatch.setattr(
        opportunity_mod,
        "working_model_v2_injection_enabled",
        lambda: True,
    )
    monkeypatch.setattr(opportunity_mod, "_autonomous_mobile_screen_target", no_mobile)
    monkeypatch.setattr(opportunity_mod, "_runtime_capabilities", capabilities)
    monkeypatch.setattr(
        opportunity_mod,
        "_runtime_context_text",
        lambda **_kwargs: "[空闲窗口实时状态]",
    )

    prepared = asyncio.run(
        opportunity_mod._prepare_opportunity_turn(
            target={"conv_id": "conv1", "model_key": "core", "last_user_ts": 1.0},
            now=1000.0,
        )
    )
    contents = [message["content"] for message in prepared.messages]
    vow_index = next(i for i, text in enumerate(contents) if "看海" in text)
    ability_index = next(i for i, text in enumerate(contents) if "[本轮可用能力]" in text)
    wm_index = next(i for i, text in enumerate(contents) if "[你对她的当前认识]" in text)
    desire_index = next(i for i, text in enumerate(contents) if "[你此刻想以怎样的姿态与她相处]" in text)
    assert vow_index < ability_index < wm_index < desire_index
    assert contents[-1].startswith("现在没人找我，我有一个空闲的瞬间。")


def _patch_opportunity_env(monkeypatch, vow_service_obj, raw_reply):
    calls = {"slot": 0, "sent": []}

    async def fake_slot_chat(_slot, _messages, **_kwargs):
        calls["slot"] += 1
        return raw_reply

    async def fake_send_message(text, **_kwargs):
        calls["sent"].append(text)
        return True

    async def fake_broadcast_log(_entry):
        return None

    async def fake_prepare(**_kwargs):
        if isinstance(vow_service_obj, _RaisingVowService):
            await vow_service_obj.load_vow_prompt_context()
        return opportunity_mod.PreparedOpportunityTurn(
            messages=[{"role": "user", "content": "idle trigger"}],
            profile=opportunity_mod.opportunity_turn_profile(
                runtime_capabilities=(),
                reflection_allowed=False,
            ),
            model_key="gemini-3-flash",
            identity_snapshot={"text": "identity"},
            reflection_context=None,
            mobile_screen_target=None,
        )

    monkeypatch.setattr(opportunity_mod, "vow_service", vow_service_obj)
    monkeypatch.setattr(opportunity_mod, "call_opportunity_core", fake_slot_chat)
    monkeypatch.setattr(opportunity_mod, "_send_message", fake_send_message)
    monkeypatch.setattr(opportunity_mod, "_prepare_opportunity_turn", fake_prepare)
    monkeypatch.setattr(opportunity_mod, "_broadcast_log", fake_broadcast_log)
    monkeypatch.setattr(opportunity_mod, "load_worldbook", lambda: {"ai_name": "AI"})
    return calls


def _run_opportunity(monkeypatch, vow_service_obj, raw_reply):
    calls = _patch_opportunity_env(monkeypatch, vow_service_obj, raw_reply)
    runner = opportunity_mod.OpportunityRunner()
    result = asyncio.run(runner._run(
        1000.0, {"conv_id": "conv1", "model_key": "gemini-3-flash", "last_user_ts": 1.0}
    ))
    return calls, result


def test_opportunity_discards_vow_and_recall_private_channels(monkeypatch):
    calls, result = _run_opportunity(
        monkeypatch, _FixedVowService(block=""),
        "嗯 [VOW:不该立的|x] [RECALL_INTENT]主动机会不得执行[/RECALL_INTENT] 在呢",
    )
    assert result["status"] == "acted"
    assert len(calls["sent"]) == 1
    assert "[VOW" not in calls["sent"][0]
    assert "RECALL_INTENT" not in calls["sent"][0]


def test_opportunity_skips_on_vow_read_error(monkeypatch):
    calls, result = _run_opportunity(
        monkeypatch, _RaisingVowService(), "[OPPORTUNITY_NONE]",
    )
    assert result["status"] == "action_failed"
    assert result["error"].startswith("vow_read_failed:")
    assert calls["slot"] == 0  # 模型从未被调用


# ── §10-5/16/17 Sentinel v2 core wake（经 ports）──


def _orchestrator_harness():
    from test_sentinel_core_wake_orchestrator import (
        _FakeCoreWakePorts,
        _execution_context,
        _wake_package,
    )
    return _FakeCoreWakePorts, _execution_context, _wake_package


def test_core_wake_strips_vow_before_toy_extraction():
    from app.sentinel import run_core_wake_orchestrator_test_execute

    _FakeCoreWakePorts, _execution_context, _wake_package = _orchestrator_harness()
    ports = _FakeCoreWakePorts(
        core_response=(
            "看海。[RECALL_INTENT]唤醒路径不得执行[/RECALL_INTENT]"
            "[VOW:藏着 [TOY:9 的约"
        )
    )

    trace = asyncio.run(run_core_wake_orchestrator_test_execute(
        wake_package=_wake_package(),
        execution_context=_execution_context(),
        ports=ports,
    ))
    assert trace["status"] == "core_succeeded"
    # 未闭合标记删到结尾：内部 TOY 指令绝不进入提取
    assert "toy_commands" not in trace or not trace.get("toy_commands")
    inserted = next(c for c in ports.calls if c[0] == "insert_assistant_message")
    assert inserted[2] == "看海。"


def test_core_wake_injects_vow_block_via_port():
    from app.sentinel import run_core_wake_orchestrator_test_execute

    _FakeCoreWakePorts, _execution_context, _wake_package = _orchestrator_harness()

    class _PortsWithVows(_FakeCoreWakePorts):
        async def load_vow_prompt_context(self):
            return "【你们之间已经说定的事】\n- 看海", ""

    ports = _PortsWithVows()
    trace = asyncio.run(run_core_wake_orchestrator_test_execute(
        wake_package=_wake_package(),
        execution_context=_execution_context(),
        ports=ports,
    ))
    assert trace["status"] == "core_succeeded"
    assert ports.stream_messages[0]["content"].startswith("【你们之间已经说定的事】")
    assert ports.stream_messages[1]["content"] == "（嗯，这些一直都算数。）"


def test_core_wake_skips_on_vow_read_error_via_port():
    from app.sentinel import run_core_wake_orchestrator_test_execute

    _FakeCoreWakePorts, _execution_context, _wake_package = _orchestrator_harness()

    class _PortsWithBrokenVows(_FakeCoreWakePorts):
        async def load_vow_prompt_context(self):
            raise VowReadError("vow store down")

    ports = _PortsWithBrokenVows()
    trace = asyncio.run(run_core_wake_orchestrator_test_execute(
        wake_package=_wake_package(),
        execution_context=_execution_context(),
        ports=ports,
    ))
    assert trace["status"] == "vow_read_failed"
    assert trace["error_type"] == "vow_read_failed"
    # 跳过本次生成：不插唤醒提示、不调模型、不落 assistant 消息；只写监控日志
    called = [c[0] for c in ports.calls]
    assert "stream_core" not in called
    assert "insert_assistant_message" not in called
    assert "insert_system_wake_notice" not in called
    assert called[-1] == "write_monitor_log"
    assert ports.monitor_logs[-1]["status"] == "vow_read_failed"


# ── prepare_regenerate_prompt：snapshot 优先，不再读 vow ──


def test_prepare_regenerate_uses_frozen_snapshot(monkeypatch):
    from test_vows_phase1 import _patch_chat_turn_env
    from app.chat import chat_turn

    service = _RaisingVowService()
    _patch_chat_turn_env(monkeypatch, service)

    async def fake_regen_ability(**_kwargs):
        return "ABILITY-BLOCK"

    monkeypatch.setattr(chat_turn, "build_regenerate_ability_block", fake_regen_ability)

    kwargs = dict(
        context_limit=5, whisper_mode=False, fast_mode=False, ai_dom_mode=False,
        safeword="", dom_history="", cnc_enabled=False, cnc_weakness="",
        resist_hits=0, short_streak=0, reply_delay_ms=0, compliance_streak=0,
        session_elapsed=0, scene_name="", scene_elapsed=0, since_last_punish=None,
        ratchet_valley=0, debt=0.0, stubborn_streak=0,
    )
    _, history, _ = asyncio.run(chat_turn.prepare_regenerate_prompt(
        "conv1", vow_snapshot=("【你们之间已经说定的事】\nSNAP", "VOW-ABILITY"), **kwargs
    ))
    # snapshot 提供时绝不再读 vow（杜绝中间态）
    assert service.load_calls == 0
    contents = [m["content"] for m in history]
    vow_idx = next(i for i, c in enumerate(contents) if "SNAP" in c)
    ability_idx = next(i for i, c in enumerate(contents) if "ABILITY-BLOCK" in c)
    assert vow_idx < ability_idx
    assert "VOW-ABILITY" in contents[ability_idx]

    # 不带 snapshot 时照常读取并 fail-closed
    with pytest.raises(VowReadError):
        asyncio.run(chat_turn.prepare_regenerate_prompt("conv1", **kwargs))
    assert service.load_calls == 1
