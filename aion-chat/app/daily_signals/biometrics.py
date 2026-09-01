from __future__ import annotations

import math
import time
from datetime import date, datetime, timedelta
from typing import Iterable, Mapping
from zoneinfo import ZoneInfo


HEART_RATE = "heart_rate"
SPO2 = "spo2"
SLEEP_SESSION = "sleep_session"
LEGACY_SLEEP = "legacy_sleep"
STEPS_DAILY = "steps_daily"
LEGACY_STEPS_DAILY = "legacy_steps_daily"
LEGACY_TICK = "legacy_tick"


def batch_leaf_count(payload: Mapping) -> int:
    heart = sum(
        len(record.get("samples") or [])
        for record in payload.get("heart_rate_records") or []
    )
    spo2 = len(payload.get("spo2_records") or [])
    sleep = sum(
        max(1, len(session.get("stages") or []))
        for session in payload.get("sleep_sessions") or []
    )
    steps = len(payload.get("steps_daily") or [])
    return heart + spo2 + sleep + steps


def batch_to_observations(payload: Mapping, timezone_name: str) -> list[dict]:
    """Convert a validated API payload into durable record-level observations."""

    timezone = ZoneInfo(timezone_name)
    device_timezone = str(payload.get("device_timezone") or "").strip() or None
    observations: list[dict] = []

    for record in payload.get("heart_rate_records") or []:
        samples = [
            {
                "observed_at": float(sample["observed_at"]),
                "bpm": int(sample["bpm"]),
            }
            for sample in record.get("samples") or []
        ]
        sample_times = [sample["observed_at"] for sample in samples]
        source_start = min(sample_times)
        source_end = max(sample_times)
        observations.append(
            {
                "source_kind": HEART_RATE,
                "source_id": str(record["source_id"]),
                # HC record shells can be much wider than their actual samples.
                "source_start_at": source_start,
                "source_end_at": source_end,
                "canonical_date": _local_date(source_start, timezone).isoformat(),
                "payload": {
                    "source_id": str(record["source_id"]),
                    "start_at": float(record["start_at"]),
                    "end_at": float(record["end_at"]),
                    "samples": samples,
                },
                "device_timezone": device_timezone,
            }
        )

    for record in payload.get("spo2_records") or []:
        observed_at = float(record["observed_at"])
        observations.append(
            {
                "source_kind": SPO2,
                "source_id": str(record["source_id"]),
                "source_start_at": observed_at,
                "source_end_at": observed_at,
                "canonical_date": _local_date(observed_at, timezone).isoformat(),
                "payload": {
                    "source_id": str(record["source_id"]),
                    "observed_at": observed_at,
                    "percentage": float(record["percentage"]),
                },
                "device_timezone": device_timezone,
            }
        )

    for session in payload.get("sleep_sessions") or []:
        start_at = float(session["start_at"])
        end_at = float(session["end_at"])
        observations.append(
            {
                "source_kind": SLEEP_SESSION,
                "source_id": str(session["source_id"]),
                "source_start_at": start_at,
                "source_end_at": end_at,
                "canonical_date": _local_date(start_at, timezone).isoformat(),
                "payload": {
                    "source_id": str(session["source_id"]),
                    "start_at": start_at,
                    "end_at": end_at,
                    "stages": [
                        {
                            "start_at": float(stage["start_at"]),
                            "end_at": float(stage["end_at"]),
                            "stage": str(stage["stage"]),
                        }
                        for stage in session.get("stages") or []
                    ],
                },
                "device_timezone": device_timezone,
            }
        )

    for record in payload.get("steps_daily") or []:
        canonical_date = str(record["daily_date"])
        observations.append(
            {
                "source_kind": STEPS_DAILY,
                "source_id": str(record["source_id"]),
                "source_start_at": float(record["observed_at"]),
                "source_end_at": float(record["observed_at"]),
                "canonical_date": canonical_date,
                "payload": {
                    "source_id": str(record["source_id"]),
                    "daily_date": canonical_date,
                    "aggregation_timezone": str(record["aggregation_timezone"]),
                    "total": int(record["total"]),
                    "observed_at": float(record["observed_at"]),
                },
                "device_timezone": device_timezone,
            }
        )

    return observations


