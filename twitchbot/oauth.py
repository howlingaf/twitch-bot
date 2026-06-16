"""Shared Twitch OAuth primitives: refresh-token grant and token validation.

Both the Helix (broadcaster) credentials and the chat (bot account) credentials
are issued under the same app, so they share these helpers — only the token
values differ.
"""
import json

import aiohttp

from .logger import logger

_TOKEN_URL = "https://id.twitch.tv/oauth2/token"
_VALIDATE_URL = "https://id.twitch.tv/oauth2/validate"


async def refresh_tokens(refresh_token: str, client_id: str, client_secret: str) -> dict | None:
    """Exchange a refresh token for a fresh token set.

    Returns Twitch's JSON response (access_token, refresh_token, expires_in, …)
    or None on any failure.
    """
    if not refresh_token or not client_secret:
        logger.error(
            "Cannot refresh Twitch token: missing refresh token or CLIENT_SECRET. "
            "Re-run scripts/twitch_auth.py."
        )
        return None

    async with aiohttp.ClientSession() as session:
        async with session.post(
            _TOKEN_URL,
            data={
                "grant_type": "refresh_token",
                "refresh_token": refresh_token,
                "client_id": client_id,
                "client_secret": client_secret,
            },
        ) as resp:
            body = await resp.text()
            if resp.status != 200:
                logger.error("Twitch token refresh failed. HTTP %s: %s", resp.status, body)
                return None
            return json.loads(body)


async def validate(access_token: str) -> dict | None:
    """Return the validation payload (login, expires_in, scopes, …) or None."""
    if not access_token:
        return None
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(
                _VALIDATE_URL,
                headers={"Authorization": f"OAuth {access_token}"},
            ) as resp:
                if resp.status != 200:
                    return None
                return json.loads(await resp.text())
    except aiohttp.ClientError as e:
        logger.error("Token validation request failed: %s", e)
        return None
