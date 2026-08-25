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


async def _post(path: str, payload: dict) -> dict | None:
    if not (CONSOLE_SECRET and DISCORD_BOT_URL):
        logger.warning("Cannot reach Discord bot: CONSOLE_SECRET or DISCORD_BOT_URL unset.")
        return None
    try:
        async with aiohttp.ClientSession(timeout=_TIMEOUT) as session:
            async with session.post(f"{DISCORD_BOT_URL}{path}", json=payload,
                                    headers={"Authorization": f"Bearer {CONSOLE_SECRET}"}) as resp:
                body = await resp.json(content_type=None)
                if resp.status == 200 and body.get("ok"):
                    return body
                logger.error("%s failed (HTTP %s): %s", path, resp.status, body)
    except Exception as e:
        logger.error("%s request failed: %r", path, e)
    return None


async def stream_alert(title: str, game: str) -> str | None:
    """Post the go-live announcement. Returns the Discord message id."""
    body = await _post("/stream-alert", {"title": title, "game": game})
    return body.get("message_id") if body else None


async def stream_alert_vod(message_id: str, title: str, game: str,
                           vod_url: str, duration: str) -> bool:
    """Edit the go-live post into its VOD card."""
    return bool(await _post("/stream-alert/vod", {
        "message_id": message_id, "title": title, "game": game,
        "vod_url": vod_url, "duration": duration}))
