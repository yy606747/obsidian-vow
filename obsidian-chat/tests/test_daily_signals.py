import asyncio
import json
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta
from threading import Barrier
from zoneinfo import ZoneInfo

from app.daily_signals.aggregation import reconcile_daily_signals
from app.daily_signals import runtime as daily_runtime
from app.daily_signals.runtime import record_location_heartbeat_safely
from app.daily_signals.store import DailySignalStore
from routes.sensing import BiometricTick


ZONE_NAME = "America/Los_Angeles"
ZONE = ZoneInfo(ZONE_NAME)
DAY = date(2026, 8, 14)


def _ts(hour, minute=0, second=0, *, day=DAY):
    return datetime(
        day.year,
        day.month,
        day.day,
        hour,
        minute,
        second,
        tzinfo=ZONE,
    ).timestamp()


def _write_jsonl(directory, name, rows):
    directory.mkdir(parents=True, exist_ok=True)
    (directory / name).write_text(
        "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows),
        encoding="utf-8",
    )


def _reconcile(tmp_path, *, activity_rows=(), sensing_rows=(), store=None):
    activity_dir = tmp_path / "activity"
    sensing_dir = tmp_path / "sensing"
    _write_jsonl(activity_dir, "2026-08-14.jsonl", activity_rows)
    _write_jsonl(sensing_dir, "2026-08-14.jsonl", sensing_rows)
    target_store = store or DailySignalStore(tmp_path / "daily.db")
    reconcile_daily_signals(
        store=target_store,
        activity_logs_dir=activity_dir,
        sensing_logs_dir=sensing_dir,
        timezone_name=ZONE_NAME,
        now=_ts(23, 59),
        dates=(DAY,),
    )
    return target_store


def test_activity_time_and_rest_never_span_an_outage(tmp_path):
    activity_rows = [
        {"timestamp": _ts(8, 0), "device": "pc", "app": "Editor", "active_state": "active"},
        {"timestamp": _ts(8, 1), "device": "pc", "app": "Editor", "active_state": "active"},
        {"timestamp": _ts(8, 2), "device": "pc", "app": None, "active_state": "locked"},
        # Two-hour outage: the 08:02 lock must not fill the missing period.
        {"timestamp": _ts(10, 0), "device": "pc", "app": "Browser", "active_state": "active"},
        {"timestamp": _ts(10, 1), "device": "pc", "app": "Browser", "active_state": "idle"},
    ]
    sensing_rows = [
        {"timestamp": _ts(12, 0), "type": "sensor", "data": {"screen_on": False}},
        # A second two-hour outage must not turn one screen sample into two hours of rest.
        {"timestamp": _ts(14, 0), "type": "sensor", "data": {"screen_on": False}},
    ]

    store = _reconcile(
        tmp_path,
        activity_rows=activity_rows,
        sensing_rows=sensing_rows,
    )
    payload = store.fetch(DAY.isoformat())["payload"]
    activity = payload["activity"]

    assert activity["active_seconds"] == 180
    assert activity["app_seconds"] == {"Browser": 60, "Editor": 120}
    assert activity["longest_observed_rest"]["seconds"] == 300
    assert "08:10" not in activity["bins"]
    assert "09:50" not in activity["bins"]
    assert payload["coverage"]["pc"]["covered_bins"] == ["08:00", "10:00"]
    assert payload["coverage"]["phone_sensing"]["covered_bins"] == ["12:00", "14:00"]
    assert "category" not in json.dumps(activity, ensure_ascii=False).lower()


