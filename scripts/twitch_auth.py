#!/usr/bin/env python3
"""One-time Twitch OAuth helper (manual paste mode).

Runs the Authorization Code grant flow once so you obtain a *refresh* token (a
token-generator site only gives a short-lived access token with no way to
refresh it). The tokens are written to a store file the bot reads and keeps
refreshed from then on.

Two accounts, both under the same app:
  broadcaster (default) -> howlingaf, Helix scopes  -> .twitch_tokens.json
  bot                   -> hairyrugaf, chat scopes   -> .twitch_bot_tokens.json

This uses the app's registered public redirect URL and asks you to paste the
code back, so the auth can happen in any browser on any machine — handy when the
bot runs on a headless server.

PREREQUISITES:
  - CLIENT_ID and CLIENT_SECRET of YOUR OWN Twitch app are in .env.
  - REDIRECT_URI below is registered under the app's OAuth Redirect URLs.

RUN:
  uv run python scripts/twitch_auth.py            # broadcaster (howlingaf)
  uv run python scripts/twitch_auth.py bot        # bot account (hairyrugaf)

Log the browser into the matching account before authorizing.
"""
import argparse
import json
import os
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

REDIRECT_URI = "https://verify.howling.one/twitch/callback"

ACCOUNTS = {
    "broadcaster": {
        "login": "howlingaf",
        # channel:read:ads is read-only: it exposes next_ad_at and
        # preroll_free_time, so ad timing can follow Twitch's own schedule and
        # the pre-roll bank instead of a local guess.
        "scopes": "channel:edit:commercial channel:read:ads "
                  "moderator:manage:shoutouts channel:bot "
                  "channel:manage:vips moderator:read:followers "
                  # Get Chatters, polled while live for per-viewer presence.
                  "moderator:read:chatters "
                  # Get Broadcaster Subscriptions: the full subscriber list,
                  # including the ones who never speak. Chat badges only ever
                  # show subs who talk.
                  "channel:read:subscriptions",
        "filename": ".twitch_tokens.json",
    },
    "bot": {
        # user:write:chat + user:bot let the app send chat as this account via
        # Helix, which is what earns the native chat-bot badge (IRC gets none).
        "login": "hairyrugaf",
        "scopes": "chat:read chat:edit user:write:chat user:bot",
        "filename": ".twitch_bot_tokens.json",
    },
}

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


def _token_login(access: str) -> str:
    """Ask Twitch which account an access token belongs to."""
    req = urllib.request.Request(
        "https://id.twitch.tv/oauth2/validate",
        headers={"Authorization": f"OAuth {access}"},
    )
    with urllib.request.urlopen(req) as resp:
        return json.loads(resp.read()).get("login", "")


def main():
    parser = argparse.ArgumentParser(description="One-time Twitch OAuth helper.")
    parser.add_argument(
        "account", nargs="?", default="broadcaster", choices=sorted(ACCOUNTS),
        help="Which account to authorize (default: broadcaster).",
    )
    parser.add_argument(
        "--code",
        help="Authorization code (or full redirect URL). Lets the exchange run "
             "non-interactively, e.g. when stdin isn't a TTY.",
    )
    args = parser.parse_args()
    acct = ACCOUNTS[args.account]
    token_path = Path(__file__).resolve().parent.parent / acct["filename"]

    if not CLIENT_ID or not CLIENT_SECRET:
        raise SystemExit("CLIENT_ID and CLIENT_SECRET are required.")

    auth_url = "https://id.twitch.tv/oauth2/authorize?" + urllib.parse.urlencode({
        "client_id": CLIENT_ID,
        "redirect_uri": REDIRECT_URI,
        "response_type": "code",
        "scope": acct["scopes"],
        "force_verify": "true",
    })

    login = acct["login"].upper()
    print(f"\n1) Open this URL in a browser logged in as the {args.account.upper()} "
          f"account ({login}):\n")
    print("   " + auth_url + "\n")
    print("2) Approve the scopes. Twitch redirects to:")
    print("   " + REDIRECT_URI + "?code=<CODE>&scope=...\n")
    print("3) Copy the 'code' value from that address bar (or paste the whole URL).")
    print("   The code is single-use and expires in minutes, so do it promptly.\n")

    if args.code:
        raw = args.code
    else:
        try:
            raw = input("Paste the code (or full redirect URL) here: ")
        except EOFError:
            raise SystemExit(
                "\nNo stdin available (e.g. running non-interactively). Re-run with "
                f"the code:\n  uv run python {Path(__file__).name} {args.account} "
                "--code '<paste the code or full redirect URL>'"
            )
    code = _extract_code(raw.strip())
    if not code:
        raise SystemExit("No code provided.")

    tokens = _exchange_code_for_tokens(code)
    access = tokens["access_token"]
    refresh = tokens["refresh_token"]

    actual_login = _token_login(access)
    if actual_login.lower() != acct["login"].lower():
        raise SystemExit(
            f"\n❌ Authorized as {actual_login!r} but expected {acct['login']!r} — "
            "the browser was logged into the wrong Twitch account. Nothing was "
            f"saved. Log the browser into {acct['login']} (an incognito window "
            "works well) and re-run."
        )

    tmp = token_path.with_suffix(".tmp")
    tmp.write_text(json.dumps({"access_token": access, "refresh_token": refresh}, indent=2))
    tmp.rename(token_path)

    print("\n✅ Success. Wrote access + refresh tokens to:\n  " + str(token_path))
    print("Granted scopes:", " ".join(tokens.get("scope", acct["scopes"].split())))
    print(f"Authorize a different account with:  uv run python {Path(__file__).name} "
          f"{'bot' if args.account == 'broadcaster' else 'broadcaster'}")


if __name__ == "__main__":
    main()
