from __future__ import annotations

import json
import math
import statistics
import time
from collections import defaultdict
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Iterable
from zoneinfo import ZoneInfo

from config import DATA_DIR

from .biometrics import (
    observations_to_sensing_entries,
    sensing_entry_dates,
    sensing_entry_to_observations,
)
from .config import daily_timezone_name
from .store import DailySignalStore, get_default_store


TOTAL_BINS = 24 * 60 // 10

# A sample describes at most one normal reporting period after it. A slightly
# late following sample may close the interval, but an outage never becomes
# activity/rest/application time.
PC_EXPECTED_SECONDS = 60
PC_MAX_FOLLOWING_GAP_SECONDS = 3 * 60
MOBILE_EXPECTED_SECONDS = 5 * 60
MOBILE_MAX_FOLLOWING_GAP_SECONDS = 10 * 60
BIOMETRIC_EXPECTED_SECONDS = 5 * 60
BIOMETRIC_MAX_FOLLOWING_GAP_SECONDS = 10 * 60

DEFAULT_ACTIVITY_LOGS_DIR = DATA_DIR / "activity_logs"
DEFAULT_SENSING_LOGS_DIR = DATA_DIR / "sensing_logs"

_SCREEN_TRANSITIONS = {"screen_on", "screen_off", "亮屏", "锁屏"}
_SLEEPING_STAGES = {"light", "deep", "rem", "sleeping"}


def reconcile_daily_signals(
    *,
    store: DailySignalStore | None = None,
    activity_logs_dir: Path | str | None = None,
    sensing_logs_dir: Path | str | None = None,
    timezone_name: str | None = None,
    now: float | None = None,
    dates: Iterable[str | date] | None = None,
) -> list[dict]:
    """Idempotently rebuild daily rows from retained raw JSONL signals.

    With ``dates=None`` this is the startup/backfill form: it rebuilds every
    retained/raw date and fills calendar gaps through today. The periodic
    runtime passes today and yesterday explicitly so a long-running process
    crosses midnight without repeatedly scanning every historical day.
    """

    target_store = store or get_default_store()
    zone_name = timezone_name or daily_timezone_name()
    timezone = ZoneInfo(zone_name)
    reference = time.time() if now is None else float(now)
    today = datetime.fromtimestamp(reference, timezone).date()

    activity_entries = _read_jsonl_directory(
        Path(activity_logs_dir or DEFAULT_ACTIVITY_LOGS_DIR)
    )
    sensing_path = Path(sensing_logs_dir or DEFAULT_SENSING_LOGS_DIR)
    sensing_entries = _read_jsonl_directory(sensing_path)
    _migrate_retained_biometrics(
        target_store,
        sensing_entries,
        timezone_name=zone_name,
        timezone=timezone,
    )
    activity_by_date = _group_entries_by_observed_date(activity_entries, timezone)
    sensing_by_date = _group_sensing_entries(sensing_entries, timezone)

    if dates is None:
        target_dates = _backfill_dates(
            today=today,
            raw_dates=set(activity_by_date) | set(sensing_by_date),
            stored_dates=target_store.list_dates(),
        )
    else:
        target_dates = sorted(
            {
                parsed
                for value in dates
                if (parsed := _parse_date(value)) is not None
            }
        )

    rows = []
    for target_date in target_dates:
        day_activity = activity_by_date.get(target_date, [])
        day_sensing = sensing_by_date.get(target_date, [])
        state = target_store.ensure_biometric_day_state(target_date.isoformat())
        canonical_entries = observations_to_sensing_entries(
            target_store.biometric_observations_for_date(
                target_date.isoformat(),
                zone_name,
            )
        )
        reconciled = aggregate_daily_signals(
            target_date,
            timezone,
            activity_entries=day_activity,
            sensing_entries=day_sensing,
            biometric_entries=canonical_entries,
        )
        rows.append(
            target_store.merge_reconciled(
                target_date.isoformat(),
                zone_name,
                reconciled,
                preserve_biometrics=state == "legacy_frozen",
            )
        )
    return rows


