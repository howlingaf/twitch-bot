import asyncio
import contextlib

import websockets

from twitchbot import Bot, overlay_handler, log_maintenance_loop, logger
from twitchbot.overlay import serve_overlay
from twitchbot.config import OVERLAY_PORT
from twitchbot.chat_auth import ChatTokenManager, chat_token_refresh_loop


async def main():
    # Resolve a fresh chat token before connecting so each start is valid, then
    # keep it refreshed in the background for seamless reconnects.
    chat_tokens = ChatTokenManager()
    token = await chat_tokens.ensure_fresh()
    bot = Bot(token=token)
    chat_refresh_task = asyncio.create_task(chat_token_refresh_loop(bot, chat_tokens))

    overlay_host = "0.0.0.0"

    server = await websockets.serve(
        overlay_handler, overlay_host, OVERLAY_PORT,
        process_request=serve_overlay,
    )
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

        chat_refresh_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await chat_refresh_task


if __name__ == "__main__":
    asyncio.run(main())
