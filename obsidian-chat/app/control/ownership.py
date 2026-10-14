"""One transition lock shared by legacy control and independent scenes."""

import asyncio
from contextlib import asynccontextmanager

control_mode_lock = asyncio.Lock()


@asynccontextmanager
async def legacy_control_mode():
    """A legacy resume/claim must not race a scene's entry either."""
    from app.scenes.session import get_scene_service
    from .service import ControlClaimRejected
    async with control_mode_lock:
        if get_scene_service().occupies_control():
            raise ControlClaimRejected("scene_control_busy")
        yield
