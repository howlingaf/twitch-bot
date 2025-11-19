import asyncio
import contextlib
import json
import logging
import os
import re
from datetime import datetime, timedelta
from urllib.parse import parse_qs, urlparse

import aiohttp
import spotipy
import websockets
from dotenv import load_dotenv
from spotipy.oauth2 import SpotifyOAuth
from twitchio.ext import commands

load_dotenv()

# ---------------------------------------------------------
# -------------------------- LOGGING ----------------------
# ---------------------------------------------------------

LOG_DIR = os.path.join(os.path.dirname(__file__), "logs")
os.makedirs(LOG_DIR, exist_ok=True)

logger = logging.getLogger("twitch_bot")
logger.setLevel(logging.INFO)

_log_formatter = logging.Formatter(
    "%(asctime)s [%(levelname)s] %(name)s - %(message)s"
)

# Console handler
_console_handler = logging.StreamHandler()
_console_handler.setFormatter(_log_formatter)
logger.addHandler(_console_handler)

_file_handler = None


def _log_path_for_date(dt: datetime) -> str:
    return os.path.join(LOG_DIR, f"bot-{dt.strftime('%Y-%m-%d')}.log")


def setup_file_handler_for_today():
    """Set up a file handler for today's date, replacing the old one if needed."""
    global _file_handler

    if _file_handler is not None:
        logger.removeHandler(_file_handler)
        try:
            _file_handler.close()
        except Exception:
            pass

    path = _log_path_for_date(datetime.now())
    _file_handler = logging.FileHandler(path, encoding="utf-8")
    _file_handler.setFormatter(_log_formatter)
    logger.addHandler(_file_handler)
    logger.info("Log file handler set to %s", path)


def cleanup_old_logs(retention_days: int = 7):
    """Delete log files older than `retention_days` days."""
    try:
        today = datetime.now().date()
        cutoff = today - timedelta(days=retention_days)

        for filename in os.listdir(LOG_DIR):
            if not (filename.startswith("bot-") and filename.endswith(".log")):
                continue

            date_str = filename[len("bot-"):-len(".log")]
            try:
                file_date = datetime.strptime(date_str, "%Y-%m-%d").date()
            except ValueError:
                continue

            if file_date < cutoff:
                full_path = os.path.join(LOG_DIR, filename)
                try:
                    os.remove(full_path)
                    logger.info("Deleted old log file: %s", full_path)
                except Exception as e:
                    logger.error("Failed to delete old log file %s: %s", full_path, e)
    except Exception:
        logger.exception("Error during log cleanup")


async def log_maintenance_loop():
    """Rotate logs daily and clean up old files."""
    while True:
        try:
            now = datetime.now()
            # Next midnight + a small buffer (5 seconds)
            tomorrow = (now + timedelta(days=1)).replace(
                hour=0, minute=0, second=5, microsecond=0
            )
            sleep_seconds = (tomorrow - now).total_seconds()
            await asyncio.sleep(sleep_seconds)

            logger.info("Running daily log rotation & cleanup...")
            setup_file_handler_for_today()
            cleanup_old_logs(retention_days=7)
            logger.info("Log rotation & cleanup complete.")
        except asyncio.CancelledError:
            logger.info("Log maintenance loop cancelled.")
            break
        except Exception:
            logger.exception("Error in log maintenance loop")


# Initialize today's file handler immediately
setup_file_handler_for_today()
cleanup_old_logs(retention_days=7)

# ---------------------------------------------------------
# ---------------- OVERLAY WEBSOCKET SERVER ---------------
# ---------------------------------------------------------

overlay_clients = set()


async def overlay_handler(websocket):
    overlay_clients.add(websocket)
    logger.info("Overlay connected (clients=%d)", len(overlay_clients))
    try:
        async for _ in websocket:
            # If you ever want messages back from overlay, handle them here.
            pass
    except Exception as e:
        logger.error("Overlay websocket error: %s", e)
    finally:
        overlay_clients.discard(websocket)
        logger.info("Overlay disconnected (clients=%d)", len(overlay_clients))


async def overlay_broadcast(data: dict):
    if not overlay_clients:
        logger.info("No overlay clients connected for broadcast.")
        return

    message = json.dumps(data)
    dead = []

    for ws in overlay_clients:
        try:
            await ws.send(message)
        except Exception as e:
            logger.error("Error sending to overlay client: %s", e)
            dead.append(ws)

    for ws in dead:
        overlay_clients.discard(ws)

    logger.info(
        "Broadcasted overlay message to %d clients: %s",
        len(overlay_clients),
        data.get("command", list(data.keys())),
    )


