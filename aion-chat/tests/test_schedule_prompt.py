from app.schedule import prompt


def test_single_alarm_prompt_and_system_message_match_legacy_shape():
    item = {"id": "sch_a", "type": "alarm", "trigger_at": "2026-05-14 20:00", "content": "喝水"}

    text = prompt.build_alarm_trigger_prompt(item, "2026年05月14日  20:00:00", "用户")

    assert text == (
        "[日程闹铃触发]\n"
        "日程内容：2026-05-14 20:00 — 喝水\n"
        "现在时间已经到了（当前 2026年05月14日  20:00:00），请提醒【用户】。"
    )
    assert prompt.build_system_message([item], "Aion") == "⏰ 日程闹铃触发：喝水"


def test_single_monitor_prompt_keeps_evidence_and_guidance():
    item = {"id": "sch_m", "type": "monitor", "trigger_at": "2026-05-14 20:00", "content": "看她有没有休息"}

    text = prompt.build_monitor_trigger_prompt(item, "2026年05月14日  20:00:00", "用户", "\n证据文本\n", [])

    assert "[定时查岗触发]" in text
    assert "查岗目的：看她有没有休息" in text
    assert "证据文本" in text
    assert "判断指引——避免误判" in text
    assert prompt.build_system_message([item], "Aion") == "Aion来查岗了"


def test_merged_prompt_contains_all_items_once():
    items = [
        {"id": "a", "type": "alarm", "trigger_at": "2026-05-14 20:00", "content": "喝水"},
        {"id": "m", "type": "monitor", "trigger_at": "2026-05-14 20:00", "content": "看看状态"},
        {"id": "a2", "type": "alarm", "trigger_at": "2026-05-14 20:00", "content": "站起来"},
    ]

    text = prompt.build_merged_trigger_prompt(items, "now", "用户", "\n证据\n", [])

    assert "以下 3 条日程/查岗同时到期" in text
    assert text.count("--- 第 ") == 3
    assert "喝水" in text
    assert "看看状态" in text
    assert "站起来" in text
    assert text.count("判断指引——避免误判") == 1
    assert prompt.build_system_message(items, "Aion") == "⏰ 3 条日程同时到期"


def test_schedule_chain_capability_block_is_registry_generated_without_false_list_offer():
    block = prompt.build_abilities_block("用户", "暂无日程")

    assert block.advertised_tools == (
        "music.search",
        "schedule.delete",
        "schedule.monitor",
        "schedule.reminder",
    )
    assert "[ALARM:" not in block
    assert "[SCHEDULE_LIST]" not in block
