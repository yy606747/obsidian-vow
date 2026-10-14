"""Durable assets, operation receipts, and the next-turn feedback inbox."""

from __future__ import annotations

import json
import time
from collections import OrderedDict

import aiosqlite

from database import get_db


async def init_tables(db) -> None:
    await db.execute('''CREATE TABLE IF NOT EXISTS stickers (
        id TEXT PRIMARY KEY, file_hash TEXT NOT NULL UNIQUE, url TEXT NOT NULL,
        mime_type TEXT NOT NULL, summary_json TEXT NOT NULL DEFAULT '{}',
        source_message_id TEXT NOT NULL DEFAULT '', creation_prompt TEXT NOT NULL DEFAULT '',
        status TEXT NOT NULL DEFAULT 'pending', error TEXT NOT NULL DEFAULT '',
        created_at REAL NOT NULL, updated_at REAL NOT NULL
    )''')
    columns = {row[1] for row in await (await db.execute('PRAGMA table_info(stickers)')).fetchall()}
    if 'ai_saved' not in columns:
        # Existing assets were explicitly stolen or made by Core.
        await db.execute('ALTER TABLE stickers ADD COLUMN ai_saved INTEGER NOT NULL DEFAULT 1')
    if 'user_saved' not in columns:
        await db.execute('ALTER TABLE stickers ADD COLUMN user_saved INTEGER NOT NULL DEFAULT 0')
        # Imported images and images stolen from a chat came from the owner's collection.
        # Merely displaying a generated image in the old shared picker was not a collection action.
        await db.execute("""UPDATE stickers SET user_saved=1
            WHERE ai_saved=0 OR COALESCE(creation_prompt,'')='' OR COALESCE(source_message_id,'')!=''""")
        message_columns = {row[1] for row in await (await db.execute('PRAGMA table_info(messages)')).fetchall()}
        if {'role', 'attachments'} <= message_columns:
            rows = await (await db.execute("""SELECT attachments FROM messages
                WHERE role='user' AND attachments LIKE '%sticker%'""")).fetchall()
            sent_ids = set()
            for (raw,) in rows:
                try:
                    attachments = json.loads(raw)
                except (ValueError, TypeError):
                    continue
                if isinstance(attachments, list):
                    sent_ids.update(item['id'] for item in attachments if isinstance(item, dict)
                                    and item.get('type') == 'sticker' and isinstance(item.get('id'), str))
            await db.executemany('UPDATE stickers SET user_saved=1 WHERE id=?', [(sid,) for sid in sent_ids])
    await db.execute('''CREATE TABLE IF NOT EXISTS sticker_events (
        id TEXT PRIMARY KEY, action TEXT NOT NULL, conv_id TEXT NOT NULL,
        source_message_id TEXT NOT NULL DEFAULT '', assistant_message_id TEXT NOT NULL DEFAULT '',
        expression TEXT NOT NULL DEFAULT '', context_json TEXT NOT NULL DEFAULT '{}',
        status TEXT NOT NULL DEFAULT 'running', result_json TEXT NOT NULL DEFAULT '{}',
        error TEXT NOT NULL DEFAULT '', feedback_state TEXT NOT NULL DEFAULT 'none',
        claimed_turn_id TEXT, claimed_at REAL, consumed_by TEXT,
        created_at REAL NOT NULL, updated_at REAL NOT NULL
    )''')
    await db.execute('CREATE INDEX IF NOT EXISTS idx_sticker_inbox ON sticker_events(conv_id,feedback_state,created_at)')
    await db.execute('CREATE INDEX IF NOT EXISTS idx_sticker_wishes ON sticker_events(action,status,created_at)')
    # Recovery performs no provider calls. A later explicit save can retry an interrupted summary.
    await db.execute("UPDATE stickers SET status='failed',error='处理被重启中断' WHERE status IN ('pending','running')")
    await db.execute("UPDATE sticker_events SET status='failed',error='处理被重启中断',feedback_state='pending' WHERE status='running'")
    await db.execute("UPDATE sticker_events SET feedback_state='pending',claimed_turn_id=NULL,claimed_at=NULL WHERE feedback_state='claimed'")


