"""Pure SQL repository for desire versions; transaction-neutral by design."""

_COLUMNS = (
    "id, previous_version_id, content, change_note, origin_request_id, "
    "working_model_id, writer_model, prompt_version, created_at"
)
_COLUMN_NAMES = [name.strip() for name in _COLUMNS.split(",")]


def _row_to_dict(row) -> dict:
    return dict(zip(_COLUMN_NAMES, row))


async def insert_version(
    db,
    *,
    version_id: str,
    previous_version_id: str | None,
    content: str,
    change_note: str,
    origin_request_id: str,
    working_model_id: str,
    writer_model: str | None,
    prompt_version: str | None,
    created_at: float,
) -> dict:
    values = (
        version_id,
        previous_version_id,
        content,
        change_note,
        origin_request_id,
        working_model_id,
        writer_model,
        prompt_version,
        created_at,
    )
    await db.execute(
        f"INSERT INTO desire_versions ({_COLUMNS}) VALUES (?,?,?,?,?,?,?,?,?)",
        values,
    )
    return _row_to_dict(values)


async def get_version(db, version_id: str) -> dict | None:
    cursor = await db.execute(
        f"SELECT {_COLUMNS} FROM desire_versions WHERE id=?",
        (version_id,),
    )
    row = await cursor.fetchone()
    return _row_to_dict(row) if row else None


async def get_head(db) -> dict | None:
    cursor = await db.execute(
        f"SELECT {_COLUMNS} FROM desire_versions AS current "
        "WHERE NOT EXISTS ("
        "SELECT 1 FROM desire_versions AS child "
        "WHERE child.previous_version_id = current.id"
        ") ORDER BY current.created_at DESC, current.rowid DESC LIMIT 1"
    )
    row = await cursor.fetchone()
    return _row_to_dict(row) if row else None


async def list_versions(db) -> list[dict]:
    cursor = await db.execute(
        f"SELECT {_COLUMNS} FROM desire_versions ORDER BY created_at, rowid"
    )
    return [_row_to_dict(row) for row in await cursor.fetchall()]


async def count_versions(db) -> int:
    cursor = await db.execute("SELECT COUNT(*) FROM desire_versions")
    row = await cursor.fetchone()
    return int(row[0])
