"""vows 仓储：纯 SQL 层，全部方法接收已有 db 连接。

事务完全由调用编排层持有——本模块不执行 BEGIN / COMMIT / ROLLBACK，
防嵌套事务破坏原子性（设计 §3）。
"""

_COLUMNS = (
    "id, root_id, previous_version_id, content, status, origin_type, "
    "origin_conv_id, origin_message_id, created_at, status_changed_at, "
    "close_action, closed_reason"
)

_COLUMN_NAMES = [c.strip() for c in _COLUMNS.split(",")]


def _row_to_dict(row) -> dict:
    return dict(zip(_COLUMN_NAMES, row))


async def insert_vow(
    db,
    *,
    vow_id: str,
    root_id: str,
    previous_version_id: str | None,
    content: str,
    origin_type: str,
    origin_conv_id: str | None,
    origin_message_id: str | None,
    created_at: float,
) -> dict:
    await db.execute(
        f"INSERT INTO vows ({_COLUMNS}) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            vow_id, root_id, previous_version_id, content, "active", origin_type,
            origin_conv_id, origin_message_id, created_at, created_at, None, None,
        ),
    )
    return {
        "id": vow_id, "root_id": root_id, "previous_version_id": previous_version_id,
        "content": content, "status": "active", "origin_type": origin_type,
        "origin_conv_id": origin_conv_id, "origin_message_id": origin_message_id,
        "created_at": created_at, "status_changed_at": created_at,
        "close_action": None, "closed_reason": None,
    }


async def close_active(
    db,
    *,
    vow_id: str,
    new_status: str,
    close_action: str,
    closed_reason: str | None,
    status_changed_at: float,
) -> int:
    """关闭一条 active 誓约。返回 rowcount，调用方必须校验 ==1（设计 §3.1）。"""
    cursor = await db.execute(
        "UPDATE vows SET status=?, close_action=?, closed_reason=?, status_changed_at=? "
        "WHERE id=? AND status='active'",
        (new_status, close_action, closed_reason, status_changed_at, vow_id),
    )
    return cursor.rowcount


async def get_vow(db, vow_id: str) -> dict | None:
    cursor = await db.execute(f"SELECT {_COLUMNS} FROM vows WHERE id=?", (vow_id,))
    row = await cursor.fetchone()
    return _row_to_dict(row) if row else None


async def close_active_by_origin_conv(db, *, conv_id: str, status_changed_at: float) -> int:
    """批量撤约（§4.5 同规则的批量形态）：退役来源消息属于该会话的全部
    active 誓约，返回撤约条数。须在删除这些消息之前、同一事务内调用——
    子查询依赖 messages 行还在。UI 修订的 active 后继（origin_message_id
    为 NULL）天然不受波及。"""
    cursor = await db.execute(
        "UPDATE vows SET status='retired', close_action='origin_deleted', "
        "closed_reason=NULL, status_changed_at=? "
        "WHERE status='active' AND origin_message_id IN "
        "(SELECT id FROM messages WHERE conv_id=?)",
        (status_changed_at, conv_id),
    )
    return cursor.rowcount


async def get_by_origin_message(db, message_id: str) -> dict | None:
    cursor = await db.execute(
        f"SELECT {_COLUMNS} FROM vows WHERE origin_message_id=?", (message_id,)
    )
    row = await cursor.fetchone()
    return _row_to_dict(row) if row else None


async def list_active(db) -> list[dict]:
    cursor = await db.execute(
        f"SELECT {_COLUMNS} FROM vows WHERE status='active' ORDER BY created_at"
    )
    rows = await cursor.fetchall()
    return [_row_to_dict(r) for r in rows]


async def list_all(db) -> list[dict]:
    cursor = await db.execute(f"SELECT {_COLUMNS} FROM vows ORDER BY created_at")
    rows = await cursor.fetchall()
    return [_row_to_dict(r) for r in rows]


_TIP_EXTRA_COLUMNS = ["version_count", "root_created_at", "root_origin_type"]


