import copy
import json
import sqlite3
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from app.context_delivery import build_context_delivery_projection, render_context_delivery_projection
from app.daily_signals.aggregation import aggregate_daily_signals, reconcile_daily_signals
from app.daily_signals.baseline import DailySignalBaselineProvider
from app.daily_signals.store import DailySignalStore
from app.daily_signals.usage import DEVICE_USAGE_SCHEMA, compare_device_usage
from app.events import EvidenceLedger
from context_delivery_runtime_readers import read_context_delivery_projection


ZONE = ZoneInfo("Asia/Shanghai")
TODAY = date(2026, 9, 10)


def _ts(day=TODAY, hour=14, minute=0, zone=ZONE):
    return datetime.combine(day, datetime.min.time(), zone).replace(hour=hour, minute=minute).timestamp()


def _entries(day, *, device="pc", identity=None, active=True, app="Editor", skip_bins=(), zone=ZONE):
    rows = []
    step = 60 if device == "pc" else 300
    for offset in range(0, 7200, step):
        if offset // 600 in skip_bins:
            continue
        rows.append({
            "timestamp": _ts(day, 12, zone=zone) + offset, "device": device,
            "device_id": identity or f"{device}-one",
            "app": app if active else (None if device == "pc" else "screen_off"),
            "active_state": "active" if active else "idle",
        })
    return rows


def _row(day, entries=None, *, zone=ZONE, **kwargs):
    payload = aggregate_daily_signals(
        day, zone, activity_entries=_entries(day, zone=zone, **kwargs) if entries is None else entries,
        sensing_entries=[], reference_time=_ts(day, zone=zone),
    )
    return {"local_date": day.isoformat(), "timezone": zone.key, "payload": payload}


def _history(*, today=TODAY, n=8, **kwargs):
    days = [today - timedelta(days=delta) for delta in range(1, 31)]
    days = [day for day in days if (day.weekday() < 5) == (today.weekday() < 5)][:n]
    return [_row(day, **kwargs) for day in days]


def _compare(rows, *, reference_time=None, zone=ZONE):
    return compare_device_usage(rows, reference_time=_ts() if reference_time is None else reference_time, timezone=zone)


def _streams(row):
    return row["payload"]["device_usage"]["streams"]


def _store(tmp_path, rows):
    store = DailySignalStore(tmp_path / "daily.db", now=lambda: _ts())
    for row in rows:
        store.merge_reconciled(row["local_date"], row["timezone"], row["payload"])
    return store


def test_separate_devices_preserve_combined_legacy_statistics():
    row = _row(TODAY, _entries(TODAY) + _entries(TODAY, device="phone"))
    assert row["payload"]["activity"]["active_seconds"] == 7200  # old union stays unchanged
    assert row["payload"]["device_usage"]["schema_version"] == DEVICE_USAGE_SCHEMA
    assert len(_streams(row)) == 2
    for stream in _streams(row).values():
        assert sum(value["active_seconds"] for value in stream["bins"].values()) == 7200
        assert all(value["observed_seconds"] == 600 for value in stream["bins"].values())


def test_outage_unknown_conflict_and_future_never_become_known_time():
    start = _ts(TODAY, 12)
    rows = [
        {"timestamp": start, "device": "pc", "app": "Editor", "active_state": "active"},
        {"timestamp": start + 120, "device": "pc", "app": "Editor", "active_state": "active"},
        {"timestamp": start + 150, "device": "pc", "active_state": "unknown"},
        {"timestamp": start + 300, "device": "pc", "active_state": "active", "app": "Editor"},
        {"timestamp": start + 300, "device": "pc", "active_state": "locked"},
        {"timestamp": _ts(TODAY, 15), "device": "pc", "active_state": "active", "app": "Future"},
    ]
    first = _row(TODAY, rows)
    second = _row(TODAY, list(reversed(rows)))
    assert _streams(first) == _streams(second)
    bins = next(iter(_streams(first).values()))["bins"]
    assert list(bins) == ["12:00"]
    assert bins["12:00"]["observed_seconds"] == 90
    assert bins["12:00"]["active_seconds"] == 90