def sensing_entry_to_observations(entry: Mapping, timezone_name: str) -> list[dict]:
    """Shadow-import one legacy/live tick without re-importing batch audit rows."""

    if entry.get("type") != "biometric" or entry.get("backfill") is True:
        return []
    report_at = _finite_timestamp(entry.get("timestamp"))
    if report_at is None:
        return []
    data = entry.get("data") if isinstance(entry.get("data"), Mapping) else {}
    timezone = ZoneInfo(timezone_name)
    report_key = _timestamp_key(report_at)
    observations: list[dict] = []

    if data.get("heart_rate") is not None:
        observed_at = _finite_timestamp(data.get("heart_rate_observed_at")) or report_at
        try:
            bpm = int(data["heart_rate"])
        except (TypeError, ValueError):
            bpm = None
        if bpm is not None:
            observations.append(
                _observation(
                    HEART_RATE,
                    f"legacy:heart_rate:{_timestamp_key(observed_at)}",
                    observed_at,
                    observed_at,
                    _local_date(observed_at, timezone).isoformat(),
                    {
                        "source_id": f"legacy:heart_rate:{_timestamp_key(observed_at)}",
                        "start_at": observed_at,
                        "end_at": observed_at,
                        "samples": [{"observed_at": observed_at, "bpm": bpm}],
                        "legacy": True,
                    },
                    data.get("steps_total_timezone"),
                )
            )

    if data.get("spo2") is not None:
        observed_at = _finite_timestamp(data.get("spo2_observed_at")) or report_at
        try:
            percentage = float(data["spo2"])
        except (TypeError, ValueError):
            percentage = None
        if percentage is not None and math.isfinite(percentage):
            observations.append(
                _observation(
                    SPO2,
                    f"legacy:spo2:{_timestamp_key(observed_at)}",
                    observed_at,
                    observed_at,
                    _local_date(observed_at, timezone).isoformat(),
                    {
                        "source_id": f"legacy:spo2:{_timestamp_key(observed_at)}",
                        "observed_at": observed_at,
                        "percentage": percentage,
                        "legacy": True,
                    },
                    data.get("steps_total_timezone"),
                )
            )

    stage = str(data.get("sleep_stage") or "").strip().lower()
    if stage:
        observations.append(
            _observation(
                LEGACY_SLEEP,
                f"legacy:sleep:{report_key}",
                report_at,
                report_at,
                _local_date(report_at, timezone).isoformat(),
                {"observed_at": report_at, "stage": stage},
                data.get("steps_total_timezone"),
            )
        )

    steps_delta_present = data.get("steps_delta") is not None
    steps_total_present = data.get("steps_total_today") is not None
    if steps_delta_present or steps_total_present:
        try:
            delta = int(data["steps_delta"]) if steps_delta_present else None
        except (TypeError, ValueError):
            delta = None
        if delta is not None or steps_total_present:
            payload = {
                "observed_at": report_at,
                "steps_total_reported": steps_total_present,
            }
            if delta is not None:
                payload["steps_delta"] = delta
            observations.append(
                _observation(
                    LEGACY_TICK,
                    f"legacy:tick:{report_key}",
                    report_at,
                    report_at,
                    _local_date(report_at, timezone).isoformat(),
                    payload,
                    data.get("steps_total_timezone"),
                )
            )

    steps_date = _parse_date(data.get("steps_total_date"))
    steps_timezone = str(data.get("steps_total_timezone") or "").strip()
    if steps_date is not None and data.get("steps_total_today") is not None and steps_timezone:
        try:
            total = int(data["steps_total_today"])
        except (TypeError, ValueError):
            total = None
        if total is not None and total >= 0:
            kind = STEPS_DAILY if steps_timezone == timezone_name else LEGACY_STEPS_DAILY
            source_id = f"{kind}:{steps_date.isoformat()}:{steps_timezone}"
            observations.append(
                _observation(
                    kind,
                    source_id,
                    report_at,
                    report_at,
                    steps_date.isoformat(),
                    {
                        "source_id": source_id,
                        "daily_date": steps_date.isoformat(),
                        "aggregation_timezone": steps_timezone,
                        "total": total,
                        "observed_at": report_at,
                        "legacy": True,
                    },
                    steps_timezone,
                )
            )

    return observations


