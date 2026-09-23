"""Restart the process when the Twitch chat connection has silently died.

On Sep 15 the IRC socket dropped and twitchio's reconnect never came back: the
process kept running (overlay, console API, live monitor all fine) with no
chat connection for three days, so a whole stream's chat went unrecorded and
nothing said so. twitchio's own logs don't reach ours, and nothing it exposes
reports "gave up reconnecting".

What it does expose: the time of Twitch's last PING. Twitch pings every ~5
minutes on a healthy connection, so a PING older than _STALE_SECONDS means
chat is dead however it got that way. Rather than trying to revive twitchio's
internals in place, log it and exit — systemd (Restart=always) brings the
whole bot back in seconds with a fresh token and a fresh connection.
"""
import asyncio
import logging
import os
import time

logger = logging.getLogger("twitch_bot")

_CHECK_SECONDS = 60
# Two missed 5-minute PINGs plus slack. Also covers a normal reconnect, which
# is back and pinged well inside this.
_STALE_SECONDS = 12 * 60


async def chat_watchdog_loop(bot):
    started = time.time()
    while True:
        await asyncio.sleep(_CHECK_SECONDS)
        conn = getattr(bot, "_connection", None)
        last = getattr(conn, "_last_ping", 0) or started
        silent = time.time() - max(last, started)
        if silent < _STALE_SECONDS:
            continue
        alive = bool(conn and conn.is_alive)
        # The [tag] is what logs.howling.one/alerts names the Stream Deck key
        # after; the prose after it is free to change.
        logger.critical(
            "[chat-dead] Twitch chat connection is dead (no PING from Twitch for %d min, socket %s); "
            "restarting the bot so chat is recorded again.",
            silent // 60, "open" if alive else "closed")
        # Let the Discord log feed flush the line above before going down.
        await asyncio.sleep(5)
        os._exit(1)