def test_notification_sensor_and_location_cannot_cover_application_history():
    payload = aggregate_daily_signals(TODAY, ZONE, activity_entries=[], sensing_entries=[
        {"timestamp": _ts(TODAY, 12) + index * 60, "type": kind, "data": {"screen_on": True}}
        for index in range(120) for kind in ("sensor", "notification", "unlock")
    ])
    assert payload["coverage"]["phone_sensing"]["covered_count"] == 12
    assert payload["device_usage"]["streams"] == {}


def test_mobile_hardware_type_and_legacy_identity_are_not_silently_merged():
    entries = _entries(TODAY, device="phone", identity="tablet-one")
    for entry in entries:
        entry["device_type"] = "tablet"
    (stream,) = _streams(_row(TODAY, entries)).values()
    assert stream["device"] == "tablet" and stream["identity_scope"] == "device"
    legacy = _entries(TODAY, device="phone", active=False)
    for entry in legacy:
        entry.pop("device_id")
    assert _compare(_history(device="phone") + [_row(TODAY, legacy)]) == ()


def test_coverage_requires_a_real_sample_not_only_an_interpolated_interval():
    entries = [
        {"timestamp": _ts(TODAY, 12, 59) + 30, "device": "pc", "active_state": "active", "app": "Editor"},
    ]
    (stream,) = _streams(_row(TODAY, entries)).values()
    assert stream["bins"]["13:00"]["observed_seconds"] == 30
    assert stream["bins"]["13:00"]["sample_count"] == 0
    assert stream["bins"]["13:00"]["last_observed_at"] is None


def test_device_app_change_uses_same_hardware_and_own_history():
    rows = _history(device="phone", app="Editor") + [_row(TODAY, device="phone", app="Reader")]
    (value,) = _compare(rows)
    assert value.metric == "app" and value.app in {"Editor", "Reader"}
    assert value.device == "phone" and value.identity_scope == "device"
    assert len(value.baseline_dates) == 8
    assert value.margin_seconds == 7200 and value.current_coverage == 1
    rows[-1] = _row(TODAY, device="phone", identity="phone-two", app="Reader")
    assert _compare(rows) == ()


@pytest.mark.parametrize("device", ["pc", "phone", "tablet"])
def test_known_zero_can_deviate_but_missing_is_never_zero(device):
    history = _history(device=device)
    (value,) = _compare(history + [_row(TODAY, device=device, active=False)])
    assert value.current_seconds == 0 and value.direction == "less"
    assert _compare(history + [_row(TODAY, [])]) == ()


def test_thin_history_wrong_day_group_timezone_and_old_mixed_rows_are_blank():
    current = _row(TODAY, active=False)
    assert _compare(_history(n=6) + [current]) == ()
    history = _history()
    for row in history[:2]:
        row["timezone"] = "UTC"
    assert _compare(history + [current]) == ()
    history = _history()
    for row in history:
        row["payload"].pop("device_usage")
    assert _compare(history + [current]) == ()
    saturday = date(2026, 9, 12)
    assert _compare(_history() + [_row(saturday, active=False)], reference_time=_ts(saturday)) == ()
    assert len(_compare(_history(today=saturday) + [_row(saturday, active=False)], reference_time=_ts(saturday))) == 1


@pytest.mark.parametrize("historical", [True, False])
def test_source_coverage_gates_current_and_each_historical_window(historical):
    rows = _history(skip_bins=(0, 1, 2)) if historical else _history()
    rows += [_row(TODAY, active=False, skip_bins=() if historical else (0, 1, 2))]
    # Global coverage claims from other sources must not rescue this window.
    for row in rows:
        row["payload"]["coverage"] = {"phone_sensing": {"ratio": 1}, "pc": {"ratio": 1}}
    assert _compare(rows) == ()


def test_missing_time_cannot_manufacture_a_deviation_at_80_percent_coverage():
    # Ten observed bins are 83% of the window. The 20 missing minutes could all
    # have been active, so 100 vs 120 is not an established reduction.
    assert _compare(_history() + [_row(TODAY, skip_bins=(0, 1))]) == ()
    # Conversely, history missing those minutes cannot manufacture an increase.
    assert _compare(_history(skip_bins=(0, 1)) + [_row(TODAY)]) == ()


def test_no_difference_or_large_personal_variation_produces_no_statement():
    assert _compare(_history() + [_row(TODAY)]) == ()
    history = _history()
    for row in history[:4]:
        day = date.fromisoformat(row["local_date"])
        row["payload"] = _row(day, active=False)["payload"]
    assert _compare(history + [_row(TODAY, active=False)]) == ()


