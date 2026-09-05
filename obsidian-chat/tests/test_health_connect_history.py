import json
import asyncio
import sqlite3
from datetime import date, datetime
from zoneinfo import ZoneInfo

import pytest

from app.daily_signals.aggregation import (
    reconcile_daily_biometrics,
    reconcile_daily_signals,
)
from app.daily_signals.store import DailySignalStore
from routes.sensing import (
    BiometricBatch,
    BiometricTick,
    BiometricTimezoneMismatch,
    ingest_biometric_batch,
    report_biometric,
    report_biometric_batch,
    shadow_biometric_tick,
)


ZONE_NAME = "America/Los_Angeles"
DEVICE_ZONE = "Asia/Shanghai"
ZONE = ZoneInfo(ZONE_NAME)
DAY = date(2026, 8, 14)


def _ts(day, hour, minute=0):
    return datetime(day.year, day.month, day.day, hour, minute, tzinfo=ZONE).timestamp()


def _batch(**overrides):
    payload = {
        "sent_at": _ts(DAY, 12),
        "device_timezone": DEVICE_ZONE,
        "heart_rate_records": [],
        "spo2_records": [],
        "sleep_sessions": [],
        "steps_daily": [],
    }
    payload.update(overrides)
    return BiometricBatch(**payload)


def _ingest(tmp_path, store, batch, audit=None):
    audit_rows = audit if audit is not None else []
    result = ingest_biometric_batch(
        batch,
        store=store,
        timezone_name=ZONE_NAME,
        audit_writer=audit_rows.extend,
        sensing_logs_dir=tmp_path / "sensing",
    )
    return result, audit_rows


def _empty_reconciled(*, marker="old"):
    empty_coverage = {
        "covered_bins": [],
        "covered_count": 0,
        "total_bins": 144,
        "ratio": 0.0,
    }
    return {
        "activity": {"marker": marker},
        "biometrics": {
            "heart_rate": None,
            "spo2": None,
            "sleep_stage_minutes": {},
            "steps_total_today": None,
            "steps_total_date": None,
            "steps_total_timezone": None,
        },
        "environment": {"marker": marker},
        "coverage": {
            "pc": {**empty_coverage, "marker": marker},
            "phone_sensing": {**empty_coverage, "marker": marker},
            "biometric": dict(empty_coverage),
        },
    }


def test_batch_keeps_every_heart_rate_sample_and_is_idempotent(tmp_path):
    store = DailySignalStore(tmp_path / "daily.db")
    samples = [
        {"observed_at": _ts(DAY, 9, minute), "bpm": 60 + minute}
        for minute in range(5)
    ]
    batch = _batch(
        heart_rate_records=[
            {
                "source_id": "hr-five-samples",
                "start_at": _ts(DAY, 8, 30),
                "end_at": _ts(DAY, 9, 30),
                "samples": samples,
            }
        ]
    )

    first, audit = _ingest(tmp_path, store, batch)
    before_row = store.fetch(DAY.isoformat())
    before = before_row["payload"]
    second, _ = _ingest(tmp_path, store, batch, audit)
    after_row = store.fetch(DAY.isoformat())
    after = after_row["payload"]

    assert first["received_count"] == 5
    assert first["affected_dates"] == [DAY.isoformat()]
    assert first["deferred_dates"] == []
    assert before["biometrics"]["heart_rate"]["sample_count"] == 5
    assert after == before
    assert after_row["updated_at"] == before_row["updated_at"]
    assert second["upserted_count"] == 0
    assert second["reconciled_dates"] == [DAY.isoformat()]
    assert len(store.list_biometric_observations()) == 1
    assert len(audit) == 10  # retries may duplicate audit rows, never canonical facts


