import asyncio
from types import SimpleNamespace

from app.chat import chat_turn, prompt_builder
from app.chat.models import MsgCreate
from app.chat.prompt_layout import latest_user_index, mark_cache_boundary
from app.control import ControlPromptContext
from app.vows.prompt import build_vow_ability_block
from prompt_cache import CACHE_BOUNDARY_KEY


def test_runtime_blocks_are_inserted_after_fixed_stable_boundary():
    history = [
        {"role": "user", "content": "stable ability"},
        {"role": "assistant", "content": "ability ack"},
        {"role": "user", "content": "older user"},
        {"role": "assistant", "content": "older answer"},
        {"role": "user", "content": "current user"},
    ]
    tail = latest_user_index(history)
    layout = mark_cache_boundary(history, before_index=2, session_id="chat:layout")
    prompt_builder.insert_prompt_ack(
        history,
        cap_idx=tail,
        inject_offset=0,
        content="runtime memory and time",
        ack="runtime ack",
    )

    assert history[0][CACHE_BOUNDARY_KEY] is True
    assert layout["cacheable_prefix_chars"] == len("stable ability")
    assert [message["content"] for message in history[-3:]] == [
        "runtime memory and time",
        "runtime ack",
        "current user",
    ]


def test_fixed_boundary_survives_a_rolling_30_message_history_window():
    stable = [
        {"role": "user", "content": "stable policy"},
        {"role": "assistant", "content": "stable ack"},
    ]
    turns = [
        {
            "role": "user" if index % 2 == 0 else "assistant",
            "content": f"turn-{index}",
        }
        for index in range(32)
    ]
    layouts = []
    for window in (turns[:30], turns[2:]):
        history = [dict(message) for message in stable + window]
        _cap_idx, _offset, layout = chat_turn._start_runtime_tail(
            history,
            conv_id="rolling-window",
            stable_inject_offset=2,
            stable_prefix_end_index=2,
        )
        boundary = next(
            message for message in history if message.get(CACHE_BOUNDARY_KEY)
        )
        assert boundary["content"] == "stable policy"
        layouts.append(layout)

    assert layouts[0]["cacheable_prefix_chars"] == layouts[1]["cacheable_prefix_chars"]
    assert layouts[0]["boundary_message_index"] == layouts[1]["boundary_message_index"]


def test_ability_builder_splits_stable_commands_from_runtime_context(monkeypatch):
    async def fake_runtime_context():
        return "【当前日程列表】\n今天 20:00 散步"

    async def fake_self_wake_context(_conv_id):
        return {}

    monkeypatch.setattr(
        prompt_builder,
        "_build_schedule_and_location_block",
        fake_runtime_context,
    )
    monkeypatch.setattr(
        prompt_builder,
        "_append_runtime_location_capability",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        prompt_builder,
        "_self_wake_prompt_context",
        fake_self_wake_context,
    )
    monkeypatch.setattr(
        prompt_builder,
        "_build_context_delivery_block",
        lambda _user_name: "",
    )

    block = asyncio.run(
        prompt_builder.build_send_ability_block(
            conv_id="conv-cache",
            body=MsgCreate(content="hi"),
            user_name="用户A",
            capabilities=("memory.remember",),
        )
    )
    stable, dynamic = prompt_builder.split_ability_prompt(block)

    assert "[REMEMBER:" in stable
    assert "她此刻明确表达的边界与安全停止" in stable
    assert "召回内容是帮助理解关系连续性的证据" in stable
    assert "当前日程列表" not in stable
    assert "当前日程列表" in dynamic


def test_context_delivery_changes_runtime_tail_without_changing_cache_prefix(monkeypatch):
    async def fake_runtime_context():
        return "【当前日程列表】\n（无）"

    async def fake_self_wake_context(_conv_id):
        return {}

    context = {"block": ""}
    monkeypatch.setattr(
        prompt_builder,
        "_build_schedule_and_location_block",
        fake_runtime_context,
    )
    monkeypatch.setattr(
        prompt_builder,
        "_append_runtime_location_capability",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        prompt_builder,
        "_self_wake_prompt_context",
        fake_self_wake_context,
    )
    monkeypatch.setattr(
        prompt_builder,
        "_build_context_delivery_block",
        lambda _user_name: context["block"],
    )
    monkeypatch.setattr(
        prompt_builder,
        "load_ai_behavior",
        lambda: {"heart_whisper_prompt": "HEART"},
    )

    def build_and_layout():
        ability = asyncio.run(prompt_builder.build_send_ability_block(
            conv_id="conv-context-cache",
            body=MsgCreate(content="current user"),
            user_name="用户A",
            capabilities=(),
        ))
        stable, dynamic = prompt_builder.split_ability_prompt(ability)
        history = [
            {"role": "user", "content": stable},
            {"role": "assistant", "content": "stable ack"},
            {"role": "user", "content": "older user"},
            {"role": "assistant", "content": "older answer"},
            {"role": "user", "content": "current user"},
        ]
        cap_idx, offset, layout = chat_turn._start_runtime_tail(
            history,
            conv_id="conv-context-cache",
            stable_inject_offset=2,
            stable_prefix_end_index=2,
        )
        prompt_builder.insert_prompt_ack(
            history,
            cap_idx=cap_idx,
            inject_offset=offset,
            content=dynamic,
            ack="runtime ack",
        )
        boundary_index = layout["boundary_message_index"]
        cache_prefix = [
            (message["role"], message["content"])
            for message in history[: boundary_index + 1]
        ]
        return stable, dynamic, history, layout, cache_prefix

    without_context = build_and_layout()
    context["block"] = (
        "[设备与环境上下文]\n直接观测：\n- 10:00 手机报告屏幕亮起。"
    )
    with_context = build_and_layout()

    assert without_context[0] == with_context[0]
    assert without_context[1] != with_context[1]
    assert without_context[3] == with_context[3]
    assert without_context[4] == with_context[4]
    assert "设备与环境上下文" not in str(with_context[4])
    assert "设备与环境上下文" in with_context[1]
    assert with_context[2][-1] == {"role": "user", "content": "current user"}


