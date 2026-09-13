"""SQLite requests, ordered idempotent receipts, and turn-scoped fact snapshots."""
import json
import time
from uuid import uuid4

import aiosqlite
from database import get_db


async def init_tables(db):
    await db.execute('''CREATE TABLE IF NOT EXISTS look_at_me_requests (
        id TEXT PRIMARY KEY, conv_id TEXT NOT NULL, origin_conv_id TEXT NOT NULL,
        model_key TEXT NOT NULL, device_id TEXT NOT NULL, source_turn TEXT NOT NULL,
        source_intent TEXT NOT NULL, expression TEXT NOT NULL, duration_seconds INTEGER NOT NULL,
        status TEXT NOT NULL DEFAULT 'queued', decision TEXT, outcome TEXT,
        reject_reason TEXT NOT NULL DEFAULT '', reason TEXT NOT NULL DEFAULT '', response_day TEXT,
        created_at REAL NOT NULL, expires_at REAL NOT NULL, notified_at REAL, offered_at REAL,
        choice_deadline REAL, accepted_at REAL, started_at REAL, expected_end_at REAL, ended_at REAL,
        actual_ms INTEGER NOT NULL DEFAULT 0, processed_seq INTEGER NOT NULL DEFAULT 0,
        core_state TEXT NOT NULL DEFAULT 'pending', memory_state TEXT NOT NULL DEFAULT 'pending',
        memory_id TEXT, UNIQUE(source_turn,source_intent))''')
    await db.execute('''CREATE TABLE IF NOT EXISTS look_at_me_events (
        event_id TEXT PRIMARY KEY, request_id TEXT NOT NULL, sequence INTEGER NOT NULL,
        type TEXT NOT NULL, occurred_at REAL NOT NULL, received_at REAL NOT NULL,
        payload TEXT NOT NULL, applied INTEGER NOT NULL DEFAULT 0,
        UNIQUE(request_id,sequence))''')
    await db.execute('''CREATE TABLE IF NOT EXISTS look_at_me_deliveries (
        request_id TEXT NOT NULL, turn_id TEXT NOT NULL, snapshot TEXT NOT NULL,
        kind TEXT NOT NULL, state TEXT NOT NULL DEFAULT 'claimed', message_id TEXT,
        claimed_at REAL NOT NULL, replayed_from TEXT NOT NULL DEFAULT '',
        PRIMARY KEY(request_id,turn_id))''')
    await db.execute('CREATE INDEX IF NOT EXISTS look_at_me_device ON look_at_me_requests(device_id,status)')
    # Core is at-most-once: a crash after dispatch must not start a second turn.
    await db.execute("UPDATE look_at_me_requests SET core_state='interrupted' WHERE core_state='running'")
    await db.execute("UPDATE look_at_me_requests SET memory_state='pending' WHERE memory_state='running'")
    await db.execute("DELETE FROM look_at_me_deliveries WHERE state='claimed'")


