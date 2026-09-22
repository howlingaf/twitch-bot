"""HTTP API for the Stream Deck on the streaming PC.

Two endpoints, both authenticated with DECK_SECRET (as ?key=... because
Stream Deck HTTP plugins can't always set headers, or as a bearer header):

  GET  /ad-status            plain text sized for a key title, e.g.
                             "next ad\n41:32" / "AD SOON\n0:42" / "AD\n2:31".
                             Poll it every second or two; the countdown is
                             computed from the ad loop's own deadlines, so it
                             is exactly in sync with the chat messages.
                             ?format=json returns {phase, seconds} instead.
  POST /run-ad?length=60     start a manual ad break now (GET works too, for
                             plugins that can only GET). Replies with plain
                             text for the key: "60s ad running" or why not.

The Stream Deck isn't on this box: it reaches this through the Cloudflare
tunnel at https://twitch.howling.one (which routes only these paths here), so
it binds loopback like the console API. The secret is the whole defense.
"""

import hmac

from aiohttp import web

from .config import DECK_SECRET
from .logger import logger


def _clock(seconds: int) -> str:
    m, s = divmod(max(0, int(seconds)), 60)
    return f"{m // 60}:{m % 60:02d}:{s:02d}" if m >= 60 else f"{m}:{s:02d}"


# What the key should read in each phase. Two short lines fit a key face.
_TITLES = {
    "offline": lambda s: "offline",
    "idle": lambda s: f"next ad\n{_clock(s)}",
    "warn": lambda s: f"AD SOON\n{_clock(s)}",
    "ad": lambda s: f"AD\n{_clock(s)}",
}


def make_deck_app(bot) -> web.Application:
    def _authed(request: web.Request) -> bool:
        supplied = request.query.get("key", "") \
            or request.headers.get("Authorization", "").removeprefix("Bearer ")
        return bool(DECK_SECRET) and hmac.compare_digest(supplied, DECK_SECRET)

    async def ad_status(request: web.Request):
        if not _authed(request):
            return web.Response(status=401, text="unauthorized")
        phase, seconds = bot.ad_status()
        if request.query.get("format") == "json":
            return web.json_response({"phase": phase, "seconds": seconds})
        return web.Response(text=_TITLES[phase](seconds))

    async def run_ad(request: web.Request):
        if not _authed(request):
            return web.Response(status=401, text="unauthorized")
        try:
            length = int(request.query.get("length", "60"))
        except ValueError:
            return web.Response(status=400, text="bad length")
        ok, msg = await bot.run_manual_ad(length)
        # msg is sized for a key title (two lines); one line in the log.
        logger.info("Deck run-ad (%ss): %s", length, msg.replace("\n", " "))
        return web.Response(status=200 if ok else 409, text=msg)

    app = web.Application()
    app.router.add_get("/ad-status", ad_status)
    app.router.add_route("*", "/run-ad", run_ad)
    return app
