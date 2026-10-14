"""Resumable creation worker. Only committed works reach the archive."""
from __future__ import annotations

import asyncio
import json
import logging
from contextvars import ContextVar

from . import prompts, protocol, tools
from .repository import Repository, WorkStopped, dumps

log = logging.getLogger(__name__)
creation_trace = ContextVar('heart_work_trace', default=None)


def trace_context(row, *, revision=None, task=None):
    from app.tools.schemas import ToolContext
    metadata = {'source': 'heart_work', 'source_chain': 'heart_work', 'work_id': row['id'],
                'revision': revision if revision is not None else row['revision']}
    if task:
        metadata.update(task_id=task['id'], attempt=task['attempt'] + 1, tool=task['tool'])
    return ToolContext(conv_id=row['conv_id'], model_key=row['model_key'], request_id=row['id'], metadata=metadata)


async def core_call(model_key, messages):
    from ai_providers import call_core_chat_once
    from app.provider_privacy import private_provider_call
    from app.tools.ledger import tool_invocation_ledger
    from app.tools.schemas import ToolContext
    invocation = tool_invocation_ledger.new_invocation_id('heart_work_core')
    context = creation_trace.get() or ToolContext(conv_id='', model_key=model_key, request_id=invocation,
                          metadata={'source': 'heart_work', 'source_chain': 'heart_work'})
    await tool_invocation_ledger.record_model_request(context, invocation_id=invocation,
                                                     request_snapshot=messages, advertised_tools=tools.capabilities(),
                                                     metadata=context.metadata)
    usage = {}
    try:
        with private_provider_call('heart_work'):
            raw = await call_core_chat_once(model_key, messages, timeout=180, temperature=.9,
                                           scope='heart_work:creation', max_tokens=16384, usage_meta=usage)
    except BaseException as exc:
        await tool_invocation_ledger.record_model_output(context, invocation_id=invocation,
            raw_output='', outcome='failed', error=type(exc).__name__, metadata=context.metadata)
        raise
    await tool_invocation_ledger.record_model_output(context, invocation_id=invocation,
                                                     raw_output=raw, outcome='succeeded', metadata={**context.metadata, 'usage': usage})
    if str(usage.get('finish_reason') or '').lower() in {'length', 'max_tokens', 'max_output_tokens'}:
        # Keep the partial work for repair, but never accept a truncated finish marker.
        raw += '\n[输出中断]'
    return raw


