"""Disposable SQLite audit store for context-trigger shadow evaluations."""

from __future__ import annotations

import hashlib
import json
import math
import sqlite3
import threading
import time
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any, Callable

from .shadow_rules import (
    EVALUATION_MATCHED,
    EVALUATION_STATUSES,
    ShadowRuleEvaluation,
)


OWNER_LABEL_RIGHT = "right"
OWNER_LABEL_WRONG = "wrong"
OWNER_LABEL_INDIFFERENT = "indifferent"
OWNER_LABELS = frozenset({
    OWNER_LABEL_RIGHT,
    OWNER_LABEL_WRONG,
    OWNER_LABEL_INDIFFERENT,
})

DEFAULT_SHADOW_LIST_LIMIT = 50
MAX_SHADOW_LIST_LIMIT = 500


class ContextTriggerShadowStore:
    def __init__(
        self,
        db_path: str | Path,
        *,
        now: Callable[[], float] | None = None,
    ):
        self.db_path = Path(db_path)
        self._now = now or time.time
        self._initialized = False
        self._init_lock = threading.Lock()

    def initialize(self) -> None:
        if self._initialized:
            return
        with self._init_lock:
            if self._initialized:
                return
            self.db_path.parent.mkdir(parents=True, exist_ok=True)
            with self._connect_raw() as conn:
                conn.execute("PRAGMA journal_mode=WAL")
                conn.executescript(
                    """
                    CREATE TABLE IF NOT EXISTS context_trigger_shadow (
                        id TEXT PRIMARY KEY,
                        occurred_at REAL NOT NULL,
                        rule TEXT NOT NULL,
                        source_event_id TEXT NOT NULL,
                        evaluation_status TEXT NOT NULL
                            CHECK (evaluation_status IN ('matched', 'not_matched', 'unavailable')),
                        evaluation_reason TEXT NOT NULL,
                        features_json TEXT NOT NULL,
                        projection_json TEXT NOT NULL,
                        gate_json TEXT,
                        legacy_sentinel_woke_within_5m INTEGER
                            CHECK (legacy_sentinel_woke_within_5m IN (0, 1)),
                        owner_message_within_30m INTEGER
                            CHECK (owner_message_within_30m IN (0, 1)),
                        owner_label TEXT
                            CHECK (owner_label IN ('right', 'wrong', 'indifferent')),
                        outcome_evaluated_at REAL,
                        labeled_at REAL,
                        UNIQUE(rule, source_event_id)
                    );
                    CREATE INDEX IF NOT EXISTS idx_context_trigger_shadow_occurred
                        ON context_trigger_shadow(occurred_at DESC);
                    CREATE INDEX IF NOT EXISTS idx_context_trigger_shadow_status
                        ON context_trigger_shadow(evaluation_status, occurred_at DESC);
                    CREATE INDEX IF NOT EXISTS idx_context_trigger_shadow_outcome_due
                        ON context_trigger_shadow(outcome_evaluated_at, occurred_at);
                    """
                )
                conn.commit()
            self._initialized = True

    def get_by_rule_source(
        self,
        *,
        rule: str,
        source_event_id: str,
    ) -> dict[str, Any] | None:
        self.initialize()
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM context_trigger_shadow WHERE rule=? AND source_event_id=?",
                (_required_text(rule, key="rule"), _required_text(source_event_id, key="source_event_id")),
            ).fetchone()
        return _decode_row(row) if row is not None else None

    def record_evaluation(
        self,
        evaluation: ShadowRuleEvaluation,
        *,
        projection: Mapping[str, Any],
        gate: Mapping[str, Any] | None,
    ) -> dict[str, Any]:
        if not isinstance(evaluation, ShadowRuleEvaluation):
            raise ValueError("shadow evaluation must use ShadowRuleEvaluation")
        projection_payload = _mapping(projection, key="projection")
        gate_payload = None if gate is None else _mapping(gate, key="gate")
        if evaluation.evaluation_status == EVALUATION_MATCHED and gate_payload is None:
            raise ValueError("matched shadow evaluation requires gate result")
        if evaluation.evaluation_status != EVALUATION_MATCHED and gate_payload is not None:
            raise ValueError("non-matched shadow evaluation cannot carry gate result")

        evaluation_id = _evaluation_id(evaluation.rule, evaluation.source_event_id)
        values = (
            evaluation_id,
            evaluation.occurred_at,
            evaluation.rule,
            evaluation.source_event_id,
            evaluation.evaluation_status,
            evaluation.evaluation_reason,
            _json_dump(evaluation.features),
            _json_dump(projection_payload),
            _json_dump(gate_payload) if gate_payload is not None else None,
        )
        self.initialize()
        with self._connect() as conn:
            conn.execute(
                """
                INSERT OR IGNORE INTO context_trigger_shadow (
                    id, occurred_at, rule, source_event_id,
                    evaluation_status, evaluation_reason, features_json,
                    projection_json, gate_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                values,
            )
            conn.commit()
            row = conn.execute(
                "SELECT * FROM context_trigger_shadow WHERE rule=? AND source_event_id=?",
                (evaluation.rule, evaluation.source_event_id),
            ).fetchone()
        if row is None:
            raise RuntimeError("shadow evaluation insert did not produce a row")
        return _decode_row(row)

    def list_evaluations(
        self,
        *,
        statuses: Iterable[str] | None = None,
        rule: str | None = None,
        limit: int = DEFAULT_SHADOW_LIST_LIMIT,
        before_occurred_at: float | None = None,
    ) -> list[dict[str, Any]]:
        self.initialize()
        limit = _bounded_limit(limit)
        status_values = _statuses(statuses)
        clauses: list[str] = []
        params: list[Any] = []
        if status_values:
            clauses.append(
                "evaluation_status IN (" + ",".join("?" for _ in status_values) + ")"
            )
            params.extend(status_values)
        if rule is not None:
            clauses.append("rule=?")
            params.append(_required_text(rule, key="rule"))
        if before_occurred_at is not None:
            clauses.append("occurred_at < ?")
            params.append(_finite_number(before_occurred_at, key="before_occurred_at"))
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        params.append(limit)
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM context_trigger_shadow"
                + where
                + " ORDER BY occurred_at DESC, id DESC LIMIT ?",
                params,
            ).fetchall()
        return [_decode_row(row) for row in rows]

    def due_outcomes(
        self,
        *,
        reference_time: float | None = None,
        owner_window_sec: float = 30 * 60,
        limit: int = 500,
    ) -> list[dict[str, Any]]:
        reference = self._now() if reference_time is None else _finite_number(
            reference_time,
            key="reference_time",
        )
        owner_window = _finite_number(owner_window_sec, key="owner_window_sec")
        if owner_window <= 0:
            raise ValueError("owner_window_sec must be positive")
        self.initialize()
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM context_trigger_shadow
                WHERE outcome_evaluated_at IS NULL AND occurred_at <= ?
                ORDER BY occurred_at ASC, id ASC
                LIMIT ?
                """,
                (reference - owner_window, _bounded_limit(limit)),
            ).fetchall()
        return [_decode_row(row) for row in rows]

    def record_outcome(
        self,
        evaluation_id: str,
        *,
        legacy_sentinel_woke_within_5m: bool,
        owner_message_within_30m: bool,
        evaluated_at: float | None = None,
    ) -> dict[str, Any]:
        if not isinstance(legacy_sentinel_woke_within_5m, bool):
            raise ValueError("legacy_sentinel_woke_within_5m must be boolean")
        if not isinstance(owner_message_within_30m, bool):
            raise ValueError("owner_message_within_30m must be boolean")
        evaluated = self._now() if evaluated_at is None else _finite_number(
            evaluated_at,
            key="evaluated_at",
        )
        self.initialize()
        with self._connect() as conn:
            cursor = conn.execute(
                """
                UPDATE context_trigger_shadow
                SET legacy_sentinel_woke_within_5m=?,
                    owner_message_within_30m=?,
                    outcome_evaluated_at=?
                WHERE id=? AND outcome_evaluated_at IS NULL
                """,
                (
                    int(legacy_sentinel_woke_within_5m),
                    int(owner_message_within_30m),
                    evaluated,
                    _required_text(evaluation_id, key="evaluation_id"),
                ),
            )
            conn.commit()
            row = conn.execute(
                "SELECT * FROM context_trigger_shadow WHERE id=?",
                (evaluation_id,),
            ).fetchone()
        if row is None:
            raise KeyError("shadow evaluation not found")
        if cursor.rowcount == 0 and row["outcome_evaluated_at"] is None:
            raise RuntimeError("shadow outcome update failed")
        return _decode_row(row)

    def set_owner_label(
        self,
        evaluation_id: str,
        label: str,
        *,
        labeled_at: float | None = None,
    ) -> dict[str, Any]:
        label = str(label or "").strip()
        if label not in OWNER_LABELS:
            raise ValueError("owner_label must be right, wrong, or indifferent")
        labeled = self._now() if labeled_at is None else _finite_number(
            labeled_at,
            key="labeled_at",
        )
        self.initialize()
        evaluation_id = _required_text(evaluation_id, key="evaluation_id")
        with self._connect() as conn:
            row = conn.execute(
                "SELECT evaluation_status FROM context_trigger_shadow WHERE id=?",
                (evaluation_id,),
            ).fetchone()
            if row is None:
                raise KeyError("shadow evaluation not found")
            if row["evaluation_status"] != EVALUATION_MATCHED:
                raise ValueError("only matched shadow evaluations may be labeled")
            conn.execute(
                """
                UPDATE context_trigger_shadow
                SET owner_label=?, labeled_at=?
                WHERE id=?
                """,
                (label, labeled, evaluation_id),
            )
            conn.commit()
            row = conn.execute(
                "SELECT * FROM context_trigger_shadow WHERE id=?",
                (evaluation_id,),
            ).fetchone()
        return _decode_row(row)

    def stats(self) -> dict[str, Any]:
        self.initialize()
        with self._connect() as conn:
            status_rows = conn.execute(
                "SELECT evaluation_status, COUNT(*) AS n "
                "FROM context_trigger_shadow GROUP BY evaluation_status"
            ).fetchall()
            rule_rows = conn.execute(
                "SELECT rule, evaluation_status, COUNT(*) AS n "
                "FROM context_trigger_shadow GROUP BY rule, evaluation_status"
            ).fetchall()
            label_rows = conn.execute(
                "SELECT owner_label, COUNT(*) AS n FROM context_trigger_shadow "
                "WHERE owner_label IS NOT NULL GROUP BY owner_label"
            ).fetchall()
            pending_outcomes = conn.execute(
                "SELECT COUNT(*) FROM context_trigger_shadow "
                "WHERE outcome_evaluated_at IS NULL"
            ).fetchone()[0]
        by_status = {status: 0 for status in sorted(EVALUATION_STATUSES)}
        by_status.update({row["evaluation_status"]: row["n"] for row in status_rows})
        by_rule: dict[str, dict[str, int]] = {}
        for row in rule_rows:
            counts = by_rule.setdefault(
                row["rule"],
                {status: 0 for status in sorted(EVALUATION_STATUSES)},
            )
            counts[row["evaluation_status"]] = row["n"]
        labels = {label: 0 for label in sorted(OWNER_LABELS)}
        labels.update({row["owner_label"]: row["n"] for row in label_rows})
        total = sum(by_status.values())
        unavailable = by_status.get("unavailable", 0)
        return {
            "total": total,
            "by_status": by_status,
            "by_rule": by_rule,
            "owner_labels": labels,
            "pending_outcomes": int(pending_outcomes),
            "unavailable_ratio": (unavailable / total) if total else 0.0,
        }

    def _connect_raw(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path), timeout=10.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=10000")
        return conn

    def _connect(self) -> sqlite3.Connection:
        return self._connect_raw()


