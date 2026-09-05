"""音乐、心语和记忆保存的意图构建与执行适配器。"""

from app.tools.schemas import ToolContext, ToolIntent
from music import get_audio_url, search_songs
from .side_effects import _store_heart_whisper, _store_remember_notes


async def _execute_heart_whisper(intent: ToolIntent, context: ToolContext) -> dict | None:
    content = str(intent.arguments.get("content") or "").strip()
    if not context.msg_id:
        raise ValueError("heart.whisper requires msg_id")
    return await _store_heart_whisper(context.conv_id, context.msg_id, content)


async def _execute_remember_note(intent: ToolIntent, context: ToolContext) -> dict:
    content = str(intent.arguments.get("content") or "").strip()
    if content:
        await _store_remember_notes([content], context.conv_id)
    return {"content": content, "stored": bool(content)}


async def _execute_music_search(intent: ToolIntent, _context: ToolContext) -> dict:
    query = str(intent.arguments.get("query") or "").strip()
    if not query:
        return {"query": query, "cards": []}
    results = search_songs(query, limit=5)
    if not results:
        return {"query": query, "cards": []}
    song = dict(results[0])
    song["audio_url"] = get_audio_url(song["id"])
    song["candidates"] = [dict(item) for item in results[1:4]]
    return {"query": query, "cards": [song]}


def _music_search_intents(postprocessed) -> list[ToolIntent]:
    return [intent for intent in getattr(postprocessed, "tool_intents", ())
            if intent.tool_name == "music.search"]


def _heart_whisper_intents(postprocessed) -> list[ToolIntent]:
    heart_intents = [intent for intent in getattr(postprocessed, "tool_intents", ())
                     if intent.tool_name == "heart.whisper"]
    if heart_intents:
        return heart_intents
    fallback: list[ToolIntent] = []
    for index, content in enumerate(getattr(postprocessed, "heart_whispers", ()) or (), 1):
        content = str(content or "").strip()
        if not content:
            continue
        fallback.append(ToolIntent(
            id=f"stream_heart_{index:03d}", tool_name="heart.whisper",
            raw_text=f"[HEART:{content}]", arguments={"content": content},
            side_effect_level="write", allowed_modes=("normal",),
            metadata={"legacy_marker": "HEART", "command_group": "heart", "source": "postprocess_result"},
        ))
    return fallback


def _remember_intents(postprocessed) -> list[ToolIntent]:
    remember_intents = [intent for intent in getattr(postprocessed, "tool_intents", ())
                       if intent.tool_name == "memory.remember"]
    if remember_intents:
        return remember_intents
    fallback: list[ToolIntent] = []
    for index, content in enumerate(getattr(postprocessed, "remember_notes", ()) or (), 1):
        content = str(content or "").strip()
        if not content:
            continue
        fallback.append(ToolIntent(
            id=f"stream_remember_{index:03d}", tool_name="memory.remember",
            raw_text=f"[REMEMBER:{content}]", arguments={"content": content},
            side_effect_level="write", allowed_modes=("normal",),
            metadata={"legacy_marker": "REMEMBER", "command_group": "remember", "source": "postprocess_result"},
        ))
    return fallback
