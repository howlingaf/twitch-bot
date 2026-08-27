#!/usr/bin/env python3
"""Who your regulars are, from the bot's chat store.

    uv run python3 scripts/viewer_stats.py            # viewer leaderboard
    uv run python3 scripts/viewer_stats.py emotes     # Twitch + 7TV emote counts
    uv run python3 scripts/viewer_stats.py streams    # per-stream summary
    uv run python3 scripts/viewer_stats.py user NAME  # one viewer, per stream

Options: --db PATH (default chat.db), --since YYYY-MM-DD, --top N
"""

import argparse
import json
import os
import re
import sqlite3
import sys
import urllib.request
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent.parent / ".env")
BROADCASTER_ID = os.getenv("BROADCASTER_ID")

_WORD = re.compile(r"\S+")


def _since_ts(s: str | None) -> int:
    if not s:
        return 0
    return int(datetime.fromisoformat(s).replace(tzinfo=timezone.utc).timestamp())


def _table(rows, headers):
    if not rows:
        print("(nothing yet)")
        return
    widths = [max(len(str(h)), *(len(str(r[i])) for r in rows)) for i, h in enumerate(headers)]
    print("  ".join(str(h).ljust(w) for h, w in zip(headers, widths)))
    for r in rows:
        print("  ".join(str(c).ljust(w) for c, w in zip(r, widths)))


def seventv_emotes() -> set[str]:
    """Names in the channel's active 7TV set (empty set if unreachable)."""
    try:
        with urllib.request.urlopen(
            f"https://7tv.io/v3/users/twitch/{BROADCASTER_ID}", timeout=10
        ) as r:
            data = json.load(r)
        return {e["name"] for e in data.get("emote_set", {}).get("emotes", [])}
    except Exception as e:  # noqa: BLE001
        print(f"(7TV lookup failed: {e}; counting Twitch emotes only)", file=sys.stderr)
        return set()


def viewers(db, since: int, top: int):
    """The leaderboard. Presence minutes and message counts per login, over
    distinct streams, plus recency — the ingredients of "regular"."""
    total_streams = db.execute(
        "SELECT COUNT(*) FROM streams WHERE started_at >= ?",
        (datetime.fromtimestamp(since, timezone.utc).isoformat(),),
    ).fetchone()[0] or 1
    rows = db.execute(
        """
        WITH m AS (
            SELECT login,
                   COUNT(*)                          AS msgs,
                   COUNT(DISTINCT stream_id)         AS chat_streams,
                   MIN(ts)                           AS first_ts,
                   MAX(ts)                           AS last_ts,
                   SUM(is_sub)                       AS sub_msgs
            FROM messages WHERE ts >= ? AND stream_id != ''
            GROUP BY login
        ),
        p AS (
            SELECT login,
                   COUNT(*)                          AS minutes,
                   COUNT(DISTINCT stream_id)         AS seen_streams
            FROM presence WHERE minute >= ?
            GROUP BY login
        )
        SELECT COALESCE(m.login, p.login)            AS login,
               COALESCE(p.seen_streams, m.chat_streams, 0) AS streams,
               COALESCE(p.minutes, 0)                AS minutes,
               COALESCE(m.msgs, 0)                   AS msgs,
               COALESCE(m.chat_streams, 0)           AS chat_streams,
               m.last_ts, m.first_ts,
               COALESCE(m.sub_msgs, 0) > 0           AS sub
        FROM m LEFT JOIN p ON p.login = m.login
        UNION
        SELECT p.login, p.seen_streams, p.minutes, 0, 0, NULL, NULL, 0
        FROM p LEFT JOIN m ON m.login = p.login WHERE m.login IS NULL
        ORDER BY streams DESC, minutes DESC, msgs DESC
        LIMIT ?
        """,
        (since, since, top),
    ).fetchall()

    def day(ts):
        return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%d") if ts else "-"

    out = []
    for login, streams, minutes, msgs, chat_streams, last, first, sub in rows:
        out.append((
            login,
            f"{streams}/{total_streams}",
            f"{minutes / 60:.1f}",
            msgs,
            f"{msgs / streams:.1f}" if streams else "-",
            day(first), day(last),
            "sub" if sub else "",
        ))
    print(f"Top {top} viewers across {total_streams} stream(s)\n")
    _table(out, ("viewer", "streams", "hours", "msgs", "msgs/stream",
                 "first chat", "last chat", ""))


