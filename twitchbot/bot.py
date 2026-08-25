import asyncio
import re
import time
from datetime import datetime, timezone
from urllib.parse import urlparse

import aiohttp
import requests
import spotipy
from spotipy.exceptions import SpotifyOauthError
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
    fetch_stream_metadata,
    is_stream_live,
    start_commercial,
    get_ad_schedule,
    send_shoutout,
    send_chat_message,
    get_follow_info,
    get_stream_started_at,
    get_user_id,
)
from .helpers import leetcode_slug, resolve_problem_name
from .notify import alert_owner

# Ad cadence. The period is measured warning-to-warning, so a break lands at
# the same point in every hour of a stream.
AD_PERIOD_SECONDS = 60 * 60
AD_WARNING_SECONDS = 60
AD_LENGTH_SECONDS = 180
# A break that doesn't run costs the whole hour: the pre-roll bank is topped up
# with about five seconds to spare, so it lapses minutes later. Retries stay
# inside the hour and never touch chat — viewers don't need the bot's plumbing,
# and a warning is only ever sent when an ad is genuinely about to run.
AD_RETRY_DELAY_SECONDS = 5 * 60
AD_MAX_ATTEMPTS = 3


async def _log_ad_state(label: str) -> dict | None:
    """Record Twitch's ad state, and shout if automation is scheduling ads.

    With Ad Manager off the bot is the only thing that can start a break, which
    is what keeps the chat warning honest. A non-zero next_ad_at means that is
    no longer true — an ad is coming that nobody announced.
    """
    state = await get_ad_schedule()
    if not state:
        return None
    logger.info(
        "Ad state (%s): preroll_free_time=%ss next_ad_at=%s snoozes=%s",
        label, state.get("preroll_free_time"), state.get("next_ad_at"),
        state.get("snooze_count"),
    )
    if state.get("next_ad_at"):
        logger.warning(
            "Twitch has an ad scheduled at %s that the bot did not trigger — "
            "Ad Manager looks re-enabled, so that break won't be announced.",
            state.get("next_ad_at"),
        )
    return state


def _ad_length_label(seconds: int) -> str:
    """'3 minutes' / '90 seconds' — Twitch can serve a length we didn't ask for."""
    if seconds % 60 == 0:
        minutes = seconds // 60
        return f"{minutes} minute" + ("" if minutes == 1 else "s")
    return f"{seconds} seconds"


_LEETCODE_SUBMISSION_RE = re.compile(
    r"https?://(?:www\.)?leetcode\.com/problems/([^/]+)/submissions/(\d+)"
)

_URL_RE = re.compile(r"https?://\S+", re.IGNORECASE)
_RECAP_SKIP_HOSTS = ("github.com", "leetcode.com", "discord.com", "discord.gg", "discordapp.com")

# Twitch usernames the bot keys behavior off of. Update these on a rename.
BOT_NICK = "hairyrugaf"      # the bot's own account
BROADCASTER = "howlingaf"    # the channel owner who streams


