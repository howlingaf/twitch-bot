#!/usr/bin/env python3
"""Who your regulars are, from the bot's chat store.

    uv run python3 scripts/viewer_stats.py            # regulars ranking
    uv run python3 scripts/viewer_stats.py viewers    # raw leaderboard
    uv run python3 scripts/viewer_stats.py emotes     # Twitch + 7TV emote counts
    uv run python3 scripts/viewer_stats.py streams    # per-stream summary
    uv run python3 scripts/viewer_stats.py user NAME  # one viewer, per stream

Options: --db PATH (default chat.db), --since YYYY-MM-DD, --top N

The same reports are available in Discord: /twitch command:viewers
args:"emotes --top 10" in #twitch-bot-console.
"""

import argparse
import json
import os
import sqlite3
import sys
import urllib.request
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
load_dotenv(ROOT / ".env")

from twitchbot import viewerstats  # noqa: E402


def seventv_emotes() -> set[str]:
    try:
        url = viewerstats.SEVENTV_SET_URL.format(broadcaster_id=os.getenv("BROADCASTER_ID"))
        with urllib.request.urlopen(url, timeout=10) as r:
            return viewerstats.seventv_names(json.load(r))
    except Exception as e:  # noqa: BLE001
        print(f"(7TV lookup failed: {e}; counting Twitch emotes only)", file=sys.stderr)
        return set()


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("report", nargs="?", default="regulars", choices=sorted(viewerstats.REPORTS))
    ap.add_argument("name", nargs="?")
    ap.add_argument("--db", default=str(ROOT / "chat.db"))
    ap.add_argument("--since")
    ap.add_argument("--top", type=int, default=25)
    a = ap.parse_args()
    if not Path(a.db).exists():
        sys.exit(f"{a.db} not found — the bot creates it on first run.")
    db = sqlite3.connect(f"file:{a.db}?mode=ro", uri=True)
    try:
        print(viewerstats.run(
            db, a.report,
            seventv=seventv_emotes() if a.report == "emotes" else None,
            name=a.name or "", since=viewerstats.since_ts(a.since), top=a.top,
        ))
    except ValueError as e:
        sys.exit(str(e))


if __name__ == "__main__":
    main()