def reconcile_daily_biometrics(
    *,
    store: DailySignalStore | None = None,
    sensing_logs_dir: Path | str | None = None,
    timezone_name: str | None = None,
    dates: Iterable[str | date],
) -> dict:
    """Rebuild only biometrics and biometric coverage from durable source facts."""

    target_store = store or get_default_store()
    zone_name = timezone_name or daily_timezone_name()
    timezone = ZoneInfo(zone_name)
    sensing_entries = _read_jsonl_directory(
        Path(sensing_logs_dir or DEFAULT_SENSING_LOGS_DIR)
    )
    sensing_by_date = _group_sensing_entries(
        [entry for entry in sensing_entries if entry.get("type") == "sensor"],
        timezone,
    )
    target_dates = sorted(
        {
            parsed
            for value in dates
            if (parsed := _parse_date(value)) is not None
        }
    )
    reconciled_dates: list[str] = []
    deferred_dates: list[str] = []
    for target_date in target_dates:
        local_date = target_date.isoformat()
        state = target_store.ensure_biometric_day_state(local_date)
        if state == "legacy_frozen":
            deferred_dates.append(local_date)
            continue
        canonical_entries = observations_to_sensing_entries(
            target_store.biometric_observations_for_date(local_date, zone_name)
        )
        day_start, day_end = _day_bounds(target_date, timezone)
        biometrics, _environment, _phone_coverage, biometric_coverage = _aggregate_sensing(
            [*sensing_by_date.get(target_date, []), *canonical_entries],
            target_date,
            timezone,
            day_start,
            day_end,
        )
        target_store.merge_biometrics(
            local_date,
            zone_name,
            biometrics=biometrics,
            coverage=_coverage(biometric_coverage),
        )
        reconciled_dates.append(local_date)
    return {
        "reconciled_dates": reconciled_dates,
        "deferred_dates": deferred_dates,
    }


def aggregate_daily_signals(
    target_date: date,
    timezone: ZoneInfo,
    *,
    activity_entries: Iterable[dict],
    sensing_entries: Iterable[dict],
    biometric_entries: Iterable[dict] | None = None,
) -> dict:
    activity_entries = list(activity_entries)
    sensing_entries = list(sensing_entries)
    day_start, day_end = _day_bounds(target_date, timezone)
    activity, pc_coverage = _aggregate_activity(
        activity_entries,
        sensing_entries,
        timezone,
        day_start,
        day_end,
    )
    aggregation_sensing = sensing_entries
    if biometric_entries is not None:
        aggregation_sensing = [
            entry for entry in sensing_entries if entry.get("type") != "biometric"
        ] + list(biometric_entries)
    biometrics, environment, sensing_coverage, biometric_coverage = _aggregate_sensing(
        aggregation_sensing,
        target_date,
        timezone,
        day_start,
        day_end,
    )
    return {
        "activity": activity,
        "biometrics": biometrics,
        "environment": environment,
        "coverage": {
            "pc": _coverage(pc_coverage),
            "phone_sensing": _coverage(sensing_coverage),
            "biometric": _coverage(biometric_coverage),
        },
    }


