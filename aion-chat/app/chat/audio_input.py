"""Voice-message attachment helpers shared by chat routes and providers."""

from __future__ import annotations

import json
import math
import mimetypes
from pathlib import Path
from typing import Any, Iterable

from config import UPLOADS_DIR


AUDIO_INPUT_UNAVAILABLE_CODE = "audio_input_unavailable"
AUDIO_INPUT_UNAVAILABLE_MESSAGE = (
    "当前主模型不支持直接听取音频，请切换到支持音频的模型后再发送。"
)
AUDIO_ATTACHMENT_UNAVAILABLE_CODE = "audio_attachment_unavailable"
AUDIO_ATTACHMENT_UNAVAILABLE_MESSAGE = "语音文件不可用，请重新录制后再发送。"

_AUDIO_SUFFIXES = {".wav", ".mp3"}
_AUDIO_MIME_TYPES = {
    "audio/wav",
    "audio/x-wav",
    "audio/wave",
    "audio/mpeg",
    "audio/mp3",
}
_MAX_TRANSCRIPT_CHARS = 20_000
_MAX_DURATION_MS = 10 * 60 * 1000


class InvalidAudioAttachment(ValueError):
    """Raised when a client claims an attachment is audio but it is malformed."""


def parse_attachments(value: Any) -> list[Any]:
    if isinstance(value, str):
        try:
            parsed = json.loads(value) if value else []
        except Exception:
            return []
        return list(parsed) if isinstance(parsed, list) else []
    return list(value or [])


def attachment_url(attachment: Any) -> str:
    if isinstance(attachment, str):
        return attachment.strip()
    if isinstance(attachment, dict):
        return str(attachment.get("url") or "").strip()
    return ""


def attachment_mime_type(attachment: Any) -> str:
    if isinstance(attachment, dict):
        explicit = str(
            attachment.get("mime_type")
            or attachment.get("mimeType")
            or attachment.get("content_type")
            or ""
        ).strip().lower()
        legacy_type = str(attachment.get("type") or "").strip().lower()
        if explicit:
            return explicit
        if legacy_type.startswith("audio/"):
            return legacy_type
    url = attachment_url(attachment)
    return (mimetypes.guess_type(url)[0] or "").lower()


def is_audio_attachment(attachment: Any) -> bool:
    if isinstance(attachment, dict) and str(attachment.get("type") or "").lower() == "voice":
        return True
    mime_type = attachment_mime_type(attachment)
    if mime_type.startswith("audio/"):
        return True
    return Path(attachment_url(attachment)).suffix.lower() in _AUDIO_SUFFIXES


def contains_audio_attachment(attachments: Any) -> bool:
    return any(is_audio_attachment(item) for item in parse_attachments(attachments))


def audio_format(attachment: Any) -> str | None:
    suffix = Path(attachment_url(attachment)).suffix.lower()
    mime_type = attachment_mime_type(attachment)
    if suffix == ".wav" or mime_type in {"audio/wav", "audio/x-wav", "audio/wave"}:
        return "wav"
    if suffix == ".mp3" or mime_type in {"audio/mpeg", "audio/mp3"}:
        return "mp3"
    return None


def uploaded_attachment_path(attachment: Any) -> Path | None:
    url = attachment_url(attachment)
    if not url.startswith("/uploads/"):
        return None
    name = Path(url).name
    if not name or name in {".", ".."}:
        return None
    return UPLOADS_DIR / name


def normalize_chat_attachments(attachments: Iterable[Any] | None) -> list[Any]:
    """Keep legacy URL attachments and canonicalize structured voice messages."""

    normalized: list[Any] = []
    for item in attachments or []:
        if isinstance(item, str):
            value = item.strip()
            if not value:
                continue
            if is_audio_attachment(value):
                _validate_audio_url_and_format(value)
                path = uploaded_attachment_path(value)
                if path is None:  # Kept explicit for type-checkers and future validators.
                    raise InvalidAudioAttachment("语音附件必须来自 /uploads/")
                value = f"/uploads/{path.name}"
            normalized.append(value)
            continue

        if not isinstance(item, dict):
            raise InvalidAudioAttachment("附件格式无效")

        if not is_audio_attachment(item):
            raise InvalidAudioAttachment("只允许结构化语音附件")

        fmt = _validate_audio_url_and_format(item)
        path = uploaded_attachment_path(item)
        if path is None:  # Kept explicit for type-checkers and future validators.
            raise InvalidAudioAttachment("语音附件必须来自 /uploads/")
        url = f"/uploads/{path.name}"
        transcript = str(item.get("transcript") or "").strip()[:_MAX_TRANSCRIPT_CHARS]
        duration_ms = _coerce_duration_ms(item)
        mime_type = "audio/wav" if fmt == "wav" else "audio/mpeg"
        voice = {
            "type": "voice",
            "url": url,
            "mime_type": mime_type,
            "duration_ms": duration_ms,
            "transcript": transcript,
        }
        name = str(item.get("name") or "").strip()
        if name:
            voice["name"] = Path(name).name[:255]
        normalized.append(voice)
    return normalized


def _validate_audio_url_and_format(attachment: Any) -> str:
    path = uploaded_attachment_path(attachment)
    if path is None:
        raise InvalidAudioAttachment("语音附件必须来自 /uploads/")
    fmt = audio_format(attachment)
    if fmt not in {"wav", "mp3"}:
        raise InvalidAudioAttachment("语音附件只支持 WAV 或 MP3")
    mime_type = attachment_mime_type(attachment)
    if mime_type and mime_type not in _AUDIO_MIME_TYPES:
        raise InvalidAudioAttachment("语音附件 MIME 类型无效")
    return fmt


def _coerce_duration_ms(attachment: dict) -> int:
    raw = attachment.get("duration_ms")
    if raw is None and attachment.get("duration") is not None:
        raw = float(attachment.get("duration") or 0) * 1000
    try:
        value = float(raw or 0)
    except (TypeError, ValueError):
        value = 0
    if not math.isfinite(value):
        value = 0
    return max(0, min(int(round(value)), _MAX_DURATION_MS))


def missing_audio_attachments(attachments: Any) -> list[Any]:
    missing = []
    for item in parse_attachments(attachments):
        if not is_audio_attachment(item):
            continue
        path = uploaded_attachment_path(item)
        if path is None or not path.is_file():
            missing.append(item)
    return missing


def audio_transcript_context(attachments: Any) -> str:
    """Return provider-only text context; the stored/displayed message stays unchanged."""

    voice_items = [
        item for item in parse_attachments(attachments)
        if is_audio_attachment(item)
    ]
    if not voice_items:
        return ""
    transcripts = [
        str(item.get("transcript") or "").strip()
        for item in voice_items
        if isinstance(item, dict) and str(item.get("transcript") or "").strip()
    ]
    if transcripts:
        joined = "\n".join(transcripts)
        return f"<meta>语音自动转写（可能有误）：{joined}</meta>"
    return "<meta>这是一条语音消息；自动转写不可用。</meta>"
