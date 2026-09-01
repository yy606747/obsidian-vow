from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
import json
from pathlib import Path
import subprocess
import sys

import aiosqlite
import pytest

import opportunity as opp
from app.chat import prompt_builder
from app.chat.action_executor import execute_postprocessed_actions
from app.chat.models import MsgCreate
from app.chat.postprocess import PostProcessResult, PostProcessor
from app.chat.prompt_builder import build_opportunity_ability_block
from app.chat.turn_profiles import chat_turn_profile, opportunity_turn_profile
from app.tools.ledger import (
    ToolInvocationLedger,
    detect_unparsed_marker_candidates,
    execution_outcome,
)
from app.tools.ledger_schema import init_tool_invocation_ledger_tables
from app.tools.feedback import format_feedback_rows
from app.tools.registry import (
    ToolRegistryError,
    registered_tools_for_surface,
    validate_tool_registry,
    validate_turn_advertisement,
)
from app.tools.schemas import (
    ToolContext,
    ToolDefinition,
    ToolIntent,
    ToolResult,
    ToolStatus,
)
from scripts.tool_invocation_ledger_report import summarize


def _run(coro):
    return asyncio.run(coro)


def _ledger_for_path(db_path, **ledger_kwargs):
    @asynccontextmanager
    async def factory():
        async with aiosqlite.connect(db_path) as db:
            yield db

    async def initialize():
        async with aiosqlite.connect(db_path) as db:
            await init_tool_invocation_ledger_tables(db)
            await db.commit()

    _run(initialize())
    return ToolInvocationLedger(db_factory=factory, **ledger_kwargs)


async def _rows(db_path, sql, params=()):
    async with aiosqlite.connect(db_path) as db:
        cursor = await db.execute(sql, params)
        return await cursor.fetchall()


def _context(turn_id="turn-1", *, capabilities=()):
    return ToolContext(
        conv_id="conv-1",
        msg_id="assistant-1",
        request_id=turn_id,
        model_key="core-model",
        capabilities=tuple(capabilities),
        metadata={"source": "send"},
    )


def test_turn_denominator_is_idempotent_and_needs_no_message_foreign_key(tmp_path):
    db_path = tmp_path / "ledger.db"
    ledger = _ledger_for_path(db_path)
    context = _context()

    _run(
        ledger.record_turn(
            context,
            prompt_source="send",
            advertised_tools=("music.search", "memory.remember"),
            turn_outcome="succeeded",
        )
    )
    _run(
        ledger.record_turn(
            context,
            prompt_source="send",
            advertised_tools=("music.search",),
            turn_outcome="provider_failed",
        )
    )

    rows = _run(
        _rows(
            db_path,
            "SELECT event_key, stage, turn_outcome, assistant_message_id, "
            "advertised_tools_json, mode FROM tool_invocation_events",
        )
    )
    assert len(rows) == 1
    assert rows[0][0:4] == ("turn:turn-1", "turn", "succeeded", "assistant-1")
    assert json.loads(rows[0][4]) == ["memory.remember", "music.search"]
    assert rows[0][5] == "normal"


def test_candidate_detector_separates_parse_failure_not_enabled_and_inert_text():
    candidates = detect_unparsed_marker_candidates(
        "【MUSIC：夜曲】 [MUSIC:晴天] [MUSCI|稻香]",
        enabled_commands={"toy"},
    )
    assert [(item.stage, item.raw_text) for item in candidates] == [
        ("parse_failed", "【MUSIC：夜曲】"),
        ("not_enabled", "[MUSIC:晴天]"),
        ("parse_failed", "[MUSCI|稻香]"),
    ]

    inert = detect_unparsed_marker_candidates(
        "[VOW:一直算数|好 [MUSIC:不执行]] <think>[MUSCI:也不执行]</think>",
        enabled_commands={"music"},
    )
    assert inert == []


def test_device_queued_outcome_is_dispatched_before_generic_rejection():
    result = ToolResult(
        tool_name="device.ring_touch",
        intent_id="ring-1",
        status=ToolStatus.EXECUTED,
        result={"type": "ring_touch", "ok": False, "status": "queued"},
    )

    assert execution_outcome(result) == "dispatched"