def test_biometrics_use_source_time_dedup_and_keep_zero_steps(tmp_path):
    sensing_rows = [
        {"timestamp": _ts(1, 0), "type": "biometric", "data": {"sleep_stage": "deep"}},
        {"timestamp": _ts(1, 5), "type": "biometric", "data": {"sleep_stage": "deep"}},
        {"timestamp": _ts(9, 0), "type": "sensor", "data": {"motion": "still", "wifi_ssid": "Home"}},
        {"timestamp": _ts(9, 3), "type": "notification", "data": {"app": "微信"}},
        {"timestamp": _ts(9, 4), "type": "unlock", "data": {}},
        {
            "timestamp": _ts(9, 6),
            "type": "biometric",
            "data": {
                "heart_rate": 61,
                "heart_rate_observed_at": _ts(9, 1),
                "spo2": 98,
                "spo2_observed_at": _ts(9, 2),
            },
        },
        # The same old source samples arrive again; neither count nor coverage grows.
        {
            "timestamp": _ts(9, 16),
            "type": "biometric",
            "data": {
                "heart_rate": 61,
                "heart_rate_observed_at": _ts(9, 1),
                "spo2": 98,
                "spo2_observed_at": _ts(9, 2),
            },
        },
        {"timestamp": _ts(9, 20), "type": "biometric", "data": {"stress": 77}},
        {
            "timestamp": _ts(23, 55),
            "type": "biometric",
            "data": {
                "steps_total_today": 0,
                "steps_total_date": DAY.isoformat(),
                "steps_total_timezone": ZONE_NAME,
            },
        },
    ]

    store = _reconcile(tmp_path, sensing_rows=sensing_rows)
    # Repeating the same reconciliation must update, not append, the day row.
    _reconcile(tmp_path, sensing_rows=sensing_rows, store=store)
    assert store.count() == 1

    payload = store.fetch(DAY.isoformat())["payload"]
    biometrics = payload["biometrics"]
    assert biometrics["heart_rate"]["sample_count"] == 1
    assert biometrics["heart_rate"]["resting_estimate_bpm"] == 61
    assert biometrics["spo2"]["sample_count"] == 1
    assert biometrics["sleep_stage_minutes"] == {"deep": 10.0}
    assert biometrics["steps_total_today"] == 0
    assert biometrics["steps_total_date"] == DAY.isoformat()
    assert biometrics["steps_total_timezone"] == ZONE_NAME
    assert "stress" not in json.dumps(biometrics).lower()
    assert payload["environment"] == {"wifi_ssids": ["Home"]}
    assert set(payload["coverage"]) == {"pc", "phone_sensing", "biometric", "location"}
    assert payload["coverage"]["phone_sensing"]["covered_bins"] == ["09:00"]
    assert payload["coverage"]["biometric"]["covered_bins"] == [
        "01:00",
        "09:00",
        "23:50",
    ]


def test_missing_biometrics_stay_blank(tmp_path):
    store = _reconcile(
        tmp_path,
        sensing_rows=[
            {"timestamp": _ts(9, 0), "type": "sensor", "data": {"motion": "walking"}},
            {"timestamp": _ts(9, 1), "type": "biometric", "data": {"stress": 50}},
        ],
    )
    biometrics = store.fetch(DAY.isoformat())["payload"]["biometrics"]
    assert biometrics == {
        "heart_rate": None,
        "sleep_stage_minutes": {},
        "spo2": None,
        "steps_total_date": None,
        "steps_total_timezone": None,
        "steps_total_today": None,
    }


def test_biometric_api_distinguishes_zero_steps_from_missing():
    present = BiometricTick(
        steps_total_today=0,
        steps_total_date=DAY.isoformat(),
        steps_total_timezone=ZONE_NAME,
    ).model_dump(exclude_none=True)
    missing = BiometricTick().model_dump(exclude_none=True)

    assert present["steps_total_today"] == 0
    assert "steps_total_today" not in missing


def test_location_heartbeat_merge_is_monotonic_and_survives_reconcile(tmp_path):
    store = DailySignalStore(tmp_path / "daily.db")
    activity_dir = tmp_path / "activity"
    sensing_dir = tmp_path / "sensing"
    activity_dir.mkdir()
    sensing_dir.mkdir()

    # Heartbeat may win the race before the batch row exists.
    record_location_heartbeat_safely(
        {"state": "outside", "heartbeat_received_at": _ts(9, 3)},
        store=store,
        timezone_name=ZONE_NAME,
    )
    reconcile_daily_signals(
        store=store,
        activity_logs_dir=activity_dir,
        sensing_logs_dir=sensing_dir,
        timezone_name=ZONE_NAME,
        now=_ts(10, 0),
        dates=(DAY,),
    )
    # A later at-home heartbeat cannot erase the fact that Owner left that day.
    record_location_heartbeat_safely(
        {"state": "at_home", "heartbeat_received_at": _ts(10, 3)},
        store=store,
        timezone_name=ZONE_NAME,
    )
    # Batch reconciliation may also win later; it must preserve heartbeat fields.
    reconcile_daily_signals(
        store=store,
        activity_logs_dir=activity_dir,
        sensing_logs_dir=sensing_dir,
        timezone_name=ZONE_NAME,
        now=_ts(10, 4),
        dates=(DAY,),
    )

    payload = store.fetch(DAY.isoformat())["payload"]
    assert payload["location"] == {
        "last_observed_at": _ts(10, 3),
        "left_usual_place": True,
        "states_observed": ["at_home", "outside"],
    }
    assert payload["coverage"]["location"]["covered_bins"] == ["09:00", "10:00"]
    assert store.count() == 1


