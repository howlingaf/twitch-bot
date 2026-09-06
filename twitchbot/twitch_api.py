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
from . import oauth, token_store
from .logger import logger

# Current Helix credentials. Seed from the persisted token store, falling back
# to .env for a first run before the store exists.
_stored = token_store.load(token_store.HELIX_TOKEN_PATH)
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

    data = await oauth.refresh_tokens(_refresh_token, CLIENT_ID, CLIENT_SECRET)
    if not data:
        return False

    _access_token = data["access_token"]
    _refresh_token = data.get("refresh_token", _refresh_token)
    token_store.save(_access_token, _refresh_token, token_store.HELIX_TOKEN_PATH)
    logger.info("Refreshed Twitch Helix access token.")
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


async def fetch_stream_metadata(verbose: bool = True) -> dict | None:
    """The current stream's Helix metadata (None if offline).

    Returns what it logs so a caller can read a field — the stream title, for
    the recap heading — without a second identical request. verbose=False for
    callers on a loop: the full dump is worth one line at go-live, not one a
    minute for twelve hours.
    """
    url = f"https://api.twitch.tv/helix/streams?user_id={BROADCASTER_ID}"
    status, body = await _twitch_request("GET", url)
    if status != 200:
        logger.error("Stream metadata fetch failed: HTTP %s", status)
        return None
    data = json.loads(body)
    if verbose:
        logger.info("STREAM METADATA:\n%s", json.dumps(data, indent=2))
    entries = data.get("data", [])
    return entries[0] if entries else None


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


async def start_commercial(length: int = 180) -> int:
    """Start an ad break. Returns the length Twitch ACTUALLY served, 0 on failure.

    The requested length is a request, not a guarantee — Twitch may serve a
    shorter break, and the caller has to time its "ad over" message off what
    came back rather than what it asked for.
    """
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
        return 0

    logger.info("Ad started successfully. Response: %s", body)
    try:
        entry = (json.loads(body).get("data") or [{}])[0]
        served = int(entry.get("length") or 0)
    except (ValueError, TypeError, IndexError):
        served = 0
    if served and served != length:
        logger.warning("Twitch served a %ss ad, not the %ss requested.", served, length)
    # A 200 with no usable length still means the break started; fall back to
    # what we asked for rather than treating it as a failure.
    return served or length


async def get_ad_schedule() -> dict | None:
    """Twitch's own view of ads: next_ad_at, duration, preroll_free_time, snoozes.

    Needs channel:read:ads. Every field reads 0 while the channel is offline,
    so it only says anything during a stream.
    """
    url = f"https://api.twitch.tv/helix/channels/ads?broadcaster_id={BROADCASTER_ID}"
    status, body = await _twitch_request("GET", url)
    if status != 200:
        logger.error("Ad schedule lookup failed. HTTP %s: %s", status, body)
        return None
    try:
        return (json.loads(body).get("data") or [{}])[0]
    except (ValueError, IndexError):
        return None


async def get_latest_vod() -> dict | None:
    """The newest past-broadcast VOD: {url, duration, title, created_at}, or None.

    Twitch publishes the VOD a little after the stream ends, so right at
    offline this can return the PREVIOUS stream's VOD — callers compare
    created_at against the stream they mean.
    """
    url = (f"https://api.twitch.tv/helix/videos?user_id={BROADCASTER_ID}"
           "&type=archive&first=1")
    status, body = await _twitch_request("GET", url)
    if status != 200:
        logger.error("VOD lookup failed. HTTP %s: %s", status, body)
        return None
    vids = json.loads(body).get("data") or []
    if not vids:
        return None
    v = vids[0]
    return {"url": v.get("url"), "duration": v.get("duration"),
            "title": v.get("title"), "created_at": v.get("created_at")}


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


async def get_stream_started_at() -> str | None:
    """ISO start time of the current stream, or None if offline/unknown."""
    url = f"https://api.twitch.tv/helix/streams?user_id={BROADCASTER_ID}"
    status, body = await _twitch_request("GET", url)
    if status != 200:
        logger.error("Stream start lookup failed: HTTP %s", status)
        return None
    data = json.loads(body).get("data", [])
    return data[0]["started_at"] if data else None


async def get_follow_info(user_id: str) -> tuple[bool, str | None]:
    """Look up when user_id followed the channel.

    Returns (ok, followed_at): ok False means the lookup itself failed;
    followed_at is the ISO timestamp, or None if they don't follow.
    """
    url = (
        "https://api.twitch.tv/helix/channels/followers"
        f"?broadcaster_id={BROADCASTER_ID}&user_id={user_id}"
    )
    status, body = await _twitch_request("GET", url)
    if status != 200:
        logger.error("Follow lookup failed. HTTP %s: %s", status, body)
        return False, None
    data = json.loads(body).get("data", [])
    return True, data[0]["followed_at"] if data else None


async def get_new_followers(since_iso: str) -> list[tuple[str, str, str]] | None:
    """Everyone who followed at or after since_iso, as (id, login, followed_at).

    Helix returns followers newest first, so paging stops at the first one
    older than the cutoff rather than walking the whole follower list. Needs
    moderator:read:followers. None means the lookup failed, which is not the
    same as nobody following.
    """
    base = ("https://api.twitch.tv/helix/channels/followers"
            f"?broadcaster_id={BROADCASTER_ID}&first=100")
    out: list[tuple[str, str, str]] = []
    cursor = ""
    for _ in range(20):         # 2000 follows is far past anything one stream does
        url = base + (f"&after={cursor}" if cursor else "")
        status, body = await _twitch_request("GET", url)
        if status != 200:
            logger.error("Get Followers failed. HTTP %s: %s", status, body)
            return None
        data = json.loads(body)
        for u in data.get("data", []):
            if u["followed_at"] < since_iso:
                return out
            out.append((u["user_id"], u["user_login"], u["followed_at"]))
        cursor = data.get("pagination", {}).get("cursor", "")
        if not cursor:
            return out
    return out