class HeartWorkService:
    def __init__(self, repository=None, *, model=core_call, execute=tools.execute,
                 capabilities=tools.capabilities, broadcast=None, memory_writer=None):
        self.repo = repository or Repository()
        self.model, self.execute, self.capabilities = model, execute, capabilities
        self.broadcast, self.memory_writer = broadcast, memory_writer
        self.tasks = {}
        self.jobs_lock = asyncio.Lock()

    async def enqueue_from_turn(self, origin, intent_raw, context_snapshot):
        if not intent_raw.strip():
            return None
        row = await self.repo.enqueue(origin=origin, intent_raw=intent_raw, context_snapshot=context_snapshot)
        self.start(row['id'])
        return row

    def start(self, work_id):
        from app.background_tasks import create_tracked_task
        if work_id not in self.tasks or self.tasks[work_id].done():
            task = create_tracked_task(self.run(work_id), name='heart_work:' + work_id)
            self.tasks[work_id] = task
            task.add_done_callback(lambda done: self.tasks.pop(work_id, None) if self.tasks.get(work_id) is done else None)

    async def resume(self, work_id):
        await self.repo.resume(work_id)
        row = await self.repo.get(work_id)
        if row and row['status'] in {'queued', 'generating', 'waiting_tools', 'ready'}:
            self.start(work_id)
        return row

    async def abandon(self, work_id, reason):
        await self.repo.abandon(work_id, reason)
        return await self.repo.get(work_id)

    async def run(self, work_id):
        token = await self.repo.claim(work_id)
        if not token:
            return
        try:
            while True:
                row = await self.repo.get(work_id)
                if row['status'] in {'paused', 'abandoned', 'published', 'deleted'}:
                    return
                snapshot = row['context_snapshot']
                names = {'ai_name': snapshot['ai_name'], 'user_name': snapshot['user_name']}
                last = await self.repo.latest_revision(work_id)
                if row['status'] in {'queued', 'generating'}:
                    await self.repo.update(work_id, token, status='generating')
                    messages = list(snapshot['messages']) + [{'role': 'user', 'content': prompts.creation_prompt(
                        **names, intent_raw=row['intent_raw'], capabilities=self.capabilities())}]
                    if last:
                        materials = await self.repo.read('SELECT marker,status,result_json,error FROM heart_work_tasks WHERE work_id=? ORDER BY updated_at', (work_id,), many=True)
                        # Keep earlier search results available across multi-step
                        # continuation, as well as the latest failure/repair.
                        feedback = [m['marker'] + '\n' + dumps(m['result'].get('block') or m['result'] or {'error': m['error']})
                                    for m in materials if m['status'] in {'succeeded', 'failed', 'unknown'}]
                        feedback.extend(row['feedback'])
                        messages.extend([{'role': 'assistant', 'content': last['raw']},
                                         {'role': 'user', 'content': prompts.feedback_prompt(**names,
                                          results='\n\n'.join(dict.fromkeys(feedback)) or '继续完成这份作品。')}])
                    trace_token = creation_trace.set(trace_context(row, revision=row['revision'] + 1))
                    try:
                        raw = await self.model(row['model_key'], messages)
                    finally:
                        creation_trace.reset(trace_token)
                    try:
                        parsed = protocol.parse_work(str(raw or ''))
                    except ValueError as exc:
                        parsed = {'status': 'repair', 'error': str(exc)}
                    await self.repo.save_revision(work_id, token, raw, parsed)
                    continue
                parsed = last['parsed']
                if parsed['status'] == 'abandoned':
                    await self.repo.abandon(work_id, parsed['reason'])
                    return
                if parsed['status'] == 'repair':
                    await self.repo.update(work_id, token, status='generating', feedback_json=dumps([
                        '上次作品格式未完成：' + parsed['error'] + '。请修复封面、正文与结束标记。']))
                    continue
                rendered, feedback = {}, []
                for section in ('cover', 'body'):
                    rendered[section] = []
                    for block in parsed[section]:
                        if block['type'] != 'tool_slot':
                            rendered[section].append(block)
                            continue
                        task = await self.repo.task(work_id, block, row['revision'])
                        result = await self._material(row, token, task)
                        if result.get('block'):
                            rendered[section].append(result['block'])
                        else:
                            feedback.append(block['marker'] + '\n' + dumps(result))
                if feedback or parsed['status'] == 'continue':
                    await self.repo.update(work_id, token, status='generating',
                                           feedback_json=dumps(feedback or ['继续写完当前作品。']))
                    continue
                event = protocol.event_text(**names, note=parsed['event_note_raw'],
                    body=rendered['body'], cover=rendered['cover'], published_at=self.repo.now())
                await self.repo.update(work_id, token, status='ready')
                await self.repo.publish(work_id, token, **rendered, note=parsed['event_note_raw'], event=event)
                self.invalidate()
                await self.run_jobs()
                return
        except WorkStopped:
            return
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            row = await self.repo.get(work_id)
            failures = row['failures'] + 1
            try:
                await self.repo.update(work_id, token, failures=failures, last_error=type(exc).__name__,
                    status='paused' if failures >= 3 else row['status'], retry_at=self.repo.now() + min(300, 10 ** failures))
            except WorkStopped:
                pass
            log.warning('heart work %s deferred: %s', work_id, type(exc).__name__)
        finally:
            await self.repo.execute("UPDATE heart_works SET lease_token='',lease_until=0 WHERE id=? AND lease_token=?", (work_id, token))

    async def _material(self, row, token, task):
        if task['status'] == 'succeeded':
            return task['result']
        if task['status'] == 'running':
            # A previous process may have finished a paid image request. Reuse the file.
            path = tools.material_root() / row['id'] / (task['id'] + '.png')
            if task['tool'] == 'IMAGE' and not path.exists():
                await self.repo.execute("UPDATE heart_work_tasks SET status='unknown',error='process_interrupted' WHERE id=?", (task['id'],))
                return {'error': '上次图片请求中断，结果未知。可以换素材、移除，或再次写原指令重试。'}
        elif task['status'] in {'failed', 'unknown'} and task['requested_revision'] == row['revision']:
            return {'error': task['error']}
        await self.repo.update(row['id'], token, status='waiting_tools')
        await self.repo.execute("UPDATE heart_work_tasks SET status='running',attempt=attempt+1,requested_revision=?,updated_at=? WHERE id=?",
                                (row['revision'], self.repo.now(), task['id']))
        from app.tools.ledger import tool_invocation_ledger
        context = trace_context(row, task=task)
        await tool_invocation_ledger.record_diagnostic(context, phase='heart_work_tool', outcome='started', metadata=context.metadata)
        try:
            result = await self.execute(work_id=row['id'], task_id=task['id'], tool=task['tool'], query=task['query'])
        except Exception as exc:
            # No upstream exception bodies or credentials enter a Core prompt.
            error = str(exc) if isinstance(exc, ValueError) else type(exc).__name__
            await self.repo.task_result(task, {}, error=error)
            await tool_invocation_ledger.record_diagnostic(context, phase='heart_work_tool', outcome='failed', metadata={**context.metadata, 'error': error})
            return {'error': error, 'action': '可以调整、换素材、重试或放弃。'}
        await self.repo.task_result(task, result)
        await tool_invocation_ledger.record_diagnostic(context, phase='heart_work_tool', outcome='succeeded', metadata=context.metadata)
        return result

    @staticmethod
    def invalidate():
        from app.memory_v2.hybrid_recall import invalidate_full_corpus_cache
        invalidate_full_corpus_cache(chunks=True, notes=True)

    async def run_jobs(self):
        async with self.jobs_lock:
            jobs = await self.repo.read("SELECT * FROM heart_work_jobs WHERE status='queued' AND retry_at<=?", (self.repo.now(),), many=True)
            for job in jobs:
                row = await self.repo.get(job['work_id'])
                if not row or row['status'] != 'published':
                    continue
                try:
                    if job['kind'] == 'notify':
                        from ws import manager
                        await (self.broadcast or manager.broadcast)({'type': 'heart_work_published', 'data': {
                            'id': row['id'], 'kind': 'work', 'created_at': row['published_at'], 'cover': row['cover']}})
                    else:
                        from .memory import write_event
                        await write_event(self.repo, row, writer=self.memory_writer)
                    await self.repo.execute("UPDATE heart_work_jobs SET status='done' WHERE work_id=? AND kind=? AND status='queued'", (row['id'], job['kind']))
                except Exception as exc:
                    await self.repo.execute('''UPDATE heart_work_jobs SET attempts=attempts+1,retry_at=?,last_error=?
                        WHERE work_id=? AND kind=? AND status='queued' ''',
                        (self.repo.now() + min(3600, 30 * 2 ** min(job['attempts'], 7)), type(exc).__name__, row['id'], job['kind']))

    async def recover(self):
        rows = await self.repo.read("SELECT id FROM heart_works WHERE status IN ('queued','generating','waiting_tools','ready') AND lease_until<? AND retry_at<=?",
                                    (self.repo.now(), self.repo.now()), many=True)
        for row in rows:
            self.start(row['id'])
        from app.memory_v2.embedding import embedding_signature
        await self.repo.execute("""UPDATE heart_work_jobs SET status='queued',retry_at=0
            WHERE kind='memory' AND status='done' AND work_id IN
            (SELECT e.work_id FROM heart_work_events e JOIN heart_works w ON w.id=e.work_id
             WHERE w.status='published' AND (e.embedding IS NULL OR e.embedding_signature!=?))""", (embedding_signature(),))
        await self.run_jobs()
        await self.cleanup_assets()

    async def cleanup_assets(self):
        """Remove only files owned by terminal drafts or unadopted final material."""
        from pathlib import Path
        assets = await self.repo.read('''SELECT a.*,w.status,w.cover_json,w.body_json FROM heart_work_assets a
            JOIN heart_works w ON w.id=a.work_id WHERE w.status IN ('published','abandoned','deleted')''', many=True)
        for asset in assets:
            adopted = {block.get('asset_id') for block in [*asset['cover'], *asset['body']]}
            if asset['status'] == 'published' and asset['id'] in adopted:
                continue
            path = Path(asset['path']).resolve()
            if path.is_relative_to(tools.material_root().resolve()):
                await asyncio.to_thread(path.unlink, missing_ok=True)
            await self.repo.execute('DELETE FROM heart_work_assets WHERE id=?', (asset['id'],))
        # A crash can occur after the atomic file rename and before asset-row
        # insertion. Terminal drafts own these files too; active tasks retain them.
        orphan_tasks = await self.repo.read('''SELECT t.id,t.work_id FROM heart_work_tasks t
            JOIN heart_works w ON w.id=t.work_id
            WHERE w.status IN ('published','abandoned','deleted') AND t.tool='IMAGE'
            AND NOT EXISTS (SELECT 1 FROM heart_work_assets a WHERE a.task_id=t.id)''', many=True)
        for task in orphan_tasks:
            path = tools.material_root() / task['work_id'] / (task['id'] + '.png')
            await asyncio.to_thread(path.unlink, missing_ok=True)
            await asyncio.to_thread(path.with_suffix('.tmp').unlink, missing_ok=True)

    async def run_loop(self):
        while True:
            try:
                await self.recover()
            except Exception:
                log.exception('heart work recovery failed')
            await asyncio.sleep(10)


heart_work_service = HeartWorkService()