class LookAtMeRepository:
    def __init__(self, db_factory=get_db, now=time.time):
        self.db, self.now = db_factory, now

    async def get(self, request_id):
        async with self.db() as db:
            db.row_factory = aiosqlite.Row
            row = await (await db.execute('SELECT * FROM look_at_me_requests WHERE id=?', (request_id,))).fetchone()
            return dict(row) if row else None

    async def create(self, *, conv_id, origin_conv_id, model_key, device_id,
                     source_turn, source_intent, expression, duration_seconds):
        async with self.db() as db:
            db.row_factory = aiosqlite.Row
            await db.execute('BEGIN IMMEDIATE')
            old = await (await db.execute('SELECT * FROM look_at_me_requests WHERE source_turn=? AND source_intent=?', (source_turn, source_intent))).fetchone()
            if old:
                return dict(old)
            busy = await (await db.execute("SELECT id FROM look_at_me_requests WHERE device_id=? AND status!='finished' AND (status IN ('accepted','active') OR COALESCE(choice_deadline,expires_at)>?)", (device_id, self.now()))).fetchone()
            if busy:
                raise ValueError('device_busy:' + busy['id'])
            rid, now = 'lam_' + uuid4().hex, self.now()
            await db.execute('''INSERT INTO look_at_me_requests
                (id,conv_id,origin_conv_id,model_key,device_id,source_turn,source_intent,expression,duration_seconds,created_at,expires_at)
                VALUES(?,?,?,?,?,?,?,?,?,?,?)''', (rid,conv_id,origin_conv_id,model_key,device_id,source_turn,source_intent,expression,duration_seconds,now,now+120))
            await db.commit()
        return await self.get(rid)

    async def pending(self, device_id):
        async with self.db() as db:
            db.row_factory = aiosqlite.Row
            row = await (await db.execute("SELECT * FROM look_at_me_requests WHERE device_id=? AND status IN ('queued','offered') AND COALESCE(choice_deadline,expires_at)>? ORDER BY created_at LIMIT 1", (device_id,self.now()))).fetchone()
            return dict(row) if row else None

    async def events(self, request_id, device_id, events, *, day_of, current_conv, valid_convs=None):
        async with self.db() as db:
            db.row_factory = aiosqlite.Row
            await db.execute('BEGIN IMMEDIATE')
            raw = await (await db.execute('SELECT * FROM look_at_me_requests WHERE id=?', (request_id,))).fetchone()
            if not raw:
                raise ValueError('not_found')
            row = dict(raw)
            valid_convs = set(valid_convs or (row['conv_id'],row['origin_conv_id'],current_conv))
            if row['device_id'] != device_id:
                raise ValueError('device_mismatch')
            for event in events:
                existing = await (await db.execute('SELECT * FROM look_at_me_events WHERE event_id=? OR (request_id=? AND sequence=?)', (event['event_id'],request_id,event['sequence']))).fetchone()
                payload = json.dumps(event.get('payload') or {}, ensure_ascii=False, sort_keys=True)
                if existing:
                    if (existing['request_id'],existing['event_id'],existing['sequence'],existing['type'],existing['occurred_at'],existing['payload']) != (request_id,event['event_id'],event['sequence'],event['type'],event['occurred_at'],payload):
                        raise ValueError('event_conflict')
                    continue
                await db.execute('INSERT INTO look_at_me_events(event_id,request_id,sequence,type,occurred_at,received_at,payload) VALUES(?,?,?,?,?,?,?)', (event['event_id'],request_id,event['sequence'],event['type'],event['occurred_at'],self.now(),payload))
            queued = await (await db.execute('SELECT * FROM look_at_me_events WHERE request_id=? AND sequence>? ORDER BY sequence', (request_id,row['processed_seq']))).fetchall()
            for event in queued:
                if event['sequence'] != row['processed_seq'] + 1:
                    break  # Missing earlier callback: retain, never reorder a decision.
                applied = self._apply(row, event, day_of=day_of, current_conv=current_conv,valid_convs=valid_convs)
                row['processed_seq'] = event['sequence']
                await db.execute('UPDATE look_at_me_events SET applied=? WHERE event_id=?', (int(applied),event['event_id']))
            fields = [key for key in row if key != 'id']
            await db.execute('UPDATE look_at_me_requests SET '+','.join(f'{key}=?' for key in fields)+' WHERE id=?', tuple(row[key] for key in fields)+(request_id,))
            await db.commit()
        return row

    def _apply(self, r, e, *, day_of, current_conv, valid_convs):
        at, kind, p = e['occurred_at'], e['type'], json.loads(e['payload'])
        if r['status'] == 'finished' or at < r['created_at'] - 5 or at > self.now() + 60:
            return False
        if kind == 'notified' and r['status'] in ('queued','offered'):
            r['notified_at'] = r['notified_at'] or at
        elif kind == 'offered' and r['status'] == 'queued' and at < r['expires_at']:
            r.update(status='offered',offered_at=at,choice_deadline=at+120)
        elif kind in ('accepted','rejected','timed_out') and r['status'] == 'offered':
            if at < r['offered_at'] or ((kind == 'timed_out') != (at >= r['choice_deadline'])):
                return False
            r.update(decision=kind,response_day=day_of(at))
            if kind == 'accepted':
                r.update(status='accepted',accepted_at=at,conv_id=current_conv or r['conv_id'])
            else:
                r.update(status='finished',ended_at=at,reject_reason=str(p.get('reason') or '') if kind == 'rejected' else '')
        elif kind == 'started' and r['status'] == 'accepted' and at >= r['accepted_at']:
            # A receipt names the daily window verified during its visual handshake.
            if p.get('conv_id') not in valid_convs or p.get('request_id') != r['id']:
                return False
            r.update(status='active',started_at=at,expected_end_at=at+r['duration_seconds'],conv_id=current_conv or p['conv_id'])
        elif kind == 'finished' and r['status'] == 'active' and at >= r['started_at']:
            actual = max(0,min(int(p.get('actual_ms',0)),r['duration_seconds']*1000,int((at-r['started_at'])*1000)+1000))
            outcome = p.get('outcome')
            if outcome not in ('completed','ended_early') or (outcome == 'completed' and actual < r['duration_seconds']*1000):
                return False
            r.update(status='finished',outcome=outcome,actual_ms=actual,ended_at=at,reason=str(p.get('reason') or ''),response_day=day_of(at))
        elif kind in ('expired','delivery_failed','start_failed','cancelled') and r['status'] in ('queued','offered','accepted'):
            if kind == 'expired' and (r['status'] != 'queued' or at < r['expires_at']):
                return False
            if kind == 'start_failed' and r['status'] != 'accepted':
                return False
            r.update(status='finished',ended_at=at,reason=str(p.get('reason') or kind),response_day=day_of(at))
        else:
            return False
        return True

    async def claim(self, *, turn_id, user_name, ai_name, timezone, replay_from=''):
        from .prompts import fact
        from app.daily_chat.service import calendar_day
        now = self.now()
        async with self.db() as db:
            db.row_factory = aiosqlite.Row
            await db.execute('BEGIN IMMEDIATE')
            await db.execute("DELETE FROM look_at_me_deliveries WHERE state='claimed' AND claimed_at<?", (now-600,))
            if replay_from:
                rows = await (await db.execute('SELECT * FROM look_at_me_deliveries WHERE message_id=?', (replay_from,))).fetchall()
                for d in rows:
                    await db.execute("UPDATE look_at_me_deliveries SET state='claimed',claimed_at=?,replayed_from=? WHERE request_id=? AND turn_id=?", (now,replay_from,d['request_id'],d['turn_id']))
                snapshots = [json.loads(d['snapshot']) for d in rows]
            else:
                snapshots = []
                requests = await (await db.execute('SELECT * FROM look_at_me_requests ORDER BY created_at')).fetchall()
                today = calendar_day(now,timezone)[0]
                for raw in requests:
                    r = dict(raw)
                    kind = 'result' if r['outcome'] else 'fact'
                    if kind == 'result':
                        count = (await (await db.execute("SELECT COUNT(*) FROM look_at_me_deliveries WHERE request_id=? AND kind='result' AND turn_id!=?", (r['id'],turn_id))).fetchone())[0]
                        if count >= 3:
                            continue
                    elif r['status'] not in ('accepted','active') and (r['response_day'] or calendar_day(r['choice_deadline'] or r['expires_at'],timezone)[0]) != today:
                        continue
                    old = await (await db.execute('SELECT snapshot FROM look_at_me_deliveries WHERE request_id=? AND turn_id=?', (r['id'],turn_id))).fetchone()
                    snapshot = json.loads(old[0]) if old else r
                    snapshots.append(snapshot)
                    await db.execute('INSERT OR IGNORE INTO look_at_me_deliveries(request_id,turn_id,snapshot,kind,claimed_at) VALUES(?,?,?,?,?)', (r['id'],turn_id,json.dumps(snapshot,ensure_ascii=False),kind,now))
            await db.commit()
        block = '\n'.join(fact(r,user_name=user_name,ai_name=ai_name,now=now,timezone=timezone) for r in snapshots)
        return {'turn_id':turn_id,'replayed_from':replay_from,'block':('[陪伴事实]\n'+block) if block else ''}

    async def finalize_in_tx(self, db, meta, message_id):
        if meta.get('replayed_from'):
            await db.execute("UPDATE look_at_me_deliveries SET state='done',message_id=?,replayed_from='' WHERE replayed_from=?", (message_id,meta['replayed_from']))
        else:
            await db.execute("UPDATE look_at_me_deliveries SET state='done',message_id=? WHERE turn_id=?", (message_id,meta.get('turn_id','')))

    async def settle(self, meta, message_id='', success=False):
        if not meta:
            return
        async with self.db() as db:
            if success:
                await self.finalize_in_tx(db,meta,message_id)
            elif meta.get('replayed_from'):
                await db.execute('DELETE FROM look_at_me_deliveries WHERE replayed_from=?', (meta['replayed_from'],))
            else:
                await db.execute("DELETE FROM look_at_me_deliveries WHERE turn_id=? AND state='claimed'", (meta.get('turn_id',''),))
            await db.commit()


repository = LookAtMeRepository()
