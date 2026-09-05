"""Transaction-neutral SQL helpers for ``reflection_log``."""

from __future__ import annotations

import time
import uuid
from typing import Any


_COLUMNS = (
    "id, created_at, target_conv_id, clue, working_model_id, inverse_query, "
    "query_model, query_prompt_version, retrieved_items_json, verdict, reason, "
    "proposed_statement, outcome, reflection_model, reflection_prompt_version, "
    "resulting_request_id"
)
_COLUMN_NAMES = [item.strip() for item in _COLUMNS.split(",")]
_UPDATABLE = frozenset(
    {
        "inverse_query",
        "query_model",
        "query_prompt_version",
        "retrieved_items_json",
        "verdict",
        "reason",
        "proposed_statement",
        "outcome",
        "reflection_model",
        "reflection_prompt_version",
        "resulting_request_id",
    }
)


def _row_to_dict(row) -> dict[str, Any] | None:
    return dict(zip(_COLUMN_NAMES, row)) if row else None


def new_reflection_log_id() -> str:
    return "refl_" + uuid.uuid4().hex


async def insert_log(
    db,
    *,
    log_id: str,
    target_conv_id: str,
    clue: str,
    working_model_id: str,
    created_at: float | None = None,
) -> dict[str, Any]:
    values = (
        log_id,
        float(time.time() if created_at is None else created_at),
        target_conv_id,
        clue,
        working_model_id,
        None,
        None,
        None,
        None,
        None,
        None,
        None,
        None,
        None,
        None,
        None,
    )
    await db.execute(
        f"INSERT INTO reflection_log ({_COLUMNS}) VALUES "
        "(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        values,
    )
    return _row_to_dict(values) or {}


async def get_log(db, log_id: str) -> dict[str, Any] | None:
    cursor = await db.execute(
        f"SELECT {_COLUMNS} FROM reflection_log WHERE id=? LIMIT 1",
        (log_id,),
    )
    return _row_to_dict(await cursor.fetchone())


async def update_log(db, log_id: str, **changes: Any) -> dict[str, Any] | None:
    invalid = sorted(set(changes) - _UPDATABLE)
    if invalid:
        raise ValueError("unsupported reflection_log columns: " + ", ".join(invalid))
    if changes:
        columns = list(changes)
        assignments = ", ".join(f"{column}=?" for column in columns)
        cursor = await db.execute(
            f"UPDATE reflection_log SET {assignments} WHERE id=?",
            (*[changes[column] for column in columns], log_id),
        )
        if cursor.rowcount != 1:
            return None
    return await get_log(db, log_id)


async def link_request(db, *, log_id: str, request_id: str) -> bool:
    cursor = await db.execute(
        "UPDATE reflection_log SET resulting_request_id=? "
        "WHERE id=? AND (resulting_request_id IS NULL OR resulting_request_id=?)",
        (request_id, log_id, request_id),
    )
    return cursor.rowcount == 1


async def list_recent_clues(db, *, limit: int) -> list[str]:
    cursor = await db.execute(
        "SELECT clue FROM reflection_log ORDER BY created_at DESC, rowid DESC LIMIT ?",
        (max(int(limit), 0),),
    )
    return [str(row[0]) for row in await cursor.fetchall() if str(row[0] or "").strip()]


async def list_logs(db, *, limit: int | None = None) -> list[dict[str, Any]]:
    sql = f"SELECT {_COLUMNS} FROM reflection_log ORDER BY created_at, rowid"
    params: tuple[Any, ...] = ()
    if limit is not None:
        sql += " LIMIT ?"
        params = (max(int(limit), 0),)
    cursor = await db.execute(sql, params)
    return [_row_to_dict(row) or {} for row in await cursor.fetchall()]


__all__ = [
    "get_log",
    "insert_log",
    "link_request",
    "list_logs",
    "list_recent_clues",
    "new_reflection_log_id",
    "update_log",
]
