"""誓约层 Phase 1 测试：send 主链路端到端（设计 §10 之 1、2、4、6、12、16、17 的 send 部分）。"""

import asyncio
import json
import sqlite3
from contextlib import asynccontextmanager
from functools import partial
from types import SimpleNamespace

import aiosqlite
import pytest

import config
from app.chat import chat_turn, side_effects, streaming
from app.chat.models import MsgCreate
from app.chat.postprocess import PostProcessor
from app.chat.streaming import _VowStreamFilter, vow_blocked_response
from app.vows.schema import init_vow_tables
from app.vows.service import VowReadError, VowService


@asynccontextmanager
async def _open_db(path):
    async with aiosqlite.connect(path) as db:
        yield db


def _init_chat_db(tmp_path):
    db_path = str(tmp_path / "phase1.db")

    async def _init():
        async with _open_db(db_path) as db:
            await db.execute(
                "CREATE TABLE conversations (id TEXT PRIMARY KEY, title TEXT, model TEXT, created_at REAL, updated_at REAL)"
            )
            await db.execute(
                "CREATE TABLE messages (id TEXT PRIMARY KEY, conv_id TEXT, role TEXT, content TEXT, created_at REAL, attachments TEXT)"
            )
            await init_vow_tables(db)
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


_processor = PostProcessor()


def _process(text, **kwargs):
    return asyncio.run(_processor.process(text, conv_id="conv_p1", **kwargs))


# ── §10-1 解析顺序：VOW 内部是惰性文本 ──


def test_tool_marker_inside_vow_not_executed():
    # 内部成对括号不提前闭合标记（括号配对文法，§4.1）
    res = _process("好。[VOW:以后都听 [TOY:9] 的安排|我记下了] 嗯")
    assert res.toy_commands == []
    assert all(i.tool_name != "device.toy" for i in res.tool_intents)
    assert "[VOW" not in res.content and "[TOY" not in res.content
    # 被拒候选的确认语尾段不得泄漏进可见文本
    assert "我记下了" not in res.content
    assert res.vow.found
    # 候选带着 [TOY:9]，进入提交编排后会被净化器拒绝
    assert "[TOY" in res.vow.content_raw


def test_unbalanced_bracket_inside_vow_rejected_as_unclosed():
    # 内部括号不配对 → 深度永不归零 → 按未闭合处理，删到结尾、整体拒绝
    res = _process("好。[VOW:以后都听 [TOY:9 的安排|我记下了] 嗯")
    assert res.toy_commands == []
    assert res.vow.found and res.vow.reject_reason
    assert "[TOY" not in res.content and "我记下了" not in res.content


def test_update_model_inside_vow_not_executed():
    res = _process("[VOW:记住 [UPDATE_MODEL:她很乖 的样子|好]")
    assert res.working_model_update == ""
    assert "[VOW" not in res.content and "[UPDATE_MODEL" not in res.content
    assert res.vow.found


def test_vow_extracted_before_remember_parsing():
    res = _process("[VOW:别忘了 [REMEMBER:今天 这回事|嗯] 以及正文")
    assert res.remember_notes == []
    assert res.vow.found


# ── §10-2 结构化 JSON：只从 assistant_text 提取 ──


def test_structured_reply_vow_only_from_assistant_text():
    payload = json.dumps({
        "assistant_text": "好。[VOW:每年今天一起看海|我记下了]",
        "actions": [
            {"tool_name": "memory.remember", "arguments": {"content": "[VOW:不该立的|x]"}},
        ],
    }, ensure_ascii=False)
    res = _process(payload)
    assert res.vow.found
    assert res.vow.content_raw == "每年今天一起看海"
    assert res.vow.affirmation_raw == "我记下了"
    assert "[VOW" not in res.content


def test_structured_reply_vow_in_actions_only_is_not_a_vow():
    payload = json.dumps({
        "assistant_text": "普通回复",
        "actions": [
            {"tool_name": "memory.remember", "arguments": {"content": "[VOW:不该立的|x]"}},
        ],
    }, ensure_ascii=False)
    res = _process(payload)
    assert res.vow.found is False
    # §10-2 后半句：既不立约也不执行——整条 action 丢弃，标记不得写进记忆
    assert res.remember_notes == []
    assert all(i.tool_name != "memory.remember" for i in res.tool_intents)


def test_actions_only_json_never_creates_vow():
    # 缺 assistant_text 的"合法 JSON"：没有合法提取来源，整段只剥除不立约
    payload = json.dumps({
        "actions": [
            {"tool_name": "memory.remember", "arguments": {"content": "[VOW:不该立的|x]"}},
        ],
    }, ensure_ascii=False)
    res = _process(payload)
    assert res.vow.found is False
    assert "[VOW" not in res.content


