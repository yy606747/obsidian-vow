"""Save original images, describe once, and match free expressive requests."""

from __future__ import annotations

import asyncio
import hashlib
import io
import json
import tempfile
from pathlib import Path

from PIL import Image

from ai_providers import call_slot_chat
from app.background_tasks import create_tracked_task
from app.chat.audio_input import attachment_url, parse_attachments
from app.tools.schemas import ToolContext, ToolIntent
from config import UPLOADS_DIR, get_slot
from .prompts import MATCH_PROMPT, SUMMARY_PROMPT
from .repository import sticker_repository
from .attachments import InvalidStickerAttachment, is_sticker
from .images import png_frames


MAX_IMAGE_BYTES = 20 * 1024 * 1024
FORMATS = {'PNG': ('png', 'image/png'), 'JPEG': ('jpg', 'image/jpeg'),
           'GIF': ('gif', 'image/gif'), 'WEBP': ('webp', 'image/webp')}


def configured(slot_name: str) -> bool:
    slot = get_slot(slot_name)
    return bool(slot and slot.get('model') and slot['endpoint'].get('base_url')
                and slot['endpoint'].get('api_key'))


def chat_capabilities() -> tuple[str, ...]:
    caps = []
    if configured('sticker_summary'):
        caps.append('sticker.save')
    if configured('sticker_matcher'):
        caps.append('sticker.send')
    return tuple(caps)


def create_available() -> bool:
    return configured('sticker_summary') and configured('sticker_image')


def asset_path(url: str, uploads: Path) -> Path:
    if not url.startswith('/uploads/') or url != '/uploads/' + Path(url).name:
        raise ValueError('图片来源不可用')
    path = (uploads / Path(url).name).resolve()
    if path.parent != uploads.resolve() or not path.is_file():
        raise ValueError('图片文件不存在')
    if not 0 < path.stat().st_size <= MAX_IMAGE_BYTES:
        raise ValueError('图片文件大小不受支持')
    return path


def attachment(row: dict, expression: str = '') -> dict:
    return {'type': 'sticker', 'id': row['id'], 'url': row['url'], 'mime_type': row['mime_type'],
            'expression': expression, 'summary': json.loads(row['summary_json'])}


