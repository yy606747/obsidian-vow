import asyncio
from contextlib import asynccontextmanager
import sqlite3
import time
from types import SimpleNamespace

from app.control import ControlPromptContext
from app.chat import chat_turn, history as history_mod, memory_prompt, prompt_builder
from app.chat.models import MsgCreate
from app.memory_v2 import digest as digest_mod
from app.memory_v3.config import normalize_memory_v3_config


def _patch_prompt_builder_context(monkeypatch):
    async def fake_get_active_schedules():
        return []

    async def fake_self_wake_context(_conv_id):
        return {}

    monkeypatch.setattr(prompt_builder, "get_active_schedules", fake_get_active_schedules)
    monkeypatch.setattr(prompt_builder, "build_schedule_prompt", lambda _schedules: "（无）")
    monkeypatch.setattr(prompt_builder, "_self_wake_prompt_context", fake_self_wake_context)
    monkeypatch.setattr(
        prompt_builder,
        "_build_context_delivery_block",
        lambda _user_name: "",
    )


def _patch_no_control_context(monkeypatch):
    class FakeControlSessionService:
        async def get_prompt_context(self, _conv_id, _payload):
            return ControlPromptContext()

    monkeypatch.setattr(chat_turn, "control_session_service", FakeControlSessionService())


def _patch_empty_vow_context(monkeypatch):
    """誓约层为空：prepare_* 不注入 vow block，保持本文件既有断言不变。"""

    class FakeVowService:
        async def load_vow_prompt_context(self):
            return "", ""

    monkeypatch.setattr(chat_turn, "vow_service", FakeVowService())


def _init_chat_history_db(path):
    conn = sqlite3.connect(path)
    try:
        conn.execute("CREATE TABLE conversations (id TEXT PRIMARY KEY, title TEXT, model TEXT, created_at REAL, updated_at REAL)")
        conn.execute("CREATE TABLE messages (id TEXT PRIMARY KEY, conv_id TEXT, role TEXT, content TEXT, created_at REAL, attachments TEXT)")
        conn.commit()
    finally:
        conn.close()


class _AsyncCursor:
    def __init__(self, cursor):
        self._cursor = cursor

    async def fetchone(self):
        return self._cursor.fetchone()

    async def fetchall(self):
        return self._cursor.fetchall()


class _AsyncConn:
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

    async def commit(self):
        self._conn.commit()


def _patch_history_db(monkeypatch, db_path):
    @asynccontextmanager
    async def fake_get_db():
        async with _AsyncConn(db_path) as db:
            yield db

    monkeypatch.setattr(history_mod, "get_db", fake_get_db)


def test_send_ability_block_filters_toy_by_capability(monkeypatch):
    _patch_prompt_builder_context(monkeypatch)

    body = MsgCreate(content="hi", whisper_mode=True)
    block = asyncio.run(prompt_builder.build_send_ability_block(
        conv_id="conv_prompt",
        body=body,
        user_name="用户A",
        capabilities=("music.search", "memory.remember"),
    ))

    assert "[TOY:" not in block
    assert "没有控制任何玩具或设备的能力" in block
    assert "[MUSIC:" in block
    assert "[REMEMBER:" in block


def test_send_and_regenerate_include_the_same_context_delivery_runtime_block(monkeypatch):
    _patch_prompt_builder_context(monkeypatch)
    context_block = "[设备与环境上下文]\n直接观测：\n- 10:00 手机报告屏幕亮起。"
    monkeypatch.setattr(
        prompt_builder,
        "_build_context_delivery_block",
        lambda _user_name: context_block,
    )

    send = asyncio.run(prompt_builder.build_send_ability_block(
        conv_id="conv-context",
        body=MsgCreate(content="hi"),
        user_name="用户A",
        capabilities=(),
    ))
    regenerate = asyncio.run(prompt_builder.build_regenerate_ability_block(
        conv_id="conv-context",
        user_name="用户A",
        ai_dom_mode=False,
        safeword="",
        dom_history="",
        cnc_enabled=False,
        cnc_weakness="",
        resist_hits=0,
        short_streak=0,
        reply_delay_ms=0,
        compliance_streak=0,
        session_elapsed=0,
        scene_name="",
        scene_elapsed=0,
        since_last_punish=None,
        ratchet_valley=0,
        debt=0.0,
        stubborn_streak=0,
        whisper_mode=False,
        capabilities=(),
    ))

    assert context_block in send.dynamic_block
    assert context_block in regenerate.dynamic_block
    assert context_block not in send.stable_block
    assert context_block not in regenerate.stable_block
    assert "设备直接观测只能证明设备报告了对应状态" in send.stable_block
    assert "不能单独证明她在宿舍、教室、是否上课" in send.stable_block
    assert "数据缺席只表示系统不知道" in regenerate.stable_block


def test_shared_context_replaces_the_legacy_location_sentence(monkeypatch):
    import location as location_module

    async def fake_get_active_schedules():
        return []

    monkeypatch.setattr(prompt_builder, "get_active_schedules", fake_get_active_schedules)
    monkeypatch.setattr(prompt_builder, "build_schedule_prompt", lambda _items: "（无）")
    monkeypatch.setattr(location_module, "load_location_config", lambda: {"enabled": True})
    monkeypatch.setattr(
        location_module,
        "format_location_for_prompt",
        lambda: "用户当前大概在家，定位精度约 30m。",
    )

    monkeypatch.setattr(
        prompt_builder,
        "load_ai_behavior",
        lambda: {"context_delivery_chat_enabled": True},
    )
    shared = asyncio.run(prompt_builder._build_schedule_and_location_block())
    assert "【当前日程列表】" in shared
    assert "用户当前大概在家" not in shared
    assert "【位置信息】" not in shared

    monkeypatch.setattr(
        prompt_builder,
        "load_ai_behavior",
        lambda: {"context_delivery_chat_enabled": False},
    )
    rollback = asyncio.run(prompt_builder._build_schedule_and_location_block())
    assert rollback.count("用户当前大概在家") == 1


def test_context_delivery_switch_off_skips_runtime_reader(monkeypatch):
    import context_delivery_runtime_readers as runtime_readers

    monkeypatch.setattr(
        prompt_builder,
        "load_ai_behavior",
        lambda: {"context_delivery_chat_enabled": False},
    )
    monkeypatch.setattr(
        runtime_readers,
        "render_current_context_delivery_async",
        lambda **_kwargs: (_ for _ in ()).throw(AssertionError("reader must stay off")),
    )

    assert asyncio.run(prompt_builder._build_context_delivery_block(
        "阿玖",
        conv_id="conv",
    )) == ""


def test_context_delivery_switch_on_uses_shared_runtime_renderer(monkeypatch):
    import context_delivery_runtime_readers as runtime_readers

    async def render(**kwargs):
        return (
            "[设备与环境上下文]\n直接观测：\n"
            f"- {kwargs['user_name']}的新鲜事实。"
        )

    monkeypatch.setattr(
        prompt_builder,
        "load_ai_behavior",
        lambda: {"context_delivery_chat_enabled": True},
    )
    monkeypatch.setattr(
        runtime_readers,
        "render_current_context_delivery_async",
        render,
    )

    block = asyncio.run(prompt_builder._build_context_delivery_block(
        "阿玖",
        conv_id="conv",
    ))
    assert block.startswith("[设备与环境上下文]")
    assert "阿玖的新鲜事实" in block


