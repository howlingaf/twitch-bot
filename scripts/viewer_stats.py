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
import asyncio
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from twitchbot import viewerstats  # noqa: E402
from twitchbot.config import BROADCASTER_ID  # noqa: E402


def main():
    parser = viewerstats.arg_parser()
    parser.add_argument("--db", default=str(ROOT / "chat.db"))
    parser.add_argument("-h", "--help", action="help")
    parser.description, parser.formatter_class = __doc__, argparse.RawDescriptionHelpFormatter
    a = parser.parse_args()
    if not Path(a.db).exists():
        sys.exit(f"{a.db} not found — the bot creates it on first run.")
    db = sqlite3.connect(f"file:{a.db}?mode=ro", uri=True)
    seventv = asyncio.run(viewerstats.fetch_seventv(BROADCASTER_ID)) if a.report == "emotes" else set()
    try:
        print(viewerstats.REPORTS[a.report](
            db, since=a.since, top=a.top, name=a.name, seventv=seventv))
    except ValueError as e:
        sys.exit(str(e))


if __name__ == "__main__":
    main()
