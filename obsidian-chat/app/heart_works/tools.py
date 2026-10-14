"""Work-local materials. No chat delivery, autoplay, or cross-turn inbox."""
from __future__ import annotations

import asyncio
import hashlib
import io
from pathlib import Path


def image_config():
    from app.presence.image_provider import _request_config, PresenceImageProviderError
    for slot in ('sticker_image', 'presence_image'):
        try:
            return _request_config(slot)
        except PresenceImageProviderError:
            continue
    raise ValueError('image_unavailable')


def capabilities() -> frozenset[str]:
    from music import get_music_dependency_status
    from app.web_search import web_search_service
    result = set()
    try:
        image_config()
        result.add('IMAGE')
    except ValueError:
        pass
    if get_music_dependency_status().get('available'):
        result.update({'MUSIC', 'MUSIC_SEARCH'})
    if web_search_service.enabled():
        result.add('WEB_SEARCH_INTENT')
    return frozenset(result)


def material_root() -> Path:
    from config import DATA_DIR
    return Path(DATA_DIR) / 'heart_works'


async def execute(*, work_id: str, task_id: str, tool: str, query: str) -> dict:
    recovered_image = tool == 'IMAGE' and (material_root() / work_id / (task_id + '.png')).is_file()
    if not recovered_image and tool not in capabilities():
        raise ValueError('capability_unavailable:' + tool)
    if tool == 'IMAGE':
        from app.presence.image_provider import ImageGenerationTransport
        from PIL import Image
        path = material_root() / work_id / (task_id + '.png')
        if not path.is_file():
            data = await ImageGenerationTransport().generate_raw(query, request_config=image_config())
            def save():
                path.parent.mkdir(parents=True, exist_ok=True)
                with Image.open(io.BytesIO(data)) as img:
                    img.load()
                    temp = path.with_suffix('.tmp')
                    img.save(temp, format='PNG')
                    temp.replace(path)
            await asyncio.to_thread(save)
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        return {'block': {'type': 'image', 'asset_id': task_id,
                         'url': f'/api/heart-whispers/works/{work_id}/assets/{task_id}',
                         'description': query},
                'asset': {'id': task_id, 'path': str(path), 'type': 'image', 'sha256': digest}}
    if tool in {'MUSIC', 'MUSIC_SEARCH'}:
        from music import search_songs, get_audio_url
        songs = await asyncio.to_thread(search_songs, query, limit=5)
        if not songs:
            raise ValueError('music_no_results')
        if tool == 'MUSIC_SEARCH':
            return {'candidates': songs}
        song = songs[0]
        if not await asyncio.to_thread(get_audio_url, song['id']):
            raise ValueError('music_unplayable')
        return {'block': {'type': 'music', 'song_id': song['id'],
                         'title': song.get('name', ''), 'artist': song.get('artist', ''),
                         'url': f"/api/music/stream/{song['id']}"}}
    if tool == 'WEB_SEARCH_INTENT':
        from app.web_search.provider import TavilySearchProvider
        return await TavilySearchProvider().search(query)
    raise ValueError('unknown_work_tool')
