"""Desktop Presence V1 services."""

from .sprites import SpriteLibrary, sprite_library
from .service import PresenceDeliveryService, presence_service

__all__ = [
    "PresenceDeliveryService",
    "SpriteLibrary",
    "presence_service",
    "sprite_library",
]
