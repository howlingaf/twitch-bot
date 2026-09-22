#!/usr/bin/env python3
"""Collect a research snapshot of the category: who streams when, how big they
are, and what their chat looks like.

For every channel live in Software and Game Development (plus any channel named
on the command line, and every category leader the bot has recorded), this
stores:

  channels  Helix profile and follower count
  vods      every archived broadcast in the last --meta-days (start, length,
            title, views) -- the schedule and topic record
  comments  the full chat replay of every VOD in the last --chat-days

The output is its own SQLite file, not chat.db: it's other people's chat,
collected for one analysis, and it can be thrown away and rebuilt. Re-running
is incremental -- a VOD whose chat is already stored is skipped -- so an
interrupted run just picks up where it stopped.

Reads are anonymous (Twitch's public chat-replay endpoint and the Helix app
token), so nobody is notified and nothing is posted anywhere.

    uv run python scripts/category_scan.py --out /path/scan.db
    uv run python scripts/category_scan.py --out /path/scan.db --extra badcop_ kuviman
"""

import argparse
import asyncio
import json
import re
import sqlite3
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from backfill_vod_chat import _gql_page  # noqa: E402
from twitchbot.config import BROADCASTER_LOGIN, CHAT_DB  # noqa: E402
from twitchbot.twitch_api import _twitch_request  # noqa: E402

GAME_ID = "1469308723"   # Software and Game Development

_SCHEMA = """
CREATE TABLE IF NOT EXISTS channels (
    login TEXT PRIMARY KEY, user_id TEXT, display TEXT, broadcaster_type TEXT,
    created_at TEXT, followers INTEGER, description TEXT, scanned_at INTEGER);
CREATE TABLE IF NOT EXISTS vods (
    vod_id TEXT PRIMARY KEY, login TEXT, created_at TEXT, duration_s INTEGER,
    title TEXT, view_count INTEGER, chat_done INTEGER DEFAULT 0, chat_n INTEGER);
CREATE TABLE IF NOT EXISTS comments (
    id TEXT PRIMARY KEY, vod_id TEXT, login TEXT, ts INTEGER, offset_s INTEGER,
    commenter TEXT, text TEXT);
CREATE INDEX IF NOT EXISTS comments_vod ON comments(vod_id);
CREATE INDEX IF NOT EXISTS comments_commenter ON comments(commenter);
"""


def _dur(s: str) -> int:
    m = re.fullmatch(r"(?:(\d+)h)?(?:(\d+)m)?(?:(\d+)s)?", s or "")
    return (int(m.group(1) or 0) * 3600 + int(m.group(2) or 0) * 60 + int(m.group(3) or 0)) if m else 0


def _utc(iso: str) -> int:
    return int(datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp())


async def _get(url: str):
    status, body = await _twitch_request("GET", url)
    return json.loads(body) if status == 200 else None


async def _channel_list(extra: list[str]) -> list[str]:
    found, cursor = set(), ""
    for _ in range(40):
        d = await _get(f"https://api.twitch.tv/helix/streams?game_id={GAME_ID}&first=100"
                       + (f"&after={cursor}" if cursor else ""))
        if not d:
            break
        found |= {s["user_login"].lower() for s in d.get("data", [])}
        cursor = d.get("pagination", {}).get("cursor", "")
        if not cursor or not d.get("data"):
            break
    db = sqlite3.connect(f"file:{CHAT_DB}?mode=ro", uri=True)
    for (blob,) in db.execute("SELECT leaders FROM viewers WHERE leaders IS NOT NULL"):
        found |= {login for login, _ in json.loads(blob)}
    found |= {e.lower() for e in extra}
    found.discard(BROADCASTER_LOGIN.lower())
    return sorted(found)


async def _profiles(out: sqlite3.Connection, logins: list[str]) -> dict[str, str]:
    ids = {}
    for i in range(0, len(logins), 100):
        chunk = logins[i:i + 100]
        d = await _get("https://api.twitch.tv/helix/users?" + "&".join(f"login={c}" for c in chunk))
        for u in (d or {}).get("data", []):
            f = await _get(f"https://api.twitch.tv/helix/channels/followers?broadcaster_id={u['id']}&first=1")
            out.execute("INSERT OR REPLACE INTO channels VALUES (?,?,?,?,?,?,?,?)",
                        (u["login"], u["id"], u["display_name"], u["broadcaster_type"],
                         u["created_at"], (f or {}).get("total"), u.get("description", ""),
                         int(time.time())))
            ids[u["login"]] = u["id"]
    out.commit()
    return ids