def test_postprocess_persists_parsed_ring_and_width_normalized_marker(tmp_path):
    db_path = tmp_path / "postprocess.db"
    ledger = _ledger_for_path(db_path)
    context = _context(
        "turn-postprocess",
        capabilities=("music.search", "device.ring_touch"),
    )
    processor = PostProcessor(ledger=ledger)

    result = _run(
        processor.process(
            "在呢。[MUSIC:夜曲] [RING:轻碰一下] 【MUSIC：晴天】",
            conv_id="conv-1",
            enabled_commands={"music", "ring"},
            tool_context=context,
        )
    )

    assert [intent.tool_name for intent in result.tool_intents] == [
        "music.search",
        "music.search",
    ]
    assert result.ring_touch_descriptions == ["轻碰一下"]

    async def music_search(_intent, _context):
        return {"query": "夜曲", "cards": [{"id": 1}]}

    _run(
        execute_postprocessed_actions(
            result,
            profile=chat_turn_profile("send"),
            context=context,
            only_capabilities=frozenset({"music.search"}),
            adapter_overrides={"music.search": music_search},
            ledger_override=ledger,
        )
    )
    rows = _run(
        _rows(
            db_path,
            "SELECT stage, intent_id, tool_name, raw_text, events_json "
            "FROM tool_invocation_events ORDER BY stage, intent_id",
        )
    )
    by_stage = {(row[0], row[1]): row for row in rows}
    parsed_music = by_stage[("parsed", "intent_001_music_search")]
    assert parsed_music[2:4] == ("music.search", "[MUSIC:夜曲]")
    assert json.loads(parsed_music[4])[0]["event_type"] == "intent_parsed"
    executed_music = by_stage[("execution", "intent_001_music_search")]
    assert executed_music[2] == "music.search"
    normalized_music = by_stage[("parsed", "intent_002_music_search")]
    assert normalized_music[2:4] == ("music.search", "[MUSIC:晴天]")
    assert by_stage[("execution", "intent_002_music_search")][2] == "music.search"
    assert by_stage[("parsed", "stream_ring_001")][2] == "device.ring_touch"
    failed = [row for row in rows if row[0] == "parse_failed"]
    assert failed == []


def test_execution_rows_are_semantic_and_deduplicated_across_reentry(tmp_path):
    db_path = tmp_path / "execution.db"
    ledger = _ledger_for_path(db_path)
    context = _context(
        "turn-execution",
        capabilities=("memory.remember", "pc.screen_check"),
    )
    postprocessed = PostProcessResult(
        content="",
        tool_intents=[
            ToolIntent(
                id="remember-1",
                tool_name="memory.remember",
                raw_text="[REMEMBER:记住这件事]",
                arguments={"content": "记住这件事"},
                side_effect_level="write",
            ),
            ToolIntent(
                id="screen-1",
                tool_name="pc.screen_check",
                raw_text="[SCREEN_CHECK:看看屏幕]",
                arguments={"reason": "看看屏幕"},
                side_effect_level="external",
            ),
        ],
    )

    async def remember_rejected(_intent, _context):
        return {"type": "memory_remember", "stored": False, "reason": "disabled"}

    async def screen_pending(_intent, _context):
        return {"type": "screen_check_pending", "request_id": "screen-request"}

    async def execute_once():
        return await execute_postprocessed_actions(
            postprocessed,
            profile=chat_turn_profile("send"),
            context=context,
            only_capabilities=frozenset({"memory.remember", "pc.screen_check"}),
            adapter_overrides={
                "memory.remember": remember_rejected,
                "pc.screen_check": screen_pending,
            },
            ledger_override=ledger,
        )

    first = _run(execute_once())
    second = _run(execute_once())
    assert len(first.results) == len(second.results) == 2

    rows = _run(
        _rows(
            db_path,
            "SELECT intent_id, status, outcome, events_json "
            "FROM tool_invocation_events WHERE stage='execution' ORDER BY intent_id",
        )
    )
    assert [(row[0], row[1], row[2]) for row in rows] == [
        ("remember-1", "executed", "rejected"),
        ("screen-1", "executed", "dispatched"),
    ]
    execution_events = json.loads(rows[0][3])
    assert [event["event_type"] for event in execution_events] == [
        "execution_started",
        "execution_finished",
    ]
    assert all(event["created_at"] is not None for event in execution_events)


