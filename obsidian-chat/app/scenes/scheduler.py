import asyncio
import logging


async def run_loop(service):
    # This is a software player/clock tick, not a model polling interval.
    while True:
        try:
            await service.tick()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logging.getLogger(__name__).error("scene tick failed: %s", type(exc).__name__)
            if service.active:
                await service.action(service.active.row["id"], owner_id=service.active.row["owner_id"], action="pause")
        await asyncio.sleep(0.05)
