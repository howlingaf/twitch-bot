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
    url = f"https://api.twitch.tv/helix/streams?user_id={BROADCASTER_ID}"
    status, body = await _twitch_request("GET", url)
    if status != 200:
        logger.error("Stream status check failed: HTTP %s", status)
        return False

    data = json.loads(body)
    live = len(data.get("data", [])) > 0
    logger.debug("is_stream_live: %s", live)
    return live


async def get_current_category():
    """
    Fetch the category using `game_name`.
    Retry several times because Twitch may delay category population.
    """
    async with aiohttp.ClientSession() as session:
        headers = _twitch_headers()
        url = f"https://api.twitch.tv/helix/streams?user_id={BROADCASTER_ID}"

        for attempt in range(5):
            async with session.get(url, headers=headers) as resp:
                data = await resp.json()

                if data.get("data"):
                    game_name = data["data"][0].get("game_name")
                    logger.info(
                        "[Category attempt %d] game_name = %r",
                        attempt, game_name
                    )

                    if game_name:
                        return game_name

            await asyncio.sleep(2)

    logger.warning("Category never populated, returning None")
    return None


async def delete_latest_vod():
    category = await get_current_category()
    logger.info("VOD deletion check \u2014 category=%r", category)

    if category != "Fitness & Health":
        logger.info("Skipping VOD deletion \u2014 category is not 'Fitness & Health'.")
        return

    logger.info("Category is 'Fitness & Health' \u2014 deleting latest VOD\u2026")

    async with aiohttp.ClientSession() as session:
        headers = _twitch_headers()

        url = (
            f"https://api.twitch.tv/helix/videos?"
            f"user_id={BROADCASTER_ID}&first=1&type=archive"
        )

        async with session.get(url, headers=headers) as resp:
            if resp.status != 200:
                body = await resp.text()
                logger.error(
                    "Failed to fetch latest VOD. HTTP %s: %s",
                    resp.status,
                    body
                )
                return

            data = await resp.json()
            if not data.get("data"):
                logger.info("No VOD found to delete.")
                return

            vod_id = data["data"][0]["id"]
            logger.info("Latest VOD to delete: %s", vod_id)

        delete_url = f"https://api.twitch.tv/helix/videos?id={vod_id}"
        async with session.delete(delete_url, headers=headers) as delete_resp:
            body = await delete_resp.text()
            logger.info(
                "Deleted VOD %s (status=%s, body=%s)",
                vod_id,
                delete_resp.status,
                body,
            )


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
