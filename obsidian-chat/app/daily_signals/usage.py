"""Pure, bounded comparisons of source-separated device reports (CP4).

No population norms, activity classification, or missing-to-zero conversion.
The thresholds below suppress small/noisy differences; they are not calibrated
confidence levels or health thresholds.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from statistics import median
from zoneinfo import ZoneInfo


DEVICE_USAGE_SCHEMA = "device_usage.v1"
LOOKBACK_DAYS = 30
MIN_BASELINE_DAYS = 7
WINDOW_SECONDS = 2 * 60 * 60
BIN_SECONDS = 10 * 60
MIN_COVERAGE = 0.8
MIN_DIFFERENCE_SECONDS = 20 * 60
MAD_MULTIPLIER = 3


@dataclass(frozen=True)
class UsageWindow:
    observed_seconds: float
    active_seconds: float
    app_observed_seconds: float
    app_seconds: dict[str, float]


@dataclass(frozen=True)
class UsageDeviation:
    stream_key: str
    device: str
    identity_scope: str
    metric: str
    app: str | None
    direction: str
    current_seconds: float
    baseline_seconds: float
    current_coverage: float
    baseline_coverage: float
    baseline_dates: tuple[str, ...]
    window_start: datetime
    window_end: datetime
    margin_seconds: float


def compare_device_usage(rows: list[dict], *, reference_time: float, timezone: ZoneInfo) -> tuple[UsageDeviation, ...]:
    local = datetime.fromtimestamp(reference_time, timezone)
    end = local.replace(minute=(local.minute // 10) * 10, second=0, microsecond=0)
    start = end - timedelta(seconds=WINDOW_SECONDS)
    # First batch intentionally compares within one local day. Do not splice
    # weekdays/weekends or ambiguous DST bins into a seemingly complete window.
    if start.date() != end.date() or not _ordinary_day(end.date(), timezone):
        return ()
    today = end.date()
    by_date = {
        row["local_date"]: row for row in rows
        if isinstance(row, dict) and row.get("timezone") == timezone.key
        and isinstance(row.get("local_date"), str)
    }
    current_streams = _streams(by_date.get(today.isoformat(), {}))
    comparisons = []
    for stream_key, stream in sorted(current_streams.items()):
        if not _valid_stream(stream):
            continue
        current = _window(stream, start, end)
        if current is None or current.observed_seconds / WINDOW_SECONDS < MIN_COVERAGE:
            continue
        history = []
        for days_ago in range(1, LOOKBACK_DAYS + 1):
            candidate = today - timedelta(days=days_ago)
            if (candidate.weekday() < 5) != (today.weekday() < 5) or not _ordinary_day(candidate, timezone):
                continue
            historical_stream = _streams(by_date.get(candidate.isoformat(), {})).get(stream_key)
            if not _valid_stream(historical_stream) or any(
                historical_stream.get(key) != stream.get(key)
                for key in ("source", "device", "device_id", "identity_scope")
            ):
                continue
            historical_start = datetime.combine(candidate, start.timetz())
            historical_end = datetime.combine(candidate, end.timetz())
            value = _window(historical_stream, historical_start, historical_end)
            if value is not None:
                history.append((candidate.isoformat(), value))

        apps = set(current.app_seconds)
        for _, value in history:
            apps.update(value.app_seconds)
        # At most one statement per device/source: app changes must not flood
        # the shared three-line budget with variants of the same observation.
        candidates = []
        for metric, app in [("active", None), *(("app", app) for app in sorted(apps))]:
            observed = current.observed_seconds if metric == "active" else current.app_observed_seconds
            if observed / WINDOW_SECONDS < MIN_COVERAGE:
                continue
            valid = [
                (day, value, value.observed_seconds if metric == "active" else value.app_observed_seconds)
                for day, value in history
                if (value.observed_seconds if metric == "active" else value.app_observed_seconds) / WINDOW_SECONDS >= MIN_COVERAGE
            ]
            if len(valid) < MIN_BASELINE_DAYS:
                continue
            current_seconds = current.active_seconds if metric == "active" else current.app_seconds.get(app, 0)
            lower = [value.active_seconds if metric == "active" else value.app_seconds.get(app, 0) for _, value, _ in valid]
            center = median(lower)
            upper_center = median(
                seconds + WINDOW_SECONDS - coverage
                for seconds, (_, _, coverage) in zip(lower, valid)
            )
            current_upper = current_seconds + WINDOW_SECONDS - observed
            up = current_seconds - upper_center
            down = center - current_upper
            margin = max(up, down)
            spread = median(abs(value - center) for value in lower)
            if margin < max(MIN_DIFFERENCE_SECONDS, MAD_MULTIPLIER * spread):
                continue
            candidates.append(UsageDeviation(
                stream_key=stream_key, device=stream["device"], identity_scope=stream["identity_scope"],
                metric=metric, app=app, direction="more" if up > down else "less",
                current_seconds=current_seconds, baseline_seconds=center,
                current_coverage=observed / WINDOW_SECONDS,
                baseline_coverage=min(coverage for _, _, coverage in valid) / WINDOW_SECONDS,
                baseline_dates=tuple(sorted(day for day, _, _ in valid)),
                window_start=start, window_end=end, margin_seconds=margin,
            ))
        if candidates:
            comparisons.append(max(candidates, key=lambda item: item.margin_seconds))
    return tuple(sorted(comparisons, key=lambda item: (-item.margin_seconds, item.stream_key)))


def _streams(row: dict) -> dict:
    payload = row.get("payload")
    usage = payload.get("device_usage") if isinstance(payload, dict) else None
    if not isinstance(usage, dict) or usage.get("schema_version") != DEVICE_USAGE_SCHEMA:
        return {}
    streams = usage.get("streams")
    return streams if isinstance(streams, dict) else {}


def _valid_stream(stream) -> bool:
    return (
        isinstance(stream, dict) and stream.get("device") in {"pc", "phone", "tablet"}
        and stream.get("source") == ("pc.activity" if stream.get("device") == "pc" else "android.activity")
        and isinstance(stream.get("device_id"), str) and bool(stream["device_id"])
        and stream.get("identity_scope") in {"device", "legacy_source"}
    )


def _window(stream: dict, start: datetime, end: datetime) -> UsageWindow | None:
    bins = stream.get("bins")
    if not isinstance(bins, dict):
        return None
    totals = {key: 0.0 for key in ("observed_seconds", "active_seconds", "app_observed_seconds")}
    apps: dict[str, float] = {}
    fresh = False
    cursor = start
    while cursor < end:
        detail = bins.get(cursor.strftime("%H:%M"))
        following = cursor + timedelta(seconds=BIN_SECONDS)
        if detail is not None:
            if not isinstance(detail, dict):
                return None
            count = detail.get("sample_count")
            # Interpolation into a bin with no real sample is not coverage.
            if not isinstance(count, int) or isinstance(count, bool) or count < 0:
                return None
            if count:
                stamp = _number(detail.get("last_observed_at"))
                if stamp is None or not cursor.timestamp() <= stamp < following.timestamp():
                    return None
                values = {key: _number(detail.get(key)) for key in totals}
                if any(value is None or not 0 <= value <= BIN_SECONDS + 0.001 for value in values.values()):
                    return None
                if any(values[key] > values["observed_seconds"] + 0.001 for key in ("active_seconds", "app_observed_seconds")):
                    return None
                app_values = detail.get("app_seconds")
                if not isinstance(app_values, dict):
                    return None
                app_values = {app: _number(value) for app, value in app_values.items()}
                if any(not isinstance(app, str) or not app or value is None or value < 0 for app, value in app_values.items()):
                    return None
                if sum(app_values.values()) > min(values["app_observed_seconds"], values["active_seconds"]) + 0.001:
                    return None
                for key, value in values.items():
                    totals[key] += min(value, BIN_SECONDS)
                for app, value in app_values.items():
                    apps[app] = apps.get(app, 0) + value
                if following == end:
                    fresh = True
        cursor = following
    return UsageWindow(**totals, app_seconds=apps) if fresh else None


def _ordinary_day(day: date, timezone: ZoneInfo) -> bool:
    start = datetime.combine(day, datetime.min.time(), timezone)
    end = datetime.combine(day + timedelta(days=1), datetime.min.time(), timezone)
    return end.timestamp() - start.timestamp() == 86400


def _number(value) -> float | None:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return None
    return float(value) if math.isfinite(value) else None
