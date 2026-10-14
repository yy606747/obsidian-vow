from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
import json
import logging
import time
from uuid import uuid4
from zoneinfo import ZoneInfo

import aiosqlite

log = logging.getLogger(__name__)


def calendar_day(now: float, timezone: str) -> tuple[str, float]:
    local = datetime.fromtimestamp(now, ZoneInfo(timezone))
    tomorrow = (local + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    return local.date().isoformat(), tomorrow.timestamp()


async def inherited_messages_in_tx(db, conv_id: str) -> list[dict]:
    try:
        cursor = await db.execute(
            "SELECT m.*, i.position FROM daily_chat_inheritance i "
            "JOIN messages m ON m.id=i.message_id WHERE i.conv_id=? ORDER BY i.position",
            (conv_id,),
        )
    except aiosqlite.OperationalError as exc:
        if 'no such table: daily_chat_inheritance' in str(exc):
            return []
        raise
    columns = [column[0] for column in cursor.description]
    return [dict(zip(columns, row)) for row in await cursor.fetchall()]


class DailyChatService:
    def __init__(self, db_factory=None, *, now=None, timezone_loader=None, broadcast=None):
        self._db_factory = db_factory
        self._now = now or time.time
        self._timezone_loader = timezone_loader
        self._broadcast = broadcast
        self.state: dict = {}
        self.redirects: dict[str, str] = {}

    def db(self):
        if self._db_factory:
            return self._db_factory()
        from database import get_db
        return get_db()

    def timezone(self):
        if self._timezone_loader:
            return self._timezone_loader()
        from app.daily_signals.config import daily_timezone_name
        return daily_timezone_name()

    def resolve(self, conv_id: str) -> str:
        if not self.state.get('enabled'):
            return conv_id
        return self.redirects.get(conv_id, conv_id)

    def scope(self, conv_id: str) -> tuple[str, ...]:
        target = self.resolve(conv_id)
        if not self.state.get('enabled'):
            return (conv_id,)
        return tuple(dict.fromkeys([target, conv_id] + [key for key, value in self.redirects.items() if value == target]))

    def public_state(self) -> dict:
        return {**self.state, 'enabled': bool(self.state.get('enabled')),
                'count_initialized': bool(self.state.get('count_initialized')),
                'redirects': dict(self.redirects)}

    async def _load_cache(self, db):
        db.row_factory = aiosqlite.Row
        cursor = await db.execute('SELECT * FROM daily_chat_state WHERE id=1')
        row = await cursor.fetchone()
        state = dict(row) if row else {}
        cursor = await db.execute('SELECT source_conv_id,target_conv_id FROM daily_chat_successors')
        redirects = {row[0]: row[1] for row in await cursor.fetchall()}
        cursor = await db.execute("SELECT MAX(created_at) FROM messages WHERE role='assistant'")
        state['latest_assistant_at'] = float((await cursor.fetchone())[0] or 0)
        self.state, self.redirects = state, redirects

    async def ensure_current(self) -> dict:
        now = self._now()
        event = None
        async with self.db() as db:
            db.row_factory = aiosqlite.Row
            await db.execute('BEGIN IMMEDIATE')
            try:
                cursor = await db.execute('SELECT * FROM daily_chat_state WHERE id=1')
                raw = await cursor.fetchone()
                if raw is None:
                    day, next_at = calendar_day(now, self.timezone())
                    cursor = await db.execute(
                        "SELECT id FROM conversations ORDER BY "
                        "EXISTS(SELECT 1 FROM messages m WHERE m.conv_id=conversations.id AND m.role='user') DESC, updated_at DESC LIMIT 1"
                    )
                    existing = await cursor.fetchone()
                    active = existing[0] if existing else None
                    await db.execute(
                        'INSERT INTO daily_chat_state(id,timezone,day_key,active_conv_id,next_rollover_at) VALUES(1,?,?,?,?)',
                        (self.timezone(), day, active, next_at),
                    )
                    cursor = await db.execute('SELECT * FROM daily_chat_state WHERE id=1')
                    raw = await cursor.fetchone()
                state = dict(raw)
                zone = self.timezone()
                if zone != state['timezone']:
                    state['timezone'] = zone
                    _, zone_next_at = calendar_day(now, zone)
                    await db.execute('UPDATE daily_chat_state SET timezone=?,next_rollover_at=? WHERE id=1', (zone, zone_next_at))
                missing_active = not state['active_conv_id']
                if state['active_conv_id']:
                    cursor = await db.execute('SELECT id FROM conversations WHERE id=?', (state['active_conv_id'],))
                    missing_active = not await cursor.fetchone()
                day, next_at = calendar_day(now, state['timezone'])
                if state['enabled'] and (day > state['day_key'] or missing_active):
                    event = await self._rollover(db, state, day, next_at, now)
                await db.commit()
                await self._load_cache(db)
            except BaseException:
                await db.rollback()
                raise
        if event:
            event['state'] = self.public_state()
            if self._broadcast:
                await self._broadcast({'type': 'daily_chat_rollover', 'data': event})
            else:
                from ws import manager
                await manager.broadcast({'type': 'daily_chat_rollover', 'data': event})
        return self.public_state()

    async def _rollover(self, db, state, day, next_at, now):
        previous = state.get('active_conv_id')
        cursor = await db.execute('SELECT * FROM conversations WHERE id=?', (previous,))
        previous_row = await cursor.fetchone()
        if previous_row is None:
            # Rebuilding a deleted daily window still continues the latest real
            # chat. Background jobs may bump updated_at on unrelated windows.
            cursor = await db.execute(
                "SELECT c.* FROM conversations c ORDER BY "
                "(SELECT MAX(m.created_at) FROM messages m WHERE m.conv_id=c.id AND m.role='user') DESC, "
                "c.created_at DESC LIMIT 1"
            )
            previous_row = await cursor.fetchone()
            previous = previous_row['id'] if previous_row else None
        from config import DEFAULT_MODEL
        model = previous_row['model'] if previous_row else DEFAULT_MODEL
        cursor = await db.execute('SELECT conv_id FROM daily_chat_windows WHERE day_key=?', (day,))
        existing = await cursor.fetchone()
        if existing:
            target = existing[0]
            cursor = await db.execute('SELECT id FROM conversations WHERE id=?', (target,))
            if not await cursor.fetchone():
                await db.execute('DELETE FROM daily_chat_windows WHERE day_key=?', (day,))
                existing = None
        if not existing:
            target = f'conv_daily_{day}_{uuid4().hex[:10]}'
            await db.execute(
                'INSERT INTO conversations(id,title,model,created_at,updated_at) VALUES(?,?,?,?,?)',
                (target, day, model, now, now),
            )
            await db.execute(
                'INSERT INTO daily_chat_windows(conv_id,day_key,previous_conv_id,inherit_count,switched_at) VALUES(?,?,?,?,?)',
                (target, day, previous, state['inherit_count'], now),
            )
            inherited = await inherited_messages_in_tx(db, previous) if previous else []
            cursor = await db.execute(
                "SELECT * FROM messages WHERE conv_id=? AND role IN ('user','assistant') ORDER BY created_at DESC,id DESC LIMIT ?",
                (previous, state['inherit_count']),
            )
            candidates = {row['id']: dict(row) for row in inherited}
            candidates.update({row['id']: dict(row) for row in await cursor.fetchall()})
            selected = sorted(candidates.values(), key=lambda row: (row['created_at'], row['id']))[-state['inherit_count']:]
            await db.executemany(
                'INSERT INTO daily_chat_inheritance(conv_id,message_id,position) VALUES(?,?,?)',
                [(target, row['id'], index) for index, row in enumerate(selected)],
            )
        # Keep redirects flat, including multiple missed dates or a deleted current window.
        cursor = await db.execute('SELECT source_conv_id,target_conv_id FROM daily_chat_successors')
        retired = {value for row in await cursor.fetchall() for value in row}
        if state.get('active_conv_id'):
            retired.add(state['active_conv_id'])
        if previous:
            retired.add(previous)
        retired.discard(target)
        if retired:
            await db.executemany(
                'INSERT INTO daily_chat_successors(source_conv_id,target_conv_id) VALUES(?,?) '
                'ON CONFLICT(source_conv_id) DO UPDATE SET target_conv_id=excluded.target_conv_id',
                [(source, target) for source in retired],
            )
            await self._carry_live_state(db, tuple(retired), target)
        await db.execute(
            'UPDATE daily_chat_state SET day_key=?,active_conv_id=?,revision=revision+1,next_rollover_at=? WHERE id=1',
            (day, target, next_at),
        )
        return {'previous_conv_id': previous, 'conv_id': target,
                'conversation': {'id': target, 'title': day, 'model': model, 'created_at': now, 'updated_at': now}}

    async def _carry_live_state(self, db, sources, target):
        placeholders = ','.join('?' for _ in sources)
        cursor = await db.execute("SELECT name FROM sqlite_master WHERE type='table'")
        tables = {row[0] for row in await cursor.fetchall()}
        if 'control_sessions' in tables:
            await db.execute(
                f"UPDATE control_sessions SET conv_id=? WHERE conv_id IN ({placeholders}) AND status IN ('active','stale')",
                (target, *sources),
            )
        # Request/source identifiers stay unchanged; only the next delivery window moves.
        for table, condition in (
            ('look_at_me_requests', "status IN ('queued','offered','accepted','active')"),
            ('web_search_pending', "status IN ('queued','ready')"),
            ('memory_pending_recalls', "status IN ('queued','ready','selected')"),
            ('self_wakes', "state IN ('pending','consumed')"),
            ('sticker_events', "(status='running' OR feedback_state IN ('pending','claimed'))"),
        ):
            if table in tables:
                await db.execute(f'UPDATE {table} SET conv_id=? WHERE conv_id IN ({placeholders}) AND {condition}', (target, *sources))

    async def configure(self, *, inherit_count=None, initial_count=None, enabled=None, timezone=None):
        await self.ensure_current()
        updates = {}
        if inherit_count is not None:
            if isinstance(inherit_count, bool) or not 1 <= int(inherit_count) <= 100:
                raise ValueError('跨日继承数量应为 1～100 条')
            updates.update(inherit_count=int(inherit_count), count_initialized=1)
        elif initial_count is not None and not self.state.get('count_initialized'):
            updates.update(inherit_count=max(1, min(100, int(initial_count))), count_initialized=1)
        if enabled is not None:
            updates['enabled'] = int(bool(enabled))
        if timezone is not None:
            ZoneInfo(timezone)
            day, next_at = calendar_day(self._now(), timezone)
            updates.update(timezone=timezone, day_key=day, next_rollover_at=next_at)
        if updates:
            async with self.db() as db:
                seed_guard = ' AND count_initialized=0' if initial_count is not None and inherit_count is None else ''
                await db.execute('UPDATE daily_chat_state SET ' + ','.join(f'{key}=?' for key in updates) + ' WHERE id=1' + seed_guard, tuple(updates.values()))
                await db.commit()
                await self._load_cache(db)
        return self.public_state()

    async def adopt(self, conv_id: str):
        await self.ensure_current()
        if self.resolve(conv_id) != conv_id:
            return
        async with self.db() as db:
            await db.execute('UPDATE daily_chat_state SET active_conv_id=? WHERE id=1', (conv_id,))
            await db.commit()
            await self._load_cache(db)

    async def message_window(self, message_id: str, fallback: str):
        if not self.state.get('enabled'):
            return fallback
        async with self.db() as db:
            cursor = await db.execute('SELECT conv_id FROM messages WHERE id=?', (message_id,))
            row = await cursor.fetchone()
        return row[0] if row else self.resolve(fallback)

    async def run_loop(self):
        while True:
            try:
                await self.ensure_current()
            except Exception:
                log.exception('daily chat rollover failed; retrying')
            await asyncio.sleep(1)


daily_chat = DailyChatService()


def resolve_conversation_id(conv_id: str) -> str:
    return daily_chat.resolve(conv_id)


def conversation_scope_ids(conv_id: str) -> tuple[str, ...]:
    return daily_chat.scope(conv_id)


async def active_conversation_in_tx(db):
    active = daily_chat.state.get('active_conv_id') if daily_chat.state.get('enabled') else None
    if not active:
        return None
    cursor = await db.execute('SELECT * FROM conversations WHERE id=?', (active,))
    return await cursor.fetchone()


def select_cross_day_messages(
    messages: list[dict], *, context_limit: int, current_message_count: int,
) -> list[dict]:
    """Fill the recent-history window with the newest inherited records."""
    remaining = max(0, context_limit - current_message_count)
    return messages[-remaining:] if remaining else []


async def cross_day_prompt(
    conv_id: str, *, user_name: str, ai_name: str, context_limit: int,
    current_message_count: int, db_factory=None,
) -> list[dict]:
    if not daily_chat.state.get('enabled'):
        return []
    async with (db_factory() if db_factory else daily_chat.db()) as db:
        records = await inherited_messages_in_tx(db, resolve_conversation_id(conv_id))
    records = select_cross_day_messages(
        records, context_limit=context_limit, current_message_count=current_message_count,
    )
    return build_cross_day_context(records, user_name=user_name, ai_name=ai_name,
                                   timezone=daily_chat.state.get('timezone') or daily_chat.timezone())


def build_cross_day_context(messages: list[dict], *, user_name: str, ai_name: str, timezone: str,
                            today_user_ids=(), image_budget=None) -> list[dict]:
    """Keep raw text/voice transcripts and apply the existing recent-image limits."""
    if not messages:
        return []
    from pathlib import Path
    from config import UPLOADS_DIR
    from app.chat.audio_input import attachment_mime_type, attachment_url
    from app.chat.image_history import image_history_config
    from app.documents.service import is_document
    from app.heart_works.chat_reference import is_reference
    limits = image_history_config()
    user_ids = [m['id'] for m in messages if m['role'] == 'user'] + list(today_user_ids)
    allowed = set(user_ids[-limits['image_history_user_turns']:]) if limits['image_history_user_turns'] else set()
    count = int((image_budget or {}).get('retained_images', 0))
    size = int((image_budget or {}).get('retained_bytes', 0))
    records = []
    images = []
    for message in reversed(messages):
        record = dict(message)
        raw = record.get('attachments') or []
        try:
            attachments = json.loads(raw) if isinstance(raw, str) else raw
        except (TypeError, ValueError):
            attachments = []
        retained = []
        for attachment in attachments:
            if is_document(attachment) or is_reference(attachment):
                retained.append(attachment)
                continue
            if record['role'] == 'assistant' and isinstance(attachment, dict) and attachment.get('type') == 'sticker':
                # The actual sent summary is kept below; no original-image budget is needed.
                continue
            if not attachment_mime_type(attachment).startswith('image/'):
                continue
            path = UPLOADS_DIR / Path(attachment_url(attachment)).name
            try:
                image_size = path.stat().st_size if path.is_file() else None
            except OSError:
                image_size = None
            if (record['id'] in allowed and image_size is not None
                    and count < limits['image_history_max_images']
                    and size + image_size <= limits['image_history_max_bytes']):
                retained.append(attachment)
                count += 1
                size += image_size
            else:
                record['content'] = str(record.get('content') or '') + (
                    f'\n[历史图片] {user_name}在这条记录中发过图片，本轮未加载这张原图；'
                    f'{ai_name}不能把文字记录当成本轮亲眼看见了原图。')
        images[0:0] = retained
        records.insert(0, record)
    block = build_cross_day_background(records, user_name=user_name, ai_name=ai_name, timezone=timezone)
    if any(not is_document(item) for item in images):
        block += '\n本轮附带的历史原图按上方记录的先后顺序排列，属于当时的画面。'
    return [{'role': 'user', 'content': block, 'attachments': images},
            {'role': 'assistant', 'content': f'（带着这些经历，继续和{user_name}度过今天。）', 'attachments': []}]


def build_cross_day_background(messages: list[dict], *, user_name: str, ai_name: str, timezone: str) -> str:
    if not messages:
        return ''
    from app.chat.audio_input import audio_transcript_context
    from app.stickers.prompts import history_note
    lines = [f'[跨日背景：{user_name}和{ai_name}上一段对话的原始记录]',
             '以下记录发生在各条标注的时间，作为进入新一天时保留的背景。当前时间以本轮时间信息为准。']
    for message in messages:
        name = user_name if message['role'] == 'user' else ai_name
        at = datetime.fromtimestamp(message['created_at'], ZoneInfo(timezone)).strftime('%Y-%m-%d %H:%M:%S')
        content = str(message.get('content') or '')
        from app.together.shared_history import ended_context_note
        content += ended_context_note(message)
        lines.append(f'[{at}] {name}：{content}')
        raw_attachments = message.get('attachments') or []
        try:
            attachments = json.loads(raw_attachments) if isinstance(raw_attachments, str) else raw_attachments
        except (TypeError, ValueError):
            attachments = []
        transcript = audio_transcript_context(attachments)
        if transcript:
            lines.append(transcript)
        sticker_note = history_note(attachments, user_name=user_name, ai_name=ai_name, role=message['role'])
        if sticker_note:
            lines.append(sticker_note)
    return '\n'.join(lines)
