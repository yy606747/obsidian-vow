import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace

from app.chat.action_executor import execute_postprocessed_actions
from app.chat.postprocess import PostProcessor
from app.chat.turn_profiles import TurnProfile, chat_turn_profile
from app.self_wake import SELF_WAKE_SURFACE_CAPABILITIES
from app.self_wake import service as self_wake_service
from app.tools.feedback import format_feedback_rows
from app.tools.parser import parse_structured_tool_intents, parse_tool_intents
from app.tools.prompt_renderers import render_registered_capabilities
from app.tools.schemas import ToolContext, ToolIntent, ToolStatus


def _run(awaitable):
    return asyncio.run(awaitable)


class _NoopLedger:
    async def record_postprocess(self, *args, **kwargs):
        return 0

    async def record_execution(self, *args, **kwargs):
        return 0


def _prompt_context():
    return {
        "self_wake_status": {
            "pending": {
                "id": "wake_old",
                "wake_at": 1_800_003_600,
                "intent": "旧意图",
                "requested_capabilities": ["memory.remember"],
            },
            "quota": {"limit": 3, "used": 1, "remaining": 2},
            "recent_nonexecution": None,
        },
        "self_wake_now_local": "2027-01-15T08:00+00:00",
        "self_wake_timezone": "UTC",
        "self_wake_quiet_hours": {
            "enabled": True,
            "start": "00:00",
            "end": "07:00",
        },
        "self_wake_legal_capabilities": tuple(
            sorted(SELF_WAKE_SURFACE_CAPABILITIES)
        ),
        "user_name": "owner",
        "pc_screen_available": True,
        "mobile_screen_available": True,
        "mobile_screen_target": {"device_id": "phone", "label": "手机"},
        "ring_available": True,
        "heart_prompt": "[HEART:内容]",
        "poi_available": True,
        "presence_draw_available": True,
        "presence_show_available": True,
    }


def test_marker_parser_splits_capabilities_from_right_and_preserves_intent_pipe():
    intents = parse_tool_intents(
        "前文[SELF_WAKE:2027-01-15T09:00:00+00:00|看看 A|B 做完没有|desktop.presence.show,memory.remember]后文"
    )
    assert len(intents) == 1
    assert intents[0].tool_name == "self_wake.schedule"
    assert intents[0].arguments == {
        "wake_at": "2027-01-15T09:00:00+00:00",
        "intent": "看看 A|B 做完没有",
        "requested_capabilities": [
            "desktop.presence.show",
            "memory.remember",
        ],
    }


def test_structured_actions_accept_empty_caps_and_ignore_model_origin_fields():
    intents = parse_structured_tool_intents(
        [
            {
                "tool_name": "self_wake.schedule",
                "arguments": {
                    "wake_at": "2027-01-15 09:00",
                    "intent": "回来看看",
                    "requested_capabilities": [],
                    "origin": "date_episode",
                    "conv_id": "forged",
                },
            },
            {
                "tool_name": "self_wake.cancel",
                "arguments": {"wake_id": "forged"},
            },
        ]
    )
    assert [item.tool_name for item in intents] == [
        "self_wake.schedule",
        "self_wake.cancel",
    ]
    assert intents[0].arguments == {
        "wake_at": "2027-01-15 09:00",
        "intent": "回来看看",
        "requested_capabilities": [],
    }
    assert intents[1].arguments == {}


def test_postprocess_strips_both_markers_and_disabled_profile_cannot_parse_them():
    async def run():
        processor = PostProcessor(ledger=_NoopLedger())
        marker = (
            "正文[SELF_WAKE:2027-01-15T09:00:00+00:00|回来|memory.remember]"
            "[SELF_WAKE_CANCEL]"
        )
        enabled = await processor.process(
            marker,
            conv_id="conv",
            enabled_commands={"self_wake"},
        )
        assert enabled.content == "正文"
        assert [item.tool_name for item in enabled.tool_intents] == [
            "self_wake.schedule",
            "self_wake.cancel",
        ]
        disabled = await processor.process(
            marker,
            conv_id="conv",
            enabled_commands=set(),
        )
        assert disabled.content == "正文"
        assert disabled.tool_intents == []

    _run(run())