def test_send_ability_block_exposes_schedule_list_and_hides_unavailable_runtime_tools(monkeypatch):
    import location as location_module

    _patch_prompt_builder_context(monkeypatch)
    monkeypatch.setattr(prompt_builder, "is_activity_tracking_enabled", lambda: False)
    monkeypatch.setattr(prompt_builder, "is_screen_capture_enabled", lambda: False)
    monkeypatch.setattr(prompt_builder.mobile_screen_service, "is_enabled", lambda: False)
    monkeypatch.setattr(location_module, "load_location_config", lambda: {"enabled": False})

    block = asyncio.run(prompt_builder.build_send_ability_block(
        conv_id="conv_prompt",
        body=MsgCreate(content="hi"),
        user_name="用户A",
        capabilities=(
            "schedule.list",
            "activity.summary",
            "pc.screen_check",
            "mobile.screen_check",
            "location.poi_search",
        ),
        model_key="vision-model",
    ))

    assert "[SCHEDULE_LIST]" in block
    assert "[查看动态:" not in block
    assert "[SCREEN_CHECK:" not in block
    assert "[MOBILE_SCREEN_CHECK:" not in block
    assert "[POI_SEARCH:" not in block
    assert block.advertised_tools == ("schedule.list",)


def test_send_ability_block_includes_toy_when_mode_capability_allows(monkeypatch):
    _patch_prompt_builder_context(monkeypatch)

    body = MsgCreate(content="hi", whisper_mode=True)
    block = asyncio.run(prompt_builder.build_send_ability_block(
        conv_id="conv_prompt",
        body=body,
        user_name="用户A",
        capabilities=("device.toy",),
    ))

    assert "[TOY:1]" in block
    assert "没有控制任何玩具或设备的能力" not in block
    assert "[MUSIC:" not in block
    assert "[REMEMBER:" not in block


def test_send_ability_block_aftercare_suppresses_control_prompt_even_with_device_capability(monkeypatch):
    _patch_prompt_builder_context(monkeypatch)

    body = MsgCreate(content="safe", ai_dom_mode=True)
    block = asyncio.run(prompt_builder.build_send_ability_block(
        conv_id="conv_prompt",
        body=body,
        user_name="用户A",
        capabilities=("device.toy", "memory.remember"),
        control_context=ControlPromptContext(
            session_id="ctrl_safe",
            kind="dom",
            active=False,
            source="safety_tombstone",
            owner_client_id="tab_safe",
            control_epoch=2,
            aftercare_active=True,
            safety_close_reason="safeword",
        ),
    ))

    assert "没有控制任何玩具或设备的能力" in block
    assert "【安全停止后的照护】" in block
    assert "不要继续控制剧情" in block
    assert "也不要下任何玩具或设备指令" in block
    assert "【身份】你爱用户A" not in block
    assert "每条回复恰好一个玩具指令" not in block


def test_send_ability_block_puts_dom_toy_contract_at_end(monkeypatch):
    _patch_prompt_builder_context(monkeypatch)

    body = MsgCreate(content="continue", ai_dom_mode=True, safeword="红灯")
    block = asyncio.run(prompt_builder.build_send_ability_block(
        conv_id="conv_prompt",
        body=body,
        user_name="用户A",
        capabilities=("device.toy",),
        control_context=ControlPromptContext(
            session_id="ctrl_dom",
            kind="dom",
            active=True,
            source="control_session",
            owner_client_id="tab_dom",
            control_epoch=0,
        ),
    ))

    assert "【主控输出硬约束】" in block
    assert "每一条可见回复必须恰好包含一个 [TOY:...] 动作标记" in block
    assert "格式必须使用半角英文方括号和半角冒号" in block
    assert block.rindex("【主控输出硬约束】") > block.rindex("<meta>标签内为消息元数据")


def test_send_ability_block_puts_tide_contract_at_end(monkeypatch):
    _patch_prompt_builder_context(monkeypatch)

    body = MsgCreate(content="continue")
    block = asyncio.run(prompt_builder.build_send_ability_block(
        conv_id="conv_prompt",
        body=body,
        user_name="用户A",
        capabilities=("device.toy",),
        control_context=ControlPromptContext(
            session_id="ctrl_tide",
            kind="tide",
            active=True,
            source="control_session",
            owner_client_id="tab_tide",
            control_resource_id="toy:muse",
        ),
    ))

    assert "【潮汐触碰】" in block
    assert "没有新的触碰想法时可以不写" not in block
    assert "【潮汐输出硬约束】" in block
    assert "每一条可见回复必须包含一个 [TIDE_INTENT:...[/TIDE_INTENT] 隐藏意图" in block
    assert "【主控输出硬约束】" not in block
    assert block.rindex("【潮汐输出硬约束】") > block.rindex("<meta>标签内为消息元数据")


def test_send_ability_block_aftercare_suppresses_tide_capability(monkeypatch):
    _patch_prompt_builder_context(monkeypatch)

    body = MsgCreate(content="safe")
    block = asyncio.run(prompt_builder.build_send_ability_block(
        conv_id="conv_prompt",
        body=body,
        user_name="用户A",
        capabilities=("device.toy",),
        control_context=ControlPromptContext(
            session_id="ctrl_tide_safe",
            kind="tide",
            active=False,
            source="safety_tombstone",
            owner_client_id="tab_tide",
            aftercare_active=True,
            safety_close_reason="safeword",
        ),
    ))

    assert "【安全停止后的照护】" in block
    assert "【潮汐触碰】" not in block
    assert "【潮汐输出硬约束】" not in block
    assert "[TIDE_INTENT:" not in block


def test_memory_prompt_fast_mode_only_injects_time_block(monkeypatch):
    calls = []

    async def fail_if_called(*_args, **_kwargs):
        calls.append("called")
        raise AssertionError("memory service should not run in fast_mode")

    monkeypatch.setattr(memory_prompt.memory_service, "local_instant_digest", fail_if_called)

    history = [{"role": "user", "content": "最近怎么样", "attachments": []}]
    offset, meta = asyncio.run(memory_prompt.inject_memory_prompt(
        history,
        conv_id="conv_test",
        cap_idx=0,
        inject_offset=0,
        actual_recent=history[-1:],
        fast_mode=True,
        whisper_mode=False,
        ai_dom_mode=False,
        prompt_source="send",
        current_user_content="最近怎么样",
    ))

    assert calls == []
    assert offset == 2
    assert history[0]["content"].startswith("系统当前的准确时间是 ")
    assert history[1]["content"] == "（嗯，我知道现在是什么时候。）"
    assert meta == {
        "recall_keywords": "",
        "recall_query": "",
        "recall_topic": "",
        "is_search_needed": False,
        "recalled_memories": [],
        "debug_top6": [],
        "memory_v2_recall": None,
    }


