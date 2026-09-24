#!/usr/bin/env python3
"""Create the song/album channel-point rewards and record their ids in .env.

Run once per reward, after the broadcaster token has the
channel:manage:redemptions scope. Created via the API on purpose: Twitch only
lets the creating app update a reward's redemptions, and the bot refunds
failed requests. Re-running is safe — existing rewards are found, not duped.
"""

import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from twitchbot import twitch_api  # noqa: E402

REWARDS = [
    ("SONG_REWARD_ID", "Track Review", 15_000,
     "Paste in a song link (Spotify / Apple Music / YouTube / Tidal) and "
     "I'll pull out the IEMs and give you a brutally honest review"),
    ("ALBUM_REWARD_ID", "Album Review", 50_000,
     "Paste in an album link (Spotify / Apple Music / Tidal) and I'll pull "
     "out the IEMs and give you a brutally honest review"),
]


def main():
    env = ROOT / ".env"
    text = env.read_text()
    for var, title, cost, prompt in REWARDS:
        if f"{var}=" in text:
            print(f"{var} already set; skipping '{title}'.")
            continue
        reward_id = asyncio.run(twitch_api.create_custom_reward(title, cost, prompt))
        if not reward_id:
            sys.exit(f"Could not create '{title}' — is channel:manage:redemptions "
                     "on the broadcaster token?")
        text = text.rstrip("\n") + f"\n{var}={reward_id}\n"
        env.write_text(text)
        print(f"✅ Reward '{title}' ({cost:,} points) -> {reward_id}, saved to .env")


if __name__ == "__main__":
    main()
