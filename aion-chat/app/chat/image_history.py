"""近期原图保留策略；不生成摘要，也不增加模型调用。"""

from __future__ import annotations

from pathlib import Path

from config import SETTINGS, UPLOADS_DIR
from .audio_input import attachment_mime_type, attachment_url


DEFAULT_IMAGE_HISTORY = {
    "image_history_user_turns": 4,
    "image_history_max_images": 4,
    "image_history_max_bytes": 20 * 1024 * 1024,
}
_MAXIMUMS = {
    "image_history_user_turns": 100,
    "image_history_max_images": 32,
    "image_history_max_bytes": 128 * 1024 * 1024,
}
_REASONS = {
    "outside_window": "超出近期发言窗口",
    "image_limit": "超过历史图片数量上限",
    "byte_limit": "超过历史图片大小上限",
    "missing_file": "原图文件缺失",
    "unreadable_file": "原图文件无法读取",
}


def image_history_config(settings: dict | None = None) -> dict[str, int]:
    raw = SETTINGS if settings is None else settings
    result = {}
    for key, default in DEFAULT_IMAGE_HISTORY.items():
        try:
            value = int(raw.get(key, default))
        except (ValueError, TypeError, OverflowError):
            value = default
        result[key] = max(0, min(_MAXIMUMS[key], value))
    return result


def _is_image(attachment) -> bool:
    return attachment_mime_type(attachment).startswith("image/")


def restore_recent_images(
    history: list[dict],
    *,
    original_attachments: dict[str, list],
    real_user_ids: list[str],
    user_name: str,
    ai_name: str,
) -> dict:
    """在原有音视频策略之后恢复近期图片，原消息位置和附件顺序保持不变。"""
    limits = image_history_config()
    turns = limits["image_history_user_turns"]
    maximum = limits["image_history_max_images"]
    byte_limit = limits["image_history_max_bytes"]
    enabled = turns > 0 and maximum > 0 and byte_limit > 0
    meta = {"enabled": enabled, "limits": limits, "retained_images": 0, "retained_bytes": 0, "omitted": []}
    if not enabled or not real_user_ids:
        return meta
    current_id = real_user_ids[-1]
    allowed_ids = set(real_user_ids[-turns:])
    by_id = {str(message.get("id") or ""): message for message in history}
    for message_id in reversed(real_user_ids):
        message = by_id.get(message_id)
        if message is None:
            continue
        originals = original_attachments.get(message_id, [])
        retained_indexes = set()
        omitted = []
        image_number = 0
        for index, attachment in enumerate(originals):
            if not _is_image(attachment):
                continue
            image_number += 1
            if message_id == current_id:
                # 本轮图片不占历史预算，不改变本轮附件的既有校验规则。
                retained_indexes.add(index)
                continue
            reason = ""
            size = 0
            if message_id not in allowed_ids:
                reason = "outside_window"
            elif meta["retained_images"] >= maximum:
                reason = "image_limit"
            else:
                path = UPLOADS_DIR / Path(attachment_url(attachment)).name
                try:
                    if not path.is_file():
                        reason = "missing_file"
                    else:
                        size = path.stat().st_size
                        if size + meta["retained_bytes"] > byte_limit:
                            reason = "byte_limit"
                except OSError:
                    reason = "unreadable_file"
            if reason:
                event = {"message_id": message_id, "image_number": image_number, "reason": reason}
                meta["omitted"].append(event)
                omitted.append(event)
            else:
                retained_indexes.add(index)
                meta["retained_images"] += 1
                meta["retained_bytes"] += size
        existing = message.get("attachments") or []
        message["attachments"] = [
            attachment for index, attachment in enumerate(originals)
            if index in retained_indexes or (not _is_image(attachment) and attachment in existing)
        ]
        if omitted:
            detail = "；".join(
                f"第{event['image_number']}张：{_REASONS[event['reason']]}" for event in omitted
            )
            note = (
                f"[图片上下文] {user_name}在这条发言中的部分图片本轮未加载（{detail}）。"
                f"{ai_name}不能据此声称本轮看见了这些原图。"
            )
            message["content"] = "\n".join(part for part in (message.get("content", ""), note) if part)
    return meta