def _aggregate_activity(
    entries: Iterable[dict],
    sensing_entries: Iterable[dict],
    timezone: ZoneInfo,
    day_start: float,
    day_end: float,
) -> tuple[dict, set[str]]:
    pc_coverage: set[str] = set()
    bins: dict[str, dict] = {}
    by_device: dict[str, list[dict]] = defaultdict(list)

    for entry in entries:
        timestamp = _timestamp(entry.get("timestamp"))
        if timestamp is None or not day_start <= timestamp < day_end:
            continue
        device = str(entry.get("device") or "unknown").strip().lower() or "unknown"
        device_key = str(entry.get("device_id") or device)
        normalized = dict(entry)
        normalized["timestamp"] = timestamp
        normalized["_device"] = device
        normalized["_device_key"] = device_key
        by_device[device_key].append(normalized)

        label = _bin_label(timestamp, timezone)
        if device == "pc":
            pc_coverage.add(label)
        detail = bins.setdefault(label, _empty_activity_bin())
        detail["sample_count"] += 1
        detail["observed_devices"].add(device_key)
        state = _activity_state(normalized)
        if state:
            detail["observed_states"].add(state)

    active_intervals: list[tuple[float, float]] = []
    inactive_intervals: list[tuple[float, float]] = []
    app_intervals: dict[str, list[tuple[float, float]]] = defaultdict(list)

    for device_entries in by_device.values():
        ordered = sorted(device_entries, key=lambda item: item["timestamp"])
        device = ordered[0]["_device"]
        if device == "pc":
            expected = PC_EXPECTED_SECONDS
            max_gap = PC_MAX_FOLLOWING_GAP_SECONDS
        else:
            expected = MOBILE_EXPECTED_SECONDS
            max_gap = MOBILE_MAX_FOLLOWING_GAP_SECONDS

        for index, entry in enumerate(ordered):
            start = entry["timestamp"]
            next_timestamp = (
                ordered[index + 1]["timestamp"]
                if index + 1 < len(ordered)
                else None
            )
            end = _bounded_sample_end(
                start,
                next_timestamp,
                expected_seconds=expected,
                max_following_gap_seconds=max_gap,
                upper_bound=day_end,
            )
            if end <= start:
                continue

            state = _activity_state(entry)
            if state == "active":
                interval = (start, end)
                active_intervals.append(interval)
                app = _activity_app(entry)
                if app:
                    app_intervals[app].append(interval)
            elif state in {"idle", "locked"}:
                inactive_intervals.append((start, end))

            for label, _seconds in _split_interval_by_bin(start, end, timezone):
                bins.setdefault(label, _empty_activity_bin())

    # The phone's periodic sensing tick continues while its screen is off, so
    # it can support an observed rest interval without extending the one-shot
    # screen_off activity event through an outage.
    screen_samples = []
    for entry in sensing_entries:
        if entry.get("type") != "sensor":
            continue
        timestamp = _timestamp(entry.get("timestamp"))
        data = entry.get("data") if isinstance(entry.get("data"), dict) else {}
        if (
            timestamp is not None
            and day_start <= timestamp < day_end
            and data.get("screen_on") is not None
        ):
            screen_samples.append(
                (timestamp, "screen_on" if data.get("screen_on") else "locked")
            )
    for start, end, state in _bounded_state_intervals(
        screen_samples,
        expected_seconds=MOBILE_EXPECTED_SECONDS,
        max_following_gap_seconds=MOBILE_MAX_FOLLOWING_GAP_SECONDS,
        upper_bound=day_end,
    ):
        if state == "locked":
            inactive_intervals.append((start, end))

    merged_active = _merge_intervals(active_intervals)
    active_seconds = _interval_seconds(merged_active)
    first_active = merged_active[0][0] if merged_active else None
    last_active = merged_active[-1][1] if merged_active else None

    merged_app_intervals = {
        app: _merge_intervals(intervals)
        for app, intervals in app_intervals.items()
    }
    app_seconds = {
        app: _rounded_seconds(_interval_seconds(intervals))
        for app, intervals in sorted(merged_app_intervals.items())
        if _interval_seconds(intervals) > 0
    }

    observed_rest = _subtract_intervals(
        _merge_intervals(inactive_intervals),
        merged_active,
    )
    longest_rest_interval = max(
        observed_rest,
        key=lambda interval: interval[1] - interval[0],
        default=None,
    )

    for label, detail in bins.items():
        bin_start, bin_end = _bin_bounds_for_label(label, day_start, timezone)
        detail["active_seconds"] = _rounded_seconds(
            _overlap_seconds(merged_active, bin_start, bin_end)
        )
        per_app = {}
        for app, intervals in merged_app_intervals.items():
            seconds = _overlap_seconds(intervals, bin_start, bin_end)
            if seconds > 0:
                per_app[app] = _rounded_seconds(seconds)
        detail["app_seconds"] = dict(sorted(per_app.items()))
        detail["observed_devices"] = sorted(detail["observed_devices"])
        detail["observed_states"] = sorted(detail["observed_states"])

    longest_rest = None
    if longest_rest_interval is not None:
        rest_start, rest_end = longest_rest_interval
        longest_rest = {
            "start_at": _iso(rest_start, timezone),
            "end_at": _iso(rest_end, timezone),
            "seconds": _rounded_seconds(rest_end - rest_start),
        }

    return (
        {
            "first_active_at": _iso(first_active, timezone),
            "last_active_at": _iso(last_active, timezone),
            "active_seconds": _rounded_seconds(active_seconds),
            "app_seconds": app_seconds,
            "longest_observed_rest": longest_rest,
            "bins": {label: bins[label] for label in sorted(bins)},
        },
        pc_coverage,
    )


