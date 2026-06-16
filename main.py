import asyncio
import contextlib

import websockets
from aiohttp import web

from twitchbot import Bot, overlay_handler, log_maintenance_loop, logger
from twitchbot.overlay import serve_overlay
from twitchbot.config import OVERLAY_PORT, CONSOLE_SECRET, CONSOLE_PORT
from twitchbot.chat_auth import ChatTokenManager, chat_token_refresh_loop
from twitchbot.console import make_console_app


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

    # Inbound console API for the Discord bot (localhost-only, opt-in via secret).
    console_runner = None
    if CONSOLE_SECRET:
        console_runner = web.AppRunner(make_console_app(bot))
        await console_runner.setup()
        await web.TCPSite(console_runner, "127.0.0.1", CONSOLE_PORT).start()
        logger.info("Console API listening on http://127.0.0.1:%d", CONSOLE_PORT)
    else:
        logger.info("CONSOLE_SECRET not set; console API disabled.")

    try:
        await bot.start()
    finally:
        logger.info("Shutting down overlay server and log maintenance task...")
        server.close()
        await server.wait_closed()

        if console_runner:
            await console_runner.cleanup()

        log_maintenance_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await log_maintenance_task

        chat_refresh_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await chat_refresh_task


if __name__ == "__main__":
    asyncio.run(main())
