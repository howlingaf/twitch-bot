import asyncio
import re
import time
from urllib.parse import urlparse

import aiohttp
import spotipy
from spotipy.oauth2 import SpotifyOAuth
from twitchio.ext import commands

from .config import (
    BOT_OAUTH_TOKEN,
    CLIENT_ID,
    SPOTIFY_CLIENT_ID,
    SPOTIFY_CLIENT_SECRET,
    SPOTIFY_REDIRECT_URI,
    DISCORD_BOT_URL,
    RECAP_SECRET,
)
from .logger import logger
from .overlay import overlay_broadcast
from .twitch_api import (
    log_stream_metadata,
    is_stream_live,
    delete_latest_vod,
    start_commercial,
)
from .helpers import extract_problem_name, make_lockin_label

_LEETCODE_SUBMISSION_RE = re.compile(
    r"https?://(?:www\.)?leetcode\.com/problems/([^/]+)/submissions/(\d+)"
)


class Bot(commands.Bot):
    def __init__(self):
        super().__init__(
            token=BOT_OAUTH_TOKEN,
            client_id=CLIENT_ID,
            nick="hairyrug_",
            prefix='!',
            initial_channels=["howlingfantods_"],
        )

        self.current_problem = None
        self.spotify = None
        self.is_live = False
        self.ad_task = None
        self.spotify_task = None
        self.lt_task = None
        self.ltlock_task = None
        self._last_spotify_track_id = None

        # Recap tracking
        self.stream_start_ts: int | None = None
        self.chatter_submissions: list[dict] = []
        self._seen_submissions: set[tuple[str, str]] = set()
        self.stream_problems: list[str] = []  # slugs from !lt commands

        self.init_spotify()

    # ---------------- SPOTIFY INIT ----------------
    def init_spotify(self):
        try:
            scope = "user-read-currently-playing user-read-playback-state"
            self.spotify = spotipy.Spotify(auth_manager=SpotifyOAuth(
                client_id=SPOTIFY_CLIENT_ID,
                client_secret=SPOTIFY_CLIENT_SECRET,
                redirect_uri=SPOTIFY_REDIRECT_URI,
                scope=scope,
                cache_path=".spotify_cache"
            ))
            logger.info("Spotify API initialized successfully.")
        except Exception as e:
            logger.error("Spotify init failed: %s", e)
            self.spotify = None

    # ---------------- LIVE STATUS MONITOR ----------------
    async def monitor_live_status(self):
        logger.info("Starting live status monitor loop...")
        first_check = True

        while True:
            try:
                live = await is_stream_live()

                if live and not self.is_live:
                    # Reset recap tracking
                    self.stream_start_ts = int(time.time())
                    self.chatter_submissions = []
                    self._seen_submissions = set()
                    self.stream_problems = []

                    if first_check:
                        logger.info(
                            "Stream was already LIVE when bot started; "
                            "marking live without immediate ad."
                        )
                        self.is_live = True
                        await log_stream_metadata()
                        self.ad_task = asyncio.create_task(
                            self._run_ad_loop(run_first_immediately=False)
                        )
                    else:
                        logger.info("Stream just went LIVE!")
                        self.is_live = True
                        await log_stream_metadata()
                        self.ad_task = asyncio.create_task(
                            self._run_ad_loop(run_first_immediately=True)
                        )

                    self.spotify_task = asyncio.create_task(
                        self.monitor_spotify()
                    )

                elif not live and self.is_live:
                    logger.info("Stream went OFFLINE")
                    self.is_live = False

                    # Send recap to Discord bot
                    await self._send_recap()

                    await delete_latest_vod()

                    if self.ad_task:
                        self.ad_task.cancel()
                        self.ad_task = None

                    if self.spotify_task:
                        self.spotify_task.cancel()
                        self.spotify_task = None

                first_check = False
                await asyncio.sleep(20)

            except asyncio.CancelledError:
                logger.info("Live status monitor loop cancelled.")
                break
            except Exception:
                logger.exception("Error in live status monitor loop")
                await asyncio.sleep(10)

    # ---------------- SPOTIFY NOW-PLAYING MONITOR ----------------
    async def monitor_spotify(self):
        logger.info("Spotify now-playing monitor started.")
        try:
            while self.is_live:
                try:
                    if not self.spotify:
                        await asyncio.sleep(5)
                        continue

                    data = await asyncio.to_thread(self.spotify.current_playback)

                    if data and data.get("is_playing") and data.get("item"):
                        item = data["item"]
                        track_id = item.get("id")
                        images = item.get("album", {}).get("images", [])
                        album_art = images[0]["url"] if images else ""

                        if track_id != self._last_spotify_track_id:
                            self._last_spotify_track_id = track_id
                            logger.info(
                                "Now playing: %s — %s",
                                item["name"],
                                ", ".join(a["name"] for a in item["artists"]),
                            )

                        await overlay_broadcast({
                            "command": "nowplaying",
                            "song": item["name"],
                            "artists": ", ".join(a["name"] for a in item["artists"]),
                            "album_art": album_art,
                            "progress_ms": data.get("progress_ms", 0),
                            "duration_ms": item.get("duration_ms", 0),
                            "is_playing": True,
                        })
                    else:
                        if self._last_spotify_track_id is not None:
                            self._last_spotify_track_id = None
                            logger.info("Spotify playback stopped/paused.")

                        await overlay_broadcast({
                            "command": "nowplaying",
                            "is_playing": False,
                        })

                except asyncio.CancelledError:
                    raise
                except Exception:
                    logger.exception("Error polling Spotify playback")

                await asyncio.sleep(5)

        except asyncio.CancelledError:
            logger.info("Spotify now-playing monitor cancelled.")
        finally:
            await overlay_broadcast({"command": "nowplaying", "is_playing": False})
            self._last_spotify_track_id = None
            logger.info("Spotify now-playing monitor stopped.")

    # ---------------- RECAP ----------------
    async def _send_recap(self):
        """POST recap data to the Discord bot."""
        if not RECAP_SECRET or not DISCORD_BOT_URL:
            logger.info("[RECAP] RECAP_SECRET or DISCORD_BOT_URL not set, skipping")
            return

        stream_end = int(time.time())
        logger.info("[RECAP] Stream problems: %s", self.stream_problems)
        payload = {
            "stream_start": self.stream_start_ts or stream_end,
            "stream_problems": self.stream_problems,
            "stream_end": stream_end,
            "chatter_submissions": self.chatter_submissions,
        }

        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    f"{DISCORD_BOT_URL}/recap",
                    json=payload,
                    headers={"Authorization": f"Bearer {RECAP_SECRET}"},
                    timeout=aiohttp.ClientTimeout(total=10),
                ) as resp:
                    logger.info(
                        "[RECAP] POST /recap -> %s (%d chatter submissions)",
                        resp.status, len(self.chatter_submissions),
                    )
        except Exception:
            logger.exception("[RECAP] Failed to POST recap to Discord bot")

    # ---------------- BOT READY EVENT ----------------
    async def event_ready(self):
        logger.info("Bot ready | %s", self.nick)
        asyncio.create_task(self.monitor_live_status())

    # ---------------- MESSAGE / COMMAND EVENTS ----------------
    async def event_message(self, message):
        if message.content.startswith('!'):
            logger.info("[COMMAND] %s ran: %s", message.author.name, message.content)

        if message.echo:
            return

        # Scan for LeetCode submission URLs from chatters
        if self.is_live and message.author.name.lower() != "hairyrug_":
            for match in _LEETCODE_SUBMISSION_RE.finditer(message.content):
                slug = match.group(1)
                url = match.group(0).rstrip("/") + "/"
                key = (message.author.name.lower(), url)
                if key not in self._seen_submissions:
                    self._seen_submissions.add(key)
                    self.chatter_submissions.append({
                        "twitch_user": message.author.name,
                        "url": url,
                        "slug": slug,
                    })
                    logger.info(
                        "[RECAP] Captured submission from %s: %s",
                        message.author.name, url,
                    )

        await self.handle_commands(message)

    async def event_command_error(self, ctx, error):
        cmd_name = ctx.command.name if getattr(ctx, "command", None) else "unknown"
        author_name = ctx.author.name if getattr(ctx, "author", None) else "unknown"
        logger.exception(
            "Error in command '%s' triggered by %s: %s",
            cmd_name,
            author_name,
            error,
        )

    # ---------------- AD LOOP ----------------
    async def _run_ad_loop(self, run_first_immediately: bool):
        logger.info(
            "Ad loop started (run_first_immediately=%s).",
            run_first_immediately,
        )

        while not self.connected_channels:
            await asyncio.sleep(1)

        channel = self.connected_channels[0]

        try:
            if run_first_immediately and self.is_live:
                await channel.send("\U0001f4e2 Ad in 1 minute!")
                logger.info("First ad alert sent immediately on stream start.")

                await asyncio.sleep(60)

                if not self.is_live:
                    logger.info("Stream ended before first ad started.")
                    return

                ok = await start_commercial(180)
                if not ok:
                    return

                await channel.send("\U0001f4fa Ad starting (3 minutes).")

                await asyncio.sleep(180)

                if not self.is_live:
                    logger.info("Stream ended during first ad break.")
                    return

                await channel.send("\u2705 Ad break over!")
                logger.info("FIRST ad break completed.")
            else:
                logger.info(
                    "Skipping immediate first ad (stream was already live at bot startup "
                    "or run_first_immediately=False)."
                )

            while self.is_live:
                await asyncio.sleep(59 * 60)

                if not self.is_live:
                    break

                await channel.send("\U0001f4e2 Ad in 1 minute!")
                logger.info("Recurring ad alert sent.")

                await asyncio.sleep(60)

                if not self.is_live:
                    break

                ok = await start_commercial(180)
                if not ok:
                    break

                await channel.send("\U0001f4fa Ad starting (3 minutes).")

                await asyncio.sleep(180)

                if not self.is_live:
                    break

                await channel.send("\u2705 Ad break over!")
                logger.info("Recurring ad break completed.")

        except asyncio.CancelledError:
            logger.info("Ad loop cancelled (stream offline).")
        except Exception:
            logger.exception("Fatal error in ad loop")

        logger.info("Ad loop stopped (stream offline).")

    # ---------------- LT TIMER COMMANDS ----------------
    @commands.command(name='lt')
    async def leetcode_timer(self, ctx, url: str = None, minutes: int = 30):
        logger.info("!lt triggered by %s (url=%r, minutes=%r)", ctx.author.name, url, minutes)
        try:
            if not (ctx.author.is_mod or ctx.author.is_broadcaster or ctx.author.is_vip):
                logger.info("!lt ignored for %s \u2014 insufficient permissions", ctx.author.name)
                return

            if url and url.lower() == "clear":
                if self.lt_task and not self.lt_task.done():
                    self.lt_task.cancel()
                    logger.info("!lt clear \u2014 cancelled existing LT timer task.")
                self.current_problem = None
                return

            if not url or minutes <= 0 or minutes > 180:
                logger.info("!lt invalid args for %s \u2014 url=%r, minutes=%r", ctx.author.name, url, minutes)
                return

            self.current_problem = url
            problem_name = extract_problem_name(url)

            # Track problem slug for recap
            slug_match = re.search(r'leetcode\.com/problems/([^/]+)', url)
            if slug_match:
                slug = slug_match.group(1)
                if slug not in self.stream_problems:
                    self.stream_problems.append(slug)
                    logger.info("[RECAP] Tracking stream problem: %s", slug)

            await ctx.send(f"\u23f0 {minutes}-minute timer started for '{problem_name}'")

            self.lt_task = asyncio.create_task(self._run_lt_timer(ctx, problem_name, minutes))
            logger.info("LT timer started for '%s' (%d minutes)", problem_name, minutes)

        except Exception:
            logger.exception("Error in !lt command")

    async def _run_lt_timer(self, ctx, problem_name, minutes):
        try:
            halfway = (minutes * 60) // 2
            final = minutes * 60 - halfway

            await asyncio.sleep(halfway)
            await ctx.send(f"\u23f0 Halfway done with '{problem_name}'")

            await asyncio.sleep(final)
            await ctx.send(f"\u23f0 Time's up for '{problem_name}'")
            logger.info("LT timer completed for '%s'", problem_name)

        except asyncio.CancelledError:
            logger.info("LT timer cancelled for '%s'", problem_name)
        except Exception:
            logger.exception("Error in LT timer loop")

    # ---------------- LOCK-IN + OVERLAY TIMER ----------------
    @commands.command(name='ltlockin')
    async def leetcode_lockin(self, ctx, *args):
        logger.info("!ltlockin triggered by %s (args=%r)", ctx.author.name, args)
        try:
            if not (ctx.author.is_mod or ctx.author.is_broadcaster or ctx.author.is_vip):
                logger.info("!ltlockin ignored for %s \u2014 insufficient permissions", ctx.author.name)
                return

            if len(args) == 1 and args[0].lower() == "clear":
                if self.ltlock_task and not self.ltlock_task.done():
                    self.ltlock_task.cancel()
                await overlay_broadcast({"command": "stop"})
                logger.info("LOCK-IN cancelled via !ltlockin clear.")
                return

            if len(args) < 2:
                logger.info("!ltlockin ignored \u2014 need at least 2 args (target, minutes)")
                return

            try:
                minutes = int(args[-1])
                if minutes <= 0 or minutes > 180:
                    logger.info("!ltlockin invalid minutes=%d", minutes)
                    return
            except ValueError:
                logger.info("!ltlockin failed \u2014 last argument not an integer minutes")
                return

            target = " ".join(args[:-1]).strip()
            self.current_problem = target

            display_name = await make_lockin_label(target)

            await ctx.send(f"\U0001f512 LOCKED IN \u2014 {minutes} minutes for: {display_name}")
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
            await ctx.send(f"\u23f0 Time's up for '{target_label}' \u2014 LOCK-IN over!")
            logger.info("LOCK-IN timer completed for '%s'", target_label)
        except asyncio.CancelledError:
            logger.info("LOCK-IN timer cancelled for '%s'", target_label)
        except Exception:
            logger.exception("Error in LTLOCK timer loop")

    # ---------------- OTHER COMMANDS ----------------
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

                    await ctx.send(f"\U0001f4c5 Daily: {title} ({diff}) | {link}")
                    logger.info("!daily responded with %s (%s)", title, diff)

        except Exception:
            logger.exception("Error in !daily command")

    @commands.command(name='problem')
    async def get_problem(self, ctx, problem_id: str = None):
        logger.info("!problem triggered by %s (problem_id=%r)", ctx.author.name, problem_id)

        try:
            if problem_id is not None:
                if not problem_id.isdigit():
                    await ctx.send("\u274c Usage: !problem <number>")
                    logger.info("!problem invalid explicit id %r", problem_id)
                    return

                async with aiohttp.ClientSession() as session:
                    async with session.get(f'https://leetcode-api-pied.vercel.app/problem/{problem_id}') as resp:
                        if resp.status != 200:
                            logger.error("!problem fetch failed: HTTP %s", resp.status)
                            await ctx.send("\u274c Failed to fetch that problem.")
                            return

                        data = await resp.json()
                        await ctx.send(
                            f"\U0001f9e9 #{problem_id}: {data['title']} ({data['difficulty']}) | {data['url']}"
                        )
                        logger.info(
                            "!problem responded with #%s: %s (%s)",
                            problem_id, data['title'], data['difficulty']
                        )
                return

            if not self.current_problem:
                await ctx.send("\u274c No problem is currently being worked on.")
                logger.info("!problem \u2014 no current_problem set")
                return

            target = self.current_problem.strip()

            if target.startswith("http://") or target.startswith("https://"):
                parsed = urlparse(target)

                if "leetcode.com" in parsed.netloc:
                    import re
                    match = re.search(r'/problems/([^/]+)/?', parsed.path)
                    if match:
                        slug = match.group(1)

                        async with aiohttp.ClientSession() as session:
                            async with session.get(f'https://leetcode-api-pied.vercel.app/slug/{slug}') as resp:
                                if resp.status != 200:
                                    await ctx.send(f"\U0001f50d Working on: {slug.replace('-', ' ').title()} | {target}")
                                    logger.info("!problem slug fetch failed")
                                    return

                                data = await resp.json()
                                await ctx.send(
                                    f"\U0001f9e9 {data['title']} ({data['difficulty']}) | https://leetcode.com/problems/{slug}/"
                                )
                                logger.info("!problem returned current problem from slug %s", slug)
                                return

                await ctx.send(f"\U0001f50d Working on: {target}")
                logger.info("!problem returned non-LeetCode URL %s", target)
                return

            await ctx.send(f"\U0001f50d Working on: {target} (no link available)")
            logger.info("!problem returned generic text target %s", target)

        except Exception:
            logger.exception("Error in !problem command")
            await ctx.send("\u274c Error while retrieving problem info.")

    @commands.command(name='discord')
    async def get_discord(self, ctx):
        logger.info("!discord triggered by %s", ctx.author.name)
        await ctx.send('https://discord.gg/tHjeDK8Cd7')

    @commands.command(name="commands")
    async def list_commands(self, ctx):
        try:
            visible_commands = []

            for name, command in self.commands.items():
                if name != command.name:
                    continue

                if command.name in {"lt", "ltlockin", "commands"}:
                    continue

                visible_commands.append(f"!{command.name}")

            visible_commands.sort()

            if not visible_commands:
                await ctx.send("No commands available.")
                return

            msg = "\U0001f4dc " + " ".join(visible_commands)
            await ctx.send(msg)

        except Exception:
            logger.exception("Error in !commands command")