def _aggregate_sensing(
    entries: Iterable[dict],
    target_date: date,
    timezone: ZoneInfo,
    day_start: float,
    day_end: float,
) -> tuple[dict, dict, set[str], set[str]]:
    phone_coverage: set[str] = set()
    biometric_coverage: set[str] = set()
    wifi_ssids: set[str] = set()
    motion_by_bin: dict[str, tuple[float, str]] = {}
    heart_rate_by_observed_at: dict[float, tuple[float, float]] = {}
    spo2_by_observed_at: dict[float, tuple[float, float]] = {}
    sleep_samples: list[tuple[float, str]] = []
    explicit_sleep_intervals: list[tuple[float, float, str]] = []
    steps_candidates: list[tuple[int, float, float, int, str]] = []

    ordered_entries = sorted(
        (entry for entry in entries if isinstance(entry, dict)),
        key=lambda entry: _timestamp(entry.get("timestamp")) or 0.0,
    )
    for entry in ordered_entries:
        report_at = _timestamp(entry.get("timestamp"))
        if report_at is None:
            continue
        entry_type = entry.get("type")
        data = entry.get("data") if isinstance(entry.get("data"), dict) else {}
        report_is_today = day_start <= report_at < day_end

        if entry_type == "sensor":
            if not report_is_today:
                continue
            label = _bin_label(report_at, timezone)
            phone_coverage.add(label)
            ssid = str(data.get("wifi_ssid") or "").strip()
            if ssid:
                wifi_ssids.add(ssid)
            motion = str(data.get("motion") or "").strip().lower()
            if motion:
                previous = motion_by_bin.get(label)
                if previous is None or report_at >= previous[0]:
                    motion_by_bin[label] = (report_at, motion)
            continue

        # Notifications and unlocks are intentionally excluded from coverage.
        if entry_type != "biometric":
            continue

        if data.get("heart_rate") is not None:
            observed_at = _timestamp(data.get("heart_rate_observed_at")) or report_at
            if day_start <= observed_at < day_end:
                value = _number(data.get("heart_rate"))
                if value is not None:
                    previous = heart_rate_by_observed_at.get(observed_at)
                    if previous is None or report_at >= previous[0]:
                        heart_rate_by_observed_at[observed_at] = (report_at, value)
                    biometric_coverage.add(_bin_label(observed_at, timezone))

        if data.get("spo2") is not None:
            observed_at = _timestamp(data.get("spo2_observed_at")) or report_at
            if day_start <= observed_at < day_end:
                value = _number(data.get("spo2"))
                if value is not None:
                    previous = spo2_by_observed_at.get(observed_at)
                    if previous is None or report_at >= previous[0]:
                        spo2_by_observed_at[observed_at] = (report_at, value)
                    biometric_coverage.add(_bin_label(observed_at, timezone))

        sleep_stage = str(data.get("sleep_stage") or "").strip().lower()
        sleep_start = _timestamp(data.get("sleep_start_at"))
        sleep_end = _timestamp(data.get("sleep_end_at"))
        if sleep_stage and sleep_start is not None and sleep_end is not None:
            clipped_start = max(day_start, sleep_start)
            clipped_end = min(day_end, sleep_end)
            if clipped_end > clipped_start:
                explicit_sleep_intervals.append(
                    (clipped_start, clipped_end, sleep_stage)
                )
                for label, _seconds in _split_interval_by_bin(
                    clipped_start,
                    clipped_end,
                    timezone,
                ):
                    biometric_coverage.add(label)
        elif sleep_stage and report_is_today:
            sleep_samples.append((report_at, sleep_stage))
            biometric_coverage.add(_bin_label(report_at, timezone))

        # The legacy overlapping delta remains prompt-only. It can establish
        # that the biometric source reported, but is never summed here.
        if (
            data.get("steps_delta") is not None
            or data.get("steps_total_reported") is True
        ) and report_is_today:
            biometric_coverage.add(_bin_label(report_at, timezone))

        steps_date = str(data.get("steps_total_date") or "").strip()
        steps_timezone = str(data.get("steps_total_timezone") or "").strip()
        if steps_date == target_date.isoformat() and data.get("steps_total_today") is not None:
            value = _integer(data.get("steps_total_today"))
            if value is not None and value >= 0 and steps_timezone:
                observed_at = _timestamp(data.get("steps_total_observed_at")) or report_at
                priority = 1 if data.get("steps_source_kind") == "steps_daily" else 0
                steps_candidates.append(
                    (priority, observed_at, report_at, value, steps_timezone)
                )
                if day_start <= observed_at < day_end:
                    biometric_coverage.add(_bin_label(observed_at, timezone))

    sleep_intervals = _bounded_state_intervals(
        sleep_samples,
        expected_seconds=BIOMETRIC_EXPECTED_SECONDS,
        max_following_gap_seconds=BIOMETRIC_MAX_FOLLOWING_GAP_SECONDS,
        upper_bound=day_end,
    )
    sleep_stage_seconds: dict[str, float] = defaultdict(float)
    sleeping_bins: set[str] = set()
    explicit_masks = _merge_intervals(
        (start, end) for start, end, _stage in explicit_sleep_intervals
    )
    for start, end, stage in explicit_sleep_intervals:
        sleep_stage_seconds[stage] += end - start
        if stage in _SLEEPING_STAGES:
            for label, _seconds in _split_interval_by_bin(start, end, timezone):
                sleeping_bins.add(label)
    for start, end, stage in sleep_intervals:
        # Explicit HC intervals are authoritative; legacy five-minute samples
        # only fill uncovered gaps.
        for gap_start, gap_end in _subtract_intervals([(start, end)], explicit_masks):
            sleep_stage_seconds[stage] += gap_end - gap_start
            if stage in _SLEEPING_STAGES:
                for label, _seconds in _split_interval_by_bin(
                    gap_start,
                    gap_end,
                    timezone,
                ):
                    sleeping_bins.add(label)

    heart_samples = sorted(
        (observed_at, value[1])
        for observed_at, value in heart_rate_by_observed_at.items()
    )
    resting_values = [
        value
        for observed_at, value in heart_samples
        if motion_by_bin.get(_bin_label(observed_at, timezone), (0, ""))[1] == "still"
        or _bin_label(observed_at, timezone) in sleeping_bins
    ]
    spo2_samples = sorted(
        (observed_at, value[1])
        for observed_at, value in spo2_by_observed_at.items()
    )

    heart_rate = None
    if heart_samples:
        values = [value for _observed_at, value in heart_samples]
        heart_rate = {
            "resting_estimate_bpm": _rounded_number(statistics.median(resting_values))
            if resting_values
            else None,
            "min_bpm": _rounded_number(min(values)),
            "max_bpm": _rounded_number(max(values)),
            "sample_count": len(values),
            "resting_sample_count": len(resting_values),
            "first_observed_at": _iso(heart_samples[0][0], timezone),
            "last_observed_at": _iso(heart_samples[-1][0], timezone),
        }

    spo2 = None
    if spo2_samples:
        values = [value for _observed_at, value in spo2_samples]
        spo2 = {
            "median_pct": _rounded_number(statistics.median(values)),
            "min_pct": _rounded_number(min(values)),
            "max_pct": _rounded_number(max(values)),
            "sample_count": len(values),
            "first_observed_at": _iso(spo2_samples[0][0], timezone),
            "last_observed_at": _iso(spo2_samples[-1][0], timezone),
        }

    steps_total = None
    steps_timezone = None
    if steps_candidates:
        _priority, _observed_at, _report_at, steps_total, steps_timezone = max(
            steps_candidates,
            key=lambda candidate: candidate[:3],
        )

    biometrics = {
        "heart_rate": heart_rate,
        "spo2": spo2,
        "sleep_stage_minutes": {
            stage: round(seconds / 60.0, 2)
            for stage, seconds in sorted(sleep_stage_seconds.items())
            if seconds > 0
        },
        "steps_total_today": steps_total,
        "steps_total_date": target_date.isoformat() if steps_total is not None else None,
        "steps_total_timezone": steps_timezone,
    }
    return (
        biometrics,
        {"wifi_ssids": sorted(wifi_ssids)},
        phone_coverage,
        biometric_coverage,
    )


