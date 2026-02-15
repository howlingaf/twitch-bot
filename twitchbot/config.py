import os

from dotenv import load_dotenv

load_dotenv()

BOT_OAUTH_TOKEN = os.getenv("BOT_OAUTH_TOKEN")
CLIENT_ID = os.getenv("CLIENT_ID")
ACCESS_TOKEN = os.getenv("ACCESS_TOKEN")
BROADCASTER_ID = os.getenv("BROADCASTER_ID")

SPOTIFY_CLIENT_ID = os.getenv("SPOTIFY_CLIENT_ID")
SPOTIFY_CLIENT_SECRET = os.getenv("SPOTIFY_CLIENT_SECRET")
SPOTIFY_REDIRECT_URI = os.getenv("SPOTIFY_REDIRECT_URI")

YOUTUBE_API_KEY = os.getenv("YOUTUBE_API_KEY")

OVERLAY_PORT = int(os.getenv("OVERLAY_PORT", "8765"))

DISCORD_BOT_URL = (os.getenv("DISCORD_BOT_URL") or "http://127.0.0.1:8787").rstrip("/")
RECAP_SECRET = os.getenv("RECAP_SECRET", "")
