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


class DiscordLogHandler(logging.Handler):
    def __init__(self):
        super().__init__()
        self.setFormatter(_formatter)
        self._buffer = deque(maxlen=_MAX_BUFFER)
        self._lock = threading.Lock()

    def emit(self, record):
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
