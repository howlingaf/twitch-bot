"""Forward the bot's logs to the Discord bot's /twitch-log endpoint.

A logging.Handler buffers formatted log lines (thread-safe — log calls come
from the event loop and from asyncio.to_thread workers), and an async loop
flushes the buffer in batches every few seconds. The Discord bot batches again
on its side, so we don't worry about Discord rate limits here.

Disabled unless both DISCORD_BOT_URL and CONSOLE_SECRET are configured.
"""
import asyncio
import logging
import threading
from collections import deque

import aiohttp

from .config import DISCORD_BOT_URL, CONSOLE_SECRET

# Bound the buffer so an extended Discord outage can't grow memory without limit.
_MAX_BUFFER = 500
_FLUSH_INTERVAL = 3

_formatter = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")

# Routine token/auth chatter that's noise in the Discord feed — successful
# refreshes and the self-healing 401 path. These are kept in the on-disk logs
# but only forwarded to Discord if they're ERROR (i.e. an actual token problem).
# Matched as lowercase substrings of the log message.
_SUPPRESS_BELOW_ERROR = (
    "refreshed twitch",            # Refreshed Twitch Helix/chat access token
    "chat token valid",           # startup validation
    "chat token refresh loop",    # loop started
    "propagated refreshed chat",  # token propagated to the connection
    "helix 401",                  # 401 -> auto refresh + retry (recovers itself)
    # daily log rotation/cleanup housekeeping
    "log rotation",               # "Running daily log rotation..." / "...complete."
    "log file handler set to",    # rolled to the new day's file
    "deleted old log file",       # retention purge
    "log maintenance loop",       # loop cancelled on shutdown
    # overlay websocket clients (OBS/browser sources) connect and drop all the
    # time; that churn is noise in the feed. Real overlay errors still forward.
    "overlay connected",
    "overlay disconnected",
)


class DiscordLogHandler(logging.Handler):
    def __init__(self):
        super().__init__()
        self.setFormatter(_formatter)
        self._buffer = deque(maxlen=_MAX_BUFFER)
        self._lock = threading.Lock()

    def emit(self, record):
        if record.levelno < logging.ERROR:
            msg = record.getMessage().lower()
            if any(p in msg for p in _SUPPRESS_BELOW_ERROR):
                return
        try:
            line = self.format(record)
        except Exception:
            return
        with self._lock:
            self._buffer.append(line)

    def drain(self) -> list:
        with self._lock:
            if not self._buffer:
                return []
            lines = list(self._buffer)
            self._buffer.clear()
            return lines


async def discord_log_loop(handler: DiscordLogHandler):
    while True:
        try:
            await asyncio.sleep(_FLUSH_INTERVAL)
            lines = handler.drain()
            if not lines:
                continue
            try:
                async with aiohttp.ClientSession() as session:
                    await session.post(
                        f"{DISCORD_BOT_URL}/twitch-log",
                        json={"lines": lines},
                        headers={"Authorization": f"Bearer {CONSOLE_SECRET}"},
                        timeout=aiohttp.ClientTimeout(total=10),
                    )
            except Exception:
                # Swallow: never log here, or the failure would re-enter the
                # buffer and loop. This batch is dropped (best-effort feed).
                pass
        except asyncio.CancelledError:
            break
        except Exception:
            # Also swallowed to avoid any feedback loop into our own handler.
            pass