def test_memory_prompt_normal_mode_injects_legacy_and_v2_blocks(monkeypatch):
    def fake_digest(_actual_recent):
        return {
            "keywords": ["记忆库", "V2"],
            "topic": "记忆库 V2",
        }

    async def fake_surfacing(topic, keywords):
        assert topic == "记忆库 V2"
        assert keywords == ["记忆库", "V2"]
        return (
            [{"id": "mem_bg", "content": "背景事项", "created_at": 0, "unresolved": False}],
            {"mem_bg"},
        )

    async def fake_recall(query, query_keywords=None):
        assert "记忆库 V2" in query
        assert query_keywords == ["记忆库", "V2"]
        return [], [
            {"id": "mem_bg", "content": "背景事项", "type": "note", "score": 0.95, "created_at": 0},
            {"id": "mem_related", "content": "相关事项", "type": "note", "score": 0.9, "created_at": 0},
            {"id": "mem_low", "content": "低分事项", "type": "note", "score": 0.2, "created_at": 0},
        ]

    async def fail_fetch_source_details(*_args, **_kwargs):
        raise AssertionError("memory prompt should not fetch legacy source details")

    async def fake_v2_debug(recall_query, recall_keywords, *, whisper_mode, ai_dom_mode, prompt_seed):
        assert recall_query
        assert recall_keywords == ["记忆库", "V2"]
        assert whisper_mode is False
        assert ai_dom_mode is False
        assert prompt_seed.startswith("conv_test:send:")
        return {
            "prompt_block": {"enabled": True, "content": "[V2记忆]\nV2 事项"},
            "prompt_decision": {"inject": True, "reason": "test"},
        }

    monkeypatch.setattr(memory_prompt.memory_service, "local_instant_digest", fake_digest)
    monkeypatch.setattr(memory_prompt.memory_service, "build_surfacing_memories", fake_surfacing)
    monkeypatch.setattr(memory_prompt.memory_service, "recall_memories", fake_recall)
    monkeypatch.setattr(memory_prompt.memory_service, "fetch_source_details", fail_fetch_source_details)
    monkeypatch.setattr(memory_prompt, "_plan_v2_recall_debug", fake_v2_debug)

    history = [
        {"role": "user", "content": "我们继续记忆库 V2", "attachments": []},
        {"role": "assistant", "content": "刚才说到回放验收", "attachments": []},
    ]
    offset, meta = asyncio.run(memory_prompt.inject_memory_prompt(
        history,
        conv_id="conv_test",
        cap_idx=0,
        inject_offset=0,
        actual_recent=history[-2:],
        fast_mode=False,
        whisper_mode=False,
        ai_dom_mode=False,
        prompt_source="send",
        current_user_content="我们继续记忆库 V2",
    ))

    prompt_text = "\n".join(message["content"] for message in history[:6])
    assert offset == 6
    assert "[背景记忆]" in prompt_text
    assert "背景事项" in prompt_text
    assert "[相关记忆]" in prompt_text
    assert "相关事项" in prompt_text
    assert "低分事项" not in prompt_text
    assert "[V2记忆]" in prompt_text
    assert meta["recall_keywords"] == "记忆库、V2"
    assert meta["recall_topic"] == "记忆库 V2"
    assert meta["is_search_needed"] is False
    assert [memory["content"] for memory in meta["recalled_memories"]] == ["相关事项"]
    assert [memory["content"] for memory in meta["debug_top6"]] == ["背景事项", "相关事项", "低分事项"]
    assert meta["memory_v2_recall"]["prompt_decision"]["inject"] is True


def test_memory_prompt_full_v2_takeover_skips_legacy_memory_blocks(monkeypatch):
    def fake_digest(_actual_recent):
        return {
            "keywords": ["记忆库", "V2"],
            "topic": "记忆库 V2",
        }

    async def fail_legacy_surfacing(*_args, **_kwargs):
        raise AssertionError("full V2 takeover should not build legacy surfacing memories")

    async def fail_legacy_recall(*_args, **_kwargs):
        raise AssertionError("full V2 takeover should not call legacy recall")

    async def fail_legacy_details(*_args, **_kwargs):
        raise AssertionError("full V2 takeover should not fetch legacy source details")

    async def fake_v2_debug(recall_query, recall_keywords, *, whisper_mode, ai_dom_mode, prompt_seed):
        assert "记忆库 V2" in recall_query
        assert recall_keywords == ["记忆库", "V2"]
        return {
            "runtime": {"v2_enabled": True, "mode": "full"},
            "rollout_decision": {"inject": True, "reason": "full", "mode": "full"},
            "prompt_decision": {"inject": True, "reason": "full", "mode": "full"},
            "prompt_block": {"enabled": True, "content": "[你现在想到的]\nV2 事项"},
        }

    monkeypatch.setattr(memory_prompt.memory_service, "local_instant_digest", fake_digest)
    monkeypatch.setattr(memory_prompt.memory_service, "build_surfacing_memories", fail_legacy_surfacing)
    monkeypatch.setattr(memory_prompt.memory_service, "recall_memories", fail_legacy_recall)
    monkeypatch.setattr(memory_prompt.memory_service, "fetch_source_details", fail_legacy_details)
    monkeypatch.setattr(memory_prompt, "_plan_v2_recall_debug", fake_v2_debug)

    history = [
        {"role": "user", "content": "我们继续记忆库 V2", "attachments": []},
        {"role": "assistant", "content": "刚才说到回放验收", "attachments": []},
    ]
    offset, meta = asyncio.run(memory_prompt.inject_memory_prompt(
        history,
        conv_id="conv_full",
        cap_idx=0,
        inject_offset=0,
        actual_recent=history[-2:],
        fast_mode=False,
        whisper_mode=False,
        ai_dom_mode=False,
        prompt_source="send",
        current_user_content="我们继续记忆库 V2",
    ))

    prompt_text = "\n".join(message["content"] for message in history[:4])
    assert offset == 2
    assert "[你现在想到的]" in prompt_text
    assert "V2 事项" in prompt_text
    assert "[背景记忆]" not in prompt_text
    assert "[相关记忆]" not in prompt_text
    assert meta["recalled_memories"] == []
    assert meta["debug_top6"] == []
    assert meta["memory_v2_recall"]["rollout_decision"]["reason"] == "full"


def test_prepare_chat_history_selects_previous_conv_by_last_user_message(monkeypatch, tmp_path):
    db_path = tmp_path / "chat_history.db"
    _init_chat_history_db(db_path)
    _patch_history_db(monkeypatch, db_path)
    now = time.time()

    conn = sqlite3.connect(db_path)
    try:
        # conv_prev: 用户最后一句很早；conv_bg: 半夜被后台任务追加了 assistant，updated 更新，但没有用户消息
        conn.execute("INSERT INTO conversations VALUES (?,?,?,?,?)", ("conv_prev", "昨天的对话", "model_a", now - 600.0, now - 300.0))
        conn.execute("INSERT INTO conversations VALUES (?,?,?,?,?)", ("conv_bg", "后台对话", "model_a", now - 600.0, now - 10.0))
        conn.execute("INSERT INTO conversations VALUES (?,?,?,?,?)", ("conv_new", "今天的对话", "model_b", now - 50.0, now))
        conn.executemany(
            "INSERT INTO messages VALUES (?,?,?,?,?,?)",
            [
                ("p1", "conv_prev", "user", "昨天说到记忆库尾巴", now - 300.0, "[]"),
                ("p2", "conv_prev", "assistant", "我说可以做窄实现", now - 298.0, "[]"),
                ("bg1", "conv_bg", "assistant", "半夜哨兵自动发的", now - 10.0, "[]"),
                ("n1", "conv_new", "user", "今天继续", now - 1.0, "[]"),
            ],
        )
        conn.commit()
    finally:
        conn.close()

    ctx = asyncio.run(history_mod.prepare_chat_history(
        "conv_new",
        context_limit=30,
        attachment_policy="last_message",
    ))

    assert ctx.model_key == "model_b"
    # 选的是有用户消息、用户最后说话最近的那个，而不是 updated_at 最新的纯后台对话
    assert ctx.previous_conversation_id == "conv_prev"
    assert ctx.previous_conversation_source["conv_id"] == "conv_prev"
    assert ctx.previous_conversation_source["title"] == "昨天的对话"
    assert ctx.visible_message_ids == ["n1"]
    assert all("id" not in message for message in ctx.history)


