import sqlite3

from scripts.tool_invocation_ledger_cleanup import cleanup_tool_invocation_events


def _ids(path):
    connection = sqlite3.connect(path)
    try:
        return [
            row[0]
            for row in connection.execute(
                "SELECT id FROM tool_invocation_events ORDER BY id"
            ).fetchall()
        ]
    finally:
        connection.close()


def test_cleanup_is_owner_triggered_and_respects_the_retention_window(tmp_path):
    db_path = tmp_path / "cleanup.db"
    now = 2_000_000_000.0
    connection = sqlite3.connect(db_path)
    try:
        connection.execute(
            "CREATE TABLE tool_invocation_events "
            "(id TEXT PRIMARY KEY, created_at REAL NOT NULL)"
        )
        connection.executemany(
            "INSERT INTO tool_invocation_events (id, created_at) VALUES (?, ?)",
            [
                ("old", now - 91 * 86400),
                ("recent", now - 89 * 86400),
            ],
        )
        connection.commit()
    finally:
        connection.close()

    preview = cleanup_tool_invocation_events(
        db_path,
        retention_days=90,
        now=now,
        dry_run=True,
    )
    assert preview["matched"] == 1
    assert preview["deleted"] == 0
    assert _ids(db_path) == ["old", "recent"]

    applied = cleanup_tool_invocation_events(
        db_path,
        retention_days=90,
        now=now,
    )
    assert applied["matched"] == 1
    assert applied["deleted"] == 1
    assert _ids(db_path) == ["recent"]
