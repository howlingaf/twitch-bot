"""Persistent store for Twitch access/refresh tokens.

Access tokens are short-lived (~4h) and refresh tokens can rotate on every
refresh, so both are written back to disk whenever they change. These files are
the source of truth at runtime; .env only seeds the very first run before a
store exists. Written atomically (tmp + rename) so a crash mid-write can't
corrupt them.

Two independent token sets are kept, both issued under the same app:
  - HELIX_TOKEN_PATH: the broadcaster (howlingaf) token used for Helix API calls
  - BOT_TOKEN_PATH:   the bot account (hairyrugaf) token used for the chat IRC
"""
import json
from pathlib import Path

from .logger import logger

_BASE = Path(__file__).resolve().parent.parent
HELIX_TOKEN_PATH = _BASE / ".twitch_tokens.json"
BOT_TOKEN_PATH = _BASE / ".twitch_bot_tokens.json"


def load(path: Path = HELIX_TOKEN_PATH) -> dict:
    try:
        return json.loads(path.read_text())
    except FileNotFoundError:
        return {}
    except Exception:
        logger.exception("Could not read token store at %s", path)
        return {}


def save(access_token: str, refresh_token: str, path: Path = HELIX_TOKEN_PATH) -> None:
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(
        {"access_token": access_token, "refresh_token": refresh_token},
        indent=2,
    ))
    tmp.rename(path)