def test_sleep_revision_reconciles_union_of_old_and_new_dates(tmp_path):
    store = DailySignalStore(tmp_path / "daily.db")
    next_day = date(2026, 8, 15)
    original = _batch(
        sleep_sessions=[
            {
                "source_id": "sleep-session",
                "start_at": _ts(DAY, 23),
                "end_at": _ts(next_day, 1),
                "stages": [
                    {
                        "start_at": _ts(DAY, 23),
                        "end_at": _ts(next_day, 1),
                        "stage": "deep",
                    }
                ],
            }
        ]
    )
    _ingest(tmp_path, store, original)

    revised = _batch(
        sleep_sessions=[
            {
                "source_id": "sleep-session",
                "start_at": _ts(next_day, 1),
                "end_at": _ts(next_day, 3),
                "stages": [
                    {
                        "start_at": _ts(next_day, 1),
                        "end_at": _ts(next_day, 3),
                        "stage": "rem",
                    }
                ],
            }
        ]
    )
    result, _ = _ingest(tmp_path, store, revised)

    assert result["affected_dates"] == [DAY.isoformat(), next_day.isoformat()]
    assert store.fetch(DAY.isoformat())["payload"]["biometrics"]["sleep_stage_minutes"] == {}
    assert store.fetch(next_day.isoformat())["payload"]["biometrics"]["sleep_stage_minutes"] == {
        "rem": 120.0
    }
    rows = store.list_biometric_observations()
    assert len(rows) == 1
    assert rows[0]["payload"]["start_at"] == _ts(next_day, 1)


def test_cross_midnight_sleep_is_clipped_by_server_timezone(tmp_path):
    store = DailySignalStore(tmp_path / "daily.db")
    next_day = date(2026, 8, 15)
    batch = _batch(
        sleep_sessions=[
            {
                "source_id": "overnight",
                "start_at": _ts(DAY, 23),
                "end_at": _ts(next_day, 7),
                "stages": [],
            }
        ]
    )

    _ingest(tmp_path, store, batch)

    first = store.fetch(DAY.isoformat())["payload"]
    second = store.fetch(next_day.isoformat())["payload"]
    assert first["biometrics"]["sleep_stage_minutes"] == {"sleeping": 60.0}
    assert second["biometrics"]["sleep_stage_minutes"] == {"sleeping": 420.0}
    assert first["coverage"]["biometric"]["covered_count"] == 6
    assert second["coverage"]["biometric"]["covered_count"] == 42


def test_explicit_sleep_replaces_overlapping_legacy_tick_minutes(tmp_path):
    store = DailySignalStore(tmp_path / "daily.db")
    _ingest(
        tmp_path,
        store,
        _batch(
            sleep_sessions=[
                {
                    "source_id": "explicit",
                    "start_at": _ts(DAY, 1),
                    "end_at": _ts(DAY, 2),
                    "stages": [
                        {
                            "start_at": _ts(DAY, 1),
                            "end_at": _ts(DAY, 2),
                            "stage": "deep",
                        }
                    ],
                }
            ]
        ),
    )
    shadow_biometric_tick(
        {
            "timestamp": _ts(DAY, 1, 5),
            "type": "biometric",
            "data": {"sleep_stage": "deep"},
        },
        store=store,
        timezone_name=ZONE_NAME,
    )
    reconcile_daily_biometrics(
        store=store,
        sensing_logs_dir=tmp_path / "sensing",
        timezone_name=ZONE_NAME,
        dates=(DAY,),
    )

    sleep = store.fetch(DAY.isoformat())["payload"]["biometrics"]["sleep_stage_minutes"]
    assert sleep == {"deep": 60.0}


def test_biometric_only_reconcile_preserves_other_domains_and_created_at(tmp_path):
    store = DailySignalStore(tmp_path / "daily.db", now=lambda: 100.0)
    store.merge_reconciled(DAY.isoformat(), ZONE_NAME, _empty_reconciled())
    store.set_biometric_day_state(DAY.isoformat(), "ready")
    before = store.fetch(DAY.isoformat())

    batch = _batch(
        heart_rate_records=[
            {
                "source_id": "late-heart",
                "start_at": _ts(DAY, 3),
                "end_at": _ts(DAY, 3, 1),
                "samples": [{"observed_at": _ts(DAY, 3), "bpm": 62}],
            }
        ]
    )
    _ingest(tmp_path, store, batch)
    after = store.fetch(DAY.isoformat())

    for field in ("activity", "environment", "location"):
        assert after["payload"].get(field) == before["payload"].get(field)
    for field in ("pc", "phone_sensing", "location"):
        assert after["payload"]["coverage"][field] == before["payload"]["coverage"][field]
    assert after["created_at"] == before["created_at"]
    assert after["payload"]["biometrics"]["heart_rate"]["sample_count"] == 1