def test_terminal_event_that_arrives_first_is_appended_to_same_execution_row(tmp_path):
    db_path = tmp_path / "terminal-race.db"
    ledger = _ledger_for_path(db_path)
    context = _context("turn-terminal")
    intent = ToolIntent(
        id="ring-1",
        tool_name="device.ring_touch",
        raw_text="[RING:轻碰一下]",
        arguments={"touch": "轻碰一下"},
        side_effect_level="device",
    )
    result = ToolResult.from_intent(
        intent,
        status=ToolStatus.EXECUTED,
        result={"status": "queued", "request_id": "ring-request-1"},
    )

    assert _run(ledger.record_terminal_outcome(
        correlation_id="ring-request-1",
        outcome="failed",
        event_type="ring_touch.timeout",
        error="timeout",
        result={"request_id": "ring-request-1"},
    )) == 0
    assert _run(ledger.record_execution(
        context,
        results=(result,),
        intents_by_id={intent.id: intent},
    )) == 1

    rows = _run(_rows(
        db_path,
        "SELECT outcome, error, events_json FROM tool_invocation_events "
        "WHERE stage='execution' AND intent_id='ring-1'",
    ))
    assert len(rows) == 1
    assert rows[0][0:2] == ("failed", "timeout")
    assert json.loads(rows[0][2])[-1]["event_type"] == "ring_touch.timeout"


def test_event_rows_inherit_frozen_invocation_source_and_advertised_tools(tmp_path):
    db_path = tmp_path / "frozen-observation.db"
    ledger = _ledger_for_path(db_path)
    context = ToolContext(
        conv_id="conv-frozen",
        msg_id="assistant-frozen",
        request_id="turn-frozen",
        capabilities=("music.search", "memory.remember"),
        metadata={
            "source": "send",
            "source_chain": "main",
            "invocation_id": "inv-frozen",
            "advertised_tools": ("music.search",),
        },
    )
    intent = ToolIntent(
        id="music-frozen",
        tool_name="music.search",
        raw_text="[MUSIC:夜曲]",
        arguments={"query": "夜曲"},
        side_effect_level="external",
    )
    result = ToolResult.from_intent(
        intent,
        status=ToolStatus.EXECUTED,
        result={"status": "succeeded", "query": "夜曲"},
    )

    _run(ledger.record_model_output(
        context,
        invocation_id="inv-frozen",
        raw_output="正文 [MUSIC:夜曲]",
        outcome="succeeded",
    ))
    _run(ledger.record_execution(
        context,
        results=(result,),
        intents_by_id={intent.id: intent},
    ))
    _run(ledger.record_marker(
        context,
        invocation_id="inv-frozen",
        marker_name="TIDE_INTENT",
        raw_text="[TIDE_INTENT:继续[/TIDE_INTENT]",
    ))

    rows = _run(_rows(
        db_path,
        "SELECT stage, invocation_id, source_chain, advertised_tools_json "
        "FROM tool_invocation_events ORDER BY stage",
    ))
    assert {row[0] for row in rows} == {"execution", "marker", "model_output"}
    assert all(row[1] == "inv-frozen" for row in rows)
    assert all(row[2] == "main" for row in rows)
    assert all(json.loads(row[3]) == ["music.search"] for row in rows)


def test_model_trace_keeps_request_raw_output_and_visible_text_separate(tmp_path):
    db_path = tmp_path / "model-trace.db"
    ledger = _ledger_for_path(db_path)
    context = ToolContext(
        conv_id="conv-trace",
        msg_id="assistant-trace",
        request_id="turn-trace",
        model_key="core-model",
        metadata={
            "source": "send",
            "source_chain": "main",
            "invocation_id": "inv-trace",
            "advertised_tools": ("music.search",),
        },
    )
    request = [{"role": "user", "content": "播放夜曲"}]

    _run(ledger.record_model_request(
        context,
        invocation_id="inv-trace",
        request_snapshot=request,
        advertised_tools=("music.search",),
    ))
    _run(ledger.record_model_output(
        context,
        invocation_id="inv-trace",
        raw_output="好。[MUSIC:夜曲 周杰伦]",
        outcome="succeeded",
    ))
    _run(ledger.record_visible_message(
        context,
        invocation_id="inv-trace",
        cleaned_content="好。",
        message_id="assistant-trace",
    ))

    rows = _run(_rows(
        db_path,
        "SELECT stage, invocation_id, request_snapshot_json, raw_output, "
        "cleaned_content FROM tool_invocation_events ORDER BY stage",
    ))
    by_stage = {row[0]: row for row in rows}
    assert set(by_stage) == {"model_request", "model_output", "visible_message"}
    assert all(row[1] == "inv-trace" for row in rows)
    assert json.loads(by_stage["model_request"][2]) == request
    assert by_stage["model_output"][3] == "好。[MUSIC:夜曲 周杰伦]"
    assert by_stage["visible_message"][4] == "好。"
    assert by_stage["visible_message"][3] == ""