def test_broken_structured_json_strips_vow_without_candidate():
    res = _process('```json\n{"assistant_text": "你好", "actions": [{"content": "[VOW:不该立的|x]"}]')
    assert res.vow.found is False
    assert "[VOW" not in res.content


# ── §10-3 净化时序：标记内部对 <meta> 剥除也是惰性的 ──


def test_meta_inside_vow_reaches_sanitizer():
    res = _process("好 [VOW:约定<meta>x</meta>内容|确认]")
    assert res.vow.found
    # 不被 strip_meta_tags 洗白：候选原样带着 <meta，提交时被黑名单拒绝
    assert "<meta" in res.vow.content_raw
    assert "<meta" not in res.content


def test_vow_inside_private_block_dies_with_block():
    res = _process("<think>偷偷 [VOW:私货|x]</think>正文")
    assert res.vow.found is False
    assert res.content == "正文"


# ── §10-4 流过滤：普通路径不漏可见文本 ──


def test_vow_stream_filter_hides_marker_across_chunks():
    f = _VowStreamFilter()
    text = "你好[VOW:每年今天|我记下了]再见"
    visible = ""
    for ch in text:
        visible += f.feed(ch)
    visible += f.flush()
    assert visible == "你好再见"


def test_vow_stream_filter_drops_unclosed_marker():
    f = _VowStreamFilter()
    visible = f.feed("正文 [VOW:没有闭合的标记")
    visible += f.flush()
    assert visible == "正文 "


def test_vow_stream_filter_no_affirmation_leak_with_nested_brackets():
    # 嵌套 [TOY:9] 不提前闭合标记：被拒候选的确认语尾段绝不进入可见流
    f = _VowStreamFilter()
    text = "你好[VOW:约定 [TOY:9]|确认语]再见"
    visible = ""
    for ch in text:
        visible += f.feed(ch)
    visible += f.flush()
    assert visible == "你好再见"


# ── §10-6 eval：照常剥除，禁止写入 ──


def test_eval_mode_strips_vow_and_forbids_write():
    res = _process("回答 [VOW:a|b]", memory_eval_mode=True)
    assert "[VOW" not in res.content
    assert res.vow.found is False


# ── §10-12 原子性：vow 与消息同事务，失败整体回滚 ──


def test_vow_rolls_back_when_message_insert_fails(tmp_path):
    db_path = _init_chat_db(tmp_path)
    service = VowService(get_db_factory=partial(_open_db, db_path))

    async def flow():
        async with _open_db(db_path) as db:
            await db.execute(
                "INSERT INTO messages VALUES ('msg_dup','conv1','assistant','旧消息',1.0,'[]')"
            )
            await db.commit()
        async with _open_db(db_path) as db:
            await db.execute("BEGIN IMMEDIATE")
            try:
                extract = SimpleNamespace(
                    found=True, content_raw="一起看海", affirmation_raw="记下了", reject_reason=None
                )
                vow, affirmation, err = await service.admit_ai_vow_in_tx(
                    db, extract=extract, conv_id="conv1", message_id="msg_dup", created_at=2.0
                )
                assert vow is not None and err is None
                # 消息落库失败（主键冲突）→ 整事务回滚，vow 不残留
                await db.execute(
                    "INSERT INTO messages VALUES ('msg_dup','conv1','assistant','新消息',2.0,'[]')"
                )
                await db.commit()
            except Exception:
                await db.rollback()

    asyncio.run(flow())
    assert _query(db_path, "SELECT * FROM vows") == []


# ── 端到端：stream_chat_response 提交编排 ──


def _patch_streaming_env(monkeypatch, db_path, reply_text):
    broadcasts = []

    async def fake_stream_ai(_history, _model_key, usage_meta, _temperature):
        usage_meta["provider"] = "mock"
        yield reply_text

    async def fake_broadcast(payload):
        broadcasts.append(payload)

    async def noop_async(*_args, **_kwargs):
        return None

    monkeypatch.setattr(streaming, "stream_ai", fake_stream_ai)
    monkeypatch.setattr(streaming, "get_db", partial(_open_db, db_path))
    monkeypatch.setattr(streaming, "manager", SimpleNamespace(broadcast=fake_broadcast))
    monkeypatch.setattr(streaming, "export_conversation", noop_async)
    monkeypatch.setattr(streaming, "_maybe_auto_digest", noop_async)
    monkeypatch.setattr(streaming, "_record_v2_memory_usage_for_chat", noop_async)
    monkeypatch.setattr(streaming, "_schedule_chunk_index_update", lambda *a, **k: None)
    return broadcasts