def observation_dates(observation: Mapping, timezone_name: str) -> set[str]:
    """Return every server-local day an observation can contribute to."""

    timezone = ZoneInfo(timezone_name)
    kind = str(observation.get("source_kind") or "")
    payload = observation.get("payload")
    if not isinstance(payload, Mapping):
        payload = observation.get("payload_json")
    if not isinstance(payload, Mapping):
        payload = {}

    dates: set[date] = set()
    if kind == HEART_RATE:
        for sample in payload.get("samples") or []:
            observed_at = _finite_timestamp(sample.get("observed_at"))
            if observed_at is not None:
                dates.add(_local_date(observed_at, timezone))
    elif kind == SPO2:
        observed_at = _finite_timestamp(payload.get("observed_at"))
        if observed_at is not None:
            dates.add(_local_date(observed_at, timezone))
    elif kind == SLEEP_SESSION:
        start_at = _finite_timestamp(payload.get("start_at"))
        end_at = _finite_timestamp(payload.get("end_at"))
        if start_at is not None and end_at is not None:
            dates.update(_interval_dates(start_at, end_at, timezone))
    elif kind in {STEPS_DAILY, LEGACY_STEPS_DAILY}:
        parsed = _parse_date(payload.get("daily_date") or observation.get("canonical_date"))
        if parsed is not None:
            dates.add(parsed)
    elif kind in {LEGACY_SLEEP, LEGACY_TICK}:
        observed_at = _finite_timestamp(payload.get("observed_at"))
        if observed_at is not None:
            dates.add(_local_date(observed_at, timezone))

    if not dates:
        parsed = _parse_date(observation.get("canonical_date"))
        if parsed is not None:
            dates.add(parsed)
    return {value.isoformat() for value in dates}


def sensing_entry_dates(entry: Mapping, timezone: ZoneInfo) -> set[date]:
    """Shared source-date indexing for retained JSONL and explicit intervals."""

    dates: set[date] = set()
    report_at = _finite_timestamp(entry.get("timestamp"))
    if report_at is not None:
        dates.add(_local_date(report_at, timezone))
    data = entry.get("data") if isinstance(entry.get("data"), Mapping) else {}
    for field in ("heart_rate_observed_at", "spo2_observed_at"):
        observed_at = _finite_timestamp(data.get(field))
        if observed_at is not None:
            dates.add(_local_date(observed_at, timezone))
    steps_date = _parse_date(data.get("steps_total_date"))
    if steps_date is not None:
        dates.add(steps_date)
    sleep_start = _finite_timestamp(data.get("sleep_start_at"))
    sleep_end = _finite_timestamp(data.get("sleep_end_at"))
    if sleep_start is not None and sleep_end is not None:
        dates.update(_interval_dates(sleep_start, sleep_end, timezone))
    return dates


def observations_to_sensing_entries(observations: Iterable[Mapping]) -> list[dict]:
    """Expand the durable latest version of each source record for aggregation."""

    entries: list[dict] = []
    for observation in observations:
        kind = str(observation.get("source_kind") or "")
        source_id = str(observation.get("source_id") or "")
        payload = observation.get("payload")
        if not isinstance(payload, Mapping):
            continue
        received_at = _finite_timestamp(observation.get("updated_at")) or time.time()
        base = {
            "timestamp": received_at,
            "type": "biometric",
            "source_kind": kind,
            "source_id": source_id,
        }
        if kind == HEART_RATE:
            for sample in payload.get("samples") or []:
                entries.append(
                    {
                        **base,
                        "data": {
                            "heart_rate": sample.get("bpm"),
                            "heart_rate_observed_at": sample.get("observed_at"),
                        },
                    }
                )
        elif kind == SPO2:
            entries.append(
                {
                    **base,
                    "data": {
                        "spo2": payload.get("percentage"),
                        "spo2_observed_at": payload.get("observed_at"),
                    },
                }
            )
        elif kind == SLEEP_SESSION:
            stages = payload.get("stages") or []
            if not stages:
                stages = [
                    {
                        "start_at": payload.get("start_at"),
                        "end_at": payload.get("end_at"),
                        "stage": "sleeping",
                    }
                ]
            for stage in stages:
                entries.append(
                    {
                        **base,
                        "data": {
                            "sleep_stage": stage.get("stage"),
                            "sleep_start_at": stage.get("start_at"),
                            "sleep_end_at": stage.get("end_at"),
                            "sleep_explicit": True,
                        },
                    }
                )
        elif kind == LEGACY_SLEEP:
            entries.append(
                {
                    **base,
                    "timestamp": payload.get("observed_at", received_at),
                    "data": {"sleep_stage": payload.get("stage")},
                }
            )
        elif kind in {STEPS_DAILY, LEGACY_STEPS_DAILY}:
            entries.append(
                {
                    **base,
                    "data": {
                        "steps_total_today": payload.get("total"),
                        "steps_total_date": payload.get("daily_date"),
                        "steps_total_timezone": payload.get("aggregation_timezone"),
                        "steps_total_observed_at": payload.get("observed_at"),
                        "steps_source_kind": kind,
                    },
                }
            )
        elif kind == LEGACY_TICK:
            data = {
                "steps_total_reported": bool(payload.get("steps_total_reported")),
            }
            if payload.get("steps_delta") is not None:
                data["steps_delta"] = payload.get("steps_delta")
            entries.append(
                {
                    **base,
                    "timestamp": payload.get("observed_at", received_at),
                    "data": data,
                }
            )
    return entries