def test_prepare_chat_history_skips_previous_tail_after_current_chat_has_started(monkeypatch, tmp_path):
    db_path = tmp_path / "chat_history_started.db"
    _init_chat_history_db(db_path)
    _patch_history_db(monkeypatch, db_path)
    now = time.time()

    conn = sqlite3.connect(db_path)
    try:
        conn.execute("INSERT INTO conversations VALUES (?,?,?,?,?)", ("conv_prev", "昨天", "model_a", now - 600.0, now - 100.0))
        conn.execute("INSERT INTO conversations VALUES (?,?,?,?,?)", ("conv_new", "今天", "model_b", now - 50.0, now))
        conn.executemany(
            "INSERT INTO messages VALUES (?,?,?,?,?,?)",
            [
                ("p1", "conv_prev", "user", "旧上下文", now - 300.0, "[]"),
                ("n1", "conv_new", "user", "第一条", now - 10.0, "[]"),
                ("n2", "conv_new", "assistant", "第一答", now - 9.0, "[]"),
                ("n3", "conv_new", "user", "第二条", now - 8.0, "[]"),
            ],
        )
        conn.commit()
    finally:
        conn.close()

    ctx = asyncio.run(history_mod.prepare_chat_history(
        "conv_new",
        context_limit=30,
        attachment_policy="last_message",
    ))

    assert ctx.previous_conversation_id is None
    assert ctx.previous_conversation_source is None


def test_prepare_send_prompt_builds_ability_memory_and_debug_meta(monkeypatch):
    _patch_empty_vow_context(monkeypatch)
    _patch_no_control_context(monkeypatch)

    async def fake_prepare_chat_history(conv_id, *, context_limit, attachment_policy, retracted=False):
        assert conv_id == "conv_send"
        assert context_limit == 12
        assert attachment_policy == "last_message"
        assert retracted is True
        history = [{"role": "user", "content": "hello", "attachments": []}]
        return SimpleNamespace(
            model_key="model_send",
            history=history,
            actual_recent=history[-1:],
            wb={"user_name": "用户A"},
            cap_idx=0,
        )

    async def fake_build_send_ability_block(*, conv_id, body, user_name, capabilities, control_context, model_key):
        assert conv_id == "conv_send"
        assert body.content == "hello"
        assert user_name == "用户A"
        assert "device.toy" not in capabilities
        assert control_context.source == "none"
        assert model_key == "model_send"
        return "[系统能力] send"

    async def fake_inject_memory_prompt(history, **kwargs):
        assert kwargs["conv_id"] == "conv_send"
        assert kwargs["cap_idx"] == 0
        assert kwargs["inject_offset"] == 2
        assert kwargs["fast_mode"] is False
        assert kwargs["prompt_source"] == "send"
        assert kwargs["current_user_content"] == "hello"
        history.insert(2, {"role": "user", "content": "[背景记忆] test"})
        history.insert(3, {"role": "assistant", "content": "收到"})
        return 4, {
            "recall_keywords": "kw",
            "recall_query": "query",
            "recall_topic": "topic",
            "is_search_needed": True,
            "recalled_memories": [],
            "debug_top6": [],
            "memory_v2_recall": {"ok": True},
        }

    monkeypatch.setattr(chat_turn, "prepare_chat_history", fake_prepare_chat_history)
    monkeypatch.setattr(chat_turn, "build_send_ability_block", fake_build_send_ability_block)
    monkeypatch.setattr(chat_turn, "inject_memory_prompt", fake_inject_memory_prompt)

    body = MsgCreate(content="hello", context_limit=12, retracted=True)
    model_key, history, meta = asyncio.run(chat_turn.prepare_send_prompt("conv_send", body))

    assert model_key == "model_send"
    assert history[0]["content"] == "[系统能力] send"
    assert history[1]["content"] == "（我知道自己现在能做什么。）"
    assert history[2]["content"] == "[背景记忆] test"
    assert meta["memory_v2_recall"] == {"ok": True}
    assert meta["chat_mode"] == "normal"
    assert "device.toy" not in meta["capabilities"]
    assert meta["prompt_count"] == len(history)
    assert meta["prompt_messages"][0]["content"] == "[系统能力] send"


def test_prepare_send_prompt_puts_web_search_in_runtime_tail(monkeypatch):
    _patch_empty_vow_context(monkeypatch)
    _patch_no_control_context(monkeypatch)

    async def fake_history(*_args, **_kwargs):
        history = [{"role": "user", "content": "hello", "attachments": []}]
        return SimpleNamespace(
            model_key="model_send",
            history=history,
            actual_recent=history,
            wb={"user_name": "用户A"},
            cap_idx=0,
            previous_turn_assistant_ids=(),
            visible_message_ids=[],
        )

    class WebSearch:
        async def prepare_dialogue_turn(self, **kwargs):
            assert kwargs["bound_turn_id"] == "send:user-current"
            return {
                "status": "bound",
                "bound_turn_id": kwargs["bound_turn_id"],
                "ids": ["web-1"],
                "block": "PRIVATE_READY_WEB_RESULT",
            }

    class EmptyPresenceOutcomes:
        async def claim_for_turn(self, **_kwargs):
            return {
                "status": "empty",
                "bound_turn_id": "send:user-current",
                "outcome_ids": [],
                "block": "",
            }

    async def ability(**_kwargs):
        return "STABLE_ABILITY"

    async def inject_working(history, **kwargs):
        return kwargs["inject_offset"]

    async def inject_memory(history, **kwargs):
        return kwargs["inject_offset"], {
            "memory_v2_recall": None,
            "recalled_memories": [],
            "debug_top6": [],
        }

    monkeypatch.setattr(chat_turn, "prepare_chat_history", fake_history)
    monkeypatch.setattr(chat_turn, "build_send_ability_block", ability)
    monkeypatch.setattr(chat_turn, "inject_working_model_prompt", inject_working)
    monkeypatch.setattr(chat_turn, "inject_memory_prompt", inject_memory)
    monkeypatch.setattr(chat_turn, "web_search_service", WebSearch())
    monkeypatch.setattr(chat_turn, "presence_outcome_inbox", EmptyPresenceOutcomes())
    monkeypatch.setattr(
        chat_turn,
        "load_ai_behavior",
        lambda: {"tool_result_feedback_enabled": False},
    )
    monkeypatch.setattr(
        chat_turn,
        "load_memory_v3_config",
        lambda: {"timeline_enabled": False, "pending_recall_enabled": False},
    )

    _model, history, meta = asyncio.run(chat_turn.prepare_send_prompt(
        "conv_send",
        MsgCreate(content="hello"),
        current_user_message_id="user-current",
    ))
    web_index = next(
        index for index, message in enumerate(history)
        if "PRIVATE_READY_WEB_RESULT" in str(message.get("content") or "")
    )
    assert web_index >= meta["cache_layout"]["runtime_insert_index"]
    assert "WEB_SEARCH_INTENT" in history[web_index]["content"]
    assert "WEB_SEARCH_INTENT" not in str(meta["prompt_messages"])
    assert meta["web_search"]["ids"] == ["web-1"]


