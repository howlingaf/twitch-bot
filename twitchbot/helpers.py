import re
from urllib.parse import parse_qs, urlparse

import aiohttp

from .config import YOUTUBE_API_KEY
from .logger import logger


def extract_problem_name(url: str) -> str:
    m = re.search(r'leetcode\.com/problems/([^/]+)', url)
    return m.group(1).replace('-', ' ').title() if m else "Problem"


async def get_youtube_label(parsed, original_url: str) -> str:
    """Fetch a nice YouTube label: 'Title \u2014 Channel' or reasonable fallback."""
    api_key = YOUTUBE_API_KEY
    if not api_key:
        logger.info("YOUTUBE_API_KEY not set; using generic YouTube label.")
        return "YouTube Video"

    domain = parsed.netloc.replace("www.", "").lower()
    path = parsed.path.strip("/")
    query = parse_qs(parsed.query)

    is_playlist = False
    playlist_id = None
    video_id = None
    kind = "video"

    try:
        if "youtu.be" in domain:
            video_id = path
            kind = "video"
        elif "youtube.com" in domain:
            if path.startswith("watch"):
                video_id = query.get("v", [None])[0]
                kind = "video"
            elif path.startswith("shorts/"):
                parts = path.split("/")
                if len(parts) >= 2:
                    video_id = parts[1]
                kind = "short"
            elif path.startswith("playlist"):
                playlist_id = query.get("list", [None])[0]
                is_playlist = True
                kind = "playlist"
            else:
                return "YouTube"
        else:
            return "YouTube"

        async with aiohttp.ClientSession() as session:
            if is_playlist and playlist_id:
                api_url = "https://www.googleapis.com/youtube/v3/playlists"
                params = {
                    "part": "snippet",
                    "id": playlist_id,
                    "key": api_key,
                }
                async with session.get(api_url, params=params) as resp:
                    if resp.status != 200:
                        body = await resp.text()
                        logger.error(
                            "YouTube playlist API error HTTP %s: %s",
                            resp.status,
                            body,
                        )
                        return "YouTube Playlist"
                    data = await resp.json()
                    items = data.get("items", [])
                    if not items:
                        return "YouTube Playlist"
                    snippet = items[0].get("snippet", {})
                    title = snippet.get("title") or "Playlist"
                    return f"YouTube Playlist \u2014 {title}"

            if video_id:
                api_url = "https://www.googleapis.com/youtube/v3/videos"
                params = {
                    "part": "snippet",
                    "id": video_id,
                    "key": api_key,
                }
                async with session.get(api_url, params=params) as resp:
                    if resp.status != 200:
                        body = await resp.text()
                        logger.error(
                            "YouTube video API error HTTP %s: %s",
                            resp.status,
                            body,
                        )
                        return "YouTube Video"
                    data = await resp.json()
                    items = data.get("items", [])
                    if not items:
                        return "YouTube Video"
                    snippet = items[0].get("snippet", {})
                    title = snippet.get("title") or "YouTube Video"
                    channel = snippet.get("channelTitle") or "YouTube"
                    return f"{title} \u2014 {channel}"

        if kind == "playlist":
            return "YouTube Playlist"
        if kind == "short":
            return "YouTube Short"
        return "YouTube Video"

    except Exception:
        logger.exception("Error while fetching YouTube label for %s", original_url)
        return "YouTube Video"


async def make_lockin_label(target: str) -> str:
    """Return a clean label for any target: URL or text."""
    target = target.strip()

    if not (target.startswith("http://") or target.startswith("https://")):
        return target

    try:
        parsed = urlparse(target)
        domain = parsed.netloc.replace("www.", "").lower()

        if "leetcode.com" in domain:
            if "/problems/" in parsed.path:
                slug = parsed.path.split("/problems/")[-1].split("/")[0]
                return slug.replace("-", " ").title()
            return "LeetCode"

        if "youtube.com" in domain or "youtu.be" in domain:
            return await get_youtube_label(parsed, target)

        return domain

    except Exception:
        logger.exception("Error building lock-in label for %s", target)
        return target