def test_legacy_frozen_day_stores_fact_but_never_overwrites_summary(tmp_path):
    store = DailySignalStore(tmp_path / "daily.db")
    old = _empty_reconciled(marker="durable-history")
    old["biometrics"]["heart_rate"] = {"sample_count": 99}
    store.merge_reconciled(DAY.isoformat(), ZONE_NAME, old)
    store.set_biometric_day_state(DAY.isoformat(), "legacy_frozen")
    before = store.fetch(DAY.isoformat())

    result, _ = _ingest(
        tmp_path,
        store,
        _batch(
            heart_rate_records=[
                {
                    "source_id": "too-late",
                    "start_at": _ts(DAY, 3),
                    "end_at": _ts(DAY, 3, 1),
                    "samples": [{"observed_at": _ts(DAY, 3), "bpm": 62}],
                }
            ]
        ),
    )

    assert result["reconciled_dates"] == []
    assert result["deferred_dates"] == [DAY.isoformat()]
    assert store.fetch(DAY.isoformat())["payload"] == before["payload"]
    assert len(store.list_biometric_observations()) == 1


def test_steps_timezone_mismatch_is_explicit_and_zero_is_valid(tmp_path):
    store = DailySignalStore(tmp_path / "daily.db")
    wrong = _batch(
        steps_daily=[
            {
                "source_id": "steps-wrong-zone",
                "daily_date": DAY.isoformat(),
                "aggregation_timezone": DEVICE_ZONE,
                "total": 0,
                "observed_at": _ts(DAY, 23, 59),
            }
        ]
    )
    with pytest.raises(BiometricTimezoneMismatch):
        _ingest(tmp_path, store, wrong)
    assert store.list_biometric_observations() == []

    correct = _batch(
        steps_daily=[
            {
                "source_id": "steps-correct-zone",
                "daily_date": DAY.isoformat(),
                "aggregation_timezone": ZONE_NAME,
                "total": 0,
                "observed_at": _ts(DAY, 23, 59),
            }
        ]
    )
    result, _ = _ingest(tmp_path, store, correct)
    bio = store.fetch(DAY.isoformat())["payload"]["biometrics"]
    assert result["daily_timezone"] == ZONE_NAME
    assert bio["steps_total_today"] == 0
    assert bio["steps_total_timezone"] == ZONE_NAME


def test_periodic_full_reconcile_uses_canonical_biometrics(tmp_path):
    store = DailySignalStore(tmp_path / "daily.db")
    _ingest(
        tmp_path,
        store,
        _batch(
            heart_rate_records=[
                {
                    "source_id": "canonical-only",
                    "start_at": _ts(DAY, 8),
                    "end_at": _ts(DAY, 8, 1),
                    "samples": [{"observed_at": _ts(DAY, 8), "bpm": 65}],
                }
            ]
        ),
    )
    empty_activity = tmp_path / "activity"
    empty_sensing = tmp_path / "empty-sensing"
    empty_activity.mkdir()
    empty_sensing.mkdir()

    reconcile_daily_signals(
        store=store,
        activity_logs_dir=empty_activity,
        sensing_logs_dir=empty_sensing,
        timezone_name=ZONE_NAME,
        now=_ts(DAY, 23, 59),
        dates=(DAY,),
    )

    bio = store.fetch(DAY.isoformat())["payload"]["biometrics"]
    assert bio["heart_rate"]["sample_count"] == 1


