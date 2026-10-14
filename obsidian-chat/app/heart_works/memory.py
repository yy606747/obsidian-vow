"""Event-only recall projection. Never fetch or embed the work's full body."""
from __future__ import annotations

import json


async def write_event(repo, row, *, writer=None):
    from app.memory_v2 import memory_service
    from app.memory_v2.shadow_write import shadow_legacy_memory
    from app.memory_v2.embedding import embedding_signature
    current = await repo.get(row['id'])
    if not current or current['status'] != 'published':
        return
    signature = embedding_signature()
    projection = await repo.read('SELECT * FROM heart_work_events WHERE work_id=?', (row['id'],))
    result = await (writer or memory_service.create_memory)(row['event_text'], 'event',
        importance=.5, source_conv=row['conv_id'], source_start_ts=row['published_at'],
        source_end_ts=row['published_at'], idempotency_key='heart_work:' + row['id'])
    # Idempotent create_memory may return before retrying a failed shadow write.
    if not writer:
        shadow = await shadow_legacy_memory(result['id'], write_path='heart_work', source='heart_work',
                                            extra_metadata={'work_id': row['id']}, strict=True)
        if not shadow.get('ok'):
            raise RuntimeError('heart_event_shadow_failed')
        await repo.execute("UPDATE memory_items SET metadata_json=json_set(COALESCE(metadata_json,'{}'),'$.work_id',?) WHERE legacy_memory_id=?",
                           (row['id'], result['id']))
    embedding_row = await repo.read('SELECT embedding FROM memories WHERE id=?', (result['id'],))
    # A newly created ordinary memory uses the current embedding model. On a
    # model change, never relabel its old vector with the new signature.
    blob = (embedding_row.get('embedding') if embedding_row else None)
    if projection and projection['embedding_signature'] not in ('', signature):
        blob = None
    if not blob:
        from app.memory_v2.embedding import get_document_embedding
        values = await get_document_embedding(row['event_text'])
        blob = memory_service.pack_embedding(values) if values else None
    # Linking and visibility check share a transaction with deletion. This also
    # covers deletion while the embedding request was in flight.
    async with repo.db() as db:
        await db.execute('BEGIN IMMEDIATE')
        cursor = await db.execute('''UPDATE heart_work_events
            SET event_memory_id=?,embedding=?,embedding_signature=? WHERE work_id=?
            AND EXISTS (SELECT 1 FROM heart_works WHERE id=? AND status='published')''',
            (result['id'], blob, signature, row['id'], row['id']))
        if cursor.rowcount and not writer:
            await db.execute('UPDATE memories SET embedding=? WHERE id=?', (blob, result['id']))
            await db.execute('UPDATE memory_items SET embedding=? WHERE legacy_memory_id=?', (blob, result['id']))
        if not cursor.rowcount and not writer:
            await db.execute('DELETE FROM memories WHERE id=?', (result['id'],))
            await db.execute("UPDATE memory_items SET status='archived',visibility='hidden' WHERE legacy_memory_id=?", (result['id'],))
        await db.commit()
    from .service import HeartWorkService
    HeartWorkService.invalidate()
    if not blob:
        raise RuntimeError('heart_event_embedding_pending')


async def iter_recall_rows(db, conv_ids=None, *, recent_after=None):
    if not await (await db.execute("SELECT 1 FROM sqlite_master WHERE name='heart_work_events'")).fetchone():
        return
    from app.memory_v2.embedding import embedding_signature
    clauses, params = ["w.status='published'"], []
    if conv_ids is not None:
        if not conv_ids:
            return
        clauses.append('w.conv_id IN (' + ','.join('?' for _ in conv_ids) + ')')
        params.extend(conv_ids)
    if recent_after is not None:
        clauses.append('e.published_at>=?')
        params.append(recent_after)
    cursor = await db.execute('SELECT e.*,w.conv_id FROM heart_work_events e JOIN heart_works w ON w.id=e.work_id WHERE '
                              + ' AND '.join(clauses) + ' ORDER BY e.published_at DESC', params)
    async for source in cursor:
        row = dict(source)
        timestamp = row['published_at']
        yield {'id': 'heart_event_' + row['work_id'], 'source_type': 'heart_work', 'work_id': row['work_id'],
               'event_memory_id': row['event_memory_id'], 'conv_id': row['conv_id'],
               'content': row['event_text'], 'source_hash': row['source_hash'],
               'embedding': row['embedding'] if row['embedding_signature'] == embedding_signature() else None,
               'message_ids_json': '[]', 'keywords_json': '[]',
               'created_at': timestamp, 'updated_at': timestamp,
               'metadata_json': json.dumps({'source_start_ts': timestamp, 'source_end_ts': timestamp,
                                             'source_message_ids': [], 'work_id': row['work_id']})}


async def recall_rows(db, conv_ids=None):
    return [row async for row in iter_recall_rows(db, conv_ids)]


async def valid_items(items, *, db_factory=None):
    if not any(item.get('work_id') for item in items):
        return items
    from database import get_db
    async with (db_factory or get_db)() as db:
        ids = {row[0] for row in await (await db.execute("SELECT id FROM heart_works WHERE status='published'")).fetchall()}
    return [item for item in items if not item.get('work_id') or item['work_id'] in ids]
