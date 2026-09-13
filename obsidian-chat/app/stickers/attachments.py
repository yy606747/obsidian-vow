"""Sticker identity on the chat wire; image MIME alone never implies a sticker."""


class InvalidStickerAttachment(ValueError):
    pass


def is_sticker(attachment) -> bool:
    return isinstance(attachment, dict) and attachment.get('type') == 'sticker'


def normalize_sticker_request(attachment: dict) -> dict:
    sticker_id = attachment.get('id')
    if not isinstance(sticker_id, str) or not sticker_id.strip() or len(sticker_id) > 80:
        raise InvalidStickerAttachment('表情不存在，请从表情面板重新选择')
    # URL, MIME and summary are resolved from the library, never from the client.
    return {'type': 'sticker', 'id': sticker_id.strip()}
