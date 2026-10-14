#!/usr/bin/env python3
"""Read-only CP4 readiness audit; no models, migrations, or production writes.

Read existing daily rows by default. --activity-stdin instead simulates the
new aggregation from JSONL activity reports (app/identity hashes suffice).
Only coverage counts are printed, never application names or raw reports.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "obsidian-chat"))

from app.daily_signals.aggregation import aggregate_daily_signals
from app.daily_signals.store import DailySignalStore
from app.daily_signals.usage import (
    LOOKBACK_DAYS, MIN_BASELINE_DAYS, MIN_COVERAGE, WINDOW_SECONDS,
    _ordinary_day, _streams, _valid_stream, _window, compare_device_usage,
)


def audit(rows: list[dict], *, now: datetime) -> dict:
    today = now.date()
    zone = now.tzinfo
    rows = [row for row in rows if row.get("timezone") == zone.key]
    streams = {key: value for row in rows for key, value in _streams(row).items() if _valid_stream(value)}
    result = []
    for index, (key, stream) in enumerate(sorted(streams.items())):
        windows = []
        for minutes in range(120, 1440, 10):
            clock_end = datetime.combine(today, datetime.min.time(), zone) + timedelta(minutes=minutes)
            clock_start = clock_end - timedelta(seconds=WINDOW_SECONDS)
            counts = {"active": 0, "app": 0}
            for row in rows:
                day = datetime.fromisoformat(row["local_date"]).date()
                if not 1 <= (today - day).days <= LOOKBACK_DAYS or (day.weekday() < 5) != (today.weekday() < 5):
                    continue
                if not _ordinary_day(day, zone):
                    continue
                historical = _streams(row).get(key)
                if not _valid_stream(historical) or any(historical.get(field) != stream.get(field) for field in ("source", "device", "device_id", "identity_scope")):
                    continue
                value = _window(historical, datetime.combine(day, clock_start.timetz()), datetime.combine(day, clock_end.timetz()))
                if value is None:
                    continue
                counts["active"] += value.observed_seconds / WINDOW_SECONDS >= MIN_COVERAGE
                counts["app"] += value.app_observed_seconds / WINDOW_SECONDS >= MIN_COVERAGE
            windows.append({"ending_at": clock_end.strftime("%H:%M"), **counts})
        result.append({
            "stream": f"{stream['device']}:{index + 1}",
            "identity_scope": stream["identity_scope"],
            "days_with_separate_data": sum(bool(_streams(row).get(key)) for row in rows),
            "eligible_windows": {metric: sum(item[metric] >= MIN_BASELINE_DAYS for item in windows) for metric in ("active", "app")},
            "eligible_window_ends": {metric: [item["ending_at"] for item in windows if item[metric] >= MIN_BASELINE_DAYS] for metric in ("active", "app")},
            "valid_days_by_two_hour_window_end": [item for item in windows if item["ending_at"][-2:] == "00"],
        })
    return {
        "as_of": now.isoformat(), "comparison_group": "weekday" if today.weekday() < 5 else "weekend",
        "required_days": MIN_BASELINE_DAYS, "required_coverage": MIN_COVERAGE,
        "row_count": len(rows), "streams": result,
        "actual_deviation_count": len(compare_device_usage(rows, reference_time=now.timestamp(), timezone=zone)),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--activity-stdin", action="store_true")
    parser.add_argument("--database", type=Path)
    parser.add_argument("--timezone", default="Asia/Shanghai")
    parser.add_argument("--as-of", help="ISO datetime; defaults to now in --timezone")
    args = parser.parse_args()
    zone = ZoneInfo(args.timezone)
    now = datetime.fromisoformat(args.as_of) if args.as_of else datetime.now(zone)
    now = now.replace(tzinfo=zone) if now.tzinfo is None else now.astimezone(zone)
    first = now.date() - timedelta(days=LOOKBACK_DAYS)
    if args.activity_stdin:
        entries = defaultdict(list)
        for line in sys.stdin:
            entry = json.loads(line)
            day = datetime.fromtimestamp(float(entry["timestamp"]), zone).date()
            if first <= day <= now.date() and entry["timestamp"] <= now.timestamp():
                entries[day].append(entry)
        rows = [{
            "local_date": day.isoformat(), "timezone": zone.key,
            "payload": aggregate_daily_signals(day, zone, activity_entries=values, sensing_entries=[], reference_time=now.timestamp()),
        } for day, values in sorted(entries.items())]
    else:
        store = DailySignalStore(args.database) if args.database else DailySignalStore()
        rows = store.read_range(first.isoformat(), now.date().isoformat())
    print(json.dumps(audit(rows, now=now), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