def _evaluation_id(rule: str, source_event_id: str) -> str:
    digest = hashlib.sha256(
        f"{rule}\0{source_event_id}".encode("utf-8")
    ).hexdigest()[:32]
    return f"ctx_shadow_{digest}"


def _decode_row(row: sqlite3.Row) -> dict[str, Any]:
    result = dict(row)
    for key in ("features_json", "projection_json", "gate_json"):
        raw = result.pop(key)
        result[key.removesuffix("_json")] = json.loads(raw) if raw is not None else None
    for key in ("legacy_sentinel_woke_within_5m", "owner_message_within_30m"):
        if result[key] is not None:
            result[key] = bool(result[key])
    return result


def _json_dump(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _mapping(value: Mapping[str, Any], *, key: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError(f"shadow {key} must be an object")
    result = dict(value)
    _json_dump(result)
    return result


def _required_text(value: Any, *, key: str) -> str:
    text = str(value or "").strip()
    if not text:
        raise ValueError(f"shadow {key} is required")
    return text


def _finite_number(value: Any, *, key: str) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise ValueError(f"shadow {key} must be a number")
    result = float(value)
    if not math.isfinite(result):
        raise ValueError(f"shadow {key} must be finite")
    return result


def _statuses(values: Iterable[str] | None) -> list[str]:
    if values is None:
        return []
    result = sorted({str(value or "").strip() for value in values})
    unknown = sorted(set(result).difference(EVALUATION_STATUSES))
    if unknown:
        raise ValueError(f"unknown shadow evaluation statuses: {unknown!r}")
    return result


def _bounded_limit(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError("shadow limit must be an integer")
    if value <= 0:
        raise ValueError("shadow limit must be positive")
    return min(value, MAX_SHADOW_LIST_LIMIT)


__all__ = [
    "ContextTriggerShadowStore",
    "DEFAULT_SHADOW_LIST_LIMIT",
    "MAX_SHADOW_LIST_LIMIT",
    "OWNER_LABEL_INDIFFERENT",
    "OWNER_LABEL_RIGHT",
    "OWNER_LABEL_WRONG",
    "OWNER_LABELS",
]