class Bot(commands.Bot):
    def __init__(self, token: str | None = None):
        super().__init__(
            token=token or BOT_OAUTH_TOKEN,
            client_id=CLIENT_ID,
            nick=BOT_NICK,
            prefix='!',
            initial_channels=[BROADCASTER],
        )

        self.current_problem = None
        self.current_problem_name = None
        self.spotify = None
        self.is_live = False
        self.ad_task = None
        self.spotify_task = None
        self.lt_task = None
        self._last_spotify_track_id = None
        self._monitor_started = False
        self.bot_user_id: str | None = None

        # Recap tracking
        self.stream_start_ts: int | None = None
        self.chatter_submissions: list[dict] = []
        self._seen_submissions: set[tuple[str, str]] = set()
        self.stream_problems: list[str] = []  # LeetCode slugs from !st commands
        self.streamer_links: list[str] = []  # broadcaster-pasted non-skip URLs
        self._seen_streamer_links: set[str] = set()
        self.stream_title: str = ""  # heads the Discord recap embed

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
    # Consecutive confirmed-offline polls required before declaring the stream
    # over. At a 20s poll interval this rides out ~1 minute of API trouble
    # before tearing down (recap, ad loop) the live session.
    OFFLINE_CONFIRMATIONS = 3

    async def monitor_live_status(self):
        logger.info("Starting live status monitor loop...")
        first_check = True
        offline_strikes = 0

        while True:
            try:
                live = await is_stream_live()

                if live is None:
                    # Unknown (API error/timeout) — not proof of anything.
                    # Leave state and the strike count untouched.
                    logger.warning(
                        "Stream status unknown this poll; leaving state unchanged "
                        "(is_live=%s)", self.is_live
                    )
                    first_check = False
                    await asyncio.sleep(20)
                    continue

                if live and self.is_live:
                    # Still live; clear any partial offline streak.
                    offline_strikes = 0

                if live and not self.is_live:
                    offline_strikes = 0
                    # Reset recap tracking
                    self.stream_start_ts = int(time.time())
                    self.chatter_submissions = []
                    self._seen_submissions = set()
                    self.stream_problems = []
                    self.streamer_links = []
                    self._seen_streamer_links = set()

                    if first_check:
                        logger.info(
                            "Stream was already LIVE when bot started; "
                            "marking live without immediate ad."
                        )
                        self.is_live = True
                        await self._capture_stream_title()
                        self.ad_task = asyncio.create_task(
                            self._run_ad_loop(run_first_immediately=False)
                        )
                    else:
                        logger.info("Stream just went LIVE!")
                        self.is_live = True
                        await self._capture_stream_title()
                        self.ad_task = asyncio.create_task(
                            self._run_ad_loop(run_first_immediately=True)
                        )

                    self.spotify_task = asyncio.create_task(
                        self.monitor_spotify()
                    )

                elif not live and self.is_live:
                    offline_strikes += 1
                    if offline_strikes < self.OFFLINE_CONFIRMATIONS:
                        logger.info(
                            "Stream appears OFFLINE (%d/%d confirmations) — "
                            "waiting before tearing down.",
                            offline_strikes, self.OFFLINE_CONFIRMATIONS,
                        )
                        first_check = False
                        await asyncio.sleep(20)
                        continue

                    logger.info(
                        "Stream confirmed OFFLINE after %d checks",
                        offline_strikes,
                    )
                    offline_strikes = 0
                    self.is_live = False

                    # Send recap to Discord bot
                    await self._send_recap()

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
    # Poll cadence while healthy. After a failure the delay doubles from
    # SPOTIFY_POLL_INTERVAL up to SPOTIFY_MAX_BACKOFF, so an outage costs a
    # handful of log lines instead of a traceback every 5s.
    SPOTIFY_POLL_INTERVAL = 5
    SPOTIFY_MAX_BACKOFF = 300

    def _spotify_backoff(self, failures: int) -> int:
        """Delay before retry number `failures` (1-based): 5, 10, 20 ... 300."""
        return min(
            self.SPOTIFY_POLL_INTERVAL * 2 ** (failures - 1),
            self.SPOTIFY_MAX_BACKOFF,
        )

    async def monitor_spotify(self):
        logger.info("Spotify now-playing monitor started.")
        failures = 0
        try:
            while self.is_live:
                try:
                    if not self.spotify:
                        await asyncio.sleep(self.SPOTIFY_POLL_INTERVAL)
                        continue

                    data = await asyncio.to_thread(self.spotify.current_playback)

                    if data and data.get("is_playing") and data.get("item"):
                        item = data["item"]
                        track_id = item.get("id")
                        images = item.get("album", {}).get("images", [])
                        album_art = images[0]["url"] if images else ""

                        self._last_spotify_track_id = track_id

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

                    if failures:
                        logger.info(
                            "Spotify polling recovered after %d failed attempt(s).",
                            failures,
                        )
                        failures = 0

                except asyncio.CancelledError:
                    raise
                except SpotifyOauthError as e:
                    # invalid_grant means the refresh token is revoked or
                    # expired. Retrying can't fix that — it needs a fresh
                    # authorization-code flow — so stop instead of spinning.
                    # Any other OAuth error (5xx from the token endpoint, a
                    # non-JSON body) may well be transient, so let it back off.
                    if e.error == "invalid_grant":
                        logger.error(
                            "Spotify authorization is dead (%s). Re-run the "
                            "authorization-code flow to refresh .spotify_cache. "
                            "Stopping now-playing monitor.",
                            e.error_description or e.error,
                        )
                        break
                    failures += 1
                    delay = self._spotify_backoff(failures)
                    logger.warning(
                        "Spotify OAuth error (%s), attempt %d — retrying in %ds",
                        e.error_description or e.error, failures, delay,
                    )
                    await asyncio.sleep(delay)
                    continue
                except requests.exceptions.RequestException as e:
                    # Connection resets, timeouts, DNS hiccups. These are
                    # routine on a long-lived poll and self-heal on retry, so
                    # a one-liner is plenty — the traceback is a hundred lines
                    # of urllib3 internals that says nothing we don't know.
                    failures += 1
                    delay = self._spotify_backoff(failures)
                    # An isolated blip is INFO; only a run of them is worth
                    # surfacing in the Discord feed.
                    log = logger.info if failures == 1 else logger.warning
                    log(
                        "Spotify poll network error (%s: %s), attempt %d — "
                        "retrying in %ds",
                        type(e).__name__, e, failures, delay,
                    )
                    await asyncio.sleep(delay)
                    continue
                except Exception:
                    failures += 1
                    delay = self._spotify_backoff(failures)
                    if failures == 1:
                        logger.exception(
                            "Error polling Spotify playback — retrying in %ds", delay
                        )
                    else:
                        logger.warning(
                            "Spotify poll still failing (attempt %d) — "
                            "retrying in %ds",
                            failures, delay,
                        )
                    await asyncio.sleep(delay)
                    continue

                await asyncio.sleep(self.SPOTIFY_POLL_INTERVAL)

        except asyncio.CancelledError:
            logger.info("Spotify now-playing monitor cancelled.")
        finally:
            await overlay_broadcast({"command": "nowplaying", "is_playing": False})
            self._last_spotify_track_id = None
            logger.info("Spotify now-playing monitor stopped.")

    # ---------------- RECAP ----------------
    async def _capture_stream_title(self):
        """Remember the stream's title while it's still live.

        The recap is built after the stream ends, and Helix returns nothing for
        an offline channel — so reading the title at that point is too late. A
        title changed mid-stream isn't picked up; this is the one at go-live.
        """
        meta = await fetch_stream_metadata()
        self.stream_title = (meta or {}).get("title") or ""

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
            "streamer_links": self.streamer_links,
            "stream_title": self.stream_title,
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
        # event_ready re-fires on every IRC reconnect. Spawn the monitor loop
        # only once, otherwise each reconnect leaks another loop and they race
        # over live state (double ad alerts, double recaps).
        if self._monitor_started:
            return
        self._monitor_started = True
        self.bot_user_id = await get_user_id(BOT_NICK)
        if not self.bot_user_id:
            logger.warning(
                "Could not resolve the bot's user ID; chat sends will use "
                "plain IRC (no bot badge) until it resolves."
            )
        asyncio.create_task(self.monitor_live_status())

    # ---------------- OUTGOING CHAT ----------------
    async def say(self, content: str) -> bool:
        """Send a chat message, preferring Helix (app token) so Twitch shows
        the native chat-bot badge; falls back to plain IRC if that fails."""
        if self.bot_user_id is None:
            self.bot_user_id = await get_user_id(BOT_NICK)
        if self.bot_user_id:
            if await send_chat_message(content, self.bot_user_id):
                return True
            logger.warning("Helix chat send failed; falling back to IRC.")
        if self.connected_channels:
            await self.connected_channels[0].send(content)
            return True
        logger.warning("No path to send chat message: %r", content)
        return False

    # ---------------- RAID AUTO-SHOUTOUT ----------------
    async def event_raw_usernotice(self, channel, tags: dict):
        if tags.get("msg-id") != "raid":
            return

        raider_login = tags.get("login") or tags.get("msg-param-login", "")
        viewer_count = tags.get("msg-param-viewerCount", "?")
        logger.info("Raid from %s with %s viewers", raider_login, viewer_count)

        if not raider_login:
            return

        raider_id = await get_user_id(raider_login)
        if not raider_id:
            logger.warning("Could not resolve user ID for raider %s", raider_login)
            return

        ok = await send_shoutout(raider_id)
        if ok:
            logger.info("Auto-shoutout sent for raider %s", raider_login)

    # ---------------- MESSAGE / COMMAND EVENTS ----------------
    async def event_message(self, message):
        if message.content.startswith('!'):
            logger.info("[COMMAND] %s ran: %s", message.author.name, message.content)

        if message.echo:
            return

        # Helix-sent messages arrive back over IRC as regular messages (the
        # echo flag only covers IRC sends), so skip the bot's own account too.
        if message.author and message.author.name.lower() == BOT_NICK:
            return

        # Scan for LeetCode submission URLs from chatters
        if self.is_live:
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

        # Capture broadcaster-pasted non-skip URLs for the recap
        if self.is_live and message.author.name.lower() == BROADCASTER:
            for raw in _URL_RE.findall(message.content):
                url = raw.rstrip(".,!?);]>'\"")
                host = (urlparse(url).hostname or "").lower()
                if not host:
                    continue
                if any(host == h or host.endswith("." + h) for h in _RECAP_SKIP_HOSTS):
                    continue
                if url in self._seen_streamer_links:
                    continue
                self._seen_streamer_links.add(url)
                self.streamer_links.append(url)
                logger.info("[RECAP] Captured streamer link: %s", url)

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
    async def _safe_send(self, content: str):
        # Ad alerts are best-effort: a send during an IRC reconnect (closing
        # transport) must not take down the ad loop for the rest of the stream.
        try:
            await self.say(content)
        except Exception:
            logger.warning("Failed to send chat message: %r", content, exc_info=True)

    async def _alert_ads_down(self) -> None:
        """Ping the owner in the console channel, once pre-rolls are actually back.

        Checked after the retries rather than at the moment of failure: the bank
        still reads a couple of minutes at that point, so an immediate check
        would say everything is fine. A state we can't read at all counts as
        bad news — the ad definitely didn't run.
        """
        state = await get_ad_schedule()
        banked = (state or {}).get("preroll_free_time")
        if state is not None and banked:
            logger.info(
                "Ad break missed but %ss of pre-roll cover remains; not alerting.",
                banked,
            )
            return
        await alert_owner(
            "⚠️ Twitch ad break failed after "
            f"{AD_MAX_ATTEMPTS} attempts and pre-rolls are back on — viewers "
            "joining now are getting a pre-roll. Next automatic attempt is at "
            "the top of the next hour."
        )

    async def _run_ad_loop(self, run_first_immediately: bool):
        logger.info(
            "Ad loop started (run_first_immediately=%s).",
            run_first_immediately,
        )

        while not self.connected_channels:
            await asyncio.sleep(1)

        if not run_first_immediately:
            logger.info(
                "Skipping immediate first ad (stream was already live at bot startup "
                "or run_first_immediately=False)."
            )
        first_cycle = run_first_immediately
        # When the NEXT cycle's warning is due. Anchored to the start of each
        # cycle rather than the end of its ad, so the 60s warning and the ad
        # itself don't push every following break later — that drift made the
        # "hourly" schedule run every 63 minutes.
        #
        # A full period out when the first ad is meant to be skipped: the loop
        # only skips the WAIT on its first cycle, so anchoring at "now" would
        # have run an ad immediately on every restart into a live stream —
        # the one case run_first_immediately=False exists to prevent.
        next_warning_at = time.monotonic() + (
            0 if run_first_immediately else AD_PERIOD_SECONDS)

        try:
            while self.is_live:
                # One ad cycle: alert -> commercial -> wrap-up. An unexpected
                # error is contained to this cycle so ads rejoin the hourly
                # schedule instead of dying for the rest of the stream.
                try:
                    if not first_cycle:
                        await asyncio.sleep(max(0, next_warning_at - time.monotonic()))
                        if not self.is_live:
                            break

                    next_warning_at = time.monotonic() + AD_PERIOD_SECONDS
                    served, before = 0, None

                    for attempt in range(1, AD_MAX_ATTEMPTS + 1):
                        if not self.is_live:
                            break

                        # Pre-flight. A read of the ad schedule proves the token
                        # is valid and Helix is reachable — the two things that
                        # would otherwise let a warning go out with no ad behind
                        # it. It can't prove the commercial call will succeed,
                        # but it catches the failures that actually happen.
                        before = await _log_ad_state(f"pre-break attempt {attempt}")
                        if before is None:
                            logger.warning(
                                "Ad pre-flight failed (attempt %d/%d); no warning sent.",
                                attempt, AD_MAX_ATTEMPTS,
                            )
                            await asyncio.sleep(AD_RETRY_DELAY_SECONDS)
                            continue

                        await self._safe_send("Ad in 1 minute!")
                        logger.info(
                            "%s ad alert sent (attempt %d).",
                            "First" if first_cycle else "Recurring", attempt,
                        )

                        await asyncio.sleep(AD_WARNING_SECONDS)
                        if not self.is_live:
                            break

                        served = await start_commercial(AD_LENGTH_SECONDS)
                        if served:
                            break

                        logger.warning(
                            "Ad failed to start (attempt %d/%d).",
                            attempt, AD_MAX_ATTEMPTS,
                        )
                        await asyncio.sleep(AD_RETRY_DELAY_SECONDS)

                    if not self.is_live:
                        break

                    if not served:
                        await self._alert_ads_down()
                        continue

                    await self._safe_send(f"Ad starting ({_ad_length_label(served)}).")

                    await asyncio.sleep(served)

                    if not self.is_live:
                        break

                    await self._safe_send("Ad break over!")
                    after = await _log_ad_state("post-break")
                    # What a break actually buys, measured rather than assumed —
                    # this is the number that decides how far the period can be
                    # stretched before pre-rolls come back.
                    if before and after:
                        gained = (after.get("preroll_free_time") or 0) \
                            - (before.get("preroll_free_time") or 0)
                        logger.info(
                            "Ad break completed (%ss): preroll_free_time %s -> %s (%+ds).",
                            served, before.get("preroll_free_time"),
                            after.get("preroll_free_time"), gained,
                        )
                    else:
                        logger.info("Ad break completed (%ss).", served)
                except Exception:
                    logger.exception(
                        "Ad cycle crashed; rejoining the hourly ad schedule."
                    )
                finally:
                    first_cycle = False

        except asyncio.CancelledError:
            logger.info("Ad loop cancelled (stream offline).")

        logger.info("Ad loop stopped (stream offline).")

    # ---------------- PROBLEM TIMER COMMANDS ----------------
    # At or above this many minutes the timer also announces "10 minutes left".
    TEN_MIN_REMINDER_THRESHOLD = 25

    def clear_problem(self) -> bool:
        """Cancel any running timer and forget the current problem.

        Returns True if there was a problem or live timer to clear. Shared by
        !st clear and the Discord console so the state can't half-clear.
        """
        active = bool(self.current_problem) or (self.lt_task and not self.lt_task.done())
        if self.lt_task and not self.lt_task.done():
            self.lt_task.cancel()
            logger.info("Cancelled running problem timer.")
        self.current_problem = None
        self.current_problem_name = None
        return bool(active)

    @commands.command(name='st')
    async def set_timer(self, ctx, url: str = None, minutes: int = 25):
        logger.info("!st triggered by %s (url=%r, minutes=%r)", ctx.author.name, url, minutes)
        try:
            if not (ctx.author.is_mod or ctx.author.is_broadcaster or ctx.author.is_vip):
                logger.info("!st ignored for %s \u2014 insufficient permissions", ctx.author.name)
                return

            if url and url.lower() == "clear":
                self.clear_problem()
                await self.say("Problem cleared.")
                return

            if not url or minutes <= 0 or minutes > 180:
                logger.info("!st invalid args for %s \u2014 url=%r, minutes=%r", ctx.author.name, url, minutes)
                return

            # Unrecognized links still get a timer, just with a generic label
            # (and no stored name, so !problem falls back to showing the URL).
            problem_name = await resolve_problem_name(url)
            self.current_problem = url
            self.current_problem_name = problem_name
            if problem_name is None:
                problem_name = "Problem"
                logger.info("!st unrecognized problem url %r — using generic label", url)

            # Track LeetCode slugs for the recap. The recap pipeline is
            # LeetCode-specific, so other sites are timed but not recapped.
            slug = leetcode_slug(url)
            if slug and slug not in self.stream_problems:
                self.stream_problems.append(slug)
                logger.info("[RECAP] Tracking stream problem: %s", slug)

            # Cancel any timer already running so a new !st replaces it instead
            # of leaving an orphaned task that keeps firing for the old problem.
            if self.lt_task and not self.lt_task.done():
                self.lt_task.cancel()
                logger.info("!st \u2014 cancelled previous timer before starting a new one.")

            await self.say(f"{minutes}-minute timer started for '{problem_name}'")

            self.lt_task = asyncio.create_task(self._run_st_timer(problem_name, minutes))
            logger.info("ST timer started for '%s' (%d minutes)", problem_name, minutes)

        except Exception:
            logger.exception("Error in !st command")

    async def _run_st_timer(self, problem_name, minutes):
        try:
            total = minutes * 60
            milestones = [(total // 2, f"Halfway done with '{problem_name}'")]
            if minutes >= self.TEN_MIN_REMINDER_THRESHOLD:
                milestones.append((total - 10 * 60, f"10 minutes left for '{problem_name}'"))
            milestones.append((total, f"Time's up for '{problem_name}'"))
            milestones.sort()

            elapsed = 0
            for at, message in milestones:
                await asyncio.sleep(at - elapsed)
                await self.say(message)
                elapsed = at
            logger.info("ST timer completed for '%s'", problem_name)

        except asyncio.CancelledError:
            logger.info("ST timer cancelled for '%s'", problem_name)
        except Exception:
            logger.exception("Error in ST timer loop")

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

                    await self.say(f"Daily: {title} ({diff}) | {link}")
                    logger.info("!daily responded with %s (%s)", title, diff)

        except Exception:
            logger.exception("Error in !daily command")

    @commands.command(name='problem')
    async def get_problem(self, ctx, problem_id: str = None):
        logger.info("!problem triggered by %s (problem_id=%r)", ctx.author.name, problem_id)

        try:
            if problem_id is not None:
                if not problem_id.isdigit():
                    await self.say("Usage: !problem <number>")
                    logger.info("!problem invalid explicit id %r", problem_id)
                    return

                async with aiohttp.ClientSession() as session:
                    async with session.get(f'https://leetcode-api-pied.vercel.app/problem/{problem_id}') as resp:
                        if resp.status != 200:
                            logger.error("!problem fetch failed: HTTP %s", resp.status)
                            await self.say("Failed to fetch that problem.")
                            return

                        data = await resp.json()
                        await self.say(
                            f"#{problem_id}: {data['title']} ({data['difficulty']}) | {data['url']}"
                        )
                        logger.info(
                            "!problem responded with #%s: %s (%s)",
                            problem_id, data['title'], data['difficulty']
                        )
                return

            if not self.current_problem:
                await self.say("No problem is currently being worked on.")
                logger.info("!problem \u2014 no current_problem set")
                return

            target = self.current_problem.strip()

            if target.startswith("http://") or target.startswith("https://"):
                # !st stores the resolved label for recognized sites only.
                if self.current_problem_name:
                    await self.say(f"Working on: {self.current_problem_name} | {target}")
                else:
                    await self.say(f"Working on: {target}")
                logger.info("!problem returned current problem %s", target)
                return

            await self.say(f"Working on: {target} (no link available)")
            logger.info("!problem returned generic text target %s", target)

        except Exception:
            logger.exception("Error in !problem command")
            await self.say("Error while retrieving problem info.")

    @commands.command(name='test')
    async def test_connection(self, ctx):
        logger.info("!test triggered by %s", ctx.author.name)
        if not (ctx.author.is_mod or ctx.author.is_broadcaster):
            return

        if not RECAP_SECRET or not DISCORD_BOT_URL:
            await self.say("RECAP_SECRET or DISCORD_BOT_URL not configured.")
            return

        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    f"{DISCORD_BOT_URL}/recap/verify",
                    headers={"Authorization": f"Bearer {RECAP_SECRET}"},
                    timeout=aiohttp.ClientTimeout(total=5),
                ) as resp:
                    if resp.status == 200:
                        await self.say("Discord bot connection verified!")
                    elif resp.status == 401:
                        await self.say("RECAP_SECRET mismatch — auth rejected by Discord bot.")
                    else:
                        await self.say(f"Discord bot returned HTTP {resp.status}")
        except aiohttp.ClientConnectorError:
            await self.say(f"Cannot reach Discord bot at {DISCORD_BOT_URL}")
        except asyncio.TimeoutError:
            await self.say(f"Discord bot timed out at {DISCORD_BOT_URL}")
        except Exception as e:
            logger.exception("[TEST] Unexpected error")
            await self.say(f"Error: {e}")

    @commands.command(name='song')
    async def now_playing(self, ctx):
        logger.info("!song triggered by %s", ctx.author.name)
        try:
            if not self.spotify:
                await self.say("Spotify is not connected.")
                return

            data = await asyncio.to_thread(self.spotify.current_playback)

            if not data or not data.get("is_playing") or not data.get("item"):
                await self.say("Nothing is playing right now.")
                return

            item = data["item"]
            name = item["name"]
            artists = ", ".join(a["name"] for a in item["artists"])
            url = item.get("external_urls", {}).get("spotify", "")
            msg = f"{name} — {artists}"
            if url:
                msg += f" | {url}"
            await self.say(msg)

        except Exception:
            logger.exception("Error in !song command")
            await self.say("Could not fetch current song.")

    @commands.command(name='discord')
    async def get_discord(self, ctx):
        logger.info("!discord triggered by %s", ctx.author.name)
        await self.say('https://discord.gg/tHjeDK8Cd7')

    @commands.command(name="commands")
    async def list_commands(self, ctx):
        try:
            visible_commands = []

            for name, command in self.commands.items():
                if name != command.name:
                    continue

                if command.name in {"st", "commands", "test"}:
                    continue

                visible_commands.append(f"!{command.name}")

            visible_commands.sort()

            if not visible_commands:
                await self.say("No commands available.")
                return

            msg = " ".join(visible_commands)
            await self.say(msg)

        except Exception:
            logger.exception("Error in !commands command")

    @commands.command(name='project')
    async def get_project(self, ctx):
        logger.info("!project triggered by %s", ctx.author.name)
        await self.say('https://github.com/howlingaf/howlingdb')

    @staticmethod
    def _duration_text(*pairs: tuple[int, str]) -> str:
        """(3, 'hour'), (24, 'minute') -> '3 hours, 24 minutes'; '' if all 0."""
        return ", ".join(
            f"{n} {unit}{'s' if n != 1 else ''}" for n, unit in pairs if n
        )

    @staticmethod
    def _since(iso_ts: str) -> tuple[datetime, int]:
        """Parse a Helix ISO timestamp; return it and whole seconds since."""
        ts = datetime.fromisoformat(iso_ts.replace("Z", "+00:00"))
        return ts, int((datetime.now(timezone.utc) - ts).total_seconds())

    @commands.command(name='pc')
    async def pc_specs(self, ctx):
        logger.info("!pc triggered by %s", ctx.author.name)
        await self.say(
            "PC Specs -> Ultra 7 255HX (20C) | RTX 5070 Ti 16GB | "
            "32GB DDR5-5600 | 1TB NVMe | Win11"
        )

    @commands.command(name='font')
    async def font(self, ctx):
        logger.info("!font triggered by %s", ctx.author.name)
        await self.say("Terminus (TTF)")

    @commands.command(name='followage')
    async def followage(self, ctx):
        logger.info("!followage triggered by %s", ctx.author.name)
        try:
            user_id = getattr(ctx.author, "id", None) or await get_user_id(ctx.author.name)
            if not user_id:
                return

            ok, followed_at = await get_follow_info(user_id)
            if not ok:
                await self.say("Could not check followage right now.")
                return
            if not followed_at:
                await self.say(f"{ctx.author.name} is not following the channel.")
                return

            followed, seconds = self._since(followed_at)
            years, rem = divmod(seconds // 86400, 365)
            months, days = divmod(rem, 30)
            duration = self._duration_text(
                (years, "year"), (months, "month"), (days, "day")
            ) or "less than a day"
            await self.say(
                f"{ctx.author.name} has been following for {duration} "
                f"(since {followed:%Y-%m-%d})."
            )

        except Exception:
            logger.exception("Error in !followage command")

    @commands.command(name='uptime')
    async def uptime(self, ctx):
        logger.info("!uptime triggered by %s", ctx.author.name)
        try:
            started_at = await get_stream_started_at()
            if not started_at:
                await self.say("Stream is offline.")
                return

            _, seconds = self._since(started_at)
            hours, rem = divmod(seconds, 3600)
            duration = self._duration_text(
                (hours, "hour"), (rem // 60, "minute")
            ) or "less than a minute"
            await self.say(f"Live for {duration}.")

        except Exception:
            logger.exception("Error in !uptime command")