def _prompt_meta():
    return {
        "recall_keywords": "", "recall_query": "", "recall_topic": "",
        "is_search_needed": False, "recalled_memories": [], "debug_top6": [],
        "memory_v2_recall": None,
        "prompt_messages": [{"role": "user", "content": "hi"}], "prompt_count": 1,
    }


async def _collect_sse(response):
    events = []
    async for raw in response.body_iterator:
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8")
        events.append(json.loads(raw[len("data: "):].strip()))
    return events


def _run_stream(monkeypatch, tmp_path, reply_text):
    db_path = _init_chat_db(tmp_path)
    broadcasts = _patch_streaming_env(monkeypatch, db_path, reply_text)

    async def flow():
        response = await streaming.stream_chat_response(
            conv_id="conv_e2e",
            model_key="gemini-3-flash",
            history=[{"role": "user", "content": "hi"}],
            prompt_meta=_prompt_meta(),
            temperature=None,
        )
        return await _collect_sse(response)

    events = asyncio.run(flow())
    return db_path, events, broadcasts


def test_e2e_vow_committed_and_confirmation_appended(monkeypatch, tmp_path):
    db_path, events, _ = _run_stream(
        monkeypatch, tmp_path, "好。[VOW:每年今天一起看海|我会一直记得这一句]"
    )
    chunks = [e["content"] for e in events if e.get("type") == "chunk"]
    # 标记不漏进可见流
    assert all("[VOW" not in c for c in chunks)
    # 确认语经 SSE 追加（提交后）
    assert any("🔏 我会一直记得这一句" in c for c in chunks)

    vows = _query(db_path, "SELECT * FROM vows")
    assert len(vows) == 1
    assert vows[0]["status"] == "active"
    assert vows[0]["origin_type"] == "ai_marker"
    msgs = _query(db_path, "SELECT * FROM messages WHERE role='assistant'")
    assert len(msgs) == 1
    # 不变量：origin_message_id 指向真实存在且含确认语的消息
    assert vows[0]["origin_message_id"] == msgs[0]["id"]
    assert "🔏 我会一直记得这一句" in msgs[0]["content"]
    assert "[VOW" not in msgs[0]["content"]


def test_e2e_vow_rejected_note_persisted(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "VOW_ACTIVE_MAX", 0)
    db_path, events, _ = _run_stream(
        monkeypatch, tmp_path, "好。[VOW:每年今天一起看海|记下了]"
    )
    assert _query(db_path, "SELECT * FROM vows") == []
    msgs = _query(db_path, "SELECT * FROM messages WHERE role='assistant'")
    assert len(msgs) == 1
    # 拒绝说明持久化在正文，刷新后仍在；确认前缀绝不出现
    assert "没有正式记下" in msgs[0]["content"]
    assert "🔏" not in msgs[0]["content"]
    chunks = [e["content"] for e in events if e.get("type") == "chunk"]
    assert any("没有正式记下" in c for c in chunks)


def test_e2e_multiple_vow_markers_rejected(monkeypatch, tmp_path):
    db_path, _, _ = _run_stream(
        monkeypatch, tmp_path, "[VOW:a1|b1] 中间 [VOW:a2|b2]"
    )
    assert _query(db_path, "SELECT * FROM vows") == []
    msgs = _query(db_path, "SELECT * FROM messages WHERE role='assistant'")
    assert "[VOW" not in msgs[0]["content"]
    assert "没有正式记下" in msgs[0]["content"]


def test_e2e_no_vow_marker_keeps_plain_flow(monkeypatch, tmp_path):
    db_path, events, _ = _run_stream(monkeypatch, tmp_path, "普通回复，无标记。")
    assert _query(db_path, "SELECT * FROM vows") == []
    msgs = _query(db_path, "SELECT * FROM messages WHERE role='assistant'")
    assert msgs[0]["content"] == "普通回复，无标记。"
    assert "没有正式记下" not in msgs[0]["content"]


# ── §10-16 fail-closed（send）──


