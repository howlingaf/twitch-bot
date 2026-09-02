import os

from dotenv import load_dotenv

load_dotenv()

BOT_OAUTH_TOKEN = os.getenv("BOT_OAUTH_TOKEN")
# Optional .env seed for the bot account's chat refresh token; the persisted
# store (.twitch_bot_tokens.json, written by `twitch_auth.py bot`) wins.
BOT_REFRESH_TOKEN = os.getenv("BOT_REFRESH_TOKEN")
CLIENT_ID = os.getenv("CLIENT_ID")
CLIENT_SECRET = os.getenv("CLIENT_SECRET")
ACCESS_TOKEN = os.getenv("ACCESS_TOKEN")
# Optional .env seed for the Helix refresh token; the persisted token store
# (.twitch_tokens.json, written by scripts/twitch_auth.py) takes precedence.
REFRESH_TOKEN = os.getenv("REFRESH_TOKEN")
BROADCASTER_ID = os.getenv("BROADCASTER_ID")
# The channel owner's login. Excluded from the viewer reports: they're present
# for every minute of every stream by definition, so ranking them among the
# regulars only pushes a real viewer off the list.
BROADCASTER_LOGIN = (os.getenv("BROADCASTER_LOGIN") or "howlingaf").lower()
BOT_LOGIN = (os.getenv("BOT_LOGIN") or "hairyrugaf").lower()
# Logins kept out of the viewer reports: accounts that sit in chat for every
# minute of every stream without being viewers. Dropping the support weight
# from the regulars score let these float to the top, since presence is all
# they have. Comma-separated additions go in REPORT_IGNORE.
REPORT_IGNORE = {BROADCASTER_LOGIN, BOT_LOGIN} | {
    x.strip().lower() for x in (os.getenv("REPORT_IGNORE") or "").split(",") if x.strip()}

SPOTIFY_CLIENT_ID = os.getenv("SPOTIFY_CLIENT_ID")
SPOTIFY_CLIENT_SECRET = os.getenv("SPOTIFY_CLIENT_SECRET")
SPOTIFY_REDIRECT_URI = os.getenv("SPOTIFY_REDIRECT_URI")

YOUTUBE_API_KEY = os.getenv("YOUTUBE_API_KEY")

OVERLAY_PORT = int(os.getenv("OVERLAY_PORT", "8765"))

# Per-viewer chat/presence history (see chatstore.py).
CHAT_DB = os.getenv("CHAT_DB", "chat.db")

DISCORD_BOT_URL = (os.getenv("DISCORD_BOT_URL") or "http://127.0.0.1:8787").rstrip("/")
RECAP_SECRET = os.getenv("RECAP_SECRET", "")

# Inbound console API (Discord -> Twitch bot). Disabled unless CONSOLE_SECRET is
# set. Bound to localhost only; the Discord bot on the same VPS calls it.
CONSOLE_SECRET = os.getenv("CONSOLE_SECRET", "")
CONSOLE_PORT = int(os.getenv("CONSOLE_PORT", "8788"))