def test_prepare_send_prompt_injects_normalized_previous_turn_feedback(monkeypatch):
    _patch_empty_vow_context(monkeypatch)
    _patch_no_control_context(monkeypatch)

    async def fake_prepare_chat_history(_conv_id, **_kwargs):
        history = [{"role": "user", "content": "下一轮", "attachments": []}]
        return SimpleNamespace(
            model_key="model_send",
            history=history,
            actual_recent=history[-1:],
            wb={"user_name": "用户A"},
            cap_idx=0,
            previous_turn_assistant_ids=("assistant-prev",),
            previous_conversation_id=None,
            previous_conversation_source=None,
            visible_message_ids=("user-current",),
        )

    async def fake_build_send_ability_block(**_kwargs):
        return "[系统能力] send"

    async def fake_feedback(*, conv_id, assistant_message_ids):
        assert conv_id == "conv_send"
        assert assistant_message_ids == ("assistant-prev",)
        return (
            "[上一轮能力执行结果]\n"
            "以下是执行层的规范化真实结果，不是模型原始标记：\n"
            '{"tool":"device.ring_touch","status":"failed","reason":"timeout"}'
        )

    async def fake_inject_memory_prompt(_history, **kwargs):
        return kwargs["inject_offset"], {}

    monkeypatch.setattr(chat_turn, "prepare_chat_history", fake_prepare_chat_history)
    monkeypatch.setattr(chat_turn, "build_send_ability_block", fake_build_send_ability_block)
    monkeypatch.setattr(chat_turn, "build_previous_turn_feedback", fake_feedback)
    monkeypatch.setattr(chat_turn, "load_ai_behavior", lambda: {"tool_result_feedback_enabled": True})
    monkeypatch.setattr(chat_turn, "inject_memory_prompt", fake_inject_memory_prompt)
    monkeypatch.setattr(
        chat_turn,
        "load_memory_v3_config",
        lambda: normalize_memory_v3_config({
            "timeline_enabled": False,
            "pending_recall_enabled": False,
        }),
    )

    _model_key, history, meta = asyncio.run(chat_turn.prepare_send_prompt(
        "conv_send",
        MsgCreate(content="下一轮", fast_mode=True),
    ))

    assert any(
        str(message.get("content", "")).startswith("[上一轮能力执行结果]")
        for message in history
    )
    assert any(
        message.get("content") == "（嗯，刚才实际执行到了哪里、结果是什么，我心里有数。）"
        for message in history
    )
    assert meta["tool_result_feedback"] == {"enabled": True, "injected": True}


def test_prepare_send_prompt_feedback_switch_off_restores_no_feedback_path(monkeypatch):
    _patch_empty_vow_context(monkeypatch)
    _patch_no_control_context(monkeypatch)

    async def fake_prepare_chat_history(_conv_id, **_kwargs):
        history = [{"role": "user", "content": "下一轮", "attachments": []}]
        return SimpleNamespace(
            model_key="model_send",
            history=history,
            actual_recent=history[-1:],
            wb={"user_name": "用户A"},
            cap_idx=0,
            previous_turn_assistant_ids=("assistant-prev",),
            previous_conversation_id=None,
            previous_conversation_source=None,
            visible_message_ids=("user-current",),
        )

    async def forbidden_feedback(**_kwargs):
        raise AssertionError("disabled feedback path must not read the ledger")

    async def fake_inject_memory_prompt(_history, **kwargs):
        return kwargs["inject_offset"], {}

    monkeypatch.setattr(chat_turn, "prepare_chat_history", fake_prepare_chat_history)
    async def fake_build_send_ability_block(**_kwargs):
        return "[系统能力] send"

    monkeypatch.setattr(chat_turn, "build_send_ability_block", fake_build_send_ability_block)
    monkeypatch.setattr(chat_turn, "build_previous_turn_feedback", forbidden_feedback)
    monkeypatch.setattr(chat_turn, "load_ai_behavior", lambda: {"tool_result_feedback_enabled": False})
    monkeypatch.setattr(chat_turn, "inject_memory_prompt", fake_inject_memory_prompt)
    monkeypatch.setattr(
        chat_turn,
        "load_memory_v3_config",
        lambda: normalize_memory_v3_config({
            "timeline_enabled": False,
            "pending_recall_enabled": False,
        }),
    )

    _model_key, history, meta = asyncio.run(chat_turn.prepare_send_prompt(
        "conv_send",
        MsgCreate(content="下一轮", fast_mode=True),
    ))

    assert not any("上一轮能力执行结果" in str(message.get("content", "")) for message in history)
    assert meta["tool_result_feedback"] == {"enabled": False, "injected": False}


def test_prepare_send_prompt_claims_presence_outcome_only_for_natural_send(monkeypatch):
    _patch_empty_vow_context(monkeypatch)
    _patch_no_control_context(monkeypatch)

    async def fake_history(_conv_id, **_kwargs):
        history = [{"role": "user", "content": "下一轮", "attachments": []}]
        return SimpleNamespace(
            model_key="model_send",
            history=history,
            actual_recent=history,
            wb={"user_name": "用户A"},
            cap_idx=0,
            previous_turn_assistant_ids=(),
            previous_conversation_id=None,
            previous_conversation_source=None,
            visible_message_ids=("user-current",),
        )

    class Outcomes:
        async def claim_for_turn(self, **kwargs):
            assert kwargs == {
                "conv_id": "conv_send",
                "bound_turn_id": "send:user-current",
            }
            return {
                "status": "claimed",
                "bound_turn_id": "send:user-current",
                "outcome_ids": ["event-1:played"],
                "block": "【桌面化身异步结果（仅本轮可见）】\n- 确实播放完成",
            }

    class WebSearch:
        async def prepare_dialogue_turn(self, **_kwargs):
            return {"status": "disabled", "block": "", "ids": []}

    async def ability(**_kwargs):
        return "STABLE_ABILITY"

    async def inject_working(_history, **kwargs):
        return kwargs["inject_offset"]

    async def inject_memory(_history, **kwargs):
        return kwargs["inject_offset"], {}

    monkeypatch.setattr(chat_turn, "prepare_chat_history", fake_history)
    monkeypatch.setattr(chat_turn, "build_send_ability_block", ability)
    monkeypatch.setattr(chat_turn, "inject_working_model_prompt", inject_working)
    monkeypatch.setattr(chat_turn, "inject_memory_prompt", inject_memory)
    monkeypatch.setattr(chat_turn, "presence_outcome_inbox", Outcomes())
    monkeypatch.setattr(chat_turn, "web_search_service", WebSearch())
    monkeypatch.setattr(
        chat_turn, "load_ai_behavior", lambda: {"tool_result_feedback_enabled": False}
    )
    monkeypatch.setattr(
        chat_turn,
        "load_memory_v3_config",
        lambda: normalize_memory_v3_config(
            {"timeline_enabled": False, "pending_recall_enabled": False}
        ),
    )

    _model, history, meta = asyncio.run(
        chat_turn.prepare_send_prompt(
            "conv_send",
            MsgCreate(content="下一轮", fast_mode=True),
            current_user_message_id="user-current",
        )
    )
    assert any(
        "桌面化身异步结果" in str(message.get("content") or "")
        for message in history
    )
    assert meta["presence_outcomes"] == {
        "status": "claimed",
        "bound_turn_id": "send:user-current",
        "outcome_ids": ["event-1:played"],
        "injected": True,
    }


def test_prepare_send_prompt_injects_handoff_note_before_memory(monkeypatch):
    _patch_empty_vow_context(monkeypatch)
    _patch_no_control_context(monkeypatch)

    async def fake_prepare_chat_history(_conv_id, *, context_limit, attachment_policy, retracted=False):
        history = [{"role": "user", "content": "今天继续", "attachments": []}]
        return SimpleNamespace(
            model_key="model_send",
            history=history,
            actual_recent=history[-1:],
            wb={"user_name": "用户A", "ai_name": "Aion"},
            cap_idx=0,
            previous_conversation_id="conv_prev",
            previous_conversation_source={"conv_id": "conv_prev", "title": "昨天的对话", "last_user_at": 11.0},
        )

    async def fake_build_send_ability_block(**_kwargs):
        return "[系统能力] send"

    async def fake_write_handoff_note(conv_id):
        assert conv_id == "conv_prev"
        return "昨晚聊到用户考研压力大，纠结要不要换方向，情绪有点低。"

    async def fake_inject_memory_prompt(history, **kwargs):
        assert kwargs["inject_offset"] == 4
        assert history[2]["content"].startswith("[昨日续点]")
        assert "考研压力大" in history[2]["content"]
        assert "不要提到、复述或解释这条续点" in history[2]["content"]
        history.insert(4, {"role": "user", "content": "[背景记忆] test"})
        history.insert(5, {"role": "assistant", "content": "收到"})
        return 6, {
            "recall_keywords": "",
            "recall_query": "",
            "recall_topic": "",
            "is_search_needed": False,
            "recalled_memories": [],
            "debug_top6": [],
            "memory_v2_recall": None,
        }

    monkeypatch.setattr(chat_turn, "prepare_chat_history", fake_prepare_chat_history)
    monkeypatch.setattr(chat_turn, "build_send_ability_block", fake_build_send_ability_block)
    monkeypatch.setattr(chat_turn, "write_handoff_note", fake_write_handoff_note)
    monkeypatch.setattr(chat_turn, "inject_memory_prompt", fake_inject_memory_prompt)

    body = MsgCreate(content="今天继续")
    _model_key, history, meta = asyncio.run(chat_turn.prepare_send_prompt("conv_send", body))

    assert history[0]["content"] == "[系统能力] send"
    assert history[2]["content"].startswith("[昨日续点]")
    assert history[3]["content"] == "（明白，我记在心里，不会主动提起。）"
    assert meta["handoff_note"]["injected"] is True
    assert meta["handoff_note"]["skip_reason"] is None
    assert "考研压力大" in meta["handoff_note"]["note"]


