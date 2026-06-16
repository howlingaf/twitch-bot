import asyncio
import json

import aiohttp

from .config import (
    ACCESS_TOKEN,
    REFRESH_TOKEN,
    CLIENT_ID,
    CLIENT_SECRET,
    BROADCASTER_ID,
)
from . import token_store
from .logger import logger

# Current Helix credentials. Seed from the persisted token store, falling back
# to .env for a first run before the store exists.
_stored = token_store.load()
_access_token = _stored.get("access_token") or ACCESS_TOKEN
_refresh_token = _stored.get("refresh_token") or REFRESH_TOKEN
_refresh_lock = asyncio.Lock()


def _twitch_headers():
    return {
        "Authorization": f"Bearer {_access_token}",
        "Client-Id": CLIENT_ID,
    }


async def _refresh_access_token() -> bool:
    """Swap the refresh token for a fresh access token. Returns success."""
    global _access_token, _refresh_token

    if not _refresh_token or not CLIENT_SECRET:
        logger.error(
            "Cannot refresh Twitch token: missing refresh token or CLIENT_SECRET. "
            "Re-run scripts/twitch_auth.py."
        )
        return False

    async with aiohttp.ClientSession() as session:
        async with session.post(
            "https://id.twitch.tv/oauth2/token",
            data={
                "grant_type": "refresh_token",
                "refresh_token": _refresh_token,
                "client_id": CLIENT_ID,
                "client_secret": CLIENT_SECRET,
            },
        ) as resp:
            body = await resp.text()
            if resp.status != 200:
                logger.error(
                    "Twitch token refresh failed. HTTP %s: %s", resp.status, body
                )
                return False
            data = json.loads(body)

    _access_token = data["access_token"]
    _refresh_token = data.get("refresh_token", _refresh_token)
    token_store.save(_access_token, _refresh_token)
    logger.info("Refreshed Twitch access token.")
    return True


async def _twitch_request(method: str, url: str, **kwargs):
    """Issue a Helix request, transparently refreshing once on a 401.

    Returns an (status, body_text) tuple; status is None on a transport-level
    failure. Callers parse the body as needed.
    """
    for attempt in range(2):
        token_before = _access_token
        try:
            async with aiohttp.ClientSession() as session:
                async with session.request(
                    method, url, headers=_twitch_headers(), **kwargs
                ) as resp:
                    body = await resp.text()
                    if resp.status == 401 and attempt == 0:
                        logger.warning(
                            "Helix 401 on %s %s — refreshing token and retrying.",
                            method, url,
                        )
                        async with _refresh_lock:
                            # Skip if another coroutine already refreshed.
                            refreshed = (token_before != _access_token) \
                                or await _refresh_access_token()
                        if refreshed:
                            continue
                    return resp.status, body
        except aiohttp.ClientError as e:
            logger.error("Helix request error on %s %s: %s", method, url, e)
            return None, ""
    return None, ""


async def log_stream_metadata():
    url = f"https://api.twitch.tv/helix/streams?user_id={BROADCASTER_ID}"
    status, body = await _twitch_request("GET", url)
    if status == 200:
        logger.info("STREAM METADATA:\n%s", json.dumps(json.loads(body), indent=2))
    else:
        logger.error("Stream metadata fetch failed: HTTP %s", status)


async def is_stream_live():
    """Return True if live, False if confirmed offline, or None if unknown.

    A non-200 response (5xx, rate limit, or a 401 the refresh couldn't fix) is
    NOT proof the stream is offline, so it returns None. Callers must treat None
    as "no signal" and leave the live/offline state unchanged, otherwise a
    transient API blip would masquerade as the stream ending.
    """
    url = f"https://api.twitch.tv/helix/streams?user_id={BROADCASTER_ID}"
    status, body = await _twitch_request("GET", url)
    if status != 200:
        logger.error("Stream status check failed: HTTP %s", status)
        return None

    data = json.loads(body)
    live = len(data.get("data", [])) > 0
    logger.debug("is_stream_live: %s", live)
    return live


async def start_commercial(length: int = 180) -> bool:
    payload = {
        "broadcaster_id": BROADCASTER_ID,
        "length": length,
    }
    status, body = await _twitch_request(
        "POST",
        "https://api.twitch.tv/helix/channels/commercial",
        json=payload,
    )
    if status != 200:
        logger.error("Failed to start ad. HTTP %s: %s", status, body)
        return False

    logger.info("Ad started successfully. Response: %s", body)
    return True


async def send_shoutout(to_broadcaster_id: str):
    params = {
        "from_broadcaster_id": BROADCASTER_ID,
        "to_broadcaster_id": to_broadcaster_id,
        "moderator_id": BROADCASTER_ID,
    }
    status, body = await _twitch_request(
        "POST",
        "https://api.twitch.tv/helix/chat/shoutouts",
        params=params,
    )
    if status == 204:
        logger.info("Shoutout sent to broadcaster %s", to_broadcaster_id)
        return True
    logger.error("Shoutout failed. HTTP %s: %s", status, body)
    return False


async def get_user_id(login: str) -> str | None:
    url = f"https://api.twitch.tv/helix/users?login={login}"
    status, body = await _twitch_request("GET", url)
    if status != 200:
        return None
    users = json.loads(body).get("data", [])
    return users[0]["id"] if users else None