def test_model_snapshots_keep_head_and_tail_with_a_per_row_byte_cap(tmp_path):
    db_path = tmp_path / "bounded-snapshots.db"
    ledger = _ledger_for_path(db_path, snapshot_max_bytes=1024)
    context = ToolContext(
        conv_id="conv-bounded",
        request_id="turn-bounded",
        metadata={"source": "send", "invocation_id": "inv-bounded"},
    )
    request = [
        {"role": "system", "content": "SYSTEM_HEAD_" + "甲" * 1200},
        {"role": "assistant", "content": "MIDDLE_" + "乙" * 1200},
        {"role": "user", "content": "丙" * 1200 + "_USER_TAIL"},
    ]
    raw_output = "RAW_HEAD_" + "丁" * 1200 + "_RAW_TAIL"

    _run(ledger.record_model_request(
        context,
        invocation_id="inv-bounded",
        request_snapshot=request,
    ))
    _run(ledger.record_model_output(
        context,
        invocation_id="inv-bounded",
        raw_output=raw_output,
        outcome="succeeded",
    ))

    rows = _run(_rows(
        db_path,
        "SELECT stage, request_snapshot_json, raw_output, truncated, "
        "snapshot_original_bytes FROM tool_invocation_events ORDER BY stage",
    ))
    by_stage = {row[0]: row for row in rows}
    request_row = by_stage["model_request"]
    output_row = by_stage["model_output"]
    wrapped_request = json.loads(request_row[1])

    assert len(request_row[1].encode("utf-8")) <= 1024
    assert request_row[3] == 1
    assert request_row[4] > 1024
    assert wrapped_request["truncated"] is True
    assert "SYSTEM_HEAD_" in wrapped_request["head"]
    assert "_USER_TAIL" in wrapped_request["tail"]

    assert len(output_row[2].encode("utf-8")) <= 1024
    assert output_row[3] == 1
    assert output_row[4] > 1024
    assert output_row[2].startswith("RAW_HEAD_")
    assert output_row[2].endswith("_RAW_TAIL")
    assert "[middle omitted]" in output_row[2]


def test_registry_gate_rejects_each_missing_binding_and_runtime_mismatch():
    definition = ToolDefinition(
        tool_name="example.tool",
        description="test",
        prompt_orders=(("example", 1),),
    )
    definitions = {definition.tool_name: definition}
    complete = {
        "prompt_renderers": {definition.tool_name: lambda *_args: "[EXAMPLE]"},
        "parser_bindings": {definition.tool_name},
        "executor_bindings": {definition.tool_name},
    }
    for binding_name, empty_value, expected in (
        ("prompt_renderers", {}, "renderer"),
        ("parser_bindings", set(), "parser"),
        ("executor_bindings", set(), "executor"),
    ):
        bindings = dict(complete)
        bindings[binding_name] = empty_value
        with pytest.raises(ToolRegistryError, match=expected):
            validate_tool_registry(definitions, **bindings)

    with pytest.raises(ToolRegistryError, match="missing_from_prompt"):
        validate_turn_advertisement(
            {"music.search", "memory.remember"},
            {"music.search"},
        )


def test_registry_keeps_private_channels_out_and_exposes_only_live_model_tools():
    registered = set(registered_tools_for_surface("main_stable"))
    assert "schedule.list" in registered
    assert "monitor.camera" not in registered
    assert registered.isdisjoint({
        "VOW",
        "UPDATE_MODEL",
        "WORKING_MODEL_REQUEST",
        "RECALL_INTENT",
        "OPPORTUNITY_NONE",
        "OPPORTUNITY_REFLECT",
        "TIDE_INTENT",
        "RETRY",
    })


