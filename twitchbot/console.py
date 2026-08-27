"""Inbound control API for the Discord "twitch-bot console".

A small aiohttp app, bound to localhost only and bearer-authenticated, that lets
the Discord bot (same VPS) run admin actions against the live Twitch bot and get
a text response back. It is request/response only — no log streaming.

Contract:
  POST /console
    Authorization: Bearer <CONSOLE_SECRET>
    body: {"command": "<name>", "args": "<optional string>"}
    ->   {"ok": <bool>, "output": "<human-readable text>"}

Commands: status, lt_clear, say, test, viewers. The Twitch bot is
prefix-agnostic — the Discord side maps its own slash commands onto these names.
"""
import shlex
import time

import aiohttp
from aiohttp import web

from . import viewerstats
from .config import BROADCASTER_ID, CONSOLE_SECRET, DISCORD_BOT_URL, RECAP_SECRET
from .logger import logger

_START_TIME = time.time()


async def _cmd_status(bot, args):
    parts = [
        f"live={bot.is_live}",
        f"current_problem={bot.current_problem!r}",
        f"problems_this_stream={len(bot.stream_problems)}",
        f"chatter_submissions={len(bot.chatter_submissions)}",
        f"spotify={'connected' if bot.spotify else 'disconnected'}",
        f"uptime={int(time.time() - _START_TIME)}s",
    ]
    return True, " | ".join(parts)


async def _cmd_lt_clear(bot, args):
    active = bot.clear_problem()
    return True, "Cleared current problem." if active else "No active problem to clear."


async def _cmd_say(bot, args):
    text = (args or "").strip()
    if not text:
        return False, "say requires a message in args."
    if not await bot.say(text):
        return False, "No path to send the message (Helix send failed and IRC is not connected)."
    return True, f"Sent to chat: {text}"


async def _cmd_test(bot, args):
    if not RECAP_SECRET or not DISCORD_BOT_URL:
        return False, "RECAP_SECRET or DISCORD_BOT_URL not configured."
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(
                f"{DISCORD_BOT_URL}/recap/verify",
                headers={"Authorization": f"Bearer {RECAP_SECRET}"},
                timeout=aiohttp.ClientTimeout(total=5),
            ) as resp:
                if resp.status == 200:
                    return True, "Discord connection verified."
                if resp.status == 401:
                    return False, "RECAP_SECRET mismatch — rejected by Discord bot."
                return False, f"Discord bot returned HTTP {resp.status}."
    except Exception as e:
        return False, f"Cannot reach Discord bot at {DISCORD_BOT_URL}: {e}"


async def _cmd_viewers(bot, args):
    """viewers [regulars|viewers|emotes|streams|user NAME] [--since YYYY-MM-DD] [--top N]

    Reports over the chat store, as text for a code block. Defaults to the
    regulars ranking, top 15 so it fits a Discord message.
    """
    words = shlex.split(args or "")
    report, name, since, top = "regulars", "", 0, 15
    i = 0
    try:
        while i < len(words):
            w = words[i]
            if w == "--since":
                since = viewerstats.since_ts(words[i + 1]); i += 2
            elif w == "--top":
                top = max(1, min(50, int(words[i + 1]))); i += 2
            elif w in viewerstats.REPORTS:
                report = w; i += 1
            elif report == "user" and not name:
                name = w; i += 1
            else:
                return False, f"unexpected {w!r}. usage: {_cmd_viewers.__doc__.strip().splitlines()[0]}"
    except (IndexError, ValueError) as e:
        return False, f"bad arguments ({e}). usage: {_cmd_viewers.__doc__.strip().splitlines()[0]}"

    seventv: set[str] = set()
    if report == "emotes":
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    viewerstats.SEVENTV_SET_URL.format(broadcaster_id=BROADCASTER_ID),
                    timeout=aiohttp.ClientTimeout(total=10),
                ) as resp:
                    seventv = viewerstats.seventv_names(await resp.json())
        except Exception as e:
            logger.warning("7TV emote set lookup failed: %s", e)
    try:
        text = viewerstats.run(bot.store.db, report, seventv=seventv, name=name,
                               since=since, top=top)
    except ValueError as e:
        return False, str(e)
    return True, f"```\n{text}\n```"


_COMMANDS = {
    "status": _cmd_status,
    "lt_clear": _cmd_lt_clear,
    "say": _cmd_say,
    "test": _cmd_test,
    "viewers": _cmd_viewers,
}


def make_console_app(bot) -> web.Application:
    async def handler(request: web.Request):
        if not CONSOLE_SECRET or request.headers.get("Authorization") != f"Bearer {CONSOLE_SECRET}":
            return web.json_response({"ok": False, "output": "unauthorized"}, status=401)

        try:
            data = await request.json()
        except Exception:
            return web.json_response({"ok": False, "output": "invalid JSON body"}, status=400)

        command = (data.get("command") or "").strip().lower()
        args = data.get("args") or ""
        fn = _COMMANDS.get(command)
        if not fn:
            return web.json_response(
                {"ok": False,
                 "output": f"unknown command {command!r}. Available: {', '.join(sorted(_COMMANDS))}"},
                status=404,
            )

        logger.info("[CONSOLE] command=%s args=%r", command, args)
        try:
            ok, output = await fn(bot, args)
        except Exception as e:
            logger.exception("[CONSOLE] error running %s", command)
            return web.json_response({"ok": False, "output": f"error: {e}"}, status=500)

        return web.json_response({"ok": ok, "output": output})

    app = web.Application()
    app.router.add_post("/console", handler)
    return app