async def _vod_meta(out: sqlite3.Connection, login: str, uid: str, since: datetime) -> None:
    cursor = ""
    for _ in range(5):
        d = await _get(f"https://api.twitch.tv/helix/videos?user_id={uid}&type=archive&first=100"
                       + (f"&after={cursor}" if cursor else ""))
        if not d:
            return
        stop = False
        for v in d.get("data", []):
            if datetime.fromisoformat(v["created_at"].replace("Z", "+00:00")) < since:
                stop = True
                break
            out.execute("INSERT OR IGNORE INTO vods (vod_id, login, created_at, duration_s, title, view_count) "
                        "VALUES (?,?,?,?,?,?)",
                        (v["id"], login, v["created_at"], _dur(v["duration"]), v["title"], v["view_count"]))
        cursor = d.get("pagination", {}).get("cursor", "")
        if stop or not cursor:
            break
    out.commit()


def _fetch_chat(vod_id: str) -> list[tuple]:
    rows, cursor = [], None
    while True:
        page = _gql_page(vod_id, cursor)
        edges = (page or {}).get("edges") or []
        for e in edges:
            c = e["node"]
            text = "".join(f.get("text", "") for f in (c.get("message") or {}).get("fragments") or [])
            rows.append((c["id"], vod_id, _utc(c["createdAt"]), c.get("contentOffsetSeconds", 0),
                         ((c.get("commenter") or {}).get("login") or "?").lower(), text[:500]))
        if not (page or {}).get("pageInfo", {}).get("hasNextPage") or not edges:
            return rows
        cursor = edges[-1]["cursor"]
        time.sleep(0.1)


async def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--out", required=True)
    ap.add_argument("--meta-days", type=int, default=60)
    ap.add_argument("--chat-days", type=int, default=7)
    ap.add_argument("--workers", type=int, default=6)
    ap.add_argument("--extra", nargs="*", default=[])
    a = ap.parse_args()

    out = sqlite3.connect(a.out, timeout=60)
    out.execute("PRAGMA journal_mode=WAL")
    out.executescript(_SCHEMA)

    logins = await _channel_list(a.extra)
    print(f"{len(logins)} channels", flush=True)
    ids = await _profiles(out, logins)
    print(f"{len(ids)} profiles stored", flush=True)

    now = datetime.now(timezone.utc)
    for i, (login, uid) in enumerate(sorted(ids.items()), 1):
        await _vod_meta(out, login, uid, now - timedelta(days=a.meta_days))
        if i % 25 == 0:
            print(f"  vod metadata {i}/{len(ids)}", flush=True)
    n_vods = out.execute("SELECT COUNT(*) FROM vods").fetchone()[0]
    print(f"{n_vods} VODs in the last {a.meta_days} days", flush=True)

    cutoff = (now - timedelta(days=a.chat_days)).strftime("%Y-%m-%dT%H:%M:%SZ")
    todo = [v for (v,) in out.execute(
        "SELECT vod_id FROM vods WHERE chat_done=0 AND created_at >= ? ORDER BY created_at DESC", (cutoff,))]
    print(f"{len(todo)} VOD chats to fetch ({a.workers} at a time)", flush=True)

    started = time.time()
    with ThreadPoolExecutor(a.workers) as pool:
        for k, (vid, rows) in enumerate(pool.map(lambda v: (v, _safe(v)), todo), 1):
            if rows is not None:
                out.executemany("INSERT OR IGNORE INTO comments (id, vod_id, ts, offset_s, commenter, text) "
                                "VALUES (?,?,?,?,?,?)", rows)
                out.execute("UPDATE vods SET chat_done=1, chat_n=? WHERE vod_id=?", (len(rows), vid))
                out.commit()
            if k % 10 == 0 or k == len(todo):
                total = out.execute("SELECT COUNT(*) FROM comments").fetchone()[0]
                print(f"  chat {k}/{len(todo)} · {total:,} messages · {time.time() - started:.0f}s", flush=True)
    out.execute("UPDATE comments SET login = (SELECT login FROM vods WHERE vods.vod_id = comments.vod_id) "
                "WHERE login IS NULL")
    out.commit()
    print("done", flush=True)


def _safe(vod_id: str):
    try:
        return _fetch_chat(vod_id)
    except Exception as e:
        print(f"  ! {vod_id}: {e!r}", flush=True)
        return None


if __name__ == "__main__":
    asyncio.run(main())
