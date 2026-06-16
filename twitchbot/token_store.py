"""Persistent store for the Twitch Helix access/refresh tokens.

The access token is short-lived (~4h) and the refresh token can rotate on every
refresh, so both are written back to disk whenever they change. This file is the
source of truth at runtime; .env only seeds the very first run before the store
exists. Written atomically (tmp + rename) so a crash mid-write can't corrupt it.
"""
import json
from pathlib import Path

from .logger import logger

TOKEN_PATH = Path(__file__).resolve().parent.parent / ".twitch_tokens.json"


def load() -> dict:
    try:
        return json.loads(TOKEN_PATH.read_text())
    except FileNotFoundError:
        return {}
    except Exception:
        logger.exception("Could not read token store at %s", TOKEN_PATH)
        return {}


def save(access_token: str, refresh_token: str) -> None:
    tmp = TOKEN_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(
        {"access_token": access_token, "refresh_token": refresh_token},
        indent=2,
    ))
    tmp.rename(TOKEN_PATH)