async def get_category_rank(game_id: str, user_login: str) -> tuple[int, int, int] | None:
    """Where user_login sits in its category right now: (viewers, rank, of).

    Helix returns a category's streams viewer-descending, so rank is position
    in that list. It pages 100 at a time and the tail is enormous, so this
    stops once past the streamer — `of` is therefore "at least this many",
    which is all the rank needs. None if the lookup fails or they're offline.
    """
    seen = 0
    cursor = ""
    for _ in range(10):             # 1000 streams deep is far past any rank worth naming
        url = (f"https://api.twitch.tv/helix/streams?game_id={game_id}&first=100"
               + (f"&after={cursor}" if cursor else ""))
        status, body = await _twitch_request("GET", url)
        if status != 200:
            logger.error("Category rank lookup failed. HTTP %s: %s", status, body)
            return None
        data = json.loads(body)
        entries = data.get("data", [])
        for i, st in enumerate(entries, 1):
            if st["user_login"].lower() == user_login.lower():
                return st["viewer_count"], seen + i, seen + len(entries)
        seen += len(entries)
        cursor = data.get("pagination", {}).get("cursor", "")
        if not cursor or not entries:
            return None
    return None


async def get_chatters() -> list[tuple[str, str]] | None:
    """Everyone currently connected to the channel's chat, as (id, login).

    Needs moderator:read:chatters on the broadcaster token. Returns None if
    the request fails (including a 403 from a token without that scope) so
    the caller can tell "nobody here" from "couldn't look".
    """
    base = (
        "https://api.twitch.tv/helix/chat/chatters"
        f"?broadcaster_id={BROADCASTER_ID}&moderator_id={BROADCASTER_ID}&first=1000"
    )
    chatters: list[tuple[str, str]] = []
    cursor = ""
    while True:
        url = base + (f"&after={cursor}" if cursor else "")
        status, body = await _twitch_request("GET", url)
        if status != 200:
            logger.error("Get Chatters failed. HTTP %s: %s", status, body)
            return None
        data = json.loads(body)
        chatters.extend((u["user_id"], u["user_login"]) for u in data.get("data", []))
        cursor = data.get("pagination", {}).get("cursor", "")
        if not cursor:
            return chatters


async def get_user_id(login: str) -> str | None:
    url = f"https://api.twitch.tv/helix/users?login={login}"
    status, body = await _twitch_request("GET", url)
    if status != 200:
        return None
    users = json.loads(body).get("data", [])
    return users[0]["id"] if users else None


# App access token (client credentials) for the Helix chat send endpoint.
# Twitch only shows the native chat-bot badge on messages sent this way — a
# user token or IRC PRIVMSG gets no badge. App tokens have no refresh token;
# a new one is requested on startup and whenever a send hits a 401.
_app_access_token: str | None = None
_app_token_lock = asyncio.Lock()


async def _fetch_app_access_token() -> bool:
    global _app_access_token

    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                "https://id.twitch.tv/oauth2/token",
                data={
                    "client_id": CLIENT_ID,
                    "client_secret": CLIENT_SECRET,
                    "grant_type": "client_credentials",
                },
            ) as resp:
                body = await resp.text()
                if resp.status != 200:
                    logger.error(
                        "App access token fetch failed. HTTP %s: %s",
                        resp.status, body,
                    )
                    return False
                _app_access_token = json.loads(body)["access_token"]
                logger.info("Fetched Twitch app access token.")
                return True
    except aiohttp.ClientError as e:
        logger.error("App access token fetch error: %s", e)
        return False


async def send_chat_message(text: str, sender_id: str) -> bool:
    """Send a chat message via Helix as the bot account. Returns success.

    Requires one-time authorization grants on the app: user:bot from the bot
    account, plus channel:bot from the broadcaster (or the bot being a mod).
    """
    global _app_access_token

    payload = {
        "broadcaster_id": BROADCASTER_ID,
        "sender_id": sender_id,
        "message": text,
    }

    for attempt in range(2):
        if _app_access_token is None:
            async with _app_token_lock:
                if _app_access_token is None and not await _fetch_app_access_token():
                    return False

        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    "https://api.twitch.tv/helix/chat/messages",
                    headers={
                        "Authorization": f"Bearer {_app_access_token}",
                        "Client-Id": CLIENT_ID,
                    },
                    json=payload,
                ) as resp:
                    body = await resp.text()
                    if resp.status == 401 and attempt == 0:
                        logger.warning(
                            "Helix chat send 401 — fetching a fresh app token "
                            "and retrying."
                        )
                        _app_access_token = None
                        continue
                    if resp.status == 200:
                        data = json.loads(body)["data"][0]
                        if data.get("is_sent"):
                            return True
                        logger.error(
                            "Helix chat message dropped: %s",
                            data.get("drop_reason"),
                        )
                        return False
                    logger.error(
                        "Helix chat send failed. HTTP %s: %s", resp.status, body
                    )
                    return False
        except aiohttp.ClientError as e:
            logger.error("Helix chat send error: %s", e)
            return False
    return False