# ---------------------------------------------------------
# ------------------------------ BOT ----------------------
# ---------------------------------------------------------

class Bot(commands.Bot):
    def __init__(self):
        super().__init__(
            token=os.getenv('BOT_OAUTH_TOKEN'),
            client_id=os.getenv('CLIENT_ID'),
            nick="hairyrug_",
            prefix='!',
            initial_channels=["howlingfantods_"],
        )

        self.current_problem = None
        self.spotify = None
        self.is_live = False
        self.ad_task = None
        self.lt_task = None
        self.ltlock_task = None

        self.init_spotify()

    # ---------------- SPOTIFY INIT ----------------
    def init_spotify(self):
        try:
            scope = "user-read-currently-playing user-read-playback-state"
            self.spotify = spotipy.Spotify(auth_manager=SpotifyOAuth(
                client_id=os.getenv('SPOTIFY_CLIENT_ID'),
                client_secret=os.getenv('SPOTIFY_CLIENT_SECRET'),
                redirect_uri=os.getenv('SPOTIFY_REDIRECT_URI'),
                scope=scope,
                cache_path=".spotify_cache"
            ))
            logger.info("Spotify API initialized successfully.")
        except Exception as e:
            logger.error("Spotify init failed: %s", e)
            self.spotify = None

    # -----------------------------------------------------
    # ---------------- STREAM METADATA LOGGER -------------
    # -----------------------------------------------------
    async def log_stream_metadata(self):
        async with aiohttp.ClientSession() as session:
            headers = {
                "Authorization": f"Bearer {os.getenv('ACCESS_TOKEN')}",
                "Client-Id": os.getenv("CLIENT_ID")
            }
            url = f"https://api.twitch.tv/helix/streams?user_id={os.getenv('BROADCASTER_ID')}"

            async with session.get(url, headers=headers) as resp:
                data = await resp.json()
                logger.info("STREAM METADATA:\n%s", json.dumps(data, indent=2))

    # -----------------------------------------------------
    # ---------------- STREAM LIVE CHECK ------------------
    # -----------------------------------------------------
    async def is_stream_live(self):
        async with aiohttp.ClientSession() as session:
            headers = {
                "Authorization": f"Bearer {os.getenv('ACCESS_TOKEN')}",
                "Client-Id": os.getenv("CLIENT_ID")
            }
            url = f"https://api.twitch.tv/helix/streams?user_id={os.getenv('BROADCASTER_ID')}"

            async with session.get(url, headers=headers) as resp:
                if resp.status != 200:
                    logger.error("Stream status check failed: HTTP %s", resp.status)
                    return False

                data = await resp.json()
                live = len(data.get("data", [])) > 0
                logger.debug("is_stream_live: %s", live)
                return live

    # -----------------------------------------------------
    # ---------------- GET CURRENT CATEGORY ---------------
    # -----------------------------------------------------
    async def get_current_category(self):
        """
        Fetch the category using `game_name`.
        Retry several times because Twitch may delay category population.
        """
        async with aiohttp.ClientSession() as session:
            headers = {
                "Authorization": f"Bearer {os.getenv('ACCESS_TOKEN')}",
                "Client-Id": os.getenv("CLIENT_ID")
            }
            url = f"https://api.twitch.tv/helix/streams?user_id={os.getenv('BROADCASTER_ID')}"

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

    # -----------------------------------------------------
    # ---------------- DELETE LATEST VOD -------------------
    # -----------------------------------------------------
    async def delete_latest_vod(self):
        category = await self.get_current_category()
        logger.info("VOD deletion check — category=%r", category)

        if category != "Fitness & Health":
            logger.info("Skipping VOD deletion — category is not 'Fitness & Health'.")
            return

        logger.info("Category is 'Fitness & Health' — deleting latest VOD…")

        async with aiohttp.ClientSession() as session:
            headers = {
                "Authorization": f"Bearer {os.getenv('ACCESS_TOKEN')}",
                "Client-Id": os.getenv("CLIENT_ID")
            }

            # Fetch latest archive VOD
            url = (
                f"https://api.twitch.tv/helix/videos?"
                f"user_id={os.getenv('BROADCASTER_ID')}&first=1&type=archive"
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

            # Delete it
            delete_url = f"https://api.twitch.tv/helix/videos?id={vod_id}"
            async with session.delete(delete_url, headers=headers) as delete_resp:
                body = await delete_resp.text()
                logger.info(
                    "Deleted VOD %s (status=%s, body=%s)",
                    vod_id,
                    delete_resp.status,
                    body,
                )

    # -----------------------------------------------------
    # ---------------- LIVE STATUS MONITOR ----------------
    # -----------------------------------------------------
    async def monitor_live_status(self):
        logger.info("Starting live status monitor loop...")
        first_check = True

        while True:
            try:
                live = await self.is_stream_live()

                if live and not self.is_live:
                    if first_check:
                        # Bot started while stream was already live
                        logger.info(
                            "Stream was already LIVE when bot started; "
                            "marking live without immediate ad."
                        )
                        self.is_live = True
                        await self.log_stream_metadata()
                        # Start ad loop WITHOUT immediate-first-ad
                        self.ad_task = asyncio.create_task(
                            self._run_ad_loop(run_first_immediately=False)
                        )
                    else:
                        # Genuine "went live" transition
                        logger.info("Stream just went LIVE!")
                        self.is_live = True
                        await self.log_stream_metadata()
                        # Start ad loop WITH immediate-first-ad
                        self.ad_task = asyncio.create_task(
                            self._run_ad_loop(run_first_immediately=True)
                        )

                elif not live and self.is_live:
                    logger.info("Stream went OFFLINE")
                    self.is_live = False

                    await self.delete_latest_vod()

                    if self.ad_task:
                        self.ad_task.cancel()
                        self.ad_task = None

                first_check = False
                await asyncio.sleep(20)

            except asyncio.CancelledError:
                logger.info("Live status monitor loop cancelled.")
                break
            except Exception:
                logger.exception("Error in live status monitor loop")
                await asyncio.sleep(10)

    # -----------------------------------------------------
    # ---------------- BOT READY EVENT --------------------
    # -----------------------------------------------------
    async def event_ready(self):
        logger.info("Bot ready | %s", self.nick)
        asyncio.create_task(self.monitor_live_status())

    # -----------------------------------------------------
    # ---------------- MESSAGE / COMMAND EVENTS -----------
    # -----------------------------------------------------
    async def event_message(self, message):
        # Only log commands, not all chat
        if message.content.startswith('!'):
            logger.info("[COMMAND] %s ran: %s", message.author.name, message.content)

        if message.echo:
            return

        await self.handle_commands(message)

    async def event_command_error(self, ctx, error):
        """
        Centralized error handler for all commands.
        Fires when a command raises, even if the command itself doesn't catch it.
        """
        cmd_name = ctx.command.name if getattr(ctx, "command", None) else "unknown"
        author_name = ctx.author.name if getattr(ctx, "author", None) else "unknown"
        logger.exception(
            "Error in command '%s' triggered by %s: %s",
            cmd_name,
            author_name,
            error,
        )

    # -----------------------------------------------------
    # ---------------- AD LOOP (Option A/B Mix) -----------
    # -----------------------------------------------------
    async def _run_ad_loop(self, run_first_immediately: bool):
        logger.info(
            "Ad loop started (run_first_immediately=%s).",
            run_first_immediately,
        )

        # Wait until channels are connected
        while not self.connected_channels:
            await asyncio.sleep(1)

        channel = self.connected_channels[0]

        try:
            #
            # ---------------- OPTIONAL FIRST AD ----------------
            #
            if run_first_immediately and self.is_live:
                await channel.send("📢 Ad in 1 minute!")
                logger.info("First ad alert sent immediately on stream start.")

                await asyncio.sleep(60)  # 1 minute warning

                if not self.is_live:
                    logger.info("Stream ended before first ad started.")
                    return

                # Run the first ad
                async with aiohttp.ClientSession() as session:
                    headers = {
                        "Authorization": f"Bearer {os.getenv('ACCESS_TOKEN')}",
                        "Client-Id": os.getenv("CLIENT_ID"),
                    }
                    payload = {
                        "broadcaster_id": os.getenv("BROADCASTER_ID"),
                        "length": 180,  # 3-minute ad
                    }

                    async with session.post(
                        "https://api.twitch.tv/helix/channels/commercial",
                        headers=headers,
                        json=payload
                    ) as resp:
                        body = await resp.text()
                        if resp.status != 200:
                            logger.error(
                                "Failed to start FIRST ad. HTTP %s: %s",
                                resp.status,
                                body,
                            )
                            return

                        logger.info("FIRST ad started successfully. Response: %s", body)
                        await channel.send("📺 Ad starting (3 minutes).")

                # Wait for ad to finish
                await asyncio.sleep(180)

                if not self.is_live:
                    logger.info("Stream ended during first ad break.")
                    return

                await channel.send("✅ Ad break over!")
                logger.info("FIRST ad break completed.")
            else:
                logger.info(
                    "Skipping immediate first ad (stream was already live at bot startup "
                    "or run_first_immediately=False)."
                )

            #
            # ---------------- RECURRING ADS ----------------
            #
            while self.is_live:
                # Wait 59 minutes (Twitch minimum spacing)
                await asyncio.sleep(59 * 60)

                if not self.is_live:
                    break

                await channel.send("📢 Ad in 1 minute!")
                logger.info("Recurring ad alert sent.")

                await asyncio.sleep(60)

                if not self.is_live:
                    break

                # Run the recurring ad
                async with aiohttp.ClientSession() as session:
                    headers = {
                        "Authorization": f"Bearer {os.getenv('ACCESS_TOKEN')}",
                        "Client-Id": os.getenv("CLIENT_ID"),
                    }
                    payload = {
                        "broadcaster_id": os.getenv("BROADCASTER_ID"),
                        "length": 180,
                    }

                    async with session.post(
                        "https://api.twitch.tv/helix/channels/commercial",
                        headers=headers,
                        json=payload
                    ) as resp:
                        body = await resp.text()
                        if resp.status != 200:
                            logger.error(
                                "Failed to start recurring ad. HTTP %s: %s",
                                resp.status,
                                body,
                            )
                            break

                        logger.info("Recurring ad started. Response: %s", body)
                        await channel.send("📺 Ad starting (3 minutes).")

                await asyncio.sleep(180)

                if not self.is_live:
                    break

                await channel.send("✅ Ad break over!")
                logger.info("Recurring ad break completed.")

        except asyncio.CancelledError:
            logger.info("Ad loop cancelled (stream offline).")
        except Exception:
            logger.exception("Fatal error in ad loop")

        logger.info("Ad loop stopped (stream offline).")

    # -----------------------------------------------------
    # ---------------- UTILITY HELPERS --------------------
    # -----------------------------------------------------
    def _extract_problem_name(self, url: str) -> str:
        m = re.search(r'leetcode\.com/problems/([^/]+)', url)
        return m.group(1).replace('-', ' ').title() if m else "LeetCode Problem"

    async def _get_youtube_label(self, parsed, original_url: str) -> str:
        """Fetch a nice YouTube label: 'Title — Channel' or reasonable fallback."""
        api_key = os.getenv("YOUTUBE_API_KEY")
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
                # Short youtu.be/<id>
                video_id = path
                kind = "video"
            elif "youtube.com" in domain:
                if path.startswith("watch"):
                    video_id = query.get("v", [None])[0]
                    kind = "video"
                elif path.startswith("shorts/"):
                    # /shorts/<id>
                    parts = path.split("/")
                    if len(parts) >= 2:
                        video_id = parts[1]
                    kind = "short"
                elif path.startswith("playlist"):
                    playlist_id = query.get("list", [None])[0]
                    is_playlist = True
                    kind = "playlist"
                else:
                    # Other paths: treat as generic YouTube
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
                        return f"YouTube Playlist — {title}"

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
                        return f"{title} — {channel}"

            # Fallback
            if kind == "playlist":
                return "YouTube Playlist"
            if kind == "short":
                return "YouTube Short"
            return "YouTube Video"

        except Exception:
            logger.exception("Error while fetching YouTube label for %s", original_url)
            return "YouTube Video"

    async def _make_lockin_label(self, target: str) -> str:
        """Return a clean label for any target: URL or text."""
        target = target.strip()

        # If plain text, return as-is
        if not (target.startswith("http://") or target.startswith("https://")):
            return target

        try:
            parsed = urlparse(target)
            domain = parsed.netloc.replace("www.", "").lower()

            # LeetCode
            if "leetcode.com" in domain:
                if "/problems/" in parsed.path:
                    slug = parsed.path.split("/problems/")[-1].split("/")[0]
                    return slug.replace("-", " ").title()
                return "LeetCode"

            # YouTube
            if "youtube.com" in domain or "youtu.be" in domain:
                return await self._get_youtube_label(parsed, target)

            # Generic URL fallback: just domain
            return domain

        except Exception:
            logger.exception("Error building lock-in label for %s", target)
            return target

    # -----------------------------------------------------
    # ---------------- LT TIMER COMMANDS ------------------
    # -----------------------------------------------------
    @commands.command(name='lt')
    async def leetcode_timer(self, ctx, url: str = None, minutes: int = 30):
        logger.info("!lt triggered by %s (url=%r, minutes=%r)", ctx.author.name, url, minutes)
        try:
            if not (ctx.author.is_mod or ctx.author.is_broadcaster or ctx.author.is_vip):
                logger.info("!lt ignored for %s — insufficient permissions", ctx.author.name)
                return

            if url and url.lower() == "clear":
                if self.lt_task and not self.lt_task.done():
                    self.lt_task.cancel()
                    logger.info("!lt clear — cancelled existing LT timer task.")
                self.current_problem = None
                return

            if not url or minutes <= 0 or minutes > 180:
                logger.info("!lt invalid args for %s — url=%r, minutes=%r", ctx.author.name, url, minutes)
                return

            self.current_problem = url
            problem_name = self._extract_problem_name(url)
            await ctx.send(f"⏰ {minutes}-minute timer started for '{problem_name}'")

            self.lt_task = asyncio.create_task(self._run_lt_timer(ctx, problem_name, minutes))
            logger.info("LT timer started for '%s' (%d minutes)", problem_name, minutes)

        except Exception:
            logger.exception("Error in !lt command")

    async def _run_lt_timer(self, ctx, problem_name, minutes):
        try:
            halfway = (minutes * 60) // 2
            final = minutes * 60 - halfway

            await asyncio.sleep(halfway)
            await ctx.send(f"⏰ Halfway done with '{problem_name}'")

            await asyncio.sleep(final)
            await ctx.send(f"⏰ Time's up for '{problem_name}'")
            logger.info("LT timer completed for '%s'", problem_name)

        except asyncio.CancelledError:
            logger.info("LT timer cancelled for '%s'", problem_name)
        except Exception:
            logger.exception("Error in LT timer loop")

    # -----------------------------------------------------
    # ---------------- LOCK-IN + OVERLAY TIMER ------------
    # -----------------------------------------------------
    @commands.command(name='ltlockin')
    async def leetcode_lockin(self, ctx, *args):
        logger.info("!ltlockin triggered by %s (args=%r)", ctx.author.name, args)
        try:
            if not (ctx.author.is_mod or ctx.author.is_broadcaster or ctx.author.is_vip):
                logger.info("!ltlockin ignored for %s — insufficient permissions", ctx.author.name)
                return

            # Clear command
            if len(args) == 1 and args[0].lower() == "clear":
                if self.ltlock_task and not self.ltlock_task.done():
                    self.ltlock_task.cancel()
                await overlay_broadcast({"command": "stop"})
                logger.info("LOCK-IN cancelled via !ltlockin clear.")
                return

            if len(args) < 2:
                logger.info("!ltlockin ignored — need at least 2 args (target, minutes)")
                return

            # Last arg = minutes
            try:
                minutes = int(args[-1])
                if minutes <= 0 or minutes > 180:
                    logger.info("!ltlockin invalid minutes=%d", minutes)
                    return
            except ValueError:
                logger.info("!ltlockin failed — last argument not an integer minutes")
                return

            # Everything except last arg = target description (URL or text)
            target = " ".join(args[:-1]).strip()
            self.current_problem = target

            display_name = await self._make_lockin_label(target)

            await ctx.send(f"🔒 LOCKED IN — {minutes} minutes for: {display_name}")
            logger.info("LOCK-IN started for %r (%d minutes)", display_name, minutes)

            await overlay_broadcast({
                "command": "start",
                "duration": minutes * 60,
                "label": display_name
            })

            self.ltlock_task = asyncio.create_task(
                self._run_ltlock_timer(ctx, display_name, minutes)
            )

        except Exception:
            logger.exception("Error in !ltlockin command")

    async def _run_ltlock_timer(self, ctx, target_label, minutes):
        try:
            await asyncio.sleep(minutes * 60)
            await overlay_broadcast({"command": "stop"})
            await ctx.send(f"⏰ Time's up for '{target_label}' — LOCK-IN over!")
            logger.info("LOCK-IN timer completed for '%s'", target_label)
        except asyncio.CancelledError:
            logger.info("LOCK-IN timer cancelled for '%s'", target_label)
        except Exception:
            logger.exception("Error in LTLOCK timer loop")

    # -----------------------------------------------------
    # ---------------- OTHER COMMANDS ---------------------
    # -----------------------------------------------------
    @commands.command(name='daily')
    async def daily_leetcode(self, ctx):
        logger.info("!daily triggered by %s", ctx.author.name)
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get('https://leetcode-api-pied.vercel.app/daily') as resp:
                    if resp.status != 200:
                        logger.error("!daily fetch failed: HTTP %s", resp.status)
                        return

                    data = await resp.json()
                    title = data['question']['title']
                    diff = data['question']['difficulty']
                    link = f"https://leetcode.com{data['link']}"

                    await ctx.send(f"📅 Daily: {title} ({diff}) | {link}")
                    logger.info("!daily responded with %s (%s)", title, diff)

        except Exception:
            logger.exception("Error in !daily command")

    @commands.command(name='song')
    async def current_song(self, ctx):
        logger.info("!song triggered by %s", ctx.author.name)
        try:
            if not self.spotify:
                logger.info("!song ignored — Spotify not initialized")
                return

            data = self.spotify.current_playback()
            if not data or not data.get("is_playing"):
                logger.info("!song — nothing is currently playing")
                return

            item = data["item"]
            song = item["name"]
            artists = ", ".join(a["name"] for a in item["artists"])
            url = item["external_urls"]["spotify"]

            await ctx.send(f"🎵 {song} — {artists} | {url}")
            logger.info("!song responded with '%s' by %s", song, artists)

        except Exception:
            logger.exception("Error in !song command")

    @commands.command(name='problem')
    async def get_problem(self, ctx, problem_id: str = None):
        logger.info("!problem triggered by %s (problem_id=%r)", ctx.author.name, problem_id)
        try:
            if problem_id is None:
                logger.info("!problem ignored — no problem_id provided")
                return

            async with aiohttp.ClientSession() as session:
                async with session.get(f'https://leetcode-api-pied.vercel.app/problem/{problem_id}') as resp:
                    if resp.status != 200:
                        logger.error("!problem fetch failed: HTTP %s", resp.status)
                        return

                    data = await resp.json()
                    await ctx.send(
                        f"🧩 #{problem_id}: {data['title']} ({data['difficulty']}) | {data['url']}"
                    )
                    logger.info(
                        "!problem responded with #%s: %s (%s)",
                        problem_id, data['title'], data['difficulty']
                    )

        except Exception:
            logger.exception("Error in !problem command")

    @commands.command(name='spotify')
    async def get_spotify(self, ctx):
        logger.info("!spotify triggered by %s", ctx.author.name)
        await ctx.send('https://open.spotify.com/user/31s6zl5xs5kqjw7qbrqgslamrcfa')

    @commands.command(name='goodreads')
    async def get_goodreads(self, ctx):
        logger.info("!goodreads triggered by %s", ctx.author.name)
        await ctx.send('https://www.goodreads.com/howlingfantods_')

    @commands.command(name='discord')
    async def get_discord(self, ctx):
        logger.info("!discord triggered by %s", ctx.author.name)
        await ctx.send('https://discord.gg/tHjeDK8Cd7')


# ---------------------------------------------------------
# ------------------------------ MAIN ----------------------
# ---------------------------------------------------------

async def main():
    bot = Bot()

    overlay_host = "0.0.0.0"
    overlay_port = int(os.getenv("OVERLAY_PORT", "8765"))

    server = await websockets.serve(overlay_handler, overlay_host, overlay_port)
    logger.info(
        "Overlay WebSocket server listening on ws://%s:%d",
        overlay_host,
        overlay_port,
    )

    log_maintenance_task = asyncio.create_task(log_maintenance_loop())

    try:
        await bot.start()
    finally:
        logger.info("Shutting down overlay server and log maintenance task...")
        server.close()
        await server.wait_closed()

        log_maintenance_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await log_maintenance_task


if __name__ == "__main__":
    asyncio.run(main())
