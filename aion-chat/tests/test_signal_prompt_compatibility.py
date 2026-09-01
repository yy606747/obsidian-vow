import json
from datetime import datetime

import activity
import sensing


def _write_jsonl(path, rows):
    path.write_text(
        "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows),
        encoding="utf-8",
    )


def _local_ts(hour, minute):
    return datetime(2026, 8, 14, hour, minute).timestamp()


def test_activity_summary_prompt_contract_is_byte_stable(monkeypatch, tmp_path):
    """Retention changes must not widen or rewrite the existing 3-hour summary."""
    now = _local_ts(12, 7)
    monkeypatch.setattr(activity, "ACTIVITY_LOGS_DIR", tmp_path)
    monkeypatch.setattr(activity.time, "time", lambda: now)
    _write_jsonl(
        tmp_path / "2026-08-14.jsonl",
        [
            {
                "timestamp": _local_ts(8, 50),
                "device": "pc",
                "app": "OldApp",
                "title": "must stay outside the prompt window",
            },
            {
                "timestamp": _local_ts(11, 41),
                "device": "pc",
                "app": "Editor",
                "title": "",
            },
            {
                "timestamp": _local_ts(11, 51),
                "device": "pc",
                "app": "Editor",
                "title": "",
            },
        ],
    )

    assert activity.generate_activity_summary(hours=3) == [
        {
            "start": "11:40",
            "end": "11:50",
            "summary": "PC: Editor 9分钟",
            "count": 1,
        },
        {
            "start": "11:50",
            "end": "12:00",
            "summary": "PC: Editor 10分钟",
            "count": 1,
        },
    ]


def test_sensing_prompt_contract_ignores_new_aggregation_metadata(monkeypatch, tmp_path):
    """Aggregation-only fields must never leak into the legacy sensing formatter."""
    now = _local_ts(12, 7)
    monkeypatch.setattr(sensing, "SENSING_LOGS_DIR", tmp_path)
    monkeypatch.setattr(sensing.time, "time", lambda: now)
    _write_jsonl(
        tmp_path / "2026-08-14.jsonl",
        [
            {
                "timestamp": _local_ts(8, 50),
                "type": "sensor",
                "data": {"motion": "running"},
            },
            {
                "timestamp": _local_ts(11, 45),
                "type": "sensor",
                "data": {
                    "motion": "still",
                    "wifi_ssid": "Home",
                    "screen_on": True,
                    "battery_pct": 80,
                    "charging": False,
                },
            },
            {
                "timestamp": _local_ts(11, 50),
                "type": "biometric",
                "data": {
                    "heart_rate": 62,
                    "heart_rate_observed_at": _local_ts(11, 49),
                    "spo2": 98,
                    "spo2_observed_at": _local_ts(11, 48),
                    "sleep_stage": "light",
                    "steps_delta": 15,
                    "steps_total_today": 1000,
                    "steps_total_date": "2026-08-14",
                    "steps_total_timezone": "America/Los_Angeles",
                },
            },
            {
                "timestamp": _local_ts(11, 55),
                "type": "notification",
                "data": {"app": "微信"},
            },
            {
                "timestamp": _local_ts(11, 56),
                "type": "unlock",
                "data": {},
            },
        ],
    )

    assert sensing.format_sensing_for_prompt(hours=3, max_entries=60) == (
        "[11:45] 静止 · 亮屏 · 电量80%\n"
        "[11:50] 心率62 · 血氧98% · 睡眠:light · 步数+15\n"
        "[11:55] 微信×1 · 解锁×1"
    )


def test_sensing_prompt_ignores_backfill_received_inside_prompt_window(monkeypatch, tmp_path):
    now = _local_ts(12, 7)
    monkeypatch.setattr(sensing, "SENSING_LOGS_DIR", tmp_path)
    monkeypatch.setattr(sensing.time, "time", lambda: now)
    _write_jsonl(
        tmp_path / "2026-08-14.jsonl",
        [
            {
                "timestamp": _local_ts(11, 45),
                "type": "sensor",
                "data": {"motion": "still"},
            },
            {
                "timestamp": _local_ts(12, 0),
                "type": "biometric",
                "backfill": True,
                "source_kind": "heart_rate",
                "source_id": "historical-record",
                "data": {
                    "heart_rate": 62,
                    "heart_rate_observed_at": _local_ts(3, 0),
                },
            },
        ],
    )

    assert sensing.format_sensing_for_prompt(hours=3, max_entries=60) == "[11:45] 静止"