class StickerService:
    def __init__(self, *, repository=sticker_repository, caller=call_slot_chat, uploads=UPLOADS_DIR):
        self.repo = repository
        self.caller = caller
        self.uploads = Path(uploads)
        self._summary_lock = asyncio.Lock()
        self._panel_jobs = set()

    async def store_bytes(self, data: bytes, *, source_message_id='', creation_prompt='', ai_saved=True,
                          user_saved=False) -> dict:
        if not 0 < len(data) <= MAX_IMAGE_BYTES:
            raise ValueError('图片文件大小不受支持')
        try:
            with Image.open(io.BytesIO(data)) as picture:
                fmt = picture.format
                picture.verify()
        except (OSError, SyntaxError, Image.DecompressionBombError) as exc:
            raise ValueError('图片损坏或格式不受支持') from exc
        if fmt not in FORMATS:
            raise ValueError('图片格式不受支持')
        ext, mime = FORMATS[fmt]
        digest = hashlib.sha256(data).hexdigest()
        filename = f'sticker_{digest}.{ext}'
        self.uploads.mkdir(parents=True, exist_ok=True)
        destination = self.uploads / filename
        if not destination.exists():
            # Readers only see the complete asset.
            with tempfile.NamedTemporaryFile(dir=self.uploads, delete=False) as temp:
                temp.write(data)
                temp_path = Path(temp.name)
            temp_path.replace(destination)
        return await self.repo.put_asset(digest=digest, url='/uploads/' + filename, mime_type=mime,
                                         source_message_id=source_message_id, creation_prompt=creation_prompt,
                                         ai_saved=ai_saved, user_saved=user_saved)

    async def panel_library(self) -> list[dict]:
        available = []
        for row in await self.repo.panel_library():
            try:
                asset_path(row['url'], self.uploads)
            except ValueError:
                continue
            available.append(attachment(row))
        return available

    async def add_to_panel(self, data: bytes) -> dict:
        row = await self.store_bytes(data, ai_saved=False, user_saved=True)
        self._schedule_panel_summary(row)
        return attachment(row)

    async def collect_from_message(self, message_id: str) -> dict:
        async with self.repo._db() as db:
            origin = await (await db.execute("""SELECT attachments FROM messages
                WHERE id=? AND role IN ('user','assistant')""", (message_id,))).fetchone()
        images = [item for item in parse_attachments(origin[0] if origin else []) if is_sticker(item)]
        if len(images) != 1 or not isinstance(images[0].get('id'), str):
            raise ValueError('这条消息中没有可收藏的表情')
        row = await self.repo.get(images[0]['id'])
        if row is None:
            raise ValueError('表情不存在，请重新添加')
        asset_path(row['url'], self.uploads)
        row = await self.repo.collect_for_user(row['id'])
        self._schedule_panel_summary(row)
        return attachment(row)

    def _schedule_panel_summary(self, row: dict) -> None:
        if row['status'] != 'ready' and row['id'] not in self._panel_jobs and configured('sticker_summary'):
            self._panel_jobs.add(row['id'])
            create_tracked_task(self._summarize_panel_asset(row), name=f'sticker_panel_summary:{row["id"]}')

    async def _summarize_panel_asset(self, row: dict) -> None:
        try:
            await self.summarize(row)
        except Exception:
            pass  # summarize persists the failure; the original remains sendable.
        finally:
            self._panel_jobs.discard(row['id'])

    async def resolve_outgoing(self, attachments: list) -> list:
        if sum(is_sticker(item) for item in attachments) > 1:
            raise InvalidStickerAttachment('每条消息最多发送一张表情')
        resolved = []
        for item in attachments:
            if not is_sticker(item):
                resolved.append(item)
                continue
            row = await self.repo.get(item['id'])
            if row is None:
                raise InvalidStickerAttachment('表情不存在，请从表情面板重新选择')
            if not row['user_saved']:
                raise InvalidStickerAttachment('请先收藏这张表情，再从表情面板发送')
            try:
                asset_path(row['url'], self.uploads)
            except ValueError as exc:
                raise InvalidStickerAttachment('表情原图不可用，请重新添加') from exc
            resolved.append(attachment(row))
        return resolved

    async def summarize(self, row: dict) -> dict:
        async with self._summary_lock:
            row = await self.repo.get(row['id'])
            if row['status'] == 'ready':
                return row
            frames = []
            try:
                path = asset_path(row['url'], self.uploads)
                samples = await asyncio.to_thread(png_frames, await asyncio.to_thread(path.read_bytes))
                for sample in samples:
                    with tempfile.NamedTemporaryFile(suffix='.png', prefix='sticker_frame_', dir=self.uploads, delete=False) as temp:
                        frames.append(Path(temp.name))
                        temp.write(sample)
                raw = await self.caller('sticker_summary', [
                    {'role': 'system', 'content': SUMMARY_PROMPT},
                    {'role': 'user', 'content': '请按时间顺序观察这些帧，为同一个表情建立摘要。',
                     'attachments': [{'url': '/uploads/' + p.name, 'mime_type': 'image/png',
                                      'expected_sha256': hashlib.sha256(p.read_bytes()).hexdigest()} for p in frames]},
                ], expect_json=True, temperature=0, max_tokens=2048, timeout=90, thinking_budget=0)
                summary = json.loads(raw)
                fields = ('visual', 'visible_text', 'intent', 'tone', 'not_for')
                if not isinstance(summary, dict) or summary.get('refused'):
                    raise ValueError('图片摘要未能生成')
                if any(not isinstance(summary.get(k), str) for k in fields) or not summary['visual'].strip():
                    raise ValueError('图片摘要格式不完整')
                summary = {k: summary[k][:1200] for k in fields}
                await self.repo.set_summary(row['id'], summary=summary)
            except asyncio.CancelledError:
                await self.repo.set_summary(row['id'], error='摘要处理被中断')
                raise
            except Exception:
                await self.repo.set_summary(row['id'], error='图片摘要生成失败')
                raise
            finally:
                for path in frames:
                    path.unlink(missing_ok=True)
        return await self.repo.get(row['id'])

    async def match(self, expression: str) -> dict | None:
        rows = []
        for row in await self.repo.library():
            try:
                asset_path(row['url'], self.uploads)
            except ValueError:
                continue
            rows.append(row)
        if not rows:
            return None
        ids = [row['id'] for row in rows]
        schema = {'type': 'object', 'properties': {'id': {'type': ['string', 'null'], 'enum': [*ids, None]}},
                  'required': ['id'], 'additionalProperties': False}
        raw = await self.caller('sticker_matcher', [
            {'role': 'system', 'content': MATCH_PROMPT},
            {'role': 'user', 'content': json.dumps({'request': expression,
             'library': [{'id': row['id'], **json.loads(row['summary_json'])} for row in rows]}, ensure_ascii=False)},
        ], response_schema=schema, expect_json=True, temperature=0, max_tokens=192, timeout=15)
        result = json.loads(raw)
        if not isinstance(result, dict) or set(result) != {'id'}:
            raise ValueError('匹配结果格式错误')
        selected = result['id']
        if selected is None:
            return None
        if not isinstance(selected, str) or selected not in ids:
            raise ValueError('匹配结果不在候选集合内')
        row = next(row for row in rows if row['id'] == selected)
        asset_path(row['url'], self.uploads)
        return attachment(row, expression)

    async def _finish_save(self, event_id: str, row: dict) -> None:
        try:
            row = await self.summarize(row)
            await self.repo.finish(event_id, status='ready', result=attachment(row))
        except asyncio.CancelledError:
            await self.repo.finish(event_id, status='failed', error='收藏处理被中断')
            raise
        except Exception:
            await self.repo.finish(event_id, status='failed', error='图片已保存，但摘要生成失败')

    async def execute(self, intent: ToolIntent, context: ToolContext) -> dict:
        action = intent.tool_name.rsplit('.', 1)[-1]
        message_id = str(context.metadata.get('current_user_message_id') or '')
        event_id = f'sticker:{action}:{context.msg_id or context.request_id}'
        expression = str(intent.arguments.get('expression') or '').strip()
        inserted, previous = await self.repo.begin(event_id, action=action, conv_id=context.conv_id,
            source_message_id=message_id, assistant_message_id=context.msg_id or '', expression=expression,
            context=dict(context.metadata.get('sticker_context') or {}))
        if not inserted:
            return {**json.loads(previous['result_json']), 'event_id': event_id, 'status': previous['status']}
        try:
            if action == 'send':
                if not expression:
                    raise ValueError('缺少表情表达')
                selected = await self.match(expression)
                return await self.repo.finish(event_id, status='matched' if selected else 'miss',
                    result={'attachment': selected}, notify=selected is None)
            if action == 'save':
                async with self.repo._db() as db:
                    origin = await (await db.execute("SELECT attachments FROM messages WHERE id=? AND role='user'", (message_id,))).fetchone()
                images = [x for x in parse_attachments(origin[0] if origin else []) if is_sticker(x)]
                if len(images) != 1:
                    raise ValueError('本轮关联消息中没有唯一可收藏的表情，普通图片不能偷图')
                source = asset_path(attachment_url(images[0]), self.uploads)
                row = await self.store_bytes(await asyncio.to_thread(source.read_bytes), source_message_id=message_id)
                create_tracked_task(self._finish_save(event_id, row), name=f'sticker_summary:{event_id}')
                return {'event_id': event_id, 'status': 'running'}
            if action == 'create':
                if context.metadata.get('round_kind') != 'night' or not expression:
                    raise ValueError('本轮不能制作表情')
                from .image_provider import generate_sticker
                data = await generate_sticker(expression)
                row = await self.store_bytes(data, creation_prompt=expression)
                row = await self.summarize(row)
                return await self.repo.finish(event_id, status='ready', result=attachment(row))
            raise ValueError('未知表情动作')
        except asyncio.CancelledError:
            await self.repo.finish(event_id, status='failed', error='表情处理被中断')
            raise
        except Exception as exc:
            # Keep provider response bodies/secrets out of Core feedback.
            reason = str(exc) if isinstance(exc, ValueError) and not isinstance(exc, json.JSONDecodeError) else '表情服务处理失败'
            return await self.repo.finish(event_id, status='failed', error=reason[:200])


sticker_service = StickerService()


async def execute_sticker(intent: ToolIntent, context: ToolContext) -> dict:
    result = await sticker_service.execute(intent, context)
    if result['status'] == 'failed':
        raise RuntimeError('sticker_service_failed:' + result['event_id'])
    return result
