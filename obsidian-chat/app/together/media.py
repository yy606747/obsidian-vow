from __future__ import annotations

import asyncio
import hashlib
import io
from pathlib import Path

from PIL import Image, ImageOps

from app.ephemeral_media import EphemeralImage


def normalize_image(data, config):
    if not data or len(data) > config["image_max_bytes"]:
        raise ValueError("together_image_too_large")
    with Image.open(io.BytesIO(data)) as original:
        if original.width * original.height > 24_000_000 or original.format not in {"JPEG", "PNG", "WEBP"}:
            raise ValueError("together_invalid_image")
        image = ImageOps.exif_transpose(original).convert("RGB")
        image.thumbnail((config["image_max_edge"], config["image_max_edge"]))
        output = io.BytesIO()
        # Re-encoding also drops EXIF/location data and unexpected extra frames.
        image.save(output, "JPEG", quality=config["jpeg_quality"])
    result = output.getvalue()
    if len(result) > config["image_max_bytes"]:
        raise ValueError("together_image_too_large")
    return result


class MediaStore:
    def __init__(self, directory: Path | None = None):
        self.directory = directory

    def root(self):
        if self.directory is not None:
            return self.directory
        from config import DATA_DIR
        return DATA_DIR / "together-media"

    def path(self, filename):
        if not filename or Path(filename).name != filename or not filename.startswith("tc_") or not filename.endswith(".jpg"):
            raise ValueError("together_invalid_media_ref")
        return self.root() / filename

    async def save(self, capture_id, data, config):
        encoded = await asyncio.to_thread(normalize_image, data, config)
        filename = capture_id+".jpg"
        path = self.path(filename)
        self.root().mkdir(parents=True, exist_ok=True)
        await asyncio.to_thread(path.write_bytes, encoded)
        return filename, hashlib.sha256(encoded).hexdigest()

    async def attachment(self, capture):
        data = await asyncio.to_thread(self.path(capture["filename"]).read_bytes)
        if hashlib.sha256(data).hexdigest() != capture["sha256"]:
            raise ValueError("together_image_changed")
        return {"url": capture["filename"], "mime_type": "image/jpeg",
                "expected_sha256": capture["sha256"], "_ephemeral_image": EphemeralImage(data)}

    async def delete(self, filename):
        if filename:
            await asyncio.to_thread(self.path(filename).unlink, missing_ok=True)
