"""Sample original sticker bytes for visual models without changing the asset."""

import io

from PIL import Image


def png_frames(data: bytes) -> list[bytes]:
    frames = []
    with Image.open(io.BytesIO(data)) as picture:
        count = getattr(picture, 'n_frames', 1)
        for index in sorted({0, count // 2, count - 1}):
            picture.seek(index)
            rgba = picture.convert('RGBA')
            rgba.thumbnail((768, 768))
            frame = Image.new('RGB', rgba.size, 'white')
            frame.paste(rgba, mask=rgba.getchannel('A'))
            output = io.BytesIO()
            frame.save(output, format='PNG')
            frames.append(output.getvalue())
    return frames
