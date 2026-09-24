#!/usr/bin/env python3
"""Create the song-queue channel-point reward and record its id in .env.

Run once, after the broadcaster token has the channel:manage:redemptions
scope. Created via the API on purpose: Twitch only lets the creating app
update a reward's redemptions, and the bot refunds failed requests.
"""

import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from twitchbot import twitch_api  # noqa: E402

TITLE = "Queue the next song"
COST = 10_000
PROMPT = ("Paste a Spotify or Apple Music track link — it queues next on "
          "the stream's Spotify. Auto-refunded if it can't be queued.")


def main():
    reward_id = asyncio.run(twitch_api.create_custom_reward(TITLE, COST, PROMPT))
    if not reward_id:
        sys.exit("Could not create the reward — is channel:manage:redemptions "
                 "on the broadcaster token? (re-run scripts/twitch_auth.py)")
    env = ROOT / ".env"
    text = env.read_text()
    if "SONG_REWARD_ID=" in text:
        print(f"Reward exists: {reward_id} (SONG_REWARD_ID already in .env — "
              "check it matches)")
    else:
        env.write_text(text.rstrip("\n") + f"\nSONG_REWARD_ID={reward_id}\n")
        print(f"✅ Reward '{TITLE}' ({COST:,} points) -> {reward_id}, saved to .env")


if __name__ == "__main__":
    main()
