#!/usr/bin/env python3
"""Seed the chat store from VOD chat replays.

The bot only started recording chat on 2026-08-27. For anything earlier, the
sole record is the chat replay attached to each VOD still on Twitch. Dump it
with TwitchDownloaderCLI (https://github.com/lay295/TwitchDownloader):

    TwitchDownloaderCLI chatdownload --id <VOD_ID> -o vod_<VOD_ID>.json

then load one or more dumps:

    uv run python3 scripts/backfill_vod_chat.py vod_*.json

Messages get source='vod' and the VOD's stream id, so they show up in
viewer_stats.py alongside live-recorded ones. Re-running is safe: message ids
dedup. Presence can't be recovered — the replay only has who spoke — so
watch-time numbers start from the day the bot began recording.
"""

import argparse
import json
import sys
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


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("dumps", nargs="+", type=Path)
    ap.add_argument("--db", default="chat.db")
    a = ap.parse_args()
    store = ChatStore(a.db)
    for p in a.dumps:
        sid, n = load(store, p)
        print(f"{p}: {n} messages -> {sid}")
    store.close()


if __name__ == "__main__":
    main()
