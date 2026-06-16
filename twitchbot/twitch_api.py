import json

import aiohttp

from .config import ACCESS_TOKEN, CLIENT_ID, BROADCASTER_ID
from .logger import logger


def _twitch_headers():
    return {
        "Authorization": f"Bearer {ACCESS_TOKEN}",
        "Client-Id": CLIENT_ID,
    }


async def log_stream_metadata():
    async with aiohttp.ClientSession() as session:
        headers = _twitch_headers()
        url = f"https://api.twitch.tv/helix/streams?user_id={BROADCASTER_ID}"

        async with session.get(url, headers=headers) as resp:
            data = await resp.json()
            logger.info("STREAM METADATA:\n%s", json.dumps(data, indent=2))


async def is_stream_live():
    """Return True if live, False if confirmed offline, or None if unknown.

    A non-200 response (auth expiry, 5xx, rate limit) is NOT proof the stream
    is offline, so it returns None. Callers must treat None as "no signal" and
    leave the live/offline state unchanged, otherwise a transient API blip would
    masquerade as the stream ending.
    """
    async with aiohttp.ClientSession() as session:
        headers = _twitch_headers()
        url = f"https://api.twitch.tv/helix/streams?user_id={BROADCASTER_ID}"

        async with session.get(url, headers=headers) as resp:
            if resp.status != 200:
                logger.error("Stream status check failed: HTTP %s", resp.status)
                return None

            data = await resp.json()
            live = len(data.get("data", [])) > 0
            logger.debug("is_stream_live: %s", live)
            return live


async def start_commercial(length: int = 180) -> bool:
    async with aiohttp.ClientSession() as session:
        headers = _twitch_headers()
        payload = {
            "broadcaster_id": BROADCASTER_ID,
            "length": length,
        }

        async with session.post(
            "https://api.twitch.tv/helix/channels/commercial",
            headers=headers,
            json=payload
        ) as resp:
            body = await resp.text()
            if resp.status != 200:
                logger.error(
                    "Failed to start ad. HTTP %s: %s",
                    resp.status,
                    body,
                )
                return False

            logger.info("Ad started successfully. Response: %s", body)
            return True


async def send_shoutout(to_broadcaster_id: str):
    async with aiohttp.ClientSession() as session:
        headers = _twitch_headers()
        params = {
            "from_broadcaster_id": BROADCASTER_ID,
            "to_broadcaster_id": to_broadcaster_id,
            "moderator_id": BROADCASTER_ID,
        }
        async with session.post(
            "https://api.twitch.tv/helix/chat/shoutouts",
            headers=headers,
            params=params,
        ) as resp:
            if resp.status == 204:
                logger.info("Shoutout sent to broadcaster %s", to_broadcaster_id)
                return True
            body = await resp.text()
            logger.error(
                "Shoutout failed. HTTP %s: %s", resp.status, body
            )
            return False


async def get_user_id(login: str) -> str | None:
    async with aiohttp.ClientSession() as session:
        headers = _twitch_headers()
        async with session.get(
            f"https://api.twitch.tv/helix/users?login={login}",
            headers=headers,
        ) as resp:
            if resp.status != 200:
                return None
            data = await resp.json()
            users = data.get("data", [])
            return users[0]["id"] if users else None