def test_self_wake_surface_and_prompt_are_exact_and_dynamic():
    entries = render_registered_capabilities(
        "self_wake",
        capabilities=SELF_WAKE_SURFACE_CAPABILITIES,
        context=_prompt_context(),
    )
    assert {name for name, _prose in entries} == SELF_WAKE_SURFACE_CAPABILITIES
    opportunity_entries = render_registered_capabilities(
        "opportunity",
        capabilities={"self_wake.schedule", "self_wake.cancel"},
        context=_prompt_context(),
    )
    text = "\n".join(prose for _name, prose in opportunity_entries)
    assert "给未来的自己留一个时刻" in text
    assert "想回来时才留，不留也是自然的选择" in text
    assert "之前这次约定会被覆盖" in text
    assert "所有对话中合计尝试主动回来 3 次" in text
    assert "已经尝试 1 次，还能再尝试 2 次" in text
    assert "旧意图" in text
    assert "memory.remember" in text
    assert "wake_old" not in text
    assert "relationship" not in text
    assert "pending" not in text
    assert "no_pending_wake" not in text
    assert "requested_capabilities" not in text
    assert "原子" not in text
    assert "[SELF_WAKE_CANCEL]" in text


def test_self_wake_prompt_translates_recent_nonexecution_outcomes():
    translations = {
        "daily_quota_exhausted": "当天所有对话合计的主动回来次数已经用完",
        "expired": "系统醒来时已经比约定晚了两个小时以上",
        "provider_failed": "那次生成没有成功",
    }

    for outcome, explanation in translations.items():
        context = _prompt_context()
        context["self_wake_status"]["recent_nonexecution"] = {
            "id": "wake_internal",
            "wake_at": 1_800_003_600,
            "intent": "回来看看她",
            "outcome": outcome,
        }
        entries = render_registered_capabilities(
            "opportunity",
            capabilities={"self_wake.schedule"},
            context=context,
        )
        text = "\n".join(prose for _name, prose in entries)
        assert explanation in text
        assert outcome not in text
        assert "wake_internal" not in text


def test_schedule_adapter_injects_context_and_rejects_unknown_capability(monkeypatch):
    calls = []

    class Repo:
        async def schedule_or_replace(self, **kwargs):
            calls.append(kwargs)
            return {
                "wake": {
                    "id": "wake_new",
                    "wake_at": kwargs["wake_at"],
                    "expires_at": kwargs["wake_at"] + 7200,
                    "intent": kwargs["intent"],
                    "requested_capabilities": list(kwargs["requested_capabilities"]),
                },
                "replaced": {
                    "id": "wake_old",
                    "wake_at": kwargs["wake_at"] - 60,
                    "intent": "旧意图",
                },
            }

    now = 1_800_000_000.0
    monkeypatch.setattr(self_wake_service, "self_wake_repository", Repo())
    monkeypatch.setattr(self_wake_service.time, "time", lambda: now)
    monkeypatch.setattr(self_wake_service, "owner_timezone_name", lambda: "UTC")
    monkeypatch.setattr(self_wake_service, "validate_not_quiet", lambda *a, **k: None)
    context = ToolContext(
        conv_id="conv-real",
        msg_id="assistant-real",
        mode="normal",
        capabilities=("self_wake.schedule",),
        metadata={"source": "send"},
    )
    wake_text = datetime.fromtimestamp(now + 3600, timezone.utc).strftime(
        "%Y-%m-%dT%H:%M"
    )
    intent = ToolIntent(
        id="intent",
        tool_name="self_wake.schedule",
        raw_text="marker",
        arguments={
            "wake_at": wake_text,
            "intent": "回来",
            "requested_capabilities": ["memory.remember"],
            "origin": "forged",
        },
        side_effect_level="write",
    )
    result = _run(self_wake_service.execute_schedule_intent(intent, context))
    assert result["ok"] is True
    assert result["replaced_wake_id"] == "wake_old"
    assert calls[0]["origin"] == "relationship"
    assert calls[0]["origin_ref"] == "conv-real"
    assert calls[0]["source"] == "chat"
    assert calls[0]["source_turn_id"] == "assistant-real"

    unknown = ToolIntent(
        id="unknown",
        tool_name="self_wake.schedule",
        raw_text="marker",
        arguments={
            "wake_at": wake_text,
            "intent": "回来",
            "requested_capabilities": ["schedule.alarm"],
        },
        side_effect_level="write",
    )
    rejected = _run(self_wake_service.execute_schedule_intent(unknown, context))
    assert rejected["reason"] == "unknown_requested_capability"
    assert len(calls) == 1