def test_vow_blocked_response_persists_system_message(monkeypatch, tmp_path):
    db_path = _init_chat_db(tmp_path)
    broadcasts = []

    async def fake_broadcast(payload):
        broadcasts.append(payload)

    monkeypatch.setattr(streaming, "get_db", partial(_open_db, db_path))
    monkeypatch.setattr(streaming, "manager", SimpleNamespace(broadcast=fake_broadcast))
    # 持久化助手落在 side_effects（截图 follow-up 复用），这里一并指向测试库
    monkeypatch.setattr(side_effects, "get_db", partial(_open_db, db_path))
    monkeypatch.setattr(side_effects, "manager", SimpleNamespace(broadcast=fake_broadcast))

    async def flow():
        response = await vow_blocked_response("conv_blocked")
        return await _collect_sse(response)

    events = asyncio.run(flow())
    assert len(events) == 1
    event = events[0]
    assert event["type"] == "generation_blocked"
    assert event["reason"] == "vow_read_failed"
    # 事件携带已持久化 system 消息的完整对象与 id
    assert event["message"]["id"] == event["id"]
    assert event["message"]["role"] == "system"

    msgs = _query(db_path, "SELECT * FROM messages")
    assert len(msgs) == 1
    assert msgs[0]["role"] == "system"
    assert msgs[0]["id"] == event["id"]
    # 绝不创建 assistant 消息
    assert _query(db_path, "SELECT * FROM messages WHERE role='assistant'") == []
    # WebSocket 双通道：msg_created 同步广播
    assert any(b.get("type") == "msg_created" and b["data"]["id"] == event["id"] for b in broadcasts)


def test_load_vow_prompt_context_raises_vow_read_error():
    @asynccontextmanager
    async def broken_db():
        raise RuntimeError("db down")
        yield  # pragma: no cover

    service = VowService(get_db_factory=broken_db)
    with pytest.raises(VowReadError):
        asyncio.run(service.load_vow_prompt_context())


# ── §10-17 注入存在性（send 管道）──


def _patch_chat_turn_env(monkeypatch, vow_service_obj):
    from app.control import ControlPromptContext

    async def fake_prepare_chat_history(conv_id, **_kwargs):
        return SimpleNamespace(
            history=[{"role": "user", "content": "hi"}],
            wb={"user_name": "用户A"},
            cap_idx=0,
            model_key="gemini-3-flash",
            actual_recent=1,
            previous_conversation_id=None,
        )

    class FakeControl:
        async def get_prompt_context(self, _conv_id, _payload):
            return ControlPromptContext()

    async def fake_ability_block(**_kwargs):
        return "ABILITY-BLOCK"

    async def fake_inject_memory_prompt(history, **_kwargs):
        return _kwargs["inject_offset"], {}

    monkeypatch.setattr(chat_turn, "prepare_chat_history", fake_prepare_chat_history)
    monkeypatch.setattr(chat_turn, "control_session_service", FakeControl())
    monkeypatch.setattr(chat_turn, "build_send_ability_block", fake_ability_block)
    monkeypatch.setattr(chat_turn, "inject_memory_prompt", fake_inject_memory_prompt)
    monkeypatch.setattr(chat_turn, "vow_service", vow_service_obj)


def test_prepare_send_prompt_injects_vow_block_before_ability(monkeypatch, tmp_path):
    db_path = _init_chat_db(tmp_path)
    service = VowService(get_db_factory=partial(_open_db, db_path))
    vow, err = asyncio.run(service.create_ui_vow("每年今天一起看海"))
    assert err is None
    _patch_chat_turn_env(monkeypatch, service)

    _, history, _ = asyncio.run(
        chat_turn.prepare_send_prompt("conv_inject", MsgCreate(content="hi"))
    )
    contents = [m["content"] for m in history]
    vow_idx = next(i for i, c in enumerate(contents) if "你们之间已经说定的事" in c)
    ability_idx = next(i for i, c in enumerate(contents) if "ABILITY-BLOCK" in c)
    # vow block 先注入，恒在 ability block 之前
    assert vow_idx < ability_idx
    assert "每年今天一起看海" in contents[vow_idx]
    # [VOW] 能力纪律与每日限额放在 ability block 内
    assert "[VOW:誓约内容|确认语]" in contents[ability_idx]
    assert "只收一年后你们仍希望它为真的东西" in contents[ability_idx]


def test_prepare_send_prompt_fail_closed_on_vow_read_error(monkeypatch):
    @asynccontextmanager
    async def broken_db():
        raise RuntimeError("db down")
        yield  # pragma: no cover

    _patch_chat_turn_env(monkeypatch, VowService(get_db_factory=broken_db))
    with pytest.raises(VowReadError):
        asyncio.run(chat_turn.prepare_send_prompt("conv_fc", MsgCreate(content="hi")))


def test_prepare_send_prompt_eval_mode_skips_vow_injection(monkeypatch):
    @asynccontextmanager
    async def broken_db():
        raise RuntimeError("db down")
        yield  # pragma: no cover

    # eval 是诊断路径：不注入也不因誓约读取失败而阻塞（§5.1 不注入清单）
    _patch_chat_turn_env(monkeypatch, VowService(get_db_factory=broken_db))
    _, history, _ = asyncio.run(
        chat_turn.prepare_send_prompt("conv_eval", MsgCreate(content="hi", memory_eval_mode=True))
    )
    assert all("你们之间已经说定的事" not in m["content"] for m in history)
