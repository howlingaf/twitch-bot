"""Outbound alerts to the Discord bot, for things worth interrupting someone over.

Twitch chat is the wrong channel for these: the broadcaster is mid-stream and a
chat line about the bot's internals is noise to viewers and easily missed by
the one person who needs it. This posts to the Discord bot's /alert endpoint,
which pings the owner in #twitch-bot-console.
"""
import aiohttp

from .config import CONSOLE_SECRET, DISCORD_BOT_URL
from .logger import logger

_TIMEOUT = aiohttp.ClientTimeout(total=10)


async def alert_owner(message: str) -> bool:
    """Best effort — an alert failing must never take down its caller."""
    if not (CONSOLE_SECRET and DISCORD_BOT_URL):
        logger.warning("Cannot alert owner: CONSOLE_SECRET or DISCORD_BOT_URL unset.")
        return False
    try:
        async with aiohttp.ClientSession(timeout=_TIMEOUT) as session:
            async with session.post(
                f"{DISCORD_BOT_URL}/alert",
                json={"message": message},
                headers={"Authorization": f"Bearer {CONSOLE_SECRET}"},
            ) as resp:
                body = await resp.json(content_type=None)
                if resp.status == 200 and body.get("ok"):
                    logger.info("Owner alert sent: %s", message)
                    return True
                logger.error("Owner alert failed (HTTP %s): %s", resp.status, body)
                return False
    except Exception as e:
        logger.error("Owner alert request failed: %r", e)
        return False