def test_prepare_send_prompt_timeline_replaces_handoff_before_memory(monkeypatch):
    _patch_empty_vow_context(monkeypatch)
    _patch_no_control_context(monkeypatch)

    async def fake_prepare_chat_history(_conv_id, **_kwargs):
        history = [{"id": "u1", "role": "user", "content": "今天继续", "attachments": []}]
        return SimpleNamespace(
            model_key="model_send",
            history=history,
            actual_recent=history[-1:],
            wb={"user_name": "用户A", "ai_name": "Aion"},
            cap_idx=0,
            previous_conversation_id="conv_prev",
            previous_conversation_source={"conv_id": "conv_prev"},
        )

    async def fake_build_send_ability_block(**_kwargs):
        return "[系统能力] send"

    async def forbidden_handoff(_conv_id):
        raise AssertionError("timeline-enabled path must not call handoff")

    class FakeTimelineService:
        async def prompt_context(self, *, visible_messages, config_snapshot):
            assert visible_messages[0]["id"] == "u1"
            assert config_snapshot["timeline_enabled"] is True
            return {
                "status": "injected",
                "block": "[最近三天的事]（现在 8月11日 星期二 14:20）\n\n昨天（8月10日 星期一）\n· 12:05 聊到考研压力。",
                "entries": [{"index": 0, "text": "聊到考研压力。"}],
            }

    async def fake_inject_memory_prompt(history, **kwargs):
        assert kwargs["inject_offset"] == 4
        assert history[2]["content"].startswith("[最近三天的事]")
        assert not any(str(m.get("content", "")).startswith("[昨日续点]") for m in history)
        return 4, {
            "recall_keywords": "", "recall_query": "", "recall_topic": "",
            "is_search_needed": False, "recalled_memories": [], "debug_top6": [],
            "memory_v2_recall": None,
        }

    monkeypatch.setattr(chat_turn, "prepare_chat_history", fake_prepare_chat_history)
    monkeypatch.setattr(chat_turn, "build_send_ability_block", fake_build_send_ability_block)
    monkeypatch.setattr(chat_turn, "write_handoff_note", forbidden_handoff)
    monkeypatch.setattr(chat_turn, "timeline_service", FakeTimelineService())
    monkeypatch.setattr(chat_turn, "inject_memory_prompt", fake_inject_memory_prompt)
    monkeypatch.setattr(
        chat_turn,
        "load_memory_v3_config",
        lambda: normalize_memory_v3_config({"timeline_enabled": True}),
    )

    body = MsgCreate(content="今天继续")
    _model_key, history, meta = asyncio.run(chat_turn.prepare_send_prompt("conv_send", body))
    assert history[3]["content"] == "（嗯，近几天的事我还记得。）"
    assert meta["timeline"]["status"] == "injected"
    assert meta["handoff_note"]["injected"] is False
    assert meta["handoff_note"]["skip_reason"] == "timeline_enabled"


def test_prepare_send_prompt_fails_open_when_handoff_raises(monkeypatch):
    _patch_empty_vow_context(monkeypatch)
    _patch_no_control_context(monkeypatch)

    async def fake_prepare_chat_history(_conv_id, *, context_limit, attachment_policy, retracted=False):
        history = [{"role": "user", "content": "早安", "attachments": []}]
        return SimpleNamespace(
            model_key="model_send",
            history=history,
            actual_recent=history[-1:],
            wb={"user_name": "用户A", "ai_name": "Aion"},
            cap_idx=0,
            previous_conversation_id="conv_prev",
            previous_conversation_source={"conv_id": "conv_prev", "title": "昨天", "last_user_at": 11.0},
        )

    async def fake_build_send_ability_block(**_kwargs):
        return "[系统能力] send"

    async def boom_write_handoff_note(_conv_id):
        raise RuntimeError("小模型/DB 炸了")

    async def fake_inject_memory_prompt(history, **kwargs):
        # 续点失败被吞掉 → 注入偏移没有被续点推进
        assert kwargs["inject_offset"] == 2
        assert not any(str(m.get("content", "")).startswith("[昨日续点]") for m in history)
        return 2, {
            "recall_keywords": "", "recall_query": "", "recall_topic": "",
            "is_search_needed": False, "recalled_memories": [], "debug_top6": [],
            "memory_v2_recall": None,
        }

    monkeypatch.setattr(chat_turn, "prepare_chat_history", fake_prepare_chat_history)
    monkeypatch.setattr(chat_turn, "build_send_ability_block", fake_build_send_ability_block)
    monkeypatch.setattr(chat_turn, "write_handoff_note", boom_write_handoff_note)
    monkeypatch.setattr(chat_turn, "inject_memory_prompt", fake_inject_memory_prompt)

    body = MsgCreate(content="早安")
    # 不抛异常即为 fail-open 成功
    _model_key, history, meta = asyncio.run(chat_turn.prepare_send_prompt("conv_send", body))
    assert meta["handoff_note"]["injected"] is False
    assert meta["handoff_note"]["skip_reason"] == "exception"
    assert not any(str(m.get("content", "")).startswith("[昨日续点]") for m in history)


def test_prepare_send_prompt_hard_caps_handoff_timeout(monkeypatch):
    _patch_empty_vow_context(monkeypatch)
    _patch_no_control_context(monkeypatch)

    async def fake_prepare_chat_history(_conv_id, *, context_limit, attachment_policy, retracted=False):
        history = [{"role": "user", "content": "早安", "attachments": []}]
        return SimpleNamespace(
            model_key="model_send",
            history=history,
            actual_recent=history[-1:],
            wb={"user_name": "用户A", "ai_name": "Aion"},
            cap_idx=0,
            previous_conversation_id="conv_prev",
            previous_conversation_source={"conv_id": "conv_prev", "title": "昨天", "last_user_at": 11.0},
        )

    async def fake_build_send_ability_block(**_kwargs):
        return "[系统能力] send"

    async def slow_write_handoff_note(_conv_id):
        await asyncio.sleep(1.0)  # 远超硬上限
        return "不该被用到的便签"

    async def fake_inject_memory_prompt(history, **kwargs):
        assert kwargs["inject_offset"] == 2  # 续点超时被跳过，没推进偏移
        return 2, {
            "recall_keywords": "", "recall_query": "", "recall_topic": "",
            "is_search_needed": False, "recalled_memories": [], "debug_top6": [],
            "memory_v2_recall": None,
        }

    monkeypatch.setattr(chat_turn, "prepare_chat_history", fake_prepare_chat_history)
    monkeypatch.setattr(chat_turn, "build_send_ability_block", fake_build_send_ability_block)
    monkeypatch.setattr(chat_turn, "write_handoff_note", slow_write_handoff_note)
    monkeypatch.setattr(chat_turn, "HANDOFF_NOTE_TIMEOUT", 0.05)
    monkeypatch.setattr(chat_turn, "inject_memory_prompt", fake_inject_memory_prompt)

    body = MsgCreate(content="早安")
    _model_key, history, meta = asyncio.run(chat_turn.prepare_send_prompt("conv_send", body))
    assert meta["handoff_note"]["injected"] is False
    assert meta["handoff_note"]["skip_reason"] == "timeout"
    assert not any(str(m.get("content", "")).startswith("[昨日续点]") for m in history)