def test_location_and_batch_updates_are_field_atomic_under_concurrency(tmp_path):
    store = DailySignalStore(tmp_path / "daily.db")
    barrier = Barrier(2)
    reconciled = {
        "activity": {
            "first_active_at": None,
            "last_active_at": None,
            "active_seconds": 60,
            "app_seconds": {"Editor": 60},
            "longest_observed_rest": None,
            "bins": {},
        },
        "biometrics": {
            "heart_rate": None,
            "spo2": None,
            "sleep_stage_minutes": {},
            "steps_total_today": None,
            "steps_total_date": None,
            "steps_total_timezone": None,
        },
        "environment": {"wifi_ssids": []},
        "coverage": {
            source: {"covered_bins": [], "covered_count": 0, "total_bins": 144, "ratio": 0.0}
            for source in ("pc", "phone_sensing", "biometric")
        },
    }

    def merge_batch():
        barrier.wait()
        store.merge_reconciled(DAY.isoformat(), ZONE_NAME, reconciled)

    def merge_location():
        barrier.wait()
        store.merge_location_heartbeat(
            DAY.isoformat(),
            ZONE_NAME,
            bin_label="09:00",
            observed_at=_ts(9, 3),
            state="outside",
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(merge_batch), executor.submit(merge_location)]
        for future in futures:
            future.result()

    payload = store.fetch(DAY.isoformat())["payload"]
    assert payload["activity"]["app_seconds"] == {"Editor": 60}
    assert payload["location"]["left_usual_place"] is True
    assert payload["coverage"]["location"]["covered_bins"] == ["09:00"]


def test_startup_reconcile_backfills_calendar_gaps(tmp_path):
    store = DailySignalStore(tmp_path / "daily.db")
    activity_dir = tmp_path / "activity"
    sensing_dir = tmp_path / "sensing"
    activity_dir.mkdir()
    sensing_dir.mkdir()
    old_day = DAY - timedelta(days=2)

    reconcile_daily_signals(
        store=store,
        activity_logs_dir=activity_dir,
        sensing_logs_dir=sensing_dir,
        timezone_name=ZONE_NAME,
        now=_ts(12),
        dates=(old_day,),
    )
    old_payload = store.fetch(old_day.isoformat())["payload"]
    old_payload["activity"]["app_seconds"] = {"HistoricalApp": 600}
    store.merge_reconciled(
        old_day.isoformat(),
        ZONE_NAME,
        {
            "activity": old_payload["activity"],
            "biometrics": old_payload["biometrics"],
            "environment": old_payload["environment"],
            "coverage": {
                source: old_payload["coverage"][source]
                for source in ("pc", "phone_sensing", "biometric")
            },
        },
    )
    reconcile_daily_signals(
        store=store,
        activity_logs_dir=activity_dir,
        sensing_logs_dir=sensing_dir,
        timezone_name=ZONE_NAME,
        now=_ts(12),
    )

    assert store.list_dates() == [
        old_day.isoformat(),
        (DAY - timedelta(days=1)).isoformat(),
        DAY.isoformat(),
    ]
    assert store.fetch(old_day.isoformat())["payload"]["activity"]["app_seconds"] == {
        "HistoricalApp": 600
    }


def test_periodic_loop_reconciles_new_day_without_restart(monkeypatch):
    calls = []

    class FrozenDatetime:
        @classmethod
        def now(cls, timezone):
            return datetime(2026, 8, 15, 0, 1, tzinfo=timezone)

    async def no_wait(_seconds):
        return None

    async def capture_to_thread(_func, **kwargs):
        calls.append(kwargs)
        raise asyncio.CancelledError

    monkeypatch.setattr(daily_runtime, "datetime", FrozenDatetime)
    monkeypatch.setattr(daily_runtime, "daily_timezone_name", lambda: ZONE_NAME)
    monkeypatch.setattr(daily_runtime.asyncio, "sleep", no_wait)
    monkeypatch.setattr(daily_runtime.asyncio, "to_thread", capture_to_thread)

    try:
        asyncio.run(daily_runtime.run_daily_signal_reconcile_loop(interval_sec=1))
    except asyncio.CancelledError:
        pass

    assert calls == [
        {
            "timezone_name": ZONE_NAME,
            "dates": (date(2026, 8, 14), date(2026, 8, 15)),
        }
    ]
