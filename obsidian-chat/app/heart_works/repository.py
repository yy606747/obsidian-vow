"""Durable work revisions, tool attempts and publication outbox."""
from __future__ import annotations

import json
import time
from uuid import uuid4

import aiosqlite
from database import get_db


def dumps(value):
    return json.dumps(value, ensure_ascii=False)


def decode(row):
    if row is None:
        return None
    result = dict(row)
    for key in list(result):
        if key.endswith('_json'):
            result[key[:-5]] = json.loads(result[key] or 'null')
    return result


async def init_tables(db):
    await db.executescript('''
    CREATE TABLE IF NOT EXISTS heart_works (
        id TEXT PRIMARY KEY, origin_source TEXT NOT NULL, origin_turn_id TEXT NOT NULL,
        conv_id TEXT NOT NULL, origin_msg_id TEXT, model_key TEXT NOT NULL,
        intent_raw TEXT NOT NULL, context_snapshot_json TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'queued', revision INTEGER NOT NULL DEFAULT 0,
        cover_json TEXT NOT NULL DEFAULT '[]', body_json TEXT NOT NULL DEFAULT '[]',
        event_note_raw TEXT NOT NULL DEFAULT '', event_text TEXT NOT NULL DEFAULT '',
        feedback_json TEXT NOT NULL DEFAULT '[]', created_at REAL NOT NULL, published_at REAL,
        updated_at REAL NOT NULL, last_error TEXT NOT NULL DEFAULT '',
        lease_token TEXT NOT NULL DEFAULT '', lease_until REAL NOT NULL DEFAULT 0,
        failures INTEGER NOT NULL DEFAULT 0, no_progress INTEGER NOT NULL DEFAULT 0,
        retry_at REAL NOT NULL DEFAULT 0, UNIQUE(origin_source, origin_turn_id)
    );
    CREATE INDEX IF NOT EXISTS heart_works_status ON heart_works(status, retry_at);
    CREATE TABLE IF NOT EXISTS heart_work_revisions (
        work_id TEXT NOT NULL, revision INTEGER NOT NULL, raw TEXT NOT NULL,
        parsed_json TEXT NOT NULL, created_at REAL NOT NULL, PRIMARY KEY(work_id, revision)
    );
    CREATE TABLE IF NOT EXISTS heart_work_tasks (
        id TEXT PRIMARY KEY, work_id TEXT NOT NULL, request_key TEXT NOT NULL,
        tool TEXT NOT NULL, query TEXT NOT NULL, marker TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'queued', attempt INTEGER NOT NULL DEFAULT 0,
        requested_revision INTEGER NOT NULL, result_json TEXT NOT NULL DEFAULT '{}',
        error TEXT NOT NULL DEFAULT '', updated_at REAL NOT NULL,
        UNIQUE(work_id, request_key)
    );
    CREATE TABLE IF NOT EXISTS heart_work_assets (
        id TEXT PRIMARY KEY, work_id TEXT NOT NULL, task_id TEXT NOT NULL,
        type TEXT NOT NULL, path TEXT NOT NULL, metadata_json TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS heart_work_jobs (
        work_id TEXT NOT NULL, kind TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'queued',
        attempts INTEGER NOT NULL DEFAULT 0, retry_at REAL NOT NULL DEFAULT 0,
        last_error TEXT NOT NULL DEFAULT '', PRIMARY KEY(work_id, kind)
    );
    CREATE TABLE IF NOT EXISTS heart_work_events (
        work_id TEXT PRIMARY KEY, event_memory_id TEXT NOT NULL DEFAULT '',
        event_text TEXT NOT NULL, embedding BLOB, embedding_signature TEXT NOT NULL DEFAULT '',
        published_at REAL NOT NULL, source_hash TEXT NOT NULL
    );
    ''')


