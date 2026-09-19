#!/usr/bin/env python3
"""Seed the chat store from VOD chat replays.

The bot only started recording chat on 2026-08-27. For anything earlier, the
sole record is the chat replay attached to each VOD still on Twitch. Dump it
with TwitchDownloaderCLI (https://github.com/lay295/TwitchDownloader):

    TwitchDownloaderCLI chatdownload --id <VOD_ID> -o vod_<VOD_ID>.json

then load one or more dumps:

    uv run python3 scripts/backfill_vod_chat.py vod_*.json

Or fetch a replay straight from Twitch and attach it to a stream the bot did
record — for a night the live chat connection was down (Sep 18, 2026):

    uv run python3 scripts/backfill_vod_chat.py --fetch <VOD_ID> --stream-id <ID>

Messages get source='vod' and the VOD's stream id, so they show up in
viewer_stats.py alongside live-recorded ones. Re-running is safe: message ids
dedup. Presence can't be recovered — the replay only has who spoke — so
watch-time numbers start from the day the bot began recording.
"""

import argparse
import json
import sys
import time
import urllib.request
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from twitchbot.chatstore import ChatStore  # noqa: E402
from twitchbot.viewerstats import _iso, _ts  # noqa: E402


def load(store: ChatStore, path: Path) -> tuple[str, int]:
    data = json.loads(path.read_text())
    video = data.get("video", {})
    # The replay is keyed by VOD id; Helix's stream id for that broadcast is
    # gone once the stream ends, so the VOD id stands in for it.
    stream_id = f"vod-{video.get('id') or path.stem}"
    started = video.get("created_at") or ""
    base = _ts(started) if started else 0
    store.start_stream(stream_id, started, video.get("title", ""),
                       (video.get("game") or video.get("chapters", [{}])[0].get("gameDisplayName") or ""))
    if base and video.get("length"):
        store.end_stream(stream_id, _iso(base + int(video["length"])))
    n = 0
    store.db.execute("BEGIN")   # one transaction per VOD, not per message
    for c in data.get("comments", []):
        who = c.get("commenter") or {}
        msg = c.get("message") or {}
        badges = {b.get("_id") or b.get("id") for b in msg.get("user_badges", [])}
        emotes = [f["text"] for f in msg.get("fragments", [])
                  if f.get("emoticon") and f.get("text")]
        ts = _ts(c["created_at"]) if c.get("created_at") \
            else base + int(c.get("content_offset_seconds", 0))
        store.add_message(
            id=c.get("_id") or c.get("id") or f"{stream_id}-{n}",
            ts=ts,
            stream_id=stream_id,
            user_id=str(who.get("_id") or who.get("id") or ""),
            login=who.get("name") or who.get("login") or "unknown",
            display=who.get("display_name"),
            content=msg.get("body", ""),
            emotes=emotes,
            is_sub="subscriber" in badges,
            is_mod="moderator" in badges,
            is_vip="vip" in badges,
            source="vod",
        )
        n += 1
    store.db.execute("COMMIT")
    return stream_id, n


# Twitch's public web client id and the persisted query its player uses for the
# chat replay. No login needed; ~100 comments a page, cursor-paged.
_GQL = "https://gql.twitch.tv/gql"
_CLIENT_ID = "kd1unb4b3q4t58fwlpcbzcbnm76a8fp"
_COMMENTS_HASH = "b70a3591ff0f4e0313d126c6a1502d79a1c02baebb288227c582044aa76adf6a"


def _gql_page(vod_id: str, cursor: str | None) -> dict:
    variables = {"videoID": vod_id}
    variables.update({"cursor": cursor} if cursor else {"contentOffsetSeconds": 0})
    body = json.dumps([{"operationName": "VideoCommentsByOffsetOrCursor", "variables": variables,
                        "extensions": {"persistedQuery": {"version": 1, "sha256Hash": _COMMENTS_HASH}}}])
    req = urllib.request.Request(_GQL, data=body.encode(), headers={
        "Client-Id": _CLIENT_ID, "Content-Type": "application/json"})
    for attempt in range(5):
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.load(r)[0]["data"]["video"]["comments"]
        except Exception as e:
            if attempt == 4:
                raise
            print(f"  retrying after {e!r}")
            time.sleep(2 ** attempt)


def _utc(iso: str) -> int:
    """GQL's "…Z" timestamps. viewerstats._ts drops the Z and reads the time
    as server-local, which lands every message hours off."""
    return int(datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp())


def fetch(store: ChatStore, vod_id: str, stream_id: str) -> tuple[int, int]:
    """Pull a VOD's chat replay from Twitch into an existing stream.

    The replay has no first-message flag, so a first-timer is anyone with no
    earlier message anywhere in the store — which, with the mod-logs history
    imported, reaches back to the channel's first chat. Returns (added, pages).
    """
    rows, cursor, pages = [], None, 0
    while True:
        page = _gql_page(vod_id, cursor)
        pages += 1
        edges = page.get("edges") or []
        rows += [e["node"] for e in edges]
        if not page.get("pageInfo", {}).get("hasNextPage") or not edges:
            break
        cursor = edges[-1]["cursor"]
        time.sleep(0.2)
    seen = {l for (l,) in store.db.execute("SELECT DISTINCT login FROM messages")}
    before = store.db.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
    store.db.execute("BEGIN")
    for c in sorted(rows, key=lambda c: c["createdAt"]):
        who = c.get("commenter") or {"login": "unknown"}
        msg = c.get("message") or {}
        badges = {b.get("setID") for b in msg.get("userBadges") or []}
        frags = msg.get("fragments") or []
        login = (who.get("login") or "unknown").lower()
        store.add_message(
            id=c["id"], ts=_utc(c["createdAt"]), stream_id=stream_id,
            user_id=str(who.get("id") or ""), login=login, display=who.get("displayName"),
            content="".join(f.get("text", "") for f in frags),
            emotes=[f["text"] for f in frags if f.get("emote") and f.get("text")],
            is_sub="subscriber" in badges, is_mod="moderator" in badges,
            is_vip="vip" in badges, is_first=login not in seen, source="vod")
        seen.add(login)
    store.db.execute("COMMIT")
    return store.db.execute("SELECT COUNT(*) FROM messages").fetchone()[0] - before, pages


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("dumps", nargs="*", type=Path)
    ap.add_argument("--db", default="chat.db")
    ap.add_argument("--fetch", metavar="VOD_ID", help="pull this VOD's replay from Twitch")
    ap.add_argument("--stream-id", help="with --fetch: the recorded stream to attach it to")
    a = ap.parse_args()
    store = ChatStore(a.db)
    if a.fetch:
        n, pages = fetch(store, a.fetch, a.stream_id or f"vod-{a.fetch}")
        print(f"VOD {a.fetch}: {n} new messages from {pages} pages -> {a.stream_id or f'vod-{a.fetch}'}")
    for p in a.dumps:
        sid, n = load(store, p)
        print(f"{p}: {n} messages -> {sid}")
    store.close()


if __name__ == "__main__":
    main()
