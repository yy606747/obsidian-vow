import asyncio

from app.schedule import manager as schedule_manager


def test_tick_marks_all_due_before_single_fire_call(monkeypatch):
    marked = []
    fired = []
    due = [
        {"id": "a1", "type": "alarm", "trigger_at": "2026-05-15 08:00", "content": "起床"},
        {"id": "m1", "type": "monitor", "trigger_at": "2026-05-15 08:00", "content": "看状态"},
    ]

    async def fake_list_due(_now):
        return due

    async def fake_mark_triggered(sid):
        marked.append(sid)

    async def fake_fire(items):
        fired.append(list(items))

    monkeypatch.setattr(schedule_manager.store, "list_due", fake_list_due)
    monkeypatch.setattr(schedule_manager.store, "mark_triggered", fake_mark_triggered)
    monkeypatch.setattr(schedule_manager.trigger, "fire_due_items", fake_fire)

    asyncio.run(schedule_manager.ScheduleManager()._tick())

    assert marked == ["a1", "m1"]
    assert fired == [due]