class Repository:
    def __init__(self, db_factory=get_db, now=time.time):
        self.db, self.now = db_factory, now

    async def read(self, sql, params=(), *, many=False):
        async with self.db() as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(sql, params)
            return [decode(r) for r in await cursor.fetchall()] if many else decode(await cursor.fetchone())

    async def execute(self, sql, params=()):
        async with self.db() as db:
            cursor = await db.execute(sql, params)
            await db.commit()
            return cursor.rowcount

    async def get(self, work_id):
        return await self.read('SELECT * FROM heart_works WHERE id=?', (work_id,))

    async def enqueue(self, *, origin, intent_raw, context_snapshot):
        now, work_id = self.now(), 'hw_' + uuid4().hex
        await self.execute('''INSERT OR IGNORE INTO heart_works
            (id,origin_source,origin_turn_id,conv_id,origin_msg_id,model_key,intent_raw,
             context_snapshot_json,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?)''',
            (work_id, origin['source'], origin['turn_id'], origin['conv_id'], origin.get('msg_id'),
             origin['model_key'], intent_raw, dumps(context_snapshot), now, now))
        return await self.read('SELECT * FROM heart_works WHERE origin_source=? AND origin_turn_id=?',
                               (origin['source'], origin['turn_id']))

    async def claim(self, work_id):
        token, now = uuid4().hex, self.now()
        changed = await self.execute('''UPDATE heart_works SET lease_token=?,lease_until=?,updated_at=?
            WHERE id=? AND status IN ('queued','generating','waiting_tools','ready')
            AND lease_until<? AND retry_at<=?''', (token, now + 1500, now, work_id, now, now))
        return token if changed else None

    async def update(self, work_id, token, **fields):
        fields.update(updated_at=self.now(), lease_until=self.now() + 1500)
        columns = ','.join(key + '=?' for key in fields)
        changed = await self.execute(f'''UPDATE heart_works SET {columns} WHERE id=? AND lease_token=?
            AND status NOT IN ('published','abandoned','deleted')''', (*fields.values(), work_id, token))
        if not changed:
            raise WorkStopped()

    async def save_revision(self, work_id, token, raw, parsed):
        async with self.db() as db:
            db.row_factory = aiosqlite.Row
            await db.execute('BEGIN IMMEDIATE')
            row = await (await db.execute('SELECT * FROM heart_works WHERE id=? AND lease_token=?', (work_id, token))).fetchone()
            if not row or row['status'] in {'deleted', 'abandoned', 'published'}:
                raise WorkStopped()
            previous = await (await db.execute('SELECT raw FROM heart_work_revisions WHERE work_id=? ORDER BY revision DESC LIMIT 1', (work_id,))).fetchone()
            repeats = row['no_progress'] + 1 if previous and previous[0] == raw else 0
            revision = row['revision'] + 1
            await db.execute('INSERT INTO heart_work_revisions VALUES (?,?,?,?,?)',
                             (work_id, revision, raw, dumps(parsed), self.now()))
            status = 'paused' if repeats >= 2 else 'waiting_tools'
            await db.execute('''UPDATE heart_works SET revision=?,status=?,no_progress=?,failures=0,
                last_error=?,updated_at=? WHERE id=?''', (revision, status, repeats,
                'no_progress' if repeats >= 2 else '', self.now(), work_id))
            await db.commit()
        return revision

    async def latest_revision(self, work_id):
        return await self.read('SELECT * FROM heart_work_revisions WHERE work_id=? ORDER BY revision DESC LIMIT 1', (work_id,))

    async def task(self, work_id, slot, revision):
        task_id = 'hwt_' + uuid4().hex
        await self.execute('''INSERT OR IGNORE INTO heart_work_tasks
            (id,work_id,request_key,tool,query,marker,requested_revision,updated_at) VALUES (?,?,?,?,?,?,?,?)''',
            (task_id, work_id, slot['request_key'], slot['tool'], slot['query'], slot['marker'], revision, self.now()))
        return await self.read('SELECT * FROM heart_work_tasks WHERE work_id=? AND request_key=?', (work_id, slot['request_key']))

    async def task_result(self, task, result, error=''):
        async with self.db() as db:
            await db.execute('UPDATE heart_work_tasks SET status=?,result_json=?,error=?,updated_at=? WHERE id=?',
                ('failed' if error else 'succeeded', dumps(result), error, self.now(), task['id']))
            if result.get('asset'):
                asset = result['asset']
                await db.execute('INSERT OR REPLACE INTO heart_work_assets VALUES (?,?,?,?,?,?)',
                    (asset['id'], task['work_id'], task['id'], asset['type'], asset['path'], dumps(asset)))
            await db.commit()

    async def publish(self, work_id, token, *, cover, body, note, event):
        import hashlib
        now = self.now()
        async with self.db() as db:
            await db.execute('BEGIN IMMEDIATE')
            cursor = await db.execute('''UPDATE heart_works SET status='published',cover_json=?,body_json=?,
                event_note_raw=?,event_text=?,published_at=?,updated_at=?,lease_until=0,lease_token=''
                WHERE id=? AND lease_token=? AND status='ready' ''',
                (dumps(cover), dumps(body), note, event, now, now, work_id, token))
            if cursor.rowcount != 1:
                raise WorkStopped()
            await db.execute('INSERT INTO heart_work_events(work_id,event_text,published_at,source_hash) VALUES (?,?,?,?)',
                             (work_id, event, now, hashlib.sha256(event.encode()).hexdigest()))
            for kind in ('notify', 'memory'):
                await db.execute('INSERT OR IGNORE INTO heart_work_jobs(work_id,kind) VALUES (?,?)', (work_id, kind))
            await db.commit()

    async def abandon(self, work_id, reason):
        return await self.execute("UPDATE heart_works SET status='abandoned',last_error=?,lease_until=0 WHERE id=? AND status NOT IN ('published','deleted')", (reason, work_id))

    async def resume(self, work_id):
        return await self.execute("UPDATE heart_works SET status='queued',failures=0,no_progress=0,retry_at=0,lease_until=0 WHERE id=? AND status='paused'", (work_id,))

    async def delete(self, work_id):
        import hashlib
        async with self.db() as db:
            await db.execute('BEGIN IMMEDIATE')
            await db.execute("UPDATE heart_works SET status='deleted',lease_token='',lease_until=0 WHERE id=?", (work_id,))
            await db.execute("UPDATE heart_work_jobs SET status='cancelled' WHERE work_id=?", (work_id,))
            row = await (await db.execute('SELECT event_memory_id FROM heart_work_events WHERE work_id=?', (work_id,))).fetchone()
            await db.execute('DELETE FROM heart_work_events WHERE work_id=?', (work_id,))
            memory_id = row[0] if row and row[0] else 'mem_key_' + hashlib.sha256(('heart_work:' + work_id).encode()).hexdigest()
            await db.execute('DELETE FROM memories WHERE id=?', (memory_id,))
            await db.execute("UPDATE memory_items SET status='archived',visibility='hidden' WHERE legacy_memory_id=?", (memory_id,))
            await db.commit()


class WorkStopped(Exception):
    """A concurrent delete/abandon/lease replacement prevents publication."""
