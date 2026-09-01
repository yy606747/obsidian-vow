from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Callable, Iterable, Mapping

from config import DATA_DIR


SCHEMA_VERSION = 2
DEFAULT_DB_PATH = DATA_DIR / "signal_daily.db"
_DEFAULT_STORE = None
_DEFAULT_STORE_LOCK = threading.Lock()


class DailySignalStore:
    """One durable JSON payload per local date, merged under a SQLite write lock."""

    def __init__(
        self,
        db_path: Path | str = DEFAULT_DB_PATH,
        *,
        now: Callable[[], float] = time.time,
    ):
        self.db_path = Path(db_path)
        self._now = now

    def fetch(self, local_date: str) -> dict | None:
        self._ensure_schema()
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM daily_signal_summaries WHERE local_date=?",
                (local_date,),
            ).fetchone()
        return _decode_row(row) if row is not None else None

    def list_dates(self) -> list[str]:
        self._ensure_schema()
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT local_date FROM daily_signal_summaries ORDER BY local_date"
            ).fetchall()
        return [str(row["local_date"]) for row in rows]

    def count(self) -> int:
        self._ensure_schema()
        with self._connect() as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS count FROM daily_signal_summaries"
            ).fetchone()
        return int(row["count"])

    def merge_reconciled(
        self,
        local_date: str,
        timezone_name: str,
        reconciled: dict,
        *,
        preserve_biometrics: bool = False,
    ) -> dict:
        """Replace batch-derived domains while preserving heartbeat-owned location data."""

        def merge(payload: dict) -> dict:
            for key in ("activity", "environment"):
                payload[key] = _json_copy(reconciled[key])
            if not preserve_biometrics:
                payload["biometrics"] = _json_copy(reconciled["biometrics"])

            coverage = dict(payload.get("coverage") or {})
            incoming_coverage = reconciled.get("coverage") or {}
            for source in ("pc", "phone_sensing"):
                coverage[source] = _json_copy(incoming_coverage[source])
            if not preserve_biometrics:
                coverage["biometric"] = _json_copy(incoming_coverage["biometric"])
            else:
                coverage.setdefault("biometric", _empty_coverage())
            coverage.setdefault("location", _empty_coverage())
            payload["coverage"] = coverage

            location = dict(payload.get("location") or {})
            location.setdefault("left_usual_place", None)
            location.setdefault("last_observed_at", None)
            location.setdefault("states_observed", [])
            payload["location"] = location
            return payload

        result = self._atomic_merge(local_date, timezone_name, merge)
        if self.biometric_day_state(local_date) is None:
            # A newly reconciled day is mutable canonical history, not a
            # pre-migration durable row.
            self.set_biometric_day_state(local_date, "ready")
        return result

    def merge_biometrics(
        self,
        local_date: str,
        timezone_name: str,
        *,
        biometrics: Mapping,
        coverage: Mapping,
    ) -> dict:
        """Atomically replace only the durable biometric domain of one day."""

        def merge(payload: dict) -> dict:
            _ensure_daily_payload_shape(payload)
            payload["biometrics"] = _json_copy(biometrics)
            all_coverage = dict(payload.get("coverage") or {})
            all_coverage["biometric"] = _json_copy(coverage)
            payload["coverage"] = all_coverage
            return payload

        result = self._atomic_merge(local_date, timezone_name, merge)
        if self.biometric_day_state(local_date) is None:
            # A newly-created biometric day is canonical, not legacy history.
            self.set_biometric_day_state(local_date, "ready")
        return result

    def upsert_biometric_observations(
        self,
        observations: Iterable[Mapping],
        timezone_name: str,
        *,
        initialize_days: bool = True,
    ) -> dict:
        """Latest-wins record upsert with old/new affected-date union."""

        from .biometrics import observation_dates

        values = [dict(value) for value in observations]
        if not values:
            return {"upserted_count": 0, "affected_dates": []}

        self._ensure_schema()
        affected_dates: set[str] = set()
        upserted_count = 0
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            received_at = self._now()
            for observation in values:
                source_kind = str(observation["source_kind"])
                source_id = str(observation["source_id"])
                old_row = conn.execute(
                    """
                    SELECT * FROM biometric_observations
                    WHERE source_kind=? AND source_id=?
                    """,
                    (source_kind, source_id),
                ).fetchone()
                if old_row is not None:
                    affected_dates.update(
                        observation_dates(_decode_observation(old_row), timezone_name)
                    )
                affected_dates.update(observation_dates(observation, timezone_name))

                payload_json = json.dumps(
                    observation.get("payload") or {},
                    ensure_ascii=False,
                    sort_keys=True,
                )
                unchanged = (
                    old_row is not None
                    and old_row["source_start_at"] == observation.get("source_start_at")
                    and old_row["source_end_at"] == observation.get("source_end_at")
                    and old_row["canonical_date"] == observation.get("canonical_date")
                    and old_row["payload_json"] == payload_json
                    and old_row["device_timezone"] == observation.get("device_timezone")
                )
                if unchanged:
                    continue
                conn.execute(
                    """
                    INSERT INTO biometric_observations (
                        source_kind, source_id, source_start_at, source_end_at,
                        canonical_date, payload_json, device_timezone,
                        first_received_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(source_kind, source_id) DO UPDATE SET
                        source_start_at=excluded.source_start_at,
                        source_end_at=excluded.source_end_at,
                        canonical_date=excluded.canonical_date,
                        payload_json=excluded.payload_json,
                        device_timezone=excluded.device_timezone,
                        updated_at=excluded.updated_at
                    """,
                    (
                        source_kind,
                        source_id,
                        observation.get("source_start_at"),
                        observation.get("source_end_at"),
                        observation.get("canonical_date"),
                        payload_json,
                        observation.get("device_timezone"),
                        received_at,
                        received_at,
                    ),
                )
                upserted_count += 1

            if initialize_days:
                for local_date in sorted(affected_dates):
                    summary_exists = conn.execute(
                        "SELECT 1 FROM daily_signal_summaries WHERE local_date=?",
                        (local_date,),
                    ).fetchone() is not None
                    default_state = "legacy_frozen" if summary_exists else "ready"
                    conn.execute(
                        """
                        INSERT OR IGNORE INTO biometric_canonical_days (
                            local_date, state, updated_at
                        ) VALUES (?, ?, ?)
                        """,
                        (local_date, default_state, received_at),
                    )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()
        return {
            "upserted_count": upserted_count,
            "affected_dates": sorted(affected_dates),
        }

    def list_biometric_observations(self) -> list[dict]:
        self._ensure_schema()
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM biometric_observations
                ORDER BY source_kind, source_id
                """
            ).fetchall()
        return [_decode_observation(row) for row in rows]

    def biometric_observations_for_date(
        self,
        local_date: str,
        timezone_name: str,
    ) -> list[dict]:
        from datetime import date as date_type, datetime, timedelta
        from zoneinfo import ZoneInfo

        from .biometrics import observation_dates

        parsed = date_type.fromisoformat(local_date)
        timezone = ZoneInfo(timezone_name)
        day_start = datetime(parsed.year, parsed.month, parsed.day, tzinfo=timezone).timestamp()
        next_date = parsed + timedelta(days=1)
        day_end = datetime(
            next_date.year,
            next_date.month,
            next_date.day,
            tzinfo=timezone,
        ).timestamp()
        self._ensure_schema()
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM biometric_observations
                WHERE canonical_date=?
                   OR (
                       source_start_at IS NOT NULL
                       AND source_start_at < ?
                       AND COALESCE(source_end_at, source_start_at) >= ?
                   )
                ORDER BY source_kind, source_id
                """,
                (local_date, day_end, day_start),
            ).fetchall()
        observations = [_decode_observation(row) for row in rows]
        return [
            observation
            for observation in observations
            if local_date in observation_dates(observation, timezone_name)
        ]

    def set_biometric_day_state(self, local_date: str, state: str) -> None:
        if state not in {"ready", "legacy_frozen"}:
            raise ValueError(f"invalid biometric canonical day state: {state}")
        self._ensure_schema()
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO biometric_canonical_days (local_date, state, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(local_date) DO UPDATE SET
                    state=excluded.state,
                    updated_at=excluded.updated_at
                """,
                (local_date, state, self._now()),
            )
            conn.commit()

    def biometric_day_state(self, local_date: str) -> str | None:
        self._ensure_schema()
        with self._connect() as conn:
            row = conn.execute(
                "SELECT state FROM biometric_canonical_days WHERE local_date=?",
                (local_date,),
            ).fetchone()
        return str(row["state"]) if row is not None else None

    def list_biometric_day_states(self) -> dict[str, str]:
        self._ensure_schema()
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT local_date, state FROM biometric_canonical_days ORDER BY local_date"
            ).fetchall()
        return {str(row["local_date"]): str(row["state"]) for row in rows}

    def ensure_biometric_day_state(self, local_date: str) -> str:
        state = self.biometric_day_state(local_date)
        if state is not None:
            return state
        state = "legacy_frozen" if self.fetch(local_date) is not None else "ready"
        self.set_biometric_day_state(local_date, state)
        return state

    def merge_location_heartbeat(
        self,
        local_date: str,
        timezone_name: str,
        *,
        bin_label: str,
        observed_at: float,
        state: str,
    ) -> dict:
        """Atomically OR location state into a daily row without replacing batch fields."""

        def merge(payload: dict) -> dict:
            coverage = dict(payload.get("coverage") or {})
            location_coverage = dict(coverage.get("location") or _empty_coverage())
            bins = set(location_coverage.get("covered_bins") or [])
            bins.add(bin_label)
            location_coverage = _coverage(sorted(bins))
            coverage["location"] = location_coverage
            payload["coverage"] = coverage

            location = dict(payload.get("location") or {})
            previous = location.get("left_usual_place")
            is_outside = state == "outside"
            if previous is True or is_outside:
                location["left_usual_place"] = True
            elif previous is False or state == "at_home":
                location["left_usual_place"] = False
            else:
                location["left_usual_place"] = None
            location["last_observed_at"] = max(
                float(location.get("last_observed_at") or 0.0),
                float(observed_at),
            )
            states = set(location.get("states_observed") or [])
            if state:
                states.add(state)
            location["states_observed"] = sorted(states)
            payload["location"] = location
            return payload

        return self._atomic_merge(local_date, timezone_name, merge)

    def _atomic_merge(
        self,
        local_date: str,
        timezone_name: str,
        merge,
    ) -> dict:
        self._ensure_schema()
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                """
                SELECT payload_json, created_at, timezone, schema_version
                FROM daily_signal_summaries WHERE local_date=?
                """,
                (local_date,),
            ).fetchone()
            if row is None:
                payload = {}
                created_at = self._now()
            else:
                payload = _load_payload(row["payload_json"])
                created_at = float(row["created_at"])
            payload = merge(payload)
            encoded_payload = json.dumps(payload, ensure_ascii=False, sort_keys=True)
            unchanged = (
                row is not None
                and row["payload_json"] == encoded_payload
                and row["timezone"] == timezone_name
                and int(row["schema_version"]) == SCHEMA_VERSION
            )
            if unchanged:
                conn.commit()
            else:
                updated_at = self._now()
                conn.execute(
                    """
                    INSERT INTO daily_signal_summaries (
                        local_date, timezone, schema_version, payload_json,
                        created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    ON CONFLICT(local_date) DO UPDATE SET
                        timezone=excluded.timezone,
                        schema_version=excluded.schema_version,
                        payload_json=excluded.payload_json,
                        updated_at=excluded.updated_at
                    """,
                    (
                        local_date,
                        timezone_name,
                        SCHEMA_VERSION,
                        encoded_payload,
                        created_at,
                        updated_at,
                    ),
                )
                conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()
        result = self.fetch(local_date)
        assert result is not None
        return result

    def _ensure_schema(self) -> None:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS daily_signal_summaries (
                    local_date TEXT PRIMARY KEY,
                    timezone TEXT NOT NULL,
                    schema_version INTEGER NOT NULL,
                    payload_json TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS biometric_observations (
                    source_kind TEXT NOT NULL,
                    source_id TEXT NOT NULL,
                    source_start_at REAL,
                    source_end_at REAL,
                    canonical_date TEXT,
                    payload_json TEXT NOT NULL,
                    device_timezone TEXT,
                    first_received_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    PRIMARY KEY (source_kind, source_id)
                )
                """
            )
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_biometric_observations_date
                ON biometric_observations(canonical_date, source_start_at, source_end_at)
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS biometric_canonical_days (
                    local_date TEXT PRIMARY KEY,
                    state TEXT NOT NULL CHECK(state IN ('ready', 'legacy_frozen')),
                    updated_at REAL NOT NULL
                )
                """
            )
            conn.commit()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=30.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=30000")
        return conn


def get_default_store() -> DailySignalStore:
    global _DEFAULT_STORE
    if _DEFAULT_STORE is None:
        with _DEFAULT_STORE_LOCK:
            if _DEFAULT_STORE is None:
                _DEFAULT_STORE = DailySignalStore()
    return _DEFAULT_STORE


def _decode_row(row: sqlite3.Row) -> dict:
    return {
        "local_date": str(row["local_date"]),
        "timezone": str(row["timezone"]),
        "schema_version": int(row["schema_version"]),
        "payload": _load_payload(row["payload_json"]),
        "created_at": float(row["created_at"]),
        "updated_at": float(row["updated_at"]),
    }


def _decode_observation(row: sqlite3.Row) -> dict:
    return {
        "source_kind": str(row["source_kind"]),
        "source_id": str(row["source_id"]),
        "source_start_at": row["source_start_at"],
        "source_end_at": row["source_end_at"],
        "canonical_date": row["canonical_date"],
        "payload": _load_payload(row["payload_json"]),
        "device_timezone": row["device_timezone"],
        "first_received_at": float(row["first_received_at"]),
        "updated_at": float(row["updated_at"]),
    }


def _load_payload(value: str) -> dict:
    try:
        payload = json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _json_copy(value):
    return json.loads(json.dumps(value, ensure_ascii=False))


def _empty_coverage() -> dict:
    return _coverage([])


def _ensure_daily_payload_shape(payload: dict) -> None:
    payload.setdefault(
        "activity",
        {
            "first_active_at": None,
            "last_active_at": None,
            "active_seconds": 0,
            "app_seconds": {},
            "longest_observed_rest": None,
            "bins": {},
        },
    )
    payload.setdefault("biometrics", _empty_biometrics())
    payload.setdefault("environment", {"wifi_ssids": []})
    payload.setdefault(
        "location",
        {
            "left_usual_place": None,
            "last_observed_at": None,
            "states_observed": [],
        },
    )
    coverage = dict(payload.get("coverage") or {})
    for source in ("pc", "phone_sensing", "biometric", "location"):
        coverage.setdefault(source, _empty_coverage())
    payload["coverage"] = coverage


def _empty_biometrics() -> dict:
    return {
        "heart_rate": None,
        "spo2": None,
        "sleep_stage_minutes": {},
        "steps_total_today": None,
        "steps_total_date": None,
        "steps_total_timezone": None,
    }


def _coverage(bins: list[str]) -> dict:
    return {
        "covered_bins": bins,
        "covered_count": len(bins),
        "total_bins": 144,
        "ratio": round(len(bins) / 144, 6),
    }