def _init_handoff_db(db_path):
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            "CREATE TABLE conversations (id TEXT PRIMARY KEY, title TEXT, model TEXT, "
            "created_at REAL, updated_at REAL, handoff_note TEXT, handoff_note_sig TEXT)"
        )
        conn.execute("CREATE TABLE messages (id TEXT PRIMARY KEY, conv_id TEXT, role TEXT, content TEXT, created_at REAL, attachments TEXT)")
        conn.execute("INSERT INTO conversations (id,title,model,created_at,updated_at) VALUES (?,?,?,?,?)", ("conv_prev", "昨天", "m", 1.0, 9.0))
        conn.executemany(
            "INSERT INTO messages VALUES (?,?,?,?,?,?)",
            [
                ("p1", "conv_prev", "user", "我最近压力好大", 2.0, "[]"),
                ("p2", "conv_prev", "assistant", "怎么了，跟我说说", 3.0, "[]"),
                ("p3", "conv_prev", "user", "考研要不要换方向", 4.0, "[]"),
            ],
        )
        conn.commit()
    finally:
        conn.close()


def _patch_handoff_digest(monkeypatch, db_path):
    @asynccontextmanager
    async def fake_get_db():
        async with _AsyncConn(db_path) as db:
            yield db

    monkeypatch.setattr(digest_mod, "get_db", fake_get_db)
    monkeypatch.setattr(digest_mod, "load_worldbook", lambda: {"user_name": "用户A", "ai_name": "Aion"})


def test_write_handoff_note_generates_and_caches(monkeypatch, tmp_path):
    db_path = tmp_path / "handoff.db"
    _init_handoff_db(db_path)
    _patch_handoff_digest(monkeypatch, db_path)

    calls = {"n": 0}

    async def fake_call_flash_lite(prompt, scope="x", timeout=60.0):
        calls["n"] += 1
        assert "考研要不要换方向" in prompt  # 整段都喂进去了
        return {"note": "昨晚聊到考研换方向的纠结，压力较大。"}

    monkeypatch.setattr(digest_mod, "_call_flash_lite", fake_call_flash_lite)

    note1 = asyncio.run(digest_mod.write_handoff_note("conv_prev"))
    assert note1 == "昨晚聊到考研换方向的纠结，压力较大。"
    assert calls["n"] == 1

    # 第二次：窗口内容没变 → 命中缓存，不再调小模型
    note2 = asyncio.run(digest_mod.write_handoff_note("conv_prev"))
    assert note2 == note1
    assert calls["n"] == 1


def test_write_handoff_note_does_not_cache_on_failure(monkeypatch, tmp_path):
    db_path = tmp_path / "handoff_fail.db"
    _init_handoff_db(db_path)
    _patch_handoff_digest(monkeypatch, db_path)

    state = {"fail": True, "calls": 0}

    async def flaky_call(prompt, scope="x", timeout=60.0):
        state["calls"] += 1
        if state["fail"]:
            return None  # 模拟超时/解析失败
        return {"note": "成功的便签"}

    monkeypatch.setattr(digest_mod, "_call_flash_lite", flaky_call)

    # 第一次失败：返回 None 且不写缓存
    assert asyncio.run(digest_mod.write_handoff_note("conv_prev")) is None
    conn = sqlite3.connect(db_path)
    try:
        row = conn.execute("SELECT handoff_note, handoff_note_sig FROM conversations WHERE id='conv_prev'").fetchone()
    finally:
        conn.close()
    assert row == (None, None)

    # 第二次恢复：应当重试而不是被失败结果卡住
    state["fail"] = False
    assert asyncio.run(digest_mod.write_handoff_note("conv_prev")) == "成功的便签"
    assert state["calls"] == 2


def test_write_handoff_note_does_not_cache_malformed_result(monkeypatch, tmp_path):
    db_path = tmp_path / "handoff_malformed.db"
    _init_handoff_db(db_path)
    _patch_handoff_digest(monkeypatch, db_path)

    state = {"bad": True, "calls": 0}

    async def call(prompt, scope="x", timeout=60.0):
        state["calls"] += 1
        if state["bad"]:
            return {"notes": []}  # 结构不对：没有 "note" 键
        return {"note": "正常便签"}

    monkeypatch.setattr(digest_mod, "_call_flash_lite", call)

    # 畸形结构：当失败处理，不写缓存
    assert asyncio.run(digest_mod.write_handoff_note("conv_prev")) is None
    conn = sqlite3.connect(db_path)
    try:
        row = conn.execute("SELECT handoff_note, handoff_note_sig FROM conversations WHERE id='conv_prev'").fetchone()
    finally:
        conn.close()
    assert row == (None, None)

    # 恢复正常结构 → 重试成功
    state["bad"] = False
    assert asyncio.run(digest_mod.write_handoff_note("conv_prev")) == "正常便签"
    assert state["calls"] == 2

    # 但 {"note": ""} 是合法的“无需续点”，应当缓存、不再重试
    state2 = {"calls": 0}

    async def empty_call(prompt, scope="x", timeout=60.0):
        state2["calls"] += 1
        return {"note": ""}

    db2 = tmp_path / "handoff_empty.db"
    _init_handoff_db(db2)
    _patch_handoff_digest(monkeypatch, db2)
    monkeypatch.setattr(digest_mod, "_call_flash_lite", empty_call)
    # "" 表示合法的“无需续点”（区别于失败的 None），且会被缓存
    assert asyncio.run(digest_mod.write_handoff_note("conv_prev")) == ""
    assert asyncio.run(digest_mod.write_handoff_note("conv_prev")) == ""
    assert state2["calls"] == 1  # 命中“空便签”缓存，没有重算


def test_write_handoff_note_rejects_non_string_note(monkeypatch, tmp_path):
    db_path = tmp_path / "handoff_nonstr.db"
    _init_handoff_db(db_path)
    _patch_handoff_digest(monkeypatch, db_path)

    state = {"bad": True, "calls": 0}

    async def call(prompt, scope="x", timeout=60.0):
        state["calls"] += 1
        if state["bad"]:
            return {"note": ["不是字符串"]}  # 类型不对
        return {"note": "好的便签"}

    monkeypatch.setattr(digest_mod, "_call_flash_lite", call)

    # 非字符串 note：不缓存
    assert asyncio.run(digest_mod.write_handoff_note("conv_prev")) is None
    conn = sqlite3.connect(db_path)
    try:
        row = conn.execute("SELECT handoff_note, handoff_note_sig FROM conversations WHERE id='conv_prev'").fetchone()
    finally:
        conn.close()
    assert row == (None, None)

    state["bad"] = False
    assert asyncio.run(digest_mod.write_handoff_note("conv_prev")) == "好的便签"
    assert state["calls"] == 2