def test_executor_runs_only_first_self_wake_action_and_profile_is_enforced():
    intents = [
        ToolIntent(
            id="first",
            tool_name="self_wake.schedule",
            raw_text="schedule",
            arguments={},
            side_effect_level="write",
        ),
        ToolIntent(
            id="second",
            tool_name="self_wake.cancel",
            raw_text="cancel",
            arguments={},
            side_effect_level="write",
        ),
    ]
    postprocessed = SimpleNamespace(tool_intents=intents)
    calls = []

    async def adapter(intent, _context):
        calls.append(intent.tool_name)
        return {"ok": True, "status": "succeeded"}

    context = ToolContext(
        conv_id="conv",
        msg_id="msg",
        mode="normal",
        capabilities=("self_wake.schedule", "self_wake.cancel"),
        metadata={"source": "send"},
    )

    async def run():
        execution = await execute_postprocessed_actions(
            postprocessed,
            profile=chat_turn_profile("send"),
            context=context,
            only_capabilities=frozenset({"self_wake.schedule", "self_wake.cancel"}),
            adapter_overrides={
                "self_wake.schedule": adapter,
                "self_wake.cancel": adapter,
            },
            ledger_override=_NoopLedger(),
        )
        assert calls == ["self_wake.schedule"]
        assert [result.status for result in execution.results] == [
            ToolStatus.SKIPPED,
            ToolStatus.EXECUTED,
        ] or [result.status for result in execution.results] == [
            ToolStatus.EXECUTED,
            ToolStatus.SKIPPED,
        ]
        extra = next(result for result in execution.results if result.intent_id == "second")
        assert extra.error == "one_self_wake_action_per_turn"

        denied_profile = TurnProfile(
            prompt_source="send",
            allowed_markers=frozenset(),
            allowed_tool_capabilities=frozenset(),
        )
        calls.clear()
        denied = await execute_postprocessed_actions(
            SimpleNamespace(tool_intents=intents[:1]),
            profile=denied_profile,
            context=context,
            only_capabilities=frozenset({"self_wake.schedule"}),
            adapter_overrides={"self_wake.schedule": adapter},
            ledger_override=_NoopLedger(),
        )
        assert calls == []
        assert denied.results[0].error == "turn_profile_not_allowed"

    _run(run())


def test_replacement_feedback_exposes_old_wake_id_and_time():
    feedback = format_feedback_rows(
        [
            {
                "tool_name": "self_wake.schedule",
                "outcome": "succeeded",
                "error": "",
                "result_summary": (
                    '{"ok":true,"status":"succeeded",'
                    '"replaced_wake_id":"wake_old",'
                    '"replaced_wake_at":1800000100.0}'
                ),
            }
        ]
    )
    assert '"replaced_wake_id":"wake_old"' in feedback
    assert '"replaced_wake_at":1800000100.0' in feedback
