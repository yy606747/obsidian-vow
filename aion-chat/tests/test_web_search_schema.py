import asyncio
import sqlite3

from app.web_search.schema import init_web_search_tables


class _Db:
    def __init__(self):
        self.conn = sqlite3.connect(":memory:")

    async def execute(self, sql, params=()):
        return self.conn.execute(sql, params)


def test_web_search_schema_has_consumption_fields_and_indexes():
    db = _Db()
    asyncio.run(init_web_search_tables(db))

    columns = {
        row[1] for row in db.conn.execute("PRAGMA table_info(web_search_pending)")
    }
    assert {
        "status",
        "expires_at",
        "bound_turn_id",
        "bound_at",
        "consumed_by_message_id",
        "consumed_at",
    } <= columns
    indexes = {
        row[1] for row in db.conn.execute("PRAGMA index_list(web_search_pending)")
    }
    assert {
        "idx_web_search_conv_status_ready",
        "idx_web_search_status_created",
        "idx_web_search_bound_turn",
    } <= indexes