def test_write_handoff_note_recomputes_after_edit(monkeypatch, tmp_path):
    db_path = tmp_path / "handoff_edit.db"
    _init_handoff_db(db_path)
    _patch_handoff_digest(monkeypatch, db_path)

    calls = {"n": 0}

    async def fake_call(prompt, scope="x", timeout=60.0):
        calls["n"] += 1
        return {"note": f"便签v{calls['n']}"}

    monkeypatch.setattr(digest_mod, "_call_flash_lite", fake_call)

    assert asyncio.run(digest_mod.write_handoff_note("conv_prev")) == "便签v1"
    assert calls["n"] == 1

    # 编辑一条旧消息内容（id 不变）→ 签名变化 → 重算，旧摘要不残留
    conn = sqlite3.connect(db_path)
    try:
        conn.execute("UPDATE messages SET content='其实是工作压力' WHERE id='p1'")
        conn.commit()
    finally:
        conn.close()

    assert asyncio.run(digest_mod.write_handoff_note("conv_prev")) == "便签v2"
    assert calls["n"] == 2


def test_prepare_send_prompt_disables_legacy_body_mode_by_default(monkeypatch):
    _patch_empty_vow_context(monkeypatch)
    _patch_no_control_context(monkeypatch)

    async def fake_prepare_chat_history(_conv_id, *, context_limit, attachment_policy, retracted=False):
        return SimpleNamespace(
            model_key="model_send",
            history=[{"role": "user", "content": "hello", "attachments": []}],
            actual_recent=[],
            wb={"user_name": "用户A"},
            cap_idx=0,
        )

    async def fake_build_send_ability_block(**_kwargs):
        return "[系统能力] send"

    async def fake_inject_memory_prompt(history, **_kwargs):
        return 2, {
            "recall_keywords": "",
            "recall_query": "",
            "recall_topic": "",
            "is_search_needed": False,
            "recalled_memories": [],
            "debug_top6": [],
            "memory_v2_recall": None,
        }

    monkeypatch.setattr(chat_turn, "prepare_chat_history", fake_prepare_chat_history)
    monkeypatch.setattr(chat_turn, "build_send_ability_block", fake_build_send_ability_block)
    monkeypatch.setattr(chat_turn, "inject_memory_prompt", fake_inject_memory_prompt)

    body = MsgCreate(content="hello", ai_dom_mode=True, whisper_mode=True)
    _model_key, _history, meta = asyncio.run(chat_turn.prepare_send_prompt("conv_send", body))

    assert meta["chat_mode"] == "normal"
    assert meta["mode_source"] == "legacy_disabled"
    assert "device.toy" not in meta["capabilities"]


def test_prepare_send_prompt_safety_tombstone_forces_normal_mode(monkeypatch):
    _patch_empty_vow_context(monkeypatch)
    class FakeControlSessionService:
        async def get_prompt_context(self, _conv_id, _payload):
            return ControlPromptContext(
                session_id="ctrl_safe",
                kind="dom",
                active=False,
                source="safety_tombstone",
                owner_client_id="tab_safe",
                control_epoch=4,
                aftercare_active=True,
                safety_close_reason="safeword",
                safety_closed_at=1234.0,
            )

    async def fake_prepare_chat_history(_conv_id, *, context_limit, attachment_policy, retracted=False):
        return SimpleNamespace(
            model_key="model_send",
            history=[{"role": "user", "content": "safe", "attachments": []}],
            actual_recent=[],
            wb={"user_name": "用户A"},
            cap_idx=0,
        )

    async def fake_build_send_ability_block(**kwargs):
        assert "device.toy" not in kwargs["capabilities"]
        assert kwargs["control_context"].aftercare_active is True
        return "[系统能力] aftercare"

    async def fake_inject_memory_prompt(history, **kwargs):
        assert kwargs["whisper_mode"] is False
        assert kwargs["ai_dom_mode"] is False
        return 2, {
            "recall_keywords": "",
            "recall_query": "",
            "recall_topic": "",
            "is_search_needed": False,
            "recalled_memories": [],
            "debug_top6": [],
            "memory_v2_recall": None,
        }

    monkeypatch.setattr(chat_turn, "control_session_service", FakeControlSessionService())
    monkeypatch.setattr(chat_turn, "prepare_chat_history", fake_prepare_chat_history)
    monkeypatch.setattr(chat_turn, "build_send_ability_block", fake_build_send_ability_block)
    monkeypatch.setattr(chat_turn, "inject_memory_prompt", fake_inject_memory_prompt)

    body = MsgCreate(content="safe", ai_dom_mode=True, whisper_mode=True)
    _model_key, _history, meta = asyncio.run(chat_turn.prepare_send_prompt("conv_send", body))

    assert meta["chat_mode"] == "normal"
    assert meta["mode_source"] == "safety_tombstone"
    assert "device.toy" not in meta["capabilities"]
    assert meta["control_context_source"] == "safety_tombstone"
    assert meta["control_status"] == "aftercare"
    assert meta["aftercare_active"] is True
    assert meta["safety_close_reason"] == "safeword"


def test_prepare_regenerate_prompt_uses_last_user_attachment_policy(monkeypatch):
    _patch_empty_vow_context(monkeypatch)
    _patch_no_control_context(monkeypatch)

    async def fake_prepare_chat_history(conv_id, *, context_limit, attachment_policy, retracted=False):
        assert conv_id == "conv_regen"
        assert context_limit == 20
        assert attachment_policy == "last_user"
        assert retracted is False
        history = [{"role": "user", "content": "regen", "attachments": []}]
        return SimpleNamespace(
            model_key="model_regen",
            history=history,
            actual_recent=history[-1:],
            wb={"user_name": "用户B"},
            cap_idx=0,
        )

    async def fake_build_regenerate_ability_block(**kwargs):
        assert kwargs["conv_id"] == "conv_regen"
        assert kwargs["user_name"] == "用户B"
        assert kwargs["whisper_mode"] is True
        assert kwargs["ai_dom_mode"] is False
        assert kwargs["safeword"] == "safe"
        assert "device.toy" not in kwargs["capabilities"]
        return "[系统能力] regen"

    async def fake_inject_memory_prompt(history, **kwargs):
        assert kwargs["conv_id"] == "conv_regen"
        assert kwargs["inject_offset"] == 2
        assert kwargs["fast_mode"] is True
        assert kwargs["whisper_mode"] is False
        assert kwargs["prompt_source"] == "regenerate"
        assert kwargs.get("current_user_content", "") == ""
        return 2, {
            "recall_keywords": "",
            "recall_query": "",
            "recall_topic": "",
            "is_search_needed": False,
            "recalled_memories": [],
            "debug_top6": [],
            "memory_v2_recall": None,
        }

    monkeypatch.setattr(chat_turn, "prepare_chat_history", fake_prepare_chat_history)
    monkeypatch.setattr(chat_turn, "build_regenerate_ability_block", fake_build_regenerate_ability_block)
    monkeypatch.setattr(chat_turn, "inject_memory_prompt", fake_inject_memory_prompt)

    model_key, history, meta = asyncio.run(chat_turn.prepare_regenerate_prompt(
        "conv_regen",
        context_limit=20,
        whisper_mode=True,
        fast_mode=True,
        ai_dom_mode=False,
        safeword="safe",
        dom_history="",
        cnc_enabled=False,
        cnc_weakness="",
        resist_hits=0,
        short_streak=0,
        reply_delay_ms=0,
        compliance_streak=0,
        session_elapsed=0,
        scene_name="",
        scene_elapsed=0,
        since_last_punish=None,
        ratchet_valley=0,
        debt=0.0,
        stubborn_streak=0,
    ))

    assert model_key == "model_regen"
    assert history[0]["content"] == "[系统能力] regen"
    assert history[1]["content"] == "（我知道自己现在能做什么。）"
    assert meta["prompt_count"] == len(history)
    assert meta["prompt_messages"][0]["content"] == "[系统能力] regen"
    assert meta["chat_mode"] == "normal"
    assert meta["mode_source"] == "legacy_disabled"
    assert "device.toy" not in meta["capabilities"]
