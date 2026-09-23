#!/usr/bin/env python3
"""Authorize Spotify for the bot (writes .spotify_cache).

Needed once, and again whenever the scope in config.SPOTIFY_SCOPE grows —
spotipy won't upgrade a cached token's scope on its own, and the bot runs
headless so it can't complete the browser flow itself.

  1) uv run python3 scripts/spotify_auth.py          # prints the URL
  2) approve in a browser logged into the Spotify account the stream uses
  3) uv run python3 scripts/spotify_auth.py --code '<full redirect URL>'
"""

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from spotipy.oauth2 import SpotifyOAuth  # noqa: E402

from twitchbot.config import (  # noqa: E402
    SPOTIFY_CLIENT_ID, SPOTIFY_CLIENT_SECRET, SPOTIFY_REDIRECT_URI, SPOTIFY_SCOPE,
)


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--code", help="the code, or the full redirect URL to paste")
    a = ap.parse_args()

    oauth = SpotifyOAuth(
        client_id=SPOTIFY_CLIENT_ID,
        client_secret=SPOTIFY_CLIENT_SECRET,
        redirect_uri=SPOTIFY_REDIRECT_URI,
        scope=SPOTIFY_SCOPE,
        cache_path=str(ROOT / ".spotify_cache"),
        open_browser=False,
    )
    if not a.code:
        print("1) Open this URL in a browser logged into the stream's Spotify:\n")
        print("   " + oauth.get_authorize_url() + "\n")
        print("2) Approve. Spotify redirects to your SPOTIFY_REDIRECT_URI.")
        print("3) Re-run with the address bar's URL:")
        print("   uv run python3 scripts/spotify_auth.py --code '<redirect URL>'")
        return
    code = oauth.parse_response_code(a.code)
    token = oauth.get_access_token(code, as_dict=True)
    print("✅ Success. Wrote", ROOT / ".spotify_cache")
    print("Granted scope:", token.get("scope"))


if __name__ == "__main__":
    main()
