#!/usr/bin/env python3
"""Count this channel's emotes in OTHER channels' chat, from their VOD replays.

Twitch reports emote use inside your own channel and nowhere else, so the only
way to see where your emotes travel is to read other channels' saved chat and
count. That's what this does: every VOD in the last week belonging to a channel
in the category, scanned for `howlin67*`.

    uv run python scripts/emote_sweep.py                     # live-now category list
    uv run python scripts/emote_sweep.py --days 7 --out sweep.json

Coverage is what Twitch allows, not everything: a category can only be listed
live, so the channel list is whoever is streaming it when this runs (plus any
leader the bot recorded during our own streams). Someone who streamed the
category on Tuesday and is offline now isn't in it, and a channel that keeps no
VODs can't be read at all.

Nobody is notified. Reading a replay is an anonymous HTTPS request for the
chat log: it doesn't join their chat, doesn't appear in their viewer list, and
isn't counted as a view of the video.
"""

import argparse
import asyncio
import json
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from backfill_vod_chat import _gql_page  # noqa: E402
from twitchbot.config import BROADCASTER_LOGIN, CHAT_DB  # noqa: E402
from twitchbot.twitch_api import _twitch_request  # noqa: E402

GAME_ID = "1469308723"          # Software and Game Development
EMOTE = re.compile(r"\bhowlin67\w+", re.I)


def _dur(s: str) -> int:
    m = re.fullmatch(r"(?:(\d+)h)?(?:(\d+)m)?(?:(\d+)s)?", s or "")
    return (int(m.group(1) or 0) * 3600 + int(m.group(2) or 0) * 60 + int(m.group(3) or 0)) if m else 0


async def _channels() -> list[tuple[str, str]]:
    """(login, user_id) for the category's live channels, plus the leaders the
    bot recorded during our own streams — those were live in it this week."""
    out, cursor = {}, ""
    for _ in range(40):
        st, b = await _twitch_request(
            "GET", f"https://api.twitch.tv/helix/streams?game_id={GAME_ID}&first=100"
                   + (f"&after={cursor}" if cursor else ""))
        if st != 200:
            break
        d = json.loads(b)
        for s in d.get("data", []):
            out[s["user_login"]] = s["user_id"]
        cursor = d.get("pagination", {}).get("cursor", "")
        if not cursor or not d.get("data"):
            break

    import sqlite3
    db = sqlite3.connect(f"file:{CHAT_DB}?mode=ro", uri=True)
    extra = set()
    for (blob,) in db.execute("SELECT leaders FROM viewers WHERE leaders IS NOT NULL"):
        extra |= {login for login, _ in json.loads(blob)}
    extra -= set(out) | {BROADCASTER_LOGIN.lower()}
    for i in range(0, len(extra), 100):
        chunk = list(extra)[i:i + 100]
        st, b = await _twitch_request(
            "GET", "https://api.twitch.tv/helix/users?" + "&".join(f"login={c}" for c in chunk))
        if st == 200:
            for u in json.loads(b).get("data", []):
                out[u["login"]] = u["id"]
    out.pop(BROADCASTER_LOGIN.lower(), None)
    return sorted(out.items())


async def _vods(user_id: str, cutoff: datetime) -> list[dict]:
    st, b = await _twitch_request(
        "GET", f"https://api.twitch.tv/helix/videos?user_id={user_id}&type=archive&first=20")
    if st != 200:
        return []
    return [v for v in json.loads(b).get("data", [])
            if datetime.fromisoformat(v["created_at"].replace("Z", "+00:00")) >= cutoff]


def _scan(vod_id: str) -> tuple[list[dict], int]:
    """Every message in this VOD's replay using one of our emotes."""
    hits, seen, cursor = [], 0, None
    while True:
        page = _gql_page(vod_id, cursor)
        edges = page.get("edges") or []
        for e in edges:
            c = e["node"]
            text = "".join(f.get("text", "") for f in (c.get("message") or {}).get("fragments") or [])
            seen += 1
            for name in EMOTE.findall(text):
                hits.append({"vod": vod_id, "at": c["createdAt"], "offset": c["contentOffsetSeconds"],
                             "who": (c.get("commenter") or {}).get("login", "?"), "emote": name})
        if not page.get("pageInfo", {}).get("hasNextPage") or not edges:
            return hits, seen
        cursor = edges[-1]["cursor"]
        time.sleep(0.15)


async def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--days", type=int, default=7)
    ap.add_argument("--out", default="emote_sweep.json")
    a = ap.parse_args()

    cutoff = datetime.now(timezone.utc) - timedelta(days=a.days)
    channels = await _channels()
    print(f"{len(channels)} channels to check", flush=True)

    hits, scanned, msgs, started = [], 0, 0, time.time()
    for i, (login, uid) in enumerate(channels, 1):
        vods = await _vods(uid, cutoff)
        for v in vods:
            found, seen = _scan(v["id"])
            for h in found:
                h["channel"] = login
                h["title"] = v["title"]
            hits += found
            scanned += 1
            msgs += seen
            print(f"[{i}/{len(channels)}] {login} vod {v['id']} "
                  f"({_dur(v['duration']) // 3600}h) {seen} msgs, {len(found)} hits "
                  f"· running total {len(hits)} · {time.time() - started:.0f}s", flush=True)

    Path(a.out).write_text(json.dumps(
        {"swept_at": datetime.now(timezone.utc).isoformat(), "days": a.days,
         "channels": len(channels), "vods": scanned, "messages_read": msgs,
         "hits": hits}, indent=1))
    print(f"\n{len(hits)} uses of our emotes across {scanned} VODs "
          f"({msgs:,} messages read) -> {a.out}")


if __name__ == "__main__":
    asyncio.run(main())
