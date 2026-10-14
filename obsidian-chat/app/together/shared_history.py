"""Bounded recent utterances shared by chat and ended companionship sessions."""

import aiosqlite


# Keep extra room for the usual 30-message window and settings changes,
# without turning an ended session into a permanent transcript archive.
RETAINED_MESSAGES = 200


async def retain_context_in_tx(db, session_id, *, conv_id):
    await db.execute("""INSERT OR IGNORE INTO together_context_messages
        (seq,id,session_id,conv_id,role,content,created_at)
        SELECT seq,id,session_id,?,role,content,created_at FROM together_messages
        WHERE session_id=? AND role IN ('user','assistant') AND content!=''
        ORDER BY seq DESC LIMIT ?""", (conv_id, session_id, RETAINED_MESSAGES))
    await db.execute("""DELETE FROM together_context_messages WHERE seq NOT IN
        (SELECT seq FROM together_context_messages ORDER BY created_at DESC,seq DESC LIMIT ?)""",
        (RETAINED_MESSAGES,))


async def context_rows_in_tx(db, conv_id, *, limit):
    from app.daily_chat.service import daily_chat
    scope = daily_chat.scope(conv_id)
    try:
        cursor = await db.execute(f"""SELECT id,conv_id,role,content,created_at,seq,
            session_id AS together_session_id, '[]' AS attachments
            FROM together_context_messages WHERE conv_id IN ({','.join('?' for _ in scope)})
            AND EXISTS (SELECT 1 FROM conversations c WHERE c.id=together_context_messages.conv_id)
            ORDER BY created_at DESC,seq DESC LIMIT ?""", (*scope, limit))
    except aiosqlite.OperationalError as exc:
        if 'no such table: together_context_messages' in str(exc):
            return []
        raise
    columns = [column[0] for column in cursor.description]
    return [dict(zip(columns, row)) for row in await cursor.fetchall()]


def select_shared_window(current, inherited, together, *, conv_id, limit):
    """N utterances, stable IDs, chronological order; events use no slots."""
    candidates = {row['id']: dict(row) for row in inherited}
    candidates.update({row['id']: dict(row) for row in current})
    candidates.update({row['id']: dict(row) for row in together})
    ordered = sorted(candidates.values(), key=lambda row: (
        row['created_at'], int(row.get('seq') or row.get('position') or 0), row['id']))
    real = [row for row in ordered if row['role'] in ('user', 'assistant')]
    selected = real[-limit:] if limit > 0 else []
    ids = {row['id'] for row in selected}
    oldest = selected[0]['created_at'] if selected else float('inf')
    selected = [row for row in ordered if row['id'] in ids or
                (row['role'] not in ('user', 'assistant') and row['created_at'] >= oldest)]
    old_ids = {row['id'] for row in inherited}
    def previous(row):
        return row['id'] in old_ids or (row.get('together_session_id') and row['conv_id'] != conv_id)
    return [row for row in selected if not previous(row)], [row for row in selected if previous(row)]


def ended_context_note(row):
    if row.get('together_session_id'):
        return '\n<meta>这句来自已经结束的相伴；当时的屏幕或镜头画面不是当前感知。</meta>'
    return ''
