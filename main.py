import asyncio
import contextlib

import websockets

from twitchbot import Bot, overlay_handler, log_maintenance_loop, logger
from twitchbot.config import OVERLAY_PORT


async def main():
    bot = Bot()

    overlay_host = "0.0.0.0"

    server = await websockets.serve(overlay_handler, overlay_host, OVERLAY_PORT)
    logger.info(
        "Overlay WebSocket server listening on ws://%s:%d",
        overlay_host,
        OVERLAY_PORT,
    )

    log_maintenance_task = asyncio.create_task(log_maintenance_loop())

    try:
        await bot.start()
    finally:
        logger.info("Shutting down overlay server and log maintenance task...")
        server.close()
        await server.wait_closed()

        log_maintenance_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await log_maintenance_task


if __name__ == "__main__":
    asyncio.run(main())
