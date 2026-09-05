"""Pure SQL repository for working-model versions and requests.

Every function receives an existing database connection.  Transaction
ownership belongs to the calling service; this module never begins, commits,
or rolls back a transaction.
"""

_VERSION_COLUMNS = (
    "id, previous_version_id, content, created_at, origin_conv_id, "
    "origin_message_id, origin_request_id, reason, writer_model, "
    "prompt_version, diff_ratio, flagged"
)
_VERSION_COLUMN_NAMES = [name.strip() for name in _VERSION_COLUMNS.split(",")]

_REQUEST_COLUMNS = (
    "id, conv_id, origin_user_message_id, origin_assistant_message_id, "
    "statement, source, route, gate_reason, gate_model, gate_prompt_version, "
    "disposition, writer_model, writer_prompt_version, writer_change_note, resulting_memory_id, "
    "status, failure_code, parse_error_code, created_at, updated_at"
)
_REQUEST_COLUMN_NAMES = [name.strip() for name in _REQUEST_COLUMNS.split(",")]


def _row_to_dict(row, names: list[str]) -> dict:
    return dict(zip(names, row))


async def insert_version(
    db,
    *,
    version_id: str,
    previous_version_id: str | None,
    content: str,
    created_at: float,
    origin_conv_id: str | None,
    origin_message_id: str | None,
    origin_request_id: str | None,
    reason: str,
    writer_model: str | None,
    prompt_version: str | None,
    diff_ratio: float | None,
    flagged: int = 0,
) -> dict:
    values = (
        version_id,
        previous_version_id,
        content,
        created_at,
        origin_conv_id,
        origin_message_id,
        origin_request_id,
        reason,
        writer_model,
        prompt_version,
        diff_ratio,
        flagged,
    )
    await db.execute(
        f"INSERT INTO working_model_versions ({_VERSION_COLUMNS}) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        values,
    )
    return _row_to_dict(values, _VERSION_COLUMN_NAMES)


async def get_version(db, version_id: str) -> dict | None:
    cursor = await db.execute(
        f"SELECT {_VERSION_COLUMNS} FROM working_model_versions WHERE id=?",
        (version_id,),
    )
    row = await cursor.fetchone()
    return _row_to_dict(row, _VERSION_COLUMN_NAMES) if row else None


async def get_head(db) -> dict | None:
    cursor = await db.execute(
        f"SELECT {_VERSION_COLUMNS} FROM working_model_versions AS current "
        "WHERE NOT EXISTS ("
        "SELECT 1 FROM working_model_versions AS child "
        "WHERE child.previous_version_id = current.id"
        ") ORDER BY current.created_at DESC, current.rowid DESC LIMIT 1"
    )
    row = await cursor.fetchone()
    return _row_to_dict(row, _VERSION_COLUMN_NAMES) if row else None


async def list_versions(db) -> list[dict]:
    cursor = await db.execute(
        f"SELECT {_VERSION_COLUMNS} FROM working_model_versions "
        "ORDER BY created_at, rowid"
    )
    return [_row_to_dict(row, _VERSION_COLUMN_NAMES) for row in await cursor.fetchall()]


async def count_versions(db) -> int:
    cursor = await db.execute("SELECT COUNT(*) FROM working_model_versions")
    row = await cursor.fetchone()
    return int(row[0])


async def insert_request(
    db,
    *,
    request_id: str,
    conv_id: str | None,
    origin_user_message_id: str | None,
    origin_assistant_message_id: str | None,
    statement: str,
    source: str,
    status: str,
    created_at: float,
    route: str | None = None,
    gate_reason: str | None = None,
    gate_model: str | None = None,
    gate_prompt_version: str | None = None,
    disposition: str | None = None,
    writer_model: str | None = None,
    writer_prompt_version: str | None = None,
    writer_change_note: str | None = None,
    resulting_memory_id: str | None = None,
    failure_code: str | None = None,
    parse_error_code: str | None = None,
    updated_at: float | None = None,
) -> dict:
    values = (
        request_id,
        conv_id,
        origin_user_message_id,
        origin_assistant_message_id,
        statement,
        source,
        route,
        gate_reason,
        gate_model,
        gate_prompt_version,
        disposition,
        writer_model,
        writer_prompt_version,
        writer_change_note,
        resulting_memory_id,
        status,
        failure_code,
        parse_error_code,
        created_at,
        created_at if updated_at is None else updated_at,
    )
    await db.execute(
        f"INSERT INTO working_model_requests ({_REQUEST_COLUMNS}) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        values,
    )
    return _row_to_dict(values, _REQUEST_COLUMN_NAMES)


async def get_request(db, request_id: str) -> dict | None:
    cursor = await db.execute(
        f"SELECT {_REQUEST_COLUMNS} FROM working_model_requests WHERE id=?",
        (request_id,),
    )
    row = await cursor.fetchone()
    return _row_to_dict(row, _REQUEST_COLUMN_NAMES) if row else None


async def update_request(
    db,
    *,
    request_id: str,
    status: str,
    updated_at: float,
    route: str | None = None,
    gate_reason: str | None = None,
    gate_model: str | None = None,
    gate_prompt_version: str | None = None,
    disposition: str | None = None,
    writer_model: str | None = None,
    writer_prompt_version: str | None = None,
    writer_change_note: str | None = None,
    resulting_memory_id: str | None = None,
    failure_code: str | None = None,
    parse_error_code: str | None = None,
) -> bool:
    """Replace the mutable audit outcome; transaction ownership stays outside."""

    cursor = await db.execute(
        "UPDATE working_model_requests SET "
        "route=?, gate_reason=?, gate_model=?, gate_prompt_version=?, "
        "disposition=?, writer_model=?, writer_prompt_version=?, writer_change_note=?, "
        "resulting_memory_id=?, status=?, failure_code=?, parse_error_code=?, updated_at=? "
        "WHERE id=?",
        (
            route,
            gate_reason,
            gate_model,
            gate_prompt_version,
            disposition,
            writer_model,
            writer_prompt_version,
            writer_change_note,
            resulting_memory_id,
            status,
            failure_code,
            parse_error_code,
            updated_at,
            request_id,
        ),
    )
    return cursor.rowcount == 1


async def list_requests(db) -> list[dict]:
    cursor = await db.execute(
        f"SELECT {_REQUEST_COLUMNS} FROM working_model_requests "
        "ORDER BY created_at, rowid"
    )
    return [_row_to_dict(row, _REQUEST_COLUMN_NAMES) for row in await cursor.fetchall()]
