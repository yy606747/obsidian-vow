import asyncio
import json
import sqlite3
from pathlib import Path
import subprocess
import sys

import pytest

import database
from app.background_tasks import _BACKGROUND_TASKS, begin_task_lifecycle, create_tracked_task
from app.chat import basic_actions, streaming
from app.turn_diagnostics import TurnDiagnostics, current_turn, mark_phase, trace_prompt


@pytest.mark.parametrize("with_tool", [False, True])
def test_prepare_model_visible_tool_and_background_share_one_persistent_turn(tmp_path, monkeypatch, with_tool):
    path = tmp_path / "trace.db"
    monkeypatch.setattr(database, "DB_PATH", path)
    asyncio.run(database.init_db())
    with sqlite3.connect(path) as db:
        db.execute("INSERT INTO conversations (id,title,created_at,updated_at) VALUES ('conv','测试',1,1)")

    async def noop(*_args, **_kwargs):
        return None

    async def store_note(notes, _conv_id):
        return len(notes)

    async def provider(_history, _model, usage, _temperature):
        assert current_turn.get() is not None
        usage.update({"prompt_tokens": 50, "completion_tokens": 5,
                      "cache_read_tokens": 30, "cache_write_tokens": 0,
                      "cache_metrics_reported": True, "cache_hit": True})
        create_tracked_task(asyncio.sleep(0.001), name="diagnostic-followup")
        yield "收到"
        if with_tool:
            yield " [REMEMBER:约好去海边]"

    monkeypatch.setattr(streaming, "stream_ai", provider)
    monkeypatch.setattr(streaming, "export_conversation", noop)
    monkeypatch.setattr(streaming.manager, "broadcast", noop)
    monkeypatch.setattr(streaming, "_schedule_chunk_index_update", lambda *_args, **_kwargs: {})
    monkeypatch.setattr(streaming, "_schedule_working_model_pipeline_after_commit", lambda **_kwargs: None)
    monkeypatch.setattr(streaming, "_maybe_auto_digest", noop)
    monkeypatch.setattr(basic_actions, "_store_remember_notes", store_note)

    @trace_prompt("send")
    async def prepare(conv_id):
        trace = current_turn.get()
        assert trace and trace.conv_id == conv_id
        mark_phase("history")
        history = [{"role": "user", "content": "早上好"}]
        mark_phase("history", finished=True)
        mark_phase("retrieval")
        await asyncio.sleep(0)
        mark_phase("retrieval", finished=True)
        return "synthetic-model", history, {
            "prompt_source": "send", "recall_keywords": "", "recall_query": "",
            "recall_topic": "", "is_search_needed": False, "recalled_memories": [],
            "debug_top6": [], "prompt_messages": history, "prompt_count": 1,
            "advertised_tools": ["memory.remember"],
        }

    async def scenario():
        begin_task_lifecycle()
        model, history, meta = await prepare("conv")
        assert current_turn.get() is None
        turn_id = meta["turn_id"]
        response = await streaming.stream_chat_response(
            conv_id="conv", model_key=model, history=history, prompt_meta=meta, temperature=None,
        )
        events = [json.loads(raw[6:].strip()) async for raw in response.body_iterator]
        pending = [task for task in _BACKGROUND_TASKS if not task.done() and task.get_loop() is asyncio.get_running_loop()]
        if pending:
            await asyncio.wait_for(asyncio.gather(*pending), timeout=2)
        return turn_id, events

    turn_id, events = asyncio.run(scenario())
    assert events[0]["turn_id"] == turn_id
    assert events[-1]["turn_id"] == turn_id
    with sqlite3.connect(path) as db:
        db.row_factory = sqlite3.Row
        rows = [dict(row) for row in db.execute("SELECT * FROM tool_invocation_events")]
    assert rows and {row["turn_id"] for row in rows} == {turn_id}
    assert {row["stage"] for row in rows} >= {"model_request", "model_output", "visible_message", "turn", "diagnostic"}
    final = json.loads(next(row["metadata_json"] for row in rows if row["stage"] == "turn"))["diagnostics"]
    assert all(final["timings"][key] is not None for key in (
        "history_ms", "retrieval_ms", "prepare_ms", "model_ms", "first_visible_ms", "tools_ms", "total_ms",
    ))
    assert final["timings"]["first_visible_ms"] >= final["timings"]["prepare_ms"]
    assert final["usage"]["cache_read_tokens"] == 30
    background = [json.loads(row["metadata_json"]) for row in rows if row["stage"] == "diagnostic"]
    assert any(row.get("task_name") == "diagnostic-followup" and row["elapsed_ms"] >= 0 for row in background)
    if with_tool:
        assert any(row["stage"] == "execution" and row["tool_name"] == "memory.remember" for row in rows)
    inspected = subprocess.run(
        [sys.executable, str(Path(__file__).resolve().parents[2] / "scripts/inspect_chat_turn.py"),
         "--db", str(path), "--turn-id", turn_id],
        capture_output=True, text=True, check=True,
    )
    report = json.loads(inspected.stdout)
    assert report["轮次"] == turn_id
    assert report["诊断"]["usage"]["cache_read_tokens"] == 30
    assert "早上好" not in inspected.stdout
    assert "约好去海边" not in inspected.stdout


def test_unreported_cache_and_unreached_phases_stay_unknown():
    trace = TurnDiagnostics("conv", "send")
    snapshot = trace.snapshot({"cache_read_tokens": 0, "cache_metrics_reported": False})
    assert snapshot["usage"]["cache_read_tokens"] is None
    assert snapshot["usage"]["cache_hit"] is None
    assert snapshot["usage"]["prompt_tokens"] is None
    assert all(value is None for value in snapshot["timings"].values())


def test_failed_preparation_is_recorded_without_replacing_original_error(monkeypatch):
    from app.tools.ledger import tool_invocation_ledger
    captured = []

    async def record(context, **kwargs):
        captured.append((context, kwargs))

    monkeypatch.setattr(tool_invocation_ledger, "record_diagnostic", record)

    @trace_prompt("regenerate")
    async def failed(conv_id):
        raise LookupError("合成准备故障")

    with pytest.raises(LookupError, match="合成准备故障"):
        asyncio.run(failed("conv"))
    assert captured[0][0].request_id.startswith("turn_")
    assert captured[0][1]["phase"] == "prepare"
    assert captured[0][1]["metadata"]["error_type"] == "LookupError"
    assert current_turn.get() is None


def test_concurrent_preparations_do_not_mix_turn_ids():
    @trace_prompt("send")
    async def prepare(conv_id):
        before = current_turn.get().turn_id
        await asyncio.sleep(0)
        assert current_turn.get().turn_id == before
        return "model", [], {}

    async def scenario():
        return await asyncio.gather(prepare("same-conv"), prepare("same-conv"))

    first, second = asyncio.run(scenario())
    assert first[2]["turn_id"] != second[2]["turn_id"]