def test_feedback_preserves_nonterminal_states_and_only_shortens_success():
    rendered = format_feedback_rows([
        {"tool_name": "music.search", "outcome": "succeeded"},
        {
            "tool_name": "device.ring_touch",
            "outcome": "failed",
            "error": "ack timeout",
        },
        {"tool_name": "device.toy", "outcome": "dispatched"},
        {"tool_name": "schedule.alarm", "outcome": "pending"},
        {"tool_name": "memory.remember", "outcome": "unknown"},
    ])

    assert '{"tool":"music.search","status":"ok"}' in rendered
    assert (
        '{"tool":"device.ring_touch","status":"failed",'
        '"reason":"ack timeout"}'
    ) in rendered
    assert '{"tool":"device.toy","status":"dispatched"}' in rendered
    assert '{"tool":"schedule.alarm","status":"pending"}' in rendered
    assert '{"tool":"memory.remember","status":"unknown"}' in rendered
    assert "[RING:" not in rendered


def test_ledger_failure_is_fail_open_for_postprocess():
    @asynccontextmanager
    async def broken_factory():
        raise RuntimeError("ledger unavailable")
        yield  # pragma: no cover

    ledger = ToolInvocationLedger(db_factory=broken_factory)
    processor = PostProcessor(ledger=ledger)
    result = _run(
        processor.process(
            "正文 [MUSIC:夜曲]",
            conv_id="conv-1",
            enabled_commands={"music"},
            tool_context=_context("turn-fail-open", capabilities=("music.search",)),
        )
    )

    assert result.content == "正文"
    assert [intent.tool_name for intent in result.tool_intents] == ["music.search"]
    assert ledger.write_failures == 1


def test_ledger_write_timeout_is_fail_open_and_bounded():
    entered = asyncio.Event()
    cancelled = asyncio.Event()

    @asynccontextmanager
    async def hanging_factory():
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()
        yield  # pragma: no cover

    ledger = ToolInvocationLedger(
        db_factory=hanging_factory,
        write_timeout_seconds=0.01,
    )

    async def exercise():
        result = await ledger.record_turn(
            _context("turn-timeout"),
            prompt_source="send",
            advertised_tools=("music.search",),
            turn_outcome="succeeded",
        )
        return result, entered.is_set(), cancelled.is_set()

    result, did_enter, was_cancelled = _run(exercise())

    assert result == 0
    assert did_enter is True
    assert was_cancelled is True
    assert ledger.write_failures == 1


def test_ledger_sqlite_lock_does_not_wait_for_default_five_seconds(
    tmp_path,
    monkeypatch,
):
    import database

    db_path = tmp_path / "locked-ledger.db"
    monkeypatch.setattr(database, "DB_PATH", str(db_path))

    async def exercise():
        async with aiosqlite.connect(db_path) as db:
            await init_tool_invocation_ledger_tables(db)
            await db.commit()

        locker = await aiosqlite.connect(db_path)
        try:
            await locker.execute("BEGIN IMMEDIATE")
            ledger = ToolInvocationLedger()
            loop = asyncio.get_running_loop()
            started_at = loop.time()
            result = await ledger.record_turn(
                _context("turn-write-lock"),
                prompt_source="send",
                advertised_tools=("music.search",),
                turn_outcome="succeeded",
            )
            elapsed = loop.time() - started_at
            return result, elapsed, ledger.write_failures
        finally:
            await locker.rollback()
            await locker.close()

    result, elapsed, failures = _run(exercise())

    assert result == 0
    assert elapsed < 1.25
    assert failures == 1


