"""Inbound control API for the Discord "twitch-bot console".

A small aiohttp app, bound to localhost only and bearer-authenticated, that lets
the Discord bot (same VPS) run admin actions against the live Twitch bot and get
a text response back. It is request/response only — no log streaming.

Contract:
  POST /console
    Authorization: Bearer <CONSOLE_SECRET>
    body: {"command": "<name>", "args": "<optional string>"}
    ->   {"ok": <bool>, "output": "<human-readable text>"}

Commands (v1): status, lt_clear, test. The Twitch bot is prefix-agnostic — the
Discord side maps its own slash commands onto these names.
"""
import time

import aiohttp
from aiohttp import web

from .config import CONSOLE_SECRET, DISCORD_BOT_URL, RECAP_SECRET
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
    active = bool(bot.current_problem) or (bot.lt_task and not bot.lt_task.done())
    if bot.lt_task and not bot.lt_task.done():
        bot.lt_task.cancel()
    bot.current_problem = None
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


_COMMANDS = {
    "status": _cmd_status,
    "lt_clear": _cmd_lt_clear,
    "say": _cmd_say,
    "test": _cmd_test,
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
