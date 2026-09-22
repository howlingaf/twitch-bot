"""Where our emotes get used in other people's chats.

Twitch reports emote use inside our own channel and nowhere else. A one-time
sweep of VOD replays answers the question backwards (scripts/emote_sweep.py);
this answers it going forward, by reading other channels' chat as it happens
and recording every message carrying a `howlin67` emote.

Which channels: a fixed list of the ones closest to us, plus -- the point of
this -- every channel currently live in our category, refreshed on a loop, so
a channel that goes live is joined within a few minutes and parted when it
ends. A hand-picked list only ever finds what we already expected: meisaka was
left off it because the VOD sweep found nothing there, and someone used two of
our emotes in that chat the same day.

It connects ANONYMOUSLY. Twitch lets anything read a public chat with no login
at all -- the connection uses a throwaway `justinfan` nick and no token -- so
it carries no account, appears in nobody's chatter list, and isn't a viewer.
The bot's own account is deliberately NOT used here: that would show up as our
bot lurking in someone else's channel. Nothing is ever sent to these channels;
the socket only ever answers Twitch's PING and issues JOIN/PART.

The same reading feeds categorystats.py, which keeps running totals about
every channel in the category -- schedule, size, how conversational its chat
is, what it's about, and where our own regulars already hang out -- for
finding people to network with. Totals only; no one's message text is kept.

Kept apart from bot.py's connection on purpose. If this one dies, chat
recording for our own stream is unaffected, and vice versa.
"""

import asyncio
import json
import random
import re
import time

import websockets

from .config import (
    EMOTE_PREFIX,
    EMOTE_WATCH_CHANNELS,
    EMOTE_WATCH_GAME_ID,
    EMOTE_WATCH_MAX,
    EMOTE_WATCH_REFRESH,
)
from .categorystats import CategoryStats
from .logger import logger
from .twitch_api import _twitch_request

_HOST = "wss://irc-ws.chat.twitch.tv:443"
# "PRIVMSG #channel :text", with the sender's login in the prefix.
_LINE = re.compile(r"^:(?P<login>[^!]+)![^ ]+ PRIVMSG #(?P<channel>[^ ]+) :(?P<text>.*)$")
_BACKOFF = (5, 15, 60, 300)
# Twitch allows ~20 joins per 10 seconds. One JOIN line can carry several
# channels, so send them in twenties and wait out the window between batches.
_JOIN_BATCH = 20
_JOIN_PAUSE = 11


def _emotes(text: str) -> list[str]:
    return re.findall(rf"\b{re.escape(EMOTE_PREFIX)}\w+", text, re.I)


async def _live_streams() -> list[dict]:
    """Everyone streaming our category right now, as Helix stream records
    (login, viewers, title, start). Empty on failure, so a Helix hiccup parts
    nobody and records nothing."""
    found, cursor = {}, ""
    for _ in range(30):
        status, body = await _twitch_request(
            "GET", f"https://api.twitch.tv/helix/streams?game_id={EMOTE_WATCH_GAME_ID}"
                   f"&first=100" + (f"&after={cursor}" if cursor else ""))
        if status != 200:
            logger.warning("Emote watch could not list the category (HTTP %s).", status)
            return []
        data = json.loads(body)
        for st in data.get("data", []):
            found[st["user_login"].lower()] = st
        cursor = data.get("pagination", {}).get("cursor", "")
        if not cursor or not data.get("data") or len(found) >= EMOTE_WATCH_MAX:
            break
    return list(found.values())


async def _send_joins(ws, channels: list[str]) -> None:
    for i in range(0, len(channels), _JOIN_BATCH):
        batch = channels[i:i + _JOIN_BATCH]
        await ws.send("JOIN " + ",".join(f"#{c}" for c in batch))
        if i + _JOIN_BATCH < len(channels):
            await asyncio.sleep(_JOIN_PAUSE)


