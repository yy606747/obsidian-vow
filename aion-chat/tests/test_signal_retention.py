import json
import os
import time
from datetime import date, timedelta

import activity
import cleanup
import config
import sensing


def _write_activity_file(directory, day, timestamp):
    path = directory / f"{day.isoformat()}.jsonl"
    path.write_text(json.dumps({"timestamp": timestamp}) + "\n", encoding="utf-8")
    return path


def test_activity_cleanup_uses_raw_retention_not_prompt_window(monkeypatch, tmp_path):
    now = time.time()
    today = date.fromtimestamp(now)
    forty_days_ago = today - timedelta(days=40)
    sixty_one_days_ago = today - timedelta(days=61)
    kept = _write_activity_file(
        tmp_path,
        forty_days_ago,
        now - 40 * 86400,
    )
    expired = _write_activity_file(
        tmp_path,
        sixty_one_days_ago,
        now - 61 * 86400,
    )

    monkeypatch.setitem(config.SETTINGS, "activity_raw_retention_days", 60)
    monkeypatch.setattr(activity, "ACTIVITY_LOGS_DIR", tmp_path)
    monkeypatch.setattr(activity, "_last_cleanup_ts", 0.0)
    activity.cleanup_old_activity_logs()

    assert activity.KEEP_HOURS == 3
    assert kept.exists()
    assert not expired.exists()


def test_sensing_cleanup_retention_is_configurable(monkeypatch, tmp_path):
    today = date.today()
    kept = tmp_path / f"{(today - timedelta(days=40)).isoformat()}.jsonl"
    expired = tmp_path / f"{(today - timedelta(days=61)).isoformat()}.jsonl"
    kept.write_text("{}\n", encoding="utf-8")
    expired.write_text("{}\n", encoding="utf-8")

    monkeypatch.setitem(config.SETTINGS, "sensing_raw_retention_days", 60)
    monkeypatch.setattr(sensing, "SENSING_LOGS_DIR", tmp_path)
    monkeypatch.setattr(sensing, "_last_cleanup_ts", 0.0)
    sensing.cleanup_old_sensing_logs()

    assert kept.exists()
    assert not expired.exists()


def test_startup_cleanup_uses_same_activity_policy(monkeypatch, tmp_path):
    activity_dir = tmp_path / "activity_logs"
    activity_dir.mkdir()
    kept = activity_dir / "kept.jsonl"
    expired = activity_dir / "expired.jsonl"
    kept.write_text("{}\n", encoding="utf-8")
    # Sparse size crosses the old independent 100 MB cap without consuming it.
    with kept.open("r+b") as handle:
        handle.truncate(101 * 1024 * 1024)
    expired.write_text("{}\n", encoding="utf-8")
    now = time.time()
    os.utime(kept, (now - 40 * 86400, now - 40 * 86400))
    os.utime(expired, (now - 61 * 86400, now - 61 * 86400))

    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "TTS_CACHE_DIR", tmp_path / "tts")
    monkeypatch.setattr(config, "MONITOR_LOGS_DIR", tmp_path / "monitor")
    monkeypatch.setitem(config.SETTINGS, "activity_raw_retention_days", 60)
    monkeypatch.setitem(config.SETTINGS, "activity_raw_max_total_mb", 512)

    cleanup.run_startup_cleanup()

    assert kept.exists()
    assert not expired.exists()
