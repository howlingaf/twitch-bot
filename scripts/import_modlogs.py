#!/usr/bin/env python3
"""Merge an exported Twitch mod-logs chat history into the chat store.

The export comes from chat_history_export.js, run in the broadcaster's browser:
every message each follower/chatter/subscriber ever sent in the channel, as
Twitch's viewer card shows it. It reaches back before the bot began recording
(2026-08-27), which is the point.

    uv run python scripts/import_modlogs.py howlingaf_chat_history.json          # preview
    uv run python scripts/import_modlogs.py howlingaf_chat_history.json --apply  # write

Message ids are Twitch's own and match the ids the bot logs live, so anything
already recorded is skipped. Each message is attached to a broadcast by time:
a recorded stream if it falls inside one, otherwise the VOD still on Twitch
that covers it (created as `vod-<id>`, the same convention as
backfill_vod_chat.py), otherwise no stream at all — chat outside a broadcast,
or from a VOD Twitch has since deleted.

What the export can't carry: emotes (only text was saved), and sub/mod/VIP
badges at the time. Those fields stay empty for imported messages.

--apply backs the database up first, and commits in chunks so the running bot
can still write between them.
"""

import argparse
import asyncio
import json
import re
import sqlite3
import sys
import time
from bisect import bisect_right
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from twitchbot.config import BROADCASTER_ID, CHAT_DB  # noqa: E402
from twitchbot.twitch_api import _twitch_request  # noqa: E402

CHUNK = 5000


def _ts(iso: str) -> int:
    """Twitch's timestamps carry nanoseconds, which fromisoformat won't take."""
    iso = re.sub(r"(\.\d{6})\d+", r"\1", iso.replace("Z", "+00:00"))
    return int(datetime.fromisoformat(iso).timestamp())


def _iso(ts: int) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).isoformat()


def _dur(s: str) -> int:
    m = re.fullmatch(r"(?:(\d+)h)?(?:(\d+)m)?(?:(\d+)s)?", s or "")
    return (int(m.group(1) or 0) * 3600 + int(m.group(2) or 0) * 60 + int(m.group(3) or 0)) if m else 0


async def _vods() -> list[dict]:
    out, cursor = [], ""
    for _ in range(10):
        st, body = await _twitch_request(
            "GET", f"https://api.twitch.tv/helix/videos?user_id={BROADCASTER_ID}&type=archive&first=100"
                   + (f"&after={cursor}" if cursor else ""))
        if st != 200:
            print(f"(couldn't list VODs: HTTP {st}; older messages will have no stream)")
            return out
        data = json.loads(body)
        out += data.get("data", [])
        cursor = data.get("pagination", {}).get("cursor", "")
        if not cursor:
            break
    return out


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("export", type=Path)
    ap.add_argument("--db", default=CHAT_DB)
    ap.add_argument("--apply", action="store_true", help="write (default is a preview)")
    a = ap.parse_args()

    data = json.loads(a.export.read_text())
    msgs = [m for m in data.get("messages", []) if m.get("id") and m.get("sent_at")]
    print(f"export: {len(msgs):,} messages, {data.get('people_checked')} people checked, "
          f"exported {data.get('exported_at')}")

    db = sqlite3.connect(a.db, timeout=30)
    db.execute("PRAGMA busy_timeout=30000")

    # ---- broadcast windows: recorded streams first, then VODs not already recorded ----
    windows = []   # (start, end, stream_id)
    recorded = {}
    now = int(time.time())
    for sid, st, en in db.execute("SELECT id, started_at, ended_at FROM streams"):
        s, e = _ts(st), (_ts(en) if en else now)
        windows.append((s, e, sid))
        recorded[sid] = True
    new_streams = {}
    for v in asyncio.run(_vods()):
        if v.get("stream_id") in recorded:
            continue                      # same broadcast we already logged live
        s = _ts(v["created_at"])
        sid = f"vod-{v['id']}"
        windows.append((s, s + _dur(v["duration"]), sid))
        new_streams[sid] = v
    windows.sort()
    starts = [w[0] for w in windows]

    def stream_for(ts: int) -> str:
        i = bisect_right(starts, ts) - 1
        return windows[i][2] if i >= 0 and windows[i][0] <= ts <= windows[i][1] else ""

    # ---- what's new ----
    existing = set()
    ids = [m["id"] for m in msgs]
    for i in range(0, len(ids), 900):
        chunk = ids[i:i + 900]
        existing |= {r[0] for r in db.execute(
            f"SELECT id FROM messages WHERE id IN ({','.join('?' * len(chunk))})", chunk)}
    fresh = [m for m in msgs if m["id"] not in existing]
    used_vods = set()
    rows = []
    by_bucket = {"recorded stream": 0, "older VOD": 0, "no stream": 0}
    for m in fresh:
        ts = _ts(m["sent_at"])
        sid = stream_for(ts)
        if sid.startswith("vod-"):
            used_vods.add(sid); by_bucket["older VOD"] += 1
        elif sid:
            by_bucket["recorded stream"] += 1
        else:
            by_bucket["no stream"] += 1
        rows.append((m["id"], ts, sid, str(m.get("user_id") or ""), (m.get("login") or "unknown").lower(),
                     m.get("display"), m.get("text") or "", "[]", 0, 0, 0, 0, "modlogs"))

    all_ts = [_ts(m["sent_at"]) for m in msgs]
    people = {m.get("login") for m in msgs}
    print(f"history spans {_iso(min(all_ts))[:10]} → {_iso(max(all_ts))[:10]} · {len(people):,} people spoke")
    print(f"already in the chat store: {len(existing):,} · new: {len(fresh):,} "
          f"({sum(1 for m in fresh if m.get('deleted')):,} of them were deleted in chat)")
    print("new messages land in:", ", ".join(f"{k} {v:,}" for k, v in by_bucket.items()))
    print(f"older VOD broadcasts that would be added: {len(used_vods)}")

    if not a.apply:
        print("\npreview only — re-run with --apply to write")
        return

    backup = Path(a.db).with_name(f"{Path(a.db).name}.bak-{datetime.now():%Y%m%d-%H%M%S}")
    dst = sqlite3.connect(backup)
    db.backup(dst)
    dst.close()
    print(f"\nbacked up to {backup}")

    for sid in sorted(used_vods):
        v = new_streams[sid]
        s = _ts(v["created_at"])
        db.execute("INSERT OR IGNORE INTO streams (id, started_at, ended_at, title, game) VALUES (?,?,?,?,?)",
                   (sid, v["created_at"], _iso(s + _dur(v["duration"])), v.get("title", ""), ""))
    db.commit()

    written = 0
    for i in range(0, len(rows), CHUNK):
        db.executemany(
            "INSERT OR IGNORE INTO messages (id, ts, stream_id, user_id, login, display, content, "
            "emotes, is_sub, is_mod, is_vip, is_first, source) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            rows[i:i + CHUNK])
        db.commit()   # between chunks the live bot gets its turn to write
        written += len(rows[i:i + CHUNK])
    total = db.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
    print(f"imported {written:,} messages · chat store now holds {total:,}")


if __name__ == "__main__":
    main()
