import asyncio
import base64
import json

import pytest

from app.heart_works import chat_reference as reference
from test_heart_works import repo, enqueue, work, worker


def test_reference_loads_actual_work_once_and_rejects_forged_payload(repo):
    async def run():
        row = await enqueue(repo)
        service, _ = worker(repo, [work('真正的作品正文', cover='原始封面')])
        await service.run(row['id'])
        items = await reference.resolve_references([
            {'type': 'heart_work', 'id': row['id'], 'name': '伪造标题', 'body': '伪造正文', '_ephemeral_image': '/secret'}],
            repository=repo)
        assert items == [{'type': 'heart_work', 'id': row['id'], 'name': '原始封面'}]
        history = [{'role': 'user', 'content': '聊聊结尾', 'attachments': items},
                   {'role': 'assistant', 'content': '以前的话'},
                   {'role': 'user', 'content': '再说一点', 'attachments': items}]
        materialized = await reference.materialize_references(history, user_name='小栀', ai_name='阿澈', repository=repo)
        text = '\n'.join(message['content'] for message in materialized)
        assert text.count('真正的作品正文') == 1
        assert '原始封面' in text and '伪造' not in text
        assert '小栀' in text and '阿澈' in text and '用户' not in text
        assert history[0]['attachments'] == items  # no mutation of persisted history
        assert not materialized[-1]['attachments']
        await repo.delete(row['id'])
        with pytest.raises(reference.InvalidHeartWork):
            await reference.resolve_references(items, repository=repo)
        materialized = await reference.materialize_references(history, user_name='小栀', ai_name='阿澈', repository=repo)
        text = '\n'.join(message['content'] for message in materialized)
        assert '现已不可用' in text and '真正的作品正文' not in text
    asyncio.run(run())


def test_referenced_image_reaches_both_provider_encoders_without_public_copy(repo, tmp_path, monkeypatch):
    import ai_providers
    from app.heart_works import tools
    monkeypatch.setattr(tools, 'material_root', lambda: tmp_path)
    png = base64.b64decode('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aS2kAAAAASUVORK5CYII=')
    path = tmp_path / 'source.png'
    path.write_bytes(png)
    async def run():
        row = await enqueue(repo)
        service, _ = worker(repo, [work('画作正文')])
        await service.run(row['id'])
        cover = [{'type': 'image', 'asset_id': 'asset', 'description': '创作时的描述'}]
        body = [{'type': 'text', 'text': '画作正文'}]
        await repo.execute('UPDATE heart_works SET body_json=?,cover_json=? WHERE id=?',
                           (json.dumps(body), json.dumps(cover), row['id']))
        await repo.execute('INSERT INTO heart_work_assets VALUES (?,?,?,?,?,?)',
                           ('asset', row['id'], 'task', 'image', str(path), '{}'))
        history = [{'role': 'user', 'content': '看这幅画', 'attachments': [{'type': 'heart_work', 'id': row['id']}]}]
        materialized = await reference.materialize_references(history, user_name='小栀', ai_name='阿澈', repository=repo)
        assert '"image_loaded": true' in materialized[0]['content']
        openai = ai_providers.build_multimodal_messages(materialized)
        assert any(part.get('type') == 'image_url' for part in openai[0]['content'])
        gemini = ai_providers.build_gemini_contents(materialized)
        assert any('inline_data' in part for part in gemini[0]['parts'])
        # A query-only read identifies the image description as a creation
        # request, without claiming that a text-only lookup saw the image.
        recalled = await reference.read_work_context(row['id'], repository=repo)
        assert recalled['body'][0]['image_loaded'] is False
        assert recalled['body'][0]['creation_request'] == '创作时的描述'
    asyncio.run(run())


def test_large_work_explicitly_reports_truncation_and_unpublished_is_unavailable(repo):
    async def run():
        row = await enqueue(repo)
        assert await reference.read_work_context(row['id'], repository=repo) is None
        await repo.execute("UPDATE heart_works SET status='published',body_json=? WHERE id=?",
                           (json.dumps([{'type': 'text', 'text': '字' * 20000}]), row['id']))
        result = await reference.read_work_context(row['id'], repository=repo)
        assert result['truncated'] is True and len(result['body'][0]['text']) == 16000
    asyncio.run(run())
