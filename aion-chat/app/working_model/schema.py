"""Working-model V2 tables.

CP0 only installs storage.  Runtime routing and writes remain disconnected.
"""


async def init_working_model_tables(db) -> None:
    await db.execute("""
        CREATE TABLE IF NOT EXISTS working_model_versions (
            id                  TEXT PRIMARY KEY,
            previous_version_id TEXT,
            content             TEXT NOT NULL,
            created_at          REAL NOT NULL,
            origin_conv_id      TEXT,
            origin_message_id   TEXT,
            origin_request_id   TEXT,
            reason              TEXT NOT NULL,
            writer_model        TEXT,
            prompt_version      TEXT,
            diff_ratio          REAL,
            flagged             INTEGER NOT NULL DEFAULT 0
        )
    """)
    await db.execute(
        "CREATE INDEX IF NOT EXISTS idx_wm_created "
        "ON working_model_versions(created_at DESC)"
    )
    await db.execute("""
        CREATE UNIQUE INDEX IF NOT EXISTS idx_wm_successor
            ON working_model_versions(previous_version_id)
            WHERE previous_version_id IS NOT NULL
    """)
    await db.execute("""
        CREATE UNIQUE INDEX IF NOT EXISTS idx_wm_request
            ON working_model_versions(origin_request_id)
            WHERE origin_request_id IS NOT NULL
    """)

    # CP2 defines the final request-state truth table in service tests.  CP0
    # deliberately leaves status unconstrained so that decision does not get
    # frozen prematurely into a migration-hostile CHECK clause.
    await db.execute("""
        CREATE TABLE IF NOT EXISTS working_model_requests (
            id                          TEXT PRIMARY KEY,
            conv_id                     TEXT,
            origin_user_message_id      TEXT,
            origin_assistant_message_id TEXT,
            statement                   TEXT NOT NULL,
            source                      TEXT NOT NULL,
            route                       TEXT CHECK (
                route IS NULL OR route IN ('memory','working_model','reject')
            ),
            gate_reason                 TEXT,
            gate_model                  TEXT,
            gate_prompt_version         TEXT,
            disposition                 TEXT CHECK (
                disposition IS NULL OR disposition IN ('integrated','memory','noop')
            ),
            writer_model                TEXT,
            writer_prompt_version       TEXT,
            writer_change_note          TEXT,
            resulting_memory_id         TEXT,
            status                      TEXT NOT NULL,
            failure_code                TEXT,
            parse_error_code            TEXT,
            created_at                  REAL NOT NULL,
            updated_at                  REAL NOT NULL
        )
    """)
    request_columns = {
        str(row[1])
        for row in await (await db.execute(
            "PRAGMA table_info(working_model_requests)"
        )).fetchall()
    }
    if "writer_change_note" not in request_columns:
        await db.execute(
            "ALTER TABLE working_model_requests ADD COLUMN writer_change_note TEXT"
        )
    if "parse_error_code" not in request_columns:
        await db.execute(
            "ALTER TABLE working_model_requests ADD COLUMN parse_error_code TEXT"
        )
    await db.execute(
        "CREATE INDEX IF NOT EXISTS idx_wm_requests_created "
        "ON working_model_requests(created_at DESC)"
    )