def test_startup_migration_freezes_expired_raw_history(tmp_path):
    store = DailySignalStore(tmp_path / "daily.db")
    old = _empty_reconciled(marker="durable")
    old["biometrics"]["heart_rate"] = {"sample_count": 17, "min_bpm": 60}
    store.merge_reconciled(DAY.isoformat(), ZONE_NAME, old)
    before = store.fetch(DAY.isoformat())["payload"]
    # Simulate a database created by the previous release: summary exists, but
    # the new migration state table did not yet have a row for it.
    with sqlite3.connect(store.db_path) as conn:
        conn.execute("DELETE FROM biometric_canonical_days")
        conn.commit()
    empty_activity = tmp_path / "activity"
    empty_sensing = tmp_path / "sensing"
    empty_activity.mkdir()
    empty_sensing.mkdir()

    reconcile_daily_signals(
        store=store,
        activity_logs_dir=empty_activity,
        sensing_logs_dir=empty_sensing,
        timezone_name=ZONE_NAME,
        now=_ts(DAY, 23, 59),
    )

    assert store.biometric_day_state(DAY.isoformat()) == "legacy_frozen"
    assert store.fetch(DAY.isoformat())["payload"] == before


def test_startup_migration_imports_retained_ticks_as_ready(tmp_path):
    store = DailySignalStore(tmp_path / "daily.db")
    activity_dir = tmp_path / "activity"
    sensing_dir = tmp_path / "sensing"
    activity_dir.mkdir()
    sensing_dir.mkdir()
    (sensing_dir / f"{DAY.isoformat()}.jsonl").write_text(
        json.dumps(
            {
                "timestamp": _ts(DAY, 9),
                "type": "biometric",
                "data": {
                    "heart_rate": 63,
                    "heart_rate_observed_at": _ts(DAY, 8, 59),
                },
            }
        )
        + "\n",
        encoding="utf-8",
    )

    reconcile_daily_signals(
        store=store,
        activity_logs_dir=activity_dir,
        sensing_logs_dir=sensing_dir,
        timezone_name=ZONE_NAME,
        now=_ts(DAY, 23, 59),
    )

    assert store.biometric_day_state(DAY.isoformat()) == "ready"
    assert len(store.list_biometric_observations()) == 1
    assert store.fetch(DAY.isoformat())["payload"]["biometrics"]["heart_rate"][
        "sample_count"
    ] == 1


def test_retained_daily_step_reports_preserve_every_coverage_bin(tmp_path):
    store = DailySignalStore(tmp_path / "daily.db")
    activity_dir = tmp_path / "activity"
    sensing_dir = tmp_path / "sensing"
    activity_dir.mkdir()
    sensing_dir.mkdir()
    rows = [
        {
            "timestamp": _ts(DAY, 9, minute),
            "type": "biometric",
            "data": {
                "steps_total_today": 1146,
                "steps_total_date": DAY.isoformat(),
                "steps_total_timezone": ZONE_NAME,
            },
        }
        for minute in (1, 11)
    ]
    (sensing_dir / f"{DAY.isoformat()}.jsonl").write_text(
        "".join(json.dumps(row) + "\n" for row in rows),
        encoding="utf-8",
    )

    reconcile_daily_signals(
        store=store,
        activity_logs_dir=activity_dir,
        sensing_logs_dir=sensing_dir,
        timezone_name=ZONE_NAME,
        now=_ts(DAY, 23, 59),
    )

    payload = store.fetch(DAY.isoformat())["payload"]
    assert payload["coverage"]["biometric"]["covered_bins"] == ["09:00", "09:10"]
    kinds = [row["source_kind"] for row in store.list_biometric_observations()]
    assert kinds.count("legacy_tick") == 2
    assert kinds.count("steps_daily") == 1


def test_reconcile_failure_keeps_canonical_upsert_retryable(monkeypatch, tmp_path):
    import routes.sensing as sensing_routes

    store = DailySignalStore(tmp_path / "daily.db")
    batch = _batch(
        spo2_records=[
            {"source_id": "spo2", "observed_at": _ts(DAY, 8), "percentage": 98}
        ]
    )

    original = sensing_routes.reconcile_daily_biometrics
    monkeypatch.setattr(
        sensing_routes,
        "reconcile_daily_biometrics",
        lambda **_kwargs: (_ for _ in ()).throw(RuntimeError("boom")),
    )
    with pytest.raises(RuntimeError, match="boom"):
        _ingest(tmp_path, store, batch)
    assert len(store.list_biometric_observations()) == 1

    monkeypatch.setattr(sensing_routes, "reconcile_daily_biometrics", original)
    result, _ = _ingest(tmp_path, store, batch)
    assert result["reconciled_dates"] == [DAY.isoformat()]
    assert store.fetch(DAY.isoformat())["payload"]["biometrics"]["spo2"]["sample_count"] == 1


