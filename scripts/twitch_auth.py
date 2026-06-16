#!/usr/bin/env python3
"""One-time Twitch OAuth helper (manual paste mode).

Runs the Authorization Code grant flow once so you obtain a *refresh* token for
the broadcaster account (a token-generator site only gives a short-lived access
token with no way to refresh it). The tokens are written to .twitch_tokens.json,
which the bot reads and keeps refreshed from then on.

This variant uses the app's registered public redirect URL
(https://verify.howling.one/twitch/callback) and asks you to paste the code
back, so the auth can happen in any browser on any machine — handy when the bot
runs on a headless server.

PREREQUISITES:
  - CLIENT_ID and CLIENT_SECRET of YOUR OWN Twitch app are in .env.
  - REDIRECT_URI below is registered under the app's OAuth Redirect URLs.

RUN:
  uv run python scripts/twitch_auth.py

The scopes requested match what the bot actually uses: starting commercials and
sending raid shoutouts.
"""
import json
import os
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

REDIRECT_URI = "https://verify.howling.one/twitch/callback"
SCOPES = "channel:edit:commercial moderator:manage:shoutouts"
TOKEN_PATH = Path(__file__).resolve().parent.parent / ".twitch_tokens.json"

CLIENT_ID = os.getenv("CLIENT_ID") or input("Twitch CLIENT_ID: ").strip()
CLIENT_SECRET = os.getenv("CLIENT_SECRET") or input("Twitch CLIENT_SECRET: ").strip()


def _extract_code(raw: str) -> str:
    """Accept either the bare code or the full redirected URL / query string."""
    raw = raw.strip()
    if "code=" in raw:
        qs = urllib.parse.urlparse(raw).query or raw
        params = urllib.parse.parse_qs(qs)
        if params.get("code"):
            return params["code"][0]
    return raw


def _exchange_code_for_tokens(code: str) -> dict:
    data = urllib.parse.urlencode({
        "client_id": CLIENT_ID,
        "client_secret": CLIENT_SECRET,
        "code": code,
        "grant_type": "authorization_code",
        "redirect_uri": REDIRECT_URI,
    }).encode()
    req = urllib.request.Request("https://id.twitch.tv/oauth2/token", data=data)
    try:
        with urllib.request.urlopen(req) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        body = e.read().decode(errors="replace")
        raise SystemExit(
            f"Token exchange failed (HTTP {e.code}): {body}\n"
            "Common causes: the code was already used/expired (they're single-use "
            "and last only minutes — grab a fresh one), or redirect_uri / client "
            "credentials don't match the app."
        )


def main():
    if not CLIENT_ID or not CLIENT_SECRET:
        raise SystemExit("CLIENT_ID and CLIENT_SECRET are required.")

    auth_url = "https://id.twitch.tv/oauth2/authorize?" + urllib.parse.urlencode({
        "client_id": CLIENT_ID,
        "redirect_uri": REDIRECT_URI,
        "response_type": "code",
        "scope": SCOPES,
        "force_verify": "true",
    })

    print("\n1) Open this URL in a browser logged in as the BROADCASTER (howlingaf):\n")
    print("   " + auth_url + "\n")
    print("2) Approve the scopes. Twitch redirects to:")
    print("   " + REDIRECT_URI + "?code=<CODE>&scope=...\n")
    print("3) Copy the 'code' value from that address bar (or paste the whole URL).")
    print("   The code is single-use and expires in minutes, so do it promptly.\n")

    raw = input("Paste the code (or full redirect URL) here: ").strip()
    code = _extract_code(raw)
    if not code:
        raise SystemExit("No code provided.")

    tokens = _exchange_code_for_tokens(code)
    access = tokens["access_token"]
    refresh = tokens["refresh_token"]

    tmp = TOKEN_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps({"access_token": access, "refresh_token": refresh}, indent=2))
    tmp.rename(TOKEN_PATH)

    print("\n✅ Success. Wrote access + refresh tokens to:\n  " + str(TOKEN_PATH))
    print("Granted scopes:", " ".join(tokens.get("scope", SCOPES.split())))
    print("Keep the refresh token private. To re-authorize later, rerun this script.")


if __name__ == "__main__":
    main()