def test_app_coverage_does_not_inherit_active_state_without_an_app():
    history = _history()
    current = _row(TODAY, app=None)
    stream = next(iter(_streams(current).values()))
    assert all(value["app_observed_seconds"] == 0 for value in stream["bins"].values())
    assert _compare(history + [current]) == ()


@pytest.mark.parametrize("field,bad", [
    ("active_seconds", 601), ("observed_seconds", float("nan")),
    ("app_seconds", {"Invented": 601}), ("last_observed_at", _ts(TODAY, 15)),
    ("sample_count", -1),
])
def test_malformed_bin_never_reaches_model(field, bad):
    current = _row(TODAY, active=False)
    next(iter(_streams(current).values()))["bins"]["12:00"][field] = bad
    assert _compare(_history() + [current]) == ()


def test_stale_partial_current_bin_midnight_and_dst_do_not_fake_a_window():
    history = _history()
    assert _compare(history + [_row(TODAY, active=False, skip_bins=(11,))]) == ()
    rows = history + [_row(TODAY, active=False)]
    assert _compare(rows, reference_time=_ts(TODAY, 1)) == ()
    # 13:59 does not consume the not-yet-finished 13:50 bin or jump to 14:00.
    (value,) = _compare(rows, reference_time=_ts(TODAY, 13, 59))
    assert value.window_end.hour == 13 and value.window_end.minute == 50
    # Even a malformed future bin cannot affect a completed window.
    next(iter(_streams(rows[-1]).values()))["bins"]["13:50"] = {"bad_future_bin": True}
    assert _compare(rows, reference_time=_ts(TODAY, 13, 59)) == (value,)
    la = ZoneInfo("America/Los_Angeles")
    for day in (date(2026, 3, 8), date(2026, 11, 1)):
        dst_rows = _history(today=day, zone=la) + [_row(day, active=False, zone=la)]
        assert _compare(dst_rows, reference_time=_ts(day, zone=la), zone=la) == ()


def test_read_only_provider_is_traceable_and_honors_renderer_names(tmp_path):
    rows = _history(device="phone", app="Editor") + [_row(TODAY, device="phone", app="Reader")]
    store = _store(tmp_path, rows)
    before = store.db_path.read_bytes()
    provider = DailySignalBaselineProvider(store=store, timezone_name=ZONE.key)
    (deviation,) = provider.deviations(reference_time=_ts())
    assert store.db_path.read_bytes() == before
    assert deviation.source == "daily_signals.activity" and deviation.coverage == 1
    assert "2026-09-10 12:00–14:00" in deviation.sample_window
    for row in rows[:-1]:
        assert row["local_date"] in deviation.sample_window
    projection = build_context_delivery_projection([], reference_time=_ts(), baseline_provider=provider)
    text = render_context_delivery_projection(projection, user_name="小栀", ai_name="阿澈")
    assert "个人基线偏离" in text and "不能据此判断小栀正在做什么" in text
    assert not any(word in text for word in ("用户", "Owner", "她", "睡觉", "上课", "正常人", "lux", "SSID"))


def test_missing_broken_or_locked_db_cannot_create_files_or_break_current(tmp_path, monkeypatch):
    missing = DailySignalStore(tmp_path / "missing" / "daily.db")
    assert DailySignalBaselineProvider(store=missing).deviations(reference_time=_ts()) == ()
    assert not missing.db_path.parent.exists()
    broken = DailySignalStore(tmp_path / "broken.db")
    broken.db_path.write_bytes(b"not a sqlite database")
    assert DailySignalBaselineProvider(store=broken).deviations(reference_time=_ts()) == ()
    store = _store(tmp_path, _history() + [_row(TODAY, active=False)])
    monkeypatch.setattr(store, "read_range", lambda *_: (_ for _ in ()).throw(sqlite3.OperationalError("locked")))
    provider = DailySignalBaselineProvider(store=store, timezone_name=ZONE.key)
    ledger = EvidenceLedger(now=lambda: _ts())
    ledger.record(kind="sensing.sensor", source="android.sensing", observed_at=_ts()-1, payload={"screen_on": True})
    projection = read_context_delivery_projection(
        reference_time=_ts(), ledger=ledger, baseline_provider=provider,
        activity_enabled_loader=lambda: False, location_config_loader=lambda: {"enabled": False},
    )
    assert projection.baseline_deviations == ()
    assert any(item.key == "phone.screen" for item in projection.observations)