def _group_entries_by_observed_date(
    entries: Iterable[dict],
    timezone: ZoneInfo,
) -> dict[date, list[dict]]:
    grouped: dict[date, list[dict]] = defaultdict(list)
    for entry in entries:
        timestamp = _timestamp(entry.get("timestamp"))
        if timestamp is not None:
            grouped[datetime.fromtimestamp(timestamp, timezone).date()].append(entry)
    return grouped


def _group_sensing_entries(
    entries: Iterable[dict],
    timezone: ZoneInfo,
) -> dict[date, list[dict]]:
    """Index each record by every source date it can contribute to."""

    grouped: dict[date, list[dict]] = defaultdict(list)
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        for local_date in sensing_entry_dates(entry, timezone):
            grouped[local_date].append(entry)
    return grouped


def _migrate_retained_biometrics(
    store: DailySignalStore,
    sensing_entries: Iterable[dict],
    *,
    timezone_name: str,
    timezone: ZoneInfo,
) -> None:
    """Idempotently seed the long-term fact table before raw-file cleanup."""

    sensing_entries = list(sensing_entries)
    observations = []
    for entry in sensing_entries:
        if isinstance(entry, dict):
            observations.extend(
                sensing_entry_to_observations(entry, timezone_name)
            )
    upsert = store.upsert_biometric_observations(
        observations,
        timezone_name,
        initialize_days=False,
    )
    sensing_by_date = _group_sensing_entries(sensing_entries, timezone)
    candidates = set(store.list_dates()) | set(upsert["affected_dates"])
    known_states = store.list_biometric_day_states()

    for local_date in sorted(candidates):
        if local_date in known_states:
            continue
        existing = store.fetch(local_date)
        if existing is None:
            store.set_biometric_day_state(local_date, "ready")
            continue

        parsed = _parse_date(local_date)
        if parsed is None or parsed not in sensing_by_date:
            store.set_biometric_day_state(local_date, "legacy_frozen")
            continue

        day_start, day_end = _day_bounds(parsed, timezone)
        raw_entries = sensing_by_date.get(parsed, [])
        raw_biometrics, _raw_env, _raw_phone, raw_coverage = _aggregate_sensing(
            raw_entries,
            parsed,
            timezone,
            day_start,
            day_end,
        )
        canonical_entries = observations_to_sensing_entries(
            store.biometric_observations_for_date(local_date, timezone_name)
        )
        sensor_entries = [
            entry for entry in raw_entries if entry.get("type") == "sensor"
        ]
        canonical_biometrics, _env, _phone, canonical_coverage = _aggregate_sensing(
            [*sensor_entries, *canonical_entries],
            parsed,
            timezone,
            day_start,
            day_end,
        )
        reproducible = (
            canonical_biometrics == raw_biometrics
            and _coverage(canonical_coverage) == _coverage(raw_coverage)
        )
        store.set_biometric_day_state(
            local_date,
            "ready" if reproducible else "legacy_frozen",
        )