def batch_audit_entries(payload: Mapping, received_at: float) -> list[dict]:
    device_timezone = str(payload.get("device_timezone") or "").strip() or None
    entries: list[dict] = []

    def add(kind: str, source_id: str, observation_id: str, data: dict):
        entries.append(
            {
                "timestamp": received_at,
                "time": time.strftime("%H:%M:%S", time.localtime(received_at)),
                "date": time.strftime("%Y-%m-%d", time.localtime(received_at)),
                "type": "biometric",
                "backfill": True,
                "source_kind": kind,
                "source_id": source_id,
                "observation_id": observation_id,
                "device_timezone": device_timezone,
                "data": data,
            }
        )

    for record in payload.get("heart_rate_records") or []:
        source_id = str(record["source_id"])
        for sample in record.get("samples") or []:
            observed_at = float(sample["observed_at"])
            add(
                HEART_RATE,
                source_id,
                f"{source_id}:{_timestamp_key(observed_at)}",
                {"heart_rate": int(sample["bpm"]), "heart_rate_observed_at": observed_at},
            )
    for record in payload.get("spo2_records") or []:
        source_id = str(record["source_id"])
        observed_at = float(record["observed_at"])
        add(
            SPO2,
            source_id,
            f"{source_id}:{_timestamp_key(observed_at)}",
            {"spo2": float(record["percentage"]), "spo2_observed_at": observed_at},
        )
    for session in payload.get("sleep_sessions") or []:
        source_id = str(session["source_id"])
        stages = session.get("stages") or [
            {
                "start_at": session["start_at"],
                "end_at": session["end_at"],
                "stage": "sleeping",
            }
        ]
        for stage in stages:
            start_at = float(stage["start_at"])
            end_at = float(stage["end_at"])
            add(
                SLEEP_SESSION,
                source_id,
                f"{source_id}:{_timestamp_key(start_at)}:{_timestamp_key(end_at)}",
                {
                    "sleep_stage": str(stage["stage"]),
                    "sleep_start_at": start_at,
                    "sleep_end_at": end_at,
                },
            )
    for record in payload.get("steps_daily") or []:
        source_id = str(record["source_id"])
        add(
            STEPS_DAILY,
            source_id,
            source_id,
            {
                "steps_total_today": int(record["total"]),
                "steps_total_date": str(record["daily_date"]),
                "steps_total_timezone": str(record["aggregation_timezone"]),
                "steps_total_observed_at": float(record["observed_at"]),
            },
        )
    return entries


def _observation(
    source_kind: str,
    source_id: str,
    source_start_at: float,
    source_end_at: float,
    canonical_date: str,
    payload: dict,
    device_timezone,
) -> dict:
    return {
        "source_kind": source_kind,
        "source_id": source_id,
        "source_start_at": source_start_at,
        "source_end_at": source_end_at,
        "canonical_date": canonical_date,
        "payload": payload,
        "device_timezone": str(device_timezone or "").strip() or None,
    }


def _interval_dates(start_at: float, end_at: float, timezone: ZoneInfo) -> set[date]:
    if end_at < start_at:
        return set()
    start_date = _local_date(start_at, timezone)
    end_date = _local_date(end_at, timezone)
    return {
        start_date + timedelta(days=offset)
        for offset in range((end_date - start_date).days + 1)
    }


def _local_date(timestamp: float, timezone: ZoneInfo) -> date:
    return datetime.fromtimestamp(timestamp, timezone).date()


def _timestamp_key(timestamp: float) -> int:
    return int(round(timestamp * 1000.0))


def _finite_timestamp(value) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) else None


def _parse_date(value) -> date | None:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None
