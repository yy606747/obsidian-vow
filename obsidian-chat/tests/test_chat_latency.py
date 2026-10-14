import asyncio
from types import SimpleNamespace

import pytest

from app.chat import chat_turn
from app.chat.models import MsgCreate
from app.control import ControlPromptContext
from app.memory_v2 import service as service_module
from app.memory_v3.config import normalize_memory_v3_config


@pytest.fixture
def chat_context(monkeypatch):
    async def history(*_args, **_kwargs):
        messages = [{"id": "current", "role": "user", "content": "还记得那次出门吗"}]
        return SimpleNamespace(
            model_key="fixture", history=messages, actual_recent=list(messages),
            visible_message_ids=["current"], cap_idx=0,
            wb={"user_name": "南枝", "ai_name": "予安"},
        )

    async def empty(*_args, **_kwargs):
        return {}

    async def no_control(*_args, **_kwargs):
        return ControlPromptContext()

    async def no_vows():
        return "", ""

    async def ability(**_kwargs):
        return "【能力】"

    async def working(_history, **kwargs):
        return kwargs["inject_offset"]

    config = normalize_memory_v3_config({"pending_recall_enabled": True, "timeline_enabled": False})
    monkeypatch.setattr(chat_turn, "prepare_chat_history", history)
    monkeypatch.setattr(chat_turn.control_session_service, "get_prompt_context", no_control)
    monkeypatch.setattr(chat_turn.vow_service, "load_vow_prompt_context", no_vows)
    monkeypatch.setattr(chat_turn, "presence_identity_head", empty)
    monkeypatch.setattr(chat_turn, "build_send_ability_block", ability)
    monkeypatch.setattr(chat_turn, "inject_working_model_prompt", working)
    monkeypatch.setattr(chat_turn, "load_ai_behavior", lambda: {"tool_result_feedback_enabled": False})
    monkeypatch.setattr(chat_turn, "load_memory_v3_config", lambda: config)
    monkeypatch.setattr("app.memory_v3.config.load_memory_v3_config", lambda: config)
    monkeypatch.setattr(chat_turn.web_search_service, "prepare_dialogue_turn", empty)
    monkeypatch.setattr(chat_turn.presence_outcome_inbox, "claim_for_turn", empty)
    monkeypatch.setattr(chat_turn.sticker_repository, "claim", empty)
    monkeypatch.setattr("app.look_at_me.service.inject_context", empty)
    monkeypatch.setattr("app.together.recent.inject_recent", empty)
    monkeypatch.setattr("app.scenes.recent.inject_recent", empty)
    monkeypatch.setattr(service_module.recall_config, "load_recall_config", lambda: {"mode": "full"})


def item(key, content, **extra):
    return {"id": key, "candidate_id": key, "content": content, "score": 0.9,
            "source_type": "chunk", **extra}


def test_send_overlaps_both_recalls_and_merges_results_once(monkeypatch, chat_context):
    async def scenario():
        ordinary_started, selector_started = asyncio.Event(), asyncio.Event()
        selected = [item("shared", "旧事片段", prompt_priority=1),
                    item("pending", "另一条回忆", prompt_priority=1)]

        async def ordinary(*_args, **kwargs):
            assert kwargs["query_timeout_sec"] == 4.0
            ordinary_started.set()
            await selector_started.wait()
            return {"selected": [item("shared", "旧事片段"), item("ordinary", "普通检索片段")],
                    "semantic_query": True, "candidate_count": 2}

        async def pending(**_kwargs):
            selector_started.set()
            await ordinary_started.wait()
            return {"status": "selected", "items": selected}

        monkeypatch.setattr(service_module.hybrid_recall, "hybrid_recall", ordinary)
        monkeypatch.setattr(chat_turn.pending_recall_service, "prepare_for_user", pending)
        _, history, meta = await asyncio.wait_for(chat_turn.prepare_send_prompt(
            "conv", MsgCreate(content="还记得那次出门吗"), current_user_message_id="current",
        ), timeout=1)
        prompt = "\n".join(m["content"] for m in history)
        assert prompt.count("旧事片段") == 1
        assert "另一条回忆" in prompt and "普通检索片段" in prompt
        assert meta["pending_recall"]["items"] == selected

    asyncio.run(scenario())


@pytest.mark.parametrize("cancel", [False, True])
def test_failed_or_cancelled_preparation_drains_pending_task(monkeypatch, chat_context, cancel):
    async def scenario():
        started, stopped, preparing = asyncio.Event(), asyncio.Event(), asyncio.Event()
        tasks = []

        async def pending(**_kwargs):
            tasks.append(asyncio.current_task())
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                stopped.set()

        async def prepare(*_args, **_kwargs):
            await started.wait()
            preparing.set()
            if cancel:
                await asyncio.Event().wait()
            raise RuntimeError("fixture preparation failed")

        monkeypatch.setattr(chat_turn.pending_recall_service, "prepare_for_user", pending)
        monkeypatch.setattr(chat_turn, "inject_memory_prompt", prepare)
        task = asyncio.create_task(chat_turn.prepare_send_prompt(
            "conv", MsgCreate(content="hello"), current_user_message_id="current",
        ))
        await asyncio.wait_for(preparing.wait(), timeout=1)
        if cancel:
            task.cancel()
        with pytest.raises(asyncio.CancelledError if cancel else RuntimeError):
            await task
        assert stopped.is_set() and all(t.done() for t in tasks)

    asyncio.run(scenario())