async def _follow_category(ws, joined: set[str], stats: CategoryStats) -> None:
    """Keep the joined set in step with who's live. The fixed channels stay
    joined whether or not they're streaming. Each poll is also the schedule
    and size record for the whole category."""
    while True:
        await asyncio.sleep(EMOTE_WATCH_REFRESH)
        streams = await _live_streams()
        if not streams:
            continue
        stats.on_poll(streams)
        live = {st["user_login"].lower() for st in streams}
        wanted = (set(EMOTE_WATCH_CHANNELS) | live)
        # Keep the fixed ones, then fill up to the cap with live channels.
        if len(wanted) > EMOTE_WATCH_MAX:
            extra = list(wanted - set(EMOTE_WATCH_CHANNELS))[:EMOTE_WATCH_MAX - len(EMOTE_WATCH_CHANNELS)]
            wanted = set(EMOTE_WATCH_CHANNELS) | set(extra)
        add, drop = sorted(wanted - joined), sorted(joined - wanted)
        for i in range(0, len(drop), _JOIN_BATCH):
            await ws.send("PART " + ",".join(f"#{c}" for c in drop[i:i + _JOIN_BATCH]))
        joined -= set(drop)
        await _send_joins(ws, add)
        joined |= set(add)
        if add or drop:
            logger.info("Emote watch now reading %d channels (+%d, -%d).",
                        len(joined), len(add), len(drop))


async def _session(store, stats: CategoryStats) -> None:
    """One connection, for as long as it lasts. Returns to be retried."""
    async with websockets.connect(_HOST, ping_interval=None) as ws:
        # No PASS line: that's what makes it anonymous. The nick must be
        # justinfan followed by digits, which is Twitch's read-only guest.
        await ws.send(f"NICK justinfan{random.randint(10000, 99999)}")
        joined: set[str] = set()
        streams = await _live_streams()
        stats.on_poll(streams)
        live = {st["user_login"].lower() for st in streams}
        start = sorted(set(EMOTE_WATCH_CHANNELS) | live)[:EMOTE_WATCH_MAX]
        logger.info("Emote watch joining %d channels anonymously (%d fixed + the live "
                    "category).", len(start), len(EMOTE_WATCH_CHANNELS))
        # Joining is rate-limited to ~20 channels per 10s, so a few hundred take
        # minutes. Do it alongside reading rather than before it, or the chat of
        # the channels already joined goes unread the whole time.
        async def _open() -> None:
            await _send_joins(ws, start)
            joined.update(start)
            logger.info("Emote watch reading %d channels.", len(joined))
            await _follow_category(ws, joined, stats)

        follower = asyncio.create_task(_open())
        try:
            async for raw in ws:
                for line in raw.split("\r\n"):
                    if line.startswith("PING"):
                        await ws.send("PONG :tmi.twitch.tv")
                        continue
                    m = _LINE.match(line)
                    if not m:
                        continue
                    stats.on_message(m["channel"], m["login"], m["text"])
                    found = _emotes(m["text"])
                    if not found:
                        continue
                    for name in found:
                        store.add_emote_sighting(ts=int(time.time()), channel=m["channel"],
                                                 login=m["login"], emote=name, content=m["text"])
                    logger.info("Emote watch: %s used %s in #%s",
                                m["login"], " ".join(found), m["channel"])
        finally:
            follower.cancel()
            stats.flush()


async def emote_watch_loop(store) -> None:
    if not EMOTE_WATCH_CHANNELS and not EMOTE_WATCH_GAME_ID:
        logger.info("Emote watch disabled (nothing to watch).")
        return
    stats = CategoryStats(store)
    fails = 0
    while True:
        try:
            await _session(store, stats)
            fails = 0            # a clean close: reconnect promptly
        except asyncio.CancelledError:
            logger.info("Emote watch cancelled.")
            return
        except Exception as e:
            fails += 1
            # Twitch drops these anonymous connections routinely and the retry
            # below picks them straight back up, so a single failure is on-disk
            # noise. Only a run of them means the watch is actually down, and
            # only that is worth the Discord feed (WARNING and above).
            level = logger.warning if fails >= 3 else logger.info
            level("Emote watch connection failed (%d): %r", fails, e)
        await asyncio.sleep(_BACKOFF[min(fails, len(_BACKOFF) - 1)])