def emotes(db, since: int, top: int):
    sev = seventv_emotes()
    twitch = Counter()
    seventv = Counter()
    by_user = Counter()
    for login, content, em in db.execute(
        "SELECT login, content, emotes FROM messages WHERE ts >= ?", (since,)
    ):
        names = json.loads(em)
        twitch.update(names)
        hits = [w for w in _WORD.findall(content) if w in sev]
        seventv.update(hits)
        by_user[login] += len(names) + len(hits)
    print(f"Twitch emotes (top {top})\n")
    _table(twitch.most_common(top), ("emote", "uses"))
    print(f"\n7TV emotes (top {top}, {len(sev)} in set)\n")
    _table(seventv.most_common(top), ("emote", "uses"))
    print(f"\nHeaviest emote users (top {top})\n")
    _table(by_user.most_common(top), ("viewer", "emotes"))


def streams(db, since: int, top: int):
    rows = db.execute(
        """
        SELECT s.id, substr(s.started_at, 1, 16), s.game, s.title,
               (SELECT COUNT(*) FROM messages m WHERE m.stream_id = s.id) AS msgs,
               (SELECT COUNT(DISTINCT login) FROM messages m WHERE m.stream_id = s.id) AS chatters,
               (SELECT COUNT(DISTINCT login) FROM presence p WHERE p.stream_id = s.id) AS present,
               (SELECT COUNT(*) FROM messages m WHERE m.stream_id = s.id AND is_first) AS newbies
        FROM streams s
        WHERE s.started_at >= ?
        ORDER BY s.started_at DESC LIMIT ?
        """,
        (datetime.fromtimestamp(since, timezone.utc).isoformat(), top),
    ).fetchall()
    _table([(i, st, g or "", (t or "")[:40], m, c, p, n) for i, st, g, t, m, c, p, n in rows],
           ("stream", "started", "game", "title", "msgs", "chatters", "present", "first-timers"))


def user(db, login: str, since: int, top: int):
    login = login.lower().lstrip("@")
    rows = db.execute(
        """
        SELECT s.id, substr(s.started_at, 1, 10),
               (SELECT COUNT(*) FROM messages m WHERE m.stream_id = s.id AND m.login = ?) AS msgs,
               (SELECT COUNT(*) FROM presence p WHERE p.stream_id = s.id AND p.login = ?) AS minutes
        FROM streams s WHERE s.started_at >= ?
        ORDER BY s.started_at DESC LIMIT ?
        """,
        (login, login, datetime.fromtimestamp(since, timezone.utc).isoformat(), top),
    ).fetchall()
    rows = [r for r in rows if r[2] or r[3]]
    print(f"{login}: {len(rows)} stream(s)\n")
    _table(rows, ("stream", "date", "msgs", "minutes"))
    fav = Counter()
    for (em,) in db.execute("SELECT emotes FROM messages WHERE login = ?", (login,)):
        fav.update(json.loads(em))
    if fav:
        print("\nFavourite Twitch emotes:", ", ".join(f"{e} x{n}" for e, n in fav.most_common(5)))


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("report", nargs="?", default="viewers",
                    choices=["viewers", "emotes", "streams", "user"])
    ap.add_argument("name", nargs="?")
    ap.add_argument("--db", default="chat.db")
    ap.add_argument("--since")
    ap.add_argument("--top", type=int, default=25)
    a = ap.parse_args()
    if not Path(a.db).exists():
        sys.exit(f"{a.db} not found — the bot creates it on first run.")
    db = sqlite3.connect(f"file:{a.db}?mode=ro", uri=True)
    since = _since_ts(a.since)
    if a.report == "user":
        if not a.name:
            sys.exit("user report needs a login: viewer_stats.py user NAME")
        user(db, a.name, since, a.top)
    else:
        {"viewers": viewers, "emotes": emotes, "streams": streams}[a.report](db, since, a.top)


if __name__ == "__main__":
    main()
