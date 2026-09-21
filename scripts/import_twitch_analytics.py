#!/usr/bin/env python3
"""Load Twitch's own analytics exports into the chat store.

Twitch keeps per-stream and per-day numbers the API never exposes -- average
viewers, live views, unique viewers, minutes watched -- and the only way out
is the Creator Dashboard export. Our own sampling starts 2026-08-27 and the
third-party sites stop around 2026-08-16, so this is the sole record of
everything before that.

    uv run python scripts/import_twitch_analytics.py /root/discord-bot/analytics/*.csv          # preview
    uv run python scripts/import_twitch_analytics.py /root/discord-bot/analytics/*.csv --apply

Granularity is taken from the row spacing -- Twitch names the file, not the
rows -- and each row is keyed by (granularity, date), so re-importing a wider
export overwrites rather than duplicates. Only the audience columns are read;
the revenue columns in the same file are deliberately left on disk.
"""

import argparse
import csv
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from twitchbot.config import CHAT_DB  # noqa: E402

_SCHEMA = """
CREATE TABLE IF NOT EXISTS twitch_analytics (
    granularity     TEXT NOT NULL,      -- 'day' | 'month'
    date            TEXT NOT NULL,      -- YYYY-MM-DD, the period's first day
    avg_viewers     REAL,
    max_viewers     INTEGER,
    follows         INTEGER,
    minutes_streamed INTEGER,
    minutes_watched INTEGER,
    live_views      INTEGER,
    unique_viewers  INTEGER,
    engaged_viewers INTEGER,
    new_engaged     INTEGER,
    returning_engaged INTEGER,
    raid_viewers_pct REAL,
    chatters        INTEGER,
    chat_messages   INTEGER,
    clips_created   INTEGER,
    clip_views      INTEGER,
    PRIMARY KEY (granularity, date)
);
"""

# csv column -> our column. Revenue and sub-tier columns are not imported.
_COLS = {
    "Average Viewers": "avg_viewers",
    "Max Viewers": "max_viewers",
    "Follows": "follows",
    "Minutes Streamed": "minutes_streamed",
    "Minutes Watched": "minutes_watched",
    "Live Views": "live_views",
    "Unique Viewers": "unique_viewers",
    "Engaged Viewers": "engaged_viewers",
    "New Engaged Viewers": "new_engaged",
    "Returning Engaged Viewers": "returning_engaged",
    "Hosts and Raids Viewers (%)": "raid_viewers_pct",
    "Chatters": "chatters",
    "Chat Messages": "chat_messages",
    "Clips Created": "clips_created",
    "Clip Views": "clip_views",
}


def _date(text: str) -> str:
    """'Sun Aug 23 2026' -> '2026-08-23'."""
    return datetime.strptime(text.strip(), "%a %b %d %Y").strftime("%Y-%m-%d")


def _number(text: str):
    text = (text or "").strip().replace(",", "")
    if not text:
        return None
    try:
        return float(text) if "." in text else int(text)
    except ValueError:
        return None


def _read(path: Path) -> tuple[str, list[dict]]:
    with path.open(newline="") as fh:
        rows = list(csv.DictReader(fh))
    dates = [_date(r["Date"]) for r in rows]
    # A monthly export has one row per month; anything tighter is daily.
    monthly = all(d.endswith("-01") for d in dates) and len(dates) > 1
    out = []
    for date, row in zip(dates, rows):
        rec = {"granularity": "month" if monthly else "day", "date": date}
        rec.update({col: _number(row.get(src)) for src, col in _COLS.items()})
        out.append(rec)
    return ("month" if monthly else "day"), out


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("csvs", nargs="+", type=Path)
    ap.add_argument("--db", default=CHAT_DB)
    ap.add_argument("--apply", action="store_true", help="write (default is a preview)")
    a = ap.parse_args()

    db = sqlite3.connect(a.db, timeout=30)
    db.execute("PRAGMA busy_timeout=30000")
    db.executescript(_SCHEMA)

    for path in a.csvs:
        grain, rows = _read(path)
        streamed = [r for r in rows if (r["minutes_streamed"] or 0) > 0]
        print(f"{path.name}: {len(rows)} {grain} rows, {len(streamed)} with a stream "
              f"({rows[0]['date']} -> {rows[-1]['date']})")
        if not a.apply:
            continue
        cols = ["granularity", "date"] + list(_COLS.values())
        db.executemany(
            f"INSERT OR REPLACE INTO twitch_analytics ({','.join(cols)}) "
            f"VALUES ({','.join('?' * len(cols))})",
            [[r[c] for c in cols] for r in rows])
        db.commit()

    if not a.apply:
        print("\npreview only — re-run with --apply to write")
        return
    total = db.execute("SELECT granularity, COUNT(*) FROM twitch_analytics GROUP BY 1").fetchall()
    print("stored:", ", ".join(f"{n} {g} rows" for g, n in total))


if __name__ == "__main__":
    main()
