from datetime import datetime

from app.chat import chat_turn, prompt_builder
from app.vows.prompt import build_vow_ability_block


def test_current_time_block_has_minute_not_second_precision():
    block = prompt_builder.build_current_time_block(
        now=datetime(2026, 8, 3, 23, 59, 41)
    )

    assert block.endswith("2026年08月03日  23:59")
    assert "23:59:41" not in block


def test_vow_policy_is_stable_but_daily_quota_is_runtime():
    block = build_vow_ability_block(remaining_today=2)
    stable, dynamic = chat_turn._split_vow_ability(block)

    assert "[VOW:誓约内容|确认语]" in stable
    assert "只收一年后" in stable
    assert "还可以主动立约 2 次" not in stable
    assert "还可以主动立约 2 次" in dynamic


def test_vow_policy_remains_present_when_daily_quota_is_exhausted():
    block = build_vow_ability_block(remaining_today=0)
    stable, dynamic = chat_turn._split_vow_ability(block)

    assert "[VOW:誓约内容|确认语]" in stable
    assert "只有本轮实时额度明确允许时" in stable
    assert "额度已经用完" in dynamic