def _backfill_dates(
    *,
    today: date,
    raw_dates: set[date],
    stored_dates: Iterable[str],
) -> list[date]:
    retained_raw_dates = {value for value in raw_dates if value <= today}
    retained_stored_dates = {
        parsed
        for value in stored_dates
        if (parsed := _parse_date(value)) is not None and parsed <= today
    }
    candidates = retained_raw_dates | retained_stored_dates
    if not candidates:
        return [today]
    start = min(candidates)
    calendar = (
        start + timedelta(days=offset)
        for offset in range((today - start).days + 1)
    )
    # Existing rows older than raw retention are the durable history. Never
    # rebuild them from an empty raw input. Recompute retained source dates and
    # insert only genuine calendar gaps.
    return [
        local_date
        for local_date in calendar
        if local_date in retained_raw_dates or local_date not in retained_stored_dates
    ]


def _read_jsonl_directory(directory: Path) -> list[dict]:
    entries = []
    if not directory.exists():
        return entries
    for path in sorted(directory.glob("*.jsonl")):
        try:
            handle = path.open("r", encoding="utf-8")
        except OSError:
            continue
        with handle:
            for line in handle:
                try:
                    value = json.loads(line)
                except (json.JSONDecodeError, TypeError):
                    continue
                if isinstance(value, dict):
                    entries.append(value)
    return entries


