"""Sticker generation keeps the requested text and background intact."""

from app.presence.image_provider import ImageGenerationTransport, _request_config


class StickerImageProvider(ImageGenerationTransport):
    @staticmethod
    def _request_config():
        return _request_config('sticker_image')

    @staticmethod
    def _render_prompt(prompt: str, *, reference_prompt: str = '') -> str:
        return ('制作一张用于日常聊天的表情图片。保留创作描述中指定的配字、动作、神态和画风；'
                '未指定配字时不额外添加文字。\n创作描述：\n' + str(prompt))


async def generate_sticker(prompt: str) -> bytes:
    return await StickerImageProvider().generate_raw(prompt)