def test_empty_batch_only_bootstraps_timezone(tmp_path):
    store = DailySignalStore(tmp_path / "daily.db")
    result, audit = _ingest(tmp_path, store, _batch())

    assert result == {
        "received_count": 0,
        "upserted_count": 0,
        "affected_dates": [],
        "reconciled_dates": [],
        "deferred_dates": [],
        "daily_timezone": ZONE_NAME,
    }
    assert audit == []
    assert store.list_dates() == []


def test_batch_route_never_records_live_evidence_or_broadcasts(monkeypatch):
    import routes.sensing as sensing_routes

    calls = []

    def fake_ingest(batch):
        calls.append(("ingest", batch))
        return {"ok": True}

    async def forbidden_broadcast(_payload):
        calls.append(("broadcast", None))

    async def inline_to_thread(func, *args, **kwargs):
        return func(*args, **kwargs)

    monkeypatch.setattr(sensing_routes, "ingest_biometric_batch", fake_ingest)
    monkeypatch.setattr(sensing_routes.asyncio, "to_thread", inline_to_thread)
    monkeypatch.setattr(
        sensing_routes,
        "record_sensing_entry_safely",
        lambda _entry: calls.append(("evidence", None)),
    )
    monkeypatch.setattr(sensing_routes.manager, "broadcast", forbidden_broadcast)

    assert asyncio.run(report_biometric_batch(_batch())) == {"ok": True}
    assert [name for name, _value in calls] == ["ingest"]


def test_legacy_tick_keeps_live_side_effects_and_shadow_upserts(monkeypatch, tmp_path):
    import routes.sensing as sensing_routes

    store = DailySignalStore(tmp_path / "daily.db")
    entry = {
        "timestamp": _ts(DAY, 9),
        "type": "biometric",
        "data": {
            "heart_rate": 61,
            "heart_rate_observed_at": _ts(DAY, 8, 59),
            "steps_delta": 0,
        },
    }
    evidence = []
    broadcasts = []

    monkeypatch.setattr(sensing_routes, "_store", lambda *_args, **_kwargs: entry)
    monkeypatch.setattr(
        sensing_routes,
        "shadow_biometric_tick",
        lambda value: shadow_biometric_tick(
            value,
            store=store,
            timezone_name=ZONE_NAME,
        ),
    )
    monkeypatch.setattr(sensing_routes, "record_sensing_entry_safely", evidence.append)

    async def capture(payload):
        broadcasts.append(payload)

    async def inline_to_thread(func, *args, **kwargs):
        return func(*args, **kwargs)

    monkeypatch.setattr(sensing_routes.manager, "broadcast", capture)
    monkeypatch.setattr(sensing_routes.asyncio, "to_thread", inline_to_thread)

    result = asyncio.run(report_biometric(BiometricTick(heart_rate=61, steps_delta=0)))

    assert result == {"ok": True}
    assert evidence == [entry]
    assert broadcasts == [{"type": "sensing_tick", "data": entry}]
    assert {row["source_kind"] for row in store.list_biometric_observations()} == {
        "heart_rate",
        "legacy_tick",
    }


def test_batch_validation_rejects_more_than_1000_leaves():
    samples = [
        {"observed_at": _ts(DAY, 9) + index, "bpm": 60}
        for index in range(1001)
    ]
    with pytest.raises(ValueError, match="1000"):
        _batch(
            heart_rate_records=[
                {
                    "source_id": "too-large",
                    "start_at": _ts(DAY, 8),
                    "end_at": _ts(DAY, 10),
                    "samples": samples,
                }
            ]
        )