def test_runtime_default_provider_and_switch_preserve_current_evidence(tmp_path, monkeypatch):
    import config
    import app.daily_signals.baseline as baseline
    rows = _history() + [_row(TODAY, active=False)]
    store = _store(tmp_path, rows)
    monkeypatch.setattr(baseline, "get_default_store", lambda: store)
    monkeypatch.setattr(baseline, "daily_timezone_name", lambda: ZONE.key)
    behavior = {"context_delivery_baseline_enabled": True}
    monkeypatch.setattr(config, "load_ai_behavior", lambda: behavior)
    ledger = EvidenceLedger(now=lambda: _ts())
    ledger.record(kind="sensing.sensor", source="android.sensing", observed_at=_ts()-1, payload={"screen_on": False})
    kwargs = dict(reference_time=_ts(), ledger=ledger, activity_enabled_loader=lambda: False, location_config_loader=lambda: {"enabled": False})
    enabled = read_context_delivery_projection(**kwargs)
    assert len(enabled.baseline_deviations) == 1
    behavior["context_delivery_baseline_enabled"] = False
    disabled = read_context_delivery_projection(**kwargs)
    assert disabled.baseline_deviations == () and enabled.observations == disabled.observations


def test_startup_backfills_new_domain_without_erasing_expired_history(tmp_path):
    old = _row(date(2026, 8, 1))
    recent = _row(TODAY - timedelta(days=1))
    store = _store(tmp_path, [old, recent])
    raw = tmp_path / "activity"
    raw.mkdir()
    entries = _entries(TODAY - timedelta(days=1), device="phone", app="Reader")
    (raw / "retained.jsonl").write_text("".join(json.dumps(row) + "\n" for row in entries), encoding="utf-8")
    reconcile_daily_signals(store=store, activity_logs_dir=raw, sensing_logs_dir=tmp_path / "sensing", timezone_name=ZONE.key, now=_ts())
    assert store.fetch(old["local_date"])["payload"]["device_usage"] == old["payload"]["device_usage"]
    recent_usage = store.fetch(recent["local_date"])["payload"]["device_usage"]
    assert {stream["device"] for stream in recent_usage["streams"].values()} == {"phone"}
    # An unrelated writer or legacy-shaped update cannot drop the new domain.
    legacy = copy.deepcopy(recent["payload"])
    legacy.pop("device_usage")
    store.merge_reconciled(recent["local_date"], ZONE.key, legacy)
    assert store.fetch(recent["local_date"])["payload"]["device_usage"] == recent_usage


def test_other_daily_writers_preserve_device_usage(tmp_path):
    store = _store(tmp_path, [_row(TODAY)])
    usage = store.fetch(TODAY.isoformat())["payload"]["device_usage"]
    store.merge_location_heartbeat(TODAY.isoformat(), ZONE.key, bin_label="14:00", observed_at=_ts(), state="outside")
    store.merge_biometrics(TODAY.isoformat(), ZONE.key, biometrics={"heart_rate": None}, coverage={"ratio": 0})
    assert store.fetch(TODAY.isoformat())["payload"]["device_usage"] == usage


def test_audit_reports_coverage_without_application_names():
    import importlib.util

    path = Path(__file__).parents[2] / "scripts" / "audit_device_usage_baseline.py"
    spec = importlib.util.spec_from_file_location("audit_device_baseline", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    result = module.audit(_history(app="private-app") + [_row(TODAY, active=False)], now=datetime.fromtimestamp(_ts(), ZONE))
    assert "private-app" not in json.dumps(result)
    assert result["streams"][0]["eligible_windows"]["active"] > 0
    assert "14:00" in result["streams"][0]["eligible_window_ends"]["active"]
    assert result["actual_deviation_count"] == 1


def test_baseline_switch_has_a_visible_settings_control():
    html = (Path(__file__).parents[1] / "static" / "settings.html").read_text(encoding="utf-8")
    assert 'id="deviceBaselineToggle"' in html
    assert 'context_delivery_baseline_enabled: enabled' in html
    assert 'renderDeviceBaselineConfig(!enabled)' in html