def _activity_state(entry: dict) -> str:
    device = entry.get("_device") or str(entry.get("device") or "").lower()
    app = str(entry.get("app") or "").strip()
    if device == "pc":
        explicit = str(entry.get("active_state") or "").strip().lower()
        if explicit in {"active", "idle", "locked"}:
            return explicit
        # Historical local-PC rows predate active_state but were only emitted
        # while a foreground app existed. Keep those usable without treating an
        # explicit "unknown" report as active.
        if not explicit and app:
            return "active"
        return "unknown"
    if app in {"screen_off", "锁屏"}:
        return "locked"
    if app in {"screen_on", "亮屏"} or not app:
        return "unknown"
    return "active"


def _activity_app(entry: dict) -> str | None:
    app = str(entry.get("app") or "").strip()
    if not app or app in _SCREEN_TRANSITIONS:
        return None
    return app


def _bounded_sample_end(
    start: float,
    following: float | None,
    *,
    expected_seconds: float,
    max_following_gap_seconds: float,
    upper_bound: float,
) -> float:
    end = start + expected_seconds
    if following is not None:
        gap = following - start
        if 0 < gap <= max_following_gap_seconds:
            end = following
    return min(end, upper_bound)


def _bounded_state_intervals(
    samples: Iterable[tuple[float, str]],
    *,
    expected_seconds: float,
    max_following_gap_seconds: float,
    upper_bound: float,
) -> list[tuple[float, float, str]]:
    by_timestamp = {}
    for timestamp, state in samples:
        by_timestamp[timestamp] = state
    ordered = sorted(by_timestamp.items())
    intervals = []
    for index, (start, state) in enumerate(ordered):
        following = ordered[index + 1][0] if index + 1 < len(ordered) else None
        end = _bounded_sample_end(
            start,
            following,
            expected_seconds=expected_seconds,
            max_following_gap_seconds=max_following_gap_seconds,
            upper_bound=upper_bound,
        )
        if end > start:
            intervals.append((start, end, state))
    return intervals


def _merge_intervals(intervals: Iterable[tuple[float, float]]) -> list[tuple[float, float]]:
    ordered = sorted((start, end) for start, end in intervals if end > start)
    if not ordered:
        return []
    merged = [ordered[0]]
    for start, end in ordered[1:]:
        previous_start, previous_end = merged[-1]
        if start <= previous_end:
            merged[-1] = (previous_start, max(previous_end, end))
        else:
            merged.append((start, end))
    return merged


