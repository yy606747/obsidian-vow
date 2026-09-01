import asyncio
from pathlib import Path

from app.presence.prompt_context import (
    build_presence_identity_block,
    presence_identity_head,
)
from app.sentinel.core_wake_orchestrator import _persona_messages


ROOT = Path(__file__).resolve().parents[1]


class Library:
    timezone_name = "UTC"

    async def human_baseline(self):
        return {
            "created_at": 1_800_000_000.0,
            "prompt": "银白短发，深紫外套，正面全身",
            "description": "阿澈选择这副样子，因为轮廓很像此刻的心情。",
            "status": "technical state must not leak",
        }

    async def non_seed_count(self):
        # Includes archived sprites by contract.
        return 4


def test_identity_head_and_human_block_use_only_durable_identity_fields():
    head = asyncio.run(presence_identity_head(library=Library()))
    block = build_presence_identity_block(
        head,
        user_name="小栀",
        ai_name="阿澈",
        time_formatter=lambda _timestamp, _timezone: "2027-01-15 08:00",
    )

    assert set(head) == {"baseline", "non_seed_count", "timezone_name"}
    assert set(head["baseline"]) == {"created_at", "prompt", "description"}
    assert "[关于阿澈曾选择的人形]" in block
    assert "2027-01-15 08:00，阿澈第一次选择以人的样子出现" in block
    assert "外观基准：银白短发，深紫外套，正面全身" in block
    assert "当时的自述：阿澈选择这副样子" in block
    assert "阿澈还留下过 3 个其他形象" in block
    assert "不要求阿澈继续选择人形" in block
    assert "向小栀谈论" in block
    assert "technical state" not in block
    assert "用户" not in block
    assert "对方" not in block
    assert "TA" not in block


def test_empty_identity_block_is_explicitly_not_a_drawing_task():
    block = build_presence_identity_block(
        {"baseline": None, "non_seed_count": 0, "timezone_name": "UTC"},
        user_name="小栀",
        ai_name="阿澈",
    )

    assert block == (
        "[关于阿澈的桌面形象]\n"
        "阿澈还没有为自己留下形象。这个事实不构成任务，也不需要向小栀提出画一个。"
    )


def test_core_wake_persona_places_identity_before_recent_chat():
    identity = build_presence_identity_block(
        {"baseline": None, "non_seed_count": 0},
        user_name="小栀",
        ai_name="阿澈",
    )
    messages = _persona_messages({
        "user_name": "小栀",
        "ai_name": "阿澈",
        "user_persona": "在准备考试。",
        "ai_persona": "会认真听完。",
        "presence_identity_block": identity,
    })

    assert messages[-2] == {"role": "user", "content": identity}
    assert messages[-1]["role"] == "assistant"
    assert "连续性基准" in messages[-1]["content"]


def test_all_core_surfaces_are_wired_and_night_has_an_explicit_skip():
    files = {
        "send_regenerate": ROOT / "app/chat/chat_turn.py",
        "idle_summon_night": ROOT / "opportunity.py",
        "self_wake": ROOT / "app/self_wake/trigger.py",
        "core_wake_reader": ROOT / "sentinel_runtime_readers.py",
        "core_wake_builder": ROOT / "app/sentinel/core_wake_orchestrator.py",
    }
    source = {name: path.read_text(encoding="utf-8") for name, path in files.items()}

    assert source["send_regenerate"].count("await presence_identity_head()") == 2
    assert "if kind != \"night\":" in source["idle_summon_night"]
    assert "await presence_identity_head()" in source["idle_summon_night"]
    assert "await presence_identity_head()" in source["self_wake"]
    assert 'payload["presence_identity_block"]' in source["core_wake_reader"]
    assert 'context.get("presence_identity_block")' in source["core_wake_builder"]