def test_opportunity_none_records_a_successful_zero_call_turn(monkeypatch):
    recorded = []

    class CapturingLedger:
        @staticmethod
        def new_invocation_id(_prefix):
            return "opportunity-invocation"

        async def record_turn(self, context, **kwargs):
            recorded.append((context, kwargs))
            return 1

    profile = opportunity_turn_profile(
        runtime_capabilities={"heart.whisper"},
        reflection_allowed=False,
    )
    prepared = opp.PreparedOpportunityTurn(
        messages=[{"role": "user", "content": "idle"}],
        profile=profile,
        model_key="core",
        identity_snapshot={},
        reflection_context=None,
        mobile_screen_target=None,
        advertised_tools=("heart.whisper",),
    )

    async def prepare(**_kwargs):
        return prepared

    async def core(*_args, **_kwargs):
        return "[OPPORTUNITY_NONE]"

    async def no_broadcast(_entry):
        return None

    monkeypatch.setattr(opp, "tool_invocation_ledger", CapturingLedger())
    monkeypatch.setattr(opp, "_prepare_opportunity_turn", prepare)
    monkeypatch.setattr(opp, "call_opportunity_core", core)
    monkeypatch.setattr(opp, "_broadcast_log", no_broadcast)

    result = _run(
        opp.OpportunityRunner()._run(
            1000.0,
            {"conv_id": "conv-1", "model_key": "core", "last_user_ts": 1.0},
        )
    )

    assert result["status"] == "none_explicit"
    assert len(recorded) == 1
    context, payload = recorded[0]
    assert context.request_id == "msg_1000000_opp"
    assert context.metadata["invocation_id"] == "opportunity-invocation"
    assert payload["turn_outcome"] == "succeeded"
    assert payload["advertised_tools"] == ("heart.whisper",)


def test_opportunity_prompt_carries_exact_advertised_tool_metadata():
    profile = opportunity_turn_profile(
        runtime_capabilities={"heart.whisper", "memory.remember", "device.ring_touch"},
        reflection_allowed=False,
    )
    block = build_opportunity_ability_block(
        profile=profile,
        user_name="用户",
        ai_name="AI",
        model_key="core",
    )

    assert block.advertised_tools == (
        "device.ring_touch",
        "heart.whisper",
        "memory.remember",
    )
    assert "vow" not in " ".join(block.advertised_tools).lower()


def test_send_prompt_builder_carries_only_rendered_tool_metadata(monkeypatch):
    async def empty_runtime_context():
        return ""

    monkeypatch.setattr(
        prompt_builder,
        "_build_schedule_and_location_block",
        empty_runtime_context,
    )
    block = _run(
        prompt_builder.build_send_ability_block(
            conv_id="conv-1",
            body=MsgCreate(content="hi"),
            user_name="用户",
            capabilities=("music.search", "memory.remember", "device.toy"),
            model_key="core",
        )
    )

    assert block.advertised_tools == ("memory.remember", "music.search")
    assert "[MUSIC:" in block
    assert "[REMEMBER:" in block
    assert "[TOY:" not in block


def test_read_only_report_uses_successful_eligible_turn_denominators():
    turns = [
        {
            "turn_id": "one",
            "prompt_source": "send",
            "history_trace_version": 0,
            "advertised_tools_json": '["music.search","memory.remember"]',
            "created_at": 1.0,
        },
        {
            "turn_id": "two",
            "prompt_source": "opportunity",
            "history_trace_version": 0,
            "advertised_tools_json": '["music.search"]',
            "created_at": 2.0,
        },
    ]
    events = [
        {"turn_id": "one", "stage": "parsed", "tool_name": "music.search"},
        {"turn_id": "two", "stage": "parse_failed", "tool_name": "music.search"},
        {
            "turn_id": "one",
            "stage": "execution",
            "tool_name": "music.search",
            "outcome": "succeeded",
        },
        {"turn_id": "one", "stage": "parsed", "tool_name": "memory.remember"},
    ]

    report = summarize(turns, events)
    assert report["eligible_turns"] == 2
    assert report["calls_per_100_eligible_turns"] == 100.0
    assert report["parse_failure_percent_of_attempts"] == 33.33
    assert report["tools"]["music.search"]["advertised_turns"] == 2
    assert report["tools"]["music.search"]["calls_per_100_advertised_turns"] == 50.0


def test_read_only_report_cli_runs_directly_from_project_root(tmp_path):
    db_path = tmp_path / "report-cli.db"
    _ledger_for_path(db_path)
    project_root = Path(__file__).resolve().parents[1]

    completed = subprocess.run(
        [
            sys.executable,
            str(project_root / "scripts" / "tool_invocation_ledger_report.py"),
            "--db",
            str(db_path),
            "--limit-turns",
            "50",
        ],
        cwd=project_root,
        check=True,
        capture_output=True,
        text=True,
    )

    report = json.loads(completed.stdout)
    assert report["mode"] == "read_only"
    assert report["eligible_turns"] == 0
