import asyncio
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
    async with aiohttp.ClientSession() as session:
        headers = _twitch_headers()
        url = f"https://api.twitch.tv/helix/streams?user_id={BROADCASTER_ID}"

        async with session.get(url, headers=headers) as resp:
            if resp.status != 200:
                logger.error("Stream status check failed: HTTP %s", resp.status)
                return False

            data = await resp.json()
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