def test_send_prompt_keeps_stable_policy_before_history_and_runtime_at_tail(monkeypatch):
    raw_history = [
        {"role": "user", "content": "older user", "attachments": []},
        {"role": "assistant", "content": "older answer", "attachments": []},
        {"role": "user", "content": "current user", "attachments": []},
    ]

    async def fake_history(*_args, **_kwargs):
        return SimpleNamespace(
            model_key="ChatGPT 5.6 sol",
            history=[dict(message) for message in raw_history],
            actual_recent=[dict(message) for message in raw_history],
            visible_message_ids=[],
            wb={"user_name": "用户A"},
            cap_idx=0,
            previous_conversation_id=None,
            previous_conversation_source=None,
        )

    class FakeVows:
        async def load_vow_prompt_context(self):
            return "STABLE ACTIVE VOW", build_vow_ability_block(remaining_today=2)

    async def fake_control(*_args, **_kwargs):
        return ControlPromptContext()

    async def fake_ability(**_kwargs):
        return prompt_builder.AbilityPrompt(
            "STABLE ABILITY [REMEMBER:一句话]",
            "RUNTIME ABILITY\n\n[设备与环境上下文]\n直接观测：\n- 手机报告屏幕亮起。",
        )

    async def fake_memory(history, **kwargs):
        offset = prompt_builder.insert_prompt_ack(
            history,
            cap_idx=kwargs["cap_idx"],
            inject_offset=kwargs["inject_offset"],
            content="RUNTIME MEMORY AND TIME",
            ack="memory ack",
        )
        return offset, {
            "recall_keywords": "",
            "recall_query": "",
            "recall_topic": "",
            "is_search_needed": False,
            "recalled_memories": [],
            "debug_top6": [],
            "memory_v2_recall": None,
        }

    async def fake_working_model(history, *, cap_idx, inject_offset):
        return prompt_builder.insert_prompt_ack(
            history,
            cap_idx=cap_idx,
            inject_offset=inject_offset,
            content="STABLE WORKING MODEL",
            ack="working model ack",
        )

    monkeypatch.setattr(chat_turn, "prepare_chat_history", fake_history)
    monkeypatch.setattr(chat_turn, "vow_service", FakeVows())
    monkeypatch.setattr(chat_turn, "_control_prompt_context", fake_control)
    monkeypatch.setattr(chat_turn, "build_send_ability_block", fake_ability)
    monkeypatch.setattr(chat_turn, "inject_memory_prompt", fake_memory)
    monkeypatch.setattr(chat_turn, "inject_working_model_prompt", fake_working_model)
    monkeypatch.setattr(
        chat_turn,
        "load_memory_v3_config",
        lambda: {"pending_recall_enabled": False, "timeline_enabled": False},
    )

    _model, history, meta = asyncio.run(
        chat_turn.prepare_send_prompt(
            "conv-layout",
            MsgCreate(content="current user"),
        )
    )
    contents = [message["content"] for message in history]

    assert contents.index("STABLE ACTIVE VOW") < contents.index("older user")
    stable_vow_ability = next(
        content
        for content in contents
        if "STABLE ABILITY [REMEMBER:一句话]" in content
        and "【立约能力纪律】" in content
    )
    runtime_ability = next(
        content for content in contents if "【本轮立约额度】" in content
    )
    assert contents.index(stable_vow_ability) < contents.index("older user")
    assert contents.index("older answer") < contents.index(runtime_ability)
    assert "RUNTIME ABILITY" in runtime_ability
    assert "[设备与环境上下文]" in runtime_ability
    assert "还可以主动立约 2 次" in runtime_ability
    assert contents.index("RUNTIME MEMORY AND TIME") < contents.index("current user")
    assert history[-1]["role"] == "user"
    assert history[-1]["content"] == "current user"
    assert all("设备与环境上下文" not in message["content"] for message in raw_history)
    boundary = next(message for message in history if message.get(CACHE_BOUNDARY_KEY))
    assert boundary["content"] == "STABLE WORKING MODEL"
    assert meta["cache_layout"]["cacheable_prefix_chars"] > len("STABLE ACTIVE VOW")