class StickerRepository:
    def __init__(self, *, get_db_factory=get_db, now=time.time):
        self._db = get_db_factory
        self.now = now

    async def get(self, sticker_id: str) -> dict | None:
        async with self._db() as db:
            db.row_factory = aiosqlite.Row
            row = await (await db.execute('SELECT * FROM stickers WHERE id=?', (sticker_id,))).fetchone()
            return dict(row) if row else None

    async def put_asset(self, *, digest: str, url: str, mime_type: str,
                        source_message_id: str = '', creation_prompt: str = '', ai_saved: bool = True,
                        user_saved: bool = False) -> dict:
        now = self.now()
        sticker_id = 'st_' + digest[:24]
        async with self._db() as db:
            await db.execute('''INSERT OR IGNORE INTO stickers
                (id,file_hash,url,mime_type,source_message_id,creation_prompt,created_at,updated_at,ai_saved,user_saved)
                VALUES (?,?,?,?,?,?,?,?,?,?)''',
                (sticker_id, digest, url, mime_type, source_message_id, creation_prompt, now, now,
                 int(ai_saved), int(user_saved)))
            if ai_saved:
                await db.execute('''UPDATE stickers SET ai_saved=1,updated_at=?,
                    source_message_id=CASE WHEN source_message_id='' THEN ? ELSE source_message_id END
                    WHERE id=?''', (now, source_message_id, sticker_id))
            if user_saved:
                await db.execute('UPDATE stickers SET user_saved=1,updated_at=? WHERE id=?', (now, sticker_id))
            await db.commit()
        return await self.get(sticker_id)

    async def collect_for_user(self, sticker_id: str) -> dict | None:
        async with self._db() as db:
            await db.execute('UPDATE stickers SET user_saved=1,updated_at=? WHERE id=?', (self.now(), sticker_id))
            await db.commit()
        return await self.get(sticker_id)

    async def remove_for_user(self, sticker_ids: list[str]) -> None:
        # Collection membership is independent of Core's collection and message attachments.
        now = self.now()
        async with self._db() as db:
            await db.executemany(
                'UPDATE stickers SET user_saved=0,updated_at=? WHERE id=? AND user_saved=1',
                [(now, sticker_id) for sticker_id in sticker_ids])
            await db.commit()

    async def set_summary(self, sticker_id: str, *, summary: dict | None = None, error: str = '') -> None:
        async with self._db() as db:
            await db.execute('UPDATE stickers SET summary_json=?,status=?,error=?,updated_at=? WHERE id=?',
                             (json.dumps(summary or {}, ensure_ascii=False), 'ready' if summary else 'failed',
                              error, self.now(), sticker_id))
            await db.commit()

    async def library(self) -> list[dict]:
        async with self._db() as db:
            db.row_factory = aiosqlite.Row
            rows = await (await db.execute("SELECT * FROM stickers WHERE status='ready' AND ai_saved=1 ORDER BY created_at,id")).fetchall()
            return [dict(row) for row in rows]

    async def panel_library(self) -> list[dict]:
        async with self._db() as db:
            db.row_factory = aiosqlite.Row
            rows = await (await db.execute('SELECT * FROM stickers WHERE user_saved=1 ORDER BY created_at DESC,id')).fetchall()
            return [dict(row) for row in rows]

    async def begin(self, event_id: str, *, action: str, conv_id: str,
                    source_message_id: str = '', assistant_message_id: str = '',
                    expression: str = '', context: dict | None = None) -> tuple[bool, dict]:
        now = self.now()
        async with self._db() as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute('''INSERT OR IGNORE INTO sticker_events
                (id,action,conv_id,source_message_id,assistant_message_id,expression,context_json,created_at,updated_at)
                VALUES (?,?,?,?,?,?,?,?,?)''',
                (event_id, action, conv_id, source_message_id, assistant_message_id, expression,
                 json.dumps(context or {}, ensure_ascii=False), now, now))
            row = await (await db.execute('SELECT * FROM sticker_events WHERE id=?', (event_id,))).fetchone()
            await db.commit()
            return cur.rowcount == 1, dict(row)

    async def finish(self, event_id: str, *, status: str, result: dict | None = None,
                     error: str = '', notify: bool = True) -> dict:
        result = {**(result or {}), 'event_id': event_id, 'status': status}
        async with self._db() as db:
            await db.execute('''UPDATE sticker_events SET status=?,result_json=?,error=?,
                feedback_state=?,updated_at=? WHERE id=?''',
                (status, json.dumps(result, ensure_ascii=False), error,
                 'pending' if notify else 'none', self.now(), event_id))
            await db.commit()
        return result

    async def claim(self, *, conv_id: str, turn_id: str, user_name: str, ai_name: str) -> dict:
        from .prompts import feedback
        now = self.now()
        async with self._db() as db:
            db.row_factory = aiosqlite.Row
            await db.execute('BEGIN IMMEDIATE')
            await db.execute("UPDATE sticker_events SET feedback_state='pending',claimed_turn_id=NULL WHERE feedback_state='claimed' AND claimed_at<?", (now - 600,))
            rows = await (await db.execute('''SELECT * FROM sticker_events WHERE conv_id=?
                AND (feedback_state='pending' OR (feedback_state='claimed' AND claimed_turn_id=?))
                ORDER BY created_at,id LIMIT 12''', (conv_id, turn_id))).fetchall()
            for row in rows:
                await db.execute("UPDATE sticker_events SET feedback_state='claimed',claimed_turn_id=?,claimed_at=? WHERE id=?",
                                 (turn_id, now, row['id']))
            await db.commit()
        return {'bound_turn_id': turn_id, 'block': feedback([dict(r) for r in rows], user_name=user_name, ai_name=ai_name)}

    async def finalize_in_tx(self, db, *, turn_id: str, message_id: str, supplemented: list[str] = (), replay_from: str = '') -> None:
        if turn_id:
            await db.execute("UPDATE sticker_events SET feedback_state='consumed',consumed_by=? WHERE claimed_turn_id=? AND feedback_state='claimed'",
                             (message_id, turn_id))
        for event_id in supplemented:
            await db.execute("UPDATE sticker_events SET feedback_state='consumed',consumed_by=? WHERE id=?", (message_id, event_id))
        if replay_from:
            await db.execute("UPDATE sticker_events SET consumed_by=? WHERE consumed_by=?", (message_id, replay_from))

    async def replay(self, message_id: str, *, user_name: str, ai_name: str) -> dict:
        from .prompts import feedback
        async with self._db() as db:
            db.row_factory = aiosqlite.Row
            rows = await (await db.execute("SELECT * FROM sticker_events WHERE consumed_by=? ORDER BY created_at,id", (message_id,))).fetchall()
        return {'bound_turn_id': '', 'replayed_from': message_id,
                'block': feedback([dict(r) for r in rows], user_name=user_name, ai_name=ai_name)}

    async def release(self, turn_id: str, *, replayed_from: str = '') -> None:
        if not turn_id and not replayed_from:
            return
        async with self._db() as db:
            if turn_id:
                await db.execute("UPDATE sticker_events SET feedback_state='pending',claimed_turn_id=NULL,claimed_at=NULL WHERE claimed_turn_id=? AND feedback_state='claimed'", (turn_id,))
            if replayed_from:
                await db.execute("""UPDATE sticker_events SET feedback_state='pending',consumed_by=NULL,
                    claimed_turn_id=NULL,claimed_at=NULL,updated_at=?
                    WHERE consumed_by=? AND feedback_state='consumed'""", (self.now(), replayed_from))
            await db.commit()

    async def wishes(self, *, now: float | None = None) -> list[dict]:
        now = self.now() if now is None else now
        async with self._db() as db:
            db.row_factory = aiosqlite.Row
            rows = await (await db.execute("SELECT expression,context_json FROM sticker_events WHERE action='send' AND status='miss' AND created_at>=? AND created_at<=? ORDER BY created_at", (now - 86400, now))).fetchall()
        grouped = OrderedDict()
        for row in rows:
            key = row['expression']
            entry = grouped.setdefault(key, {'expression': key, 'count': 0, 'context': {}})
            entry['count'] += 1
            entry['context'] = json.loads(row['context_json'])
        return list(grouped.values())


sticker_repository = StickerRepository()