def _subtract_intervals(
    intervals: Iterable[tuple[float, float]],
    subtractors: Iterable[tuple[float, float]],
) -> list[tuple[float, float]]:
    remaining = []
    masks = _merge_intervals(subtractors)
    for start, end in _merge_intervals(intervals):
        cursor = start
        for mask_start, mask_end in masks:
            if mask_end <= cursor:
                continue
            if mask_start >= end:
                break
            if mask_start > cursor:
                remaining.append((cursor, min(mask_start, end)))
            cursor = max(cursor, mask_end)
            if cursor >= end:
                break
        if cursor < end:
            remaining.append((cursor, end))
    return remaining


def _overlap_seconds(
    intervals: Iterable[tuple[float, float]],
    start: float,
    end: float,
) -> float:
    return sum(
        max(0.0, min(interval_end, end) - max(interval_start, start))
        for interval_start, interval_end in intervals
    )


def _interval_seconds(intervals: Iterable[tuple[float, float]]) -> float:
    return sum(end - start for start, end in intervals)


def _split_interval_by_bin(
    start: float,
    end: float,
    timezone: ZoneInfo,
) -> list[tuple[str, float]]:
    parts = []
    cursor = start
    while cursor < end:
        local = datetime.fromtimestamp(cursor, timezone)
        minute = (local.minute // 10) * 10
        bin_start_local = local.replace(minute=minute, second=0, microsecond=0)
        bin_end = (bin_start_local + timedelta(minutes=10)).timestamp()
        part_end = min(end, bin_end)
        if part_end <= cursor:
            break
        parts.append((_bin_label(cursor, timezone), part_end - cursor))
        cursor = part_end
    return parts


def _empty_activity_bin() -> dict:
    return {
        "sample_count": 0,
        "observed_devices": set(),
        "observed_states": set(),
        "active_seconds": 0,
        "app_seconds": {},
    }


def _coverage(bins: Iterable[str]) -> dict:
    covered = sorted(set(bins))
    return {
        "covered_bins": covered,
        "covered_count": len(covered),
        "total_bins": TOTAL_BINS,
        "ratio": round(len(covered) / TOTAL_BINS, 6),
    }


def _day_bounds(local_date: date, timezone: ZoneInfo) -> tuple[float, float]:
    start = datetime(
        local_date.year,
        local_date.month,
        local_date.day,
        tzinfo=timezone,
    )
    end = datetime(
        *(local_date + timedelta(days=1)).timetuple()[:3],
        tzinfo=timezone,
    )
    return start.timestamp(), end.timestamp()


def _bin_label(timestamp: float, timezone: ZoneInfo) -> str:
    local = datetime.fromtimestamp(timestamp, timezone)
    return f"{local.hour:02d}:{(local.minute // 10) * 10:02d}"


def _bin_bounds_for_label(
    label: str,
    day_start: float,
    timezone: ZoneInfo,
) -> tuple[float, float]:
    hour, minute = (int(value) for value in label.split(":", 1))
    start_local = datetime.fromtimestamp(day_start, timezone).replace(
        hour=hour,
        minute=minute,
        second=0,
        microsecond=0,
    )
    return start_local.timestamp(), (start_local + timedelta(minutes=10)).timestamp()


def _iso(timestamp: float | None, timezone: ZoneInfo) -> str | None:
    if timestamp is None:
        return None
    return datetime.fromtimestamp(timestamp, timezone).isoformat(timespec="seconds")


def _timestamp(value) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) else None


def _number(value) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) else None


def _integer(value) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed


def _rounded_number(value: float) -> int | float:
    rounded = round(float(value), 2)
    return int(rounded) if rounded.is_integer() else rounded


def _rounded_seconds(value: float) -> int:
    return max(0, int(round(value)))


def _parse_date(value) -> date | None:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None
