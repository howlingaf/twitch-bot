"""Where our emotes get used in other people's chats.

Twitch reports emote use inside our own channel and nowhere else. A one-time
sweep of VOD replays answers the question backwards (scripts/emote_sweep.py);
this answers it going forward, by reading a handful of other channels' chat as
it happens and recording every message carrying a `howlin67` emote.

It connects ANONYMOUSLY. Twitch lets anything read a public chat with no login
at all -- the connection uses a throwaway `justinfan` nick and no token -- so
it carries no account, appears in nobody's chatter list, and isn't a viewer.
The bot's own account is deliberately NOT used here: that would show up as our
bot lurking in someone else's channel. Nothing is ever sent to these channels;
the socket only ever answers Twitch's PING.

Kept apart from bot.py's connection on purpose. If this one dies, chat
recording for our own stream is unaffected, and vice versa.
"""

import asyncio
import random
import re
import time

import websockets

from .config import EMOTE_PREFIX, EMOTE_WATCH_CHANNELS
from .logger import logger

_HOST = "wss://irc-ws.chat.twitch.tv:443"
# "PRIVMSG #channel :text", with the sender's login in the prefix.
_LINE = re.compile(r"^:(?P<login>[^!]+)![^ ]+ PRIVMSG #(?P<channel>[^ ]+) :(?P<text>.*)$")
_BACKOFF = (5, 15, 60, 300)


def _emotes(text: str) -> list[str]:
    return re.findall(rf"\b{re.escape(EMOTE_PREFIX)}\w+", text, re.I)


async def _session(store) -> None:
    """One connection, for as long as it lasts. Returns to be retried."""
    async with websockets.connect(_HOST, ping_interval=None) as ws:
        # No PASS line: that's what makes it anonymous. The nick must be
        # justinfan followed by digits, which is Twitch's read-only guest.
        await ws.send(f"NICK justinfan{random.randint(10000, 99999)}")
        await ws.send("JOIN " + ",".join(f"#{c}" for c in EMOTE_WATCH_CHANNELS))
        logger.info("Emote watch reading %d channels anonymously: %s",
                    len(EMOTE_WATCH_CHANNELS), ", ".join(EMOTE_WATCH_CHANNELS))
        async for raw in ws:
            for line in raw.split("\r\n"):
                if line.startswith("PING"):
                    await ws.send("PONG :tmi.twitch.tv")
                    continue
                m = _LINE.match(line)
                if not m:
                    continue
                found = _emotes(m["text"])
                if not found:
                    continue
                for name in found:
                    store.add_emote_sighting(ts=int(time.time()), channel=m["channel"],
                                             login=m["login"], emote=name, content=m["text"])
                logger.info("Emote watch: %s used %s in #%s",
                            m["login"], " ".join(found), m["channel"])


async def emote_watch_loop(store) -> None:
    if not EMOTE_WATCH_CHANNELS:
        logger.info("Emote watch disabled (no channels configured).")
        return
    fails = 0
    while True:
        try:
            await _session(store)
            fails = 0            # a clean close: reconnect promptly
        except asyncio.CancelledError:
            logger.info("Emote watch cancelled.")
            return
        except Exception as e:
            fails += 1
            # Reading someone else's chat is a nice-to-have, so a failure here
            # is logged quietly and retried rather than escalated.
            logger.warning("Emote watch connection failed (%d): %r", fails, e)
        await asyncio.sleep(_BACKOFF[min(fails, len(_BACKOFF) - 1)])