async def _list_tips_for_status(
    db,
    *,
    status: str,
    order_by: str,
    limit: int | None = None,
    offset: int = 0,
) -> list[dict]:
    sql = (
        f"SELECT {_COLUMNS}, "
        "(SELECT COUNT(*) FROM vows v2 WHERE v2.root_id = vows.root_id) AS version_count, "
        "(SELECT v3.created_at FROM vows v3 WHERE v3.id = vows.root_id) AS root_created_at, "
        "(SELECT v3.origin_type FROM vows v3 WHERE v3.id = vows.root_id) AS root_origin_type "
        f"FROM vows WHERE status=? ORDER BY {order_by}"
    )
    params: list = [status]
    if limit is not None:
        sql += " LIMIT ? OFFSET ?"
        params.extend((limit, offset))
    cursor = await db.execute(sql, params)
    rows = await cursor.fetchall()
    return [dict(zip(_COLUMN_NAMES + _TIP_EXTRA_COLUMNS, r)) for r in rows]


async def list_tips(
    db,
    *,
    fulfilled_limit: int = 50,
    fulfilled_offset: int = 0,
) -> dict:
    """管理页列表（§13-6）：每条链只取当前版本（非 superseded 行即链尾），
    只含页面展示的 active / fulfilled，附 root 起源信息与版本数；
    active 数量受业务上限约束，fulfilled 使用分页避免列表永久膨胀。"""
    active_items = await _list_tips_for_status(
        db, status="active", order_by="created_at"
    )
    fulfilled_page = await _list_tips_for_status(
        db,
        status="fulfilled",
        order_by="status_changed_at DESC, created_at DESC",
        limit=fulfilled_limit + 1,
        offset=fulfilled_offset,
    )
    fulfilled_has_more = len(fulfilled_page) > fulfilled_limit
    fulfilled_items = fulfilled_page[:fulfilled_limit]
    return {
        "items": active_items + fulfilled_items,
        "active_items": active_items,
        "fulfilled_items": fulfilled_items,
        "fulfilled_has_more": fulfilled_has_more,
        "fulfilled_next_offset": (
            fulfilled_offset + len(fulfilled_items) if fulfilled_has_more else None
        ),
    }


async def list_chain(db, root_id: str) -> list[dict]:
    cursor = await db.execute(
        f"SELECT {_COLUMNS} FROM vows WHERE root_id=? ORDER BY created_at", (root_id,)
    )
    rows = await cursor.fetchall()
    return [_row_to_dict(r) for r in rows]


async def list_chain_page(
    db,
    root_id: str,
    *,
    limit: int = 50,
    offset: int = 0,
) -> dict:
    """从链尾向前分页；页内仍按时间正序，便于前端拼接和展示。"""
    cursor = await db.execute(
        f"SELECT {_COLUMNS}, COUNT(*) OVER() FROM vows WHERE root_id=? "
        "ORDER BY created_at DESC, rowid DESC LIMIT ? OFFSET ?",
        (root_id, limit, offset),
    )
    rows = await cursor.fetchall()
    total = rows[0][-1] if rows else 0
    items = [_row_to_dict(r[:-1]) for r in reversed(rows)]
    next_offset = offset + len(items)
    has_more = next_offset < total
    return {
        "items": items,
        "total": total,
        "has_more": has_more,
        "next_offset": next_offset if has_more else None,
    }


async def count_active_with_content(db, content: str) -> int:
    cursor = await db.execute(
        "SELECT COUNT(*) FROM vows WHERE status='active' AND content=?", (content,)
    )
    row = await cursor.fetchone()
    return row[0]


async def count_active(db) -> int:
    cursor = await db.execute("SELECT COUNT(*) FROM vows WHERE status='active'")
    row = await cursor.fetchone()
    return row[0]


async def sum_active_chars(db) -> int:
    cursor = await db.execute(
        "SELECT COALESCE(SUM(LENGTH(content)), 0) FROM vows WHERE status='active'"
    )
    row = await cursor.fetchone()
    return row[0]


async def count_ai_created_between(db, start_ts: float, end_ts: float) -> int:
    """当日 AI 立约计数。按 created_at 统计所有成功建立过的 AI vow，
    不论其后是否 retired / fulfilled / superseded / 被撤销（设计 §6）。"""
    cursor = await db.execute(
        "SELECT COUNT(*) FROM vows WHERE origin_type='ai_marker' "
        "AND created_at >= ? AND created_at < ?",
        (start_ts, end_ts),
    )
    row = await cursor.fetchone()
    return row[0]
