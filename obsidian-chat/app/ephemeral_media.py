"""Server-created image bytes, never a client-supplied path or public upload."""
from dataclasses import dataclass, field


@dataclass(frozen=True)
class EphemeralImage:
    data: bytes = field(repr=False)


def ephemeral_image_bytes(attachment):
    value = attachment.get("_ephemeral_image") if isinstance(attachment, dict) else None
    return value.data if isinstance(value, EphemeralImage) else None
