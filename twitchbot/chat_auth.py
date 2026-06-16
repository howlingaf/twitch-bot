"""Refresh handling for the bot account's chat (IRC) token.

twitchio holds the chat token at bot._connection._token and only validates it on
the first connect — every reconnect replays it verbatim. So an expired token
isn't noticed until a reconnect, where it fails to log in. We handle this two
ways:

  - ensure_fresh(): called at startup, hands twitchio a freshly refreshed token
    so each process start (incl. systemd restarts) begins valid.
  - chat_token_refresh_loop(): refreshes ahead of expiry and writes the new
    token back into bot._connection._token, so any later reconnect carries a
    valid token without a disruptive crash/restart.

Both the chat token and the Helix token are issued under the same app, so the
client id/secret are shared.
"""
import asyncio

from .config import BOT_OAUTH_TOKEN, BOT_REFRESH_TOKEN, CLIENT_ID, CLIENT_SECRET
from . import oauth, token_store
from .logger import logger

# Refresh this many seconds before the token actually expires.
_REFRESH_MARGIN = 600


class ChatTokenManager:
    """Owns the bot account's access/refresh tokens and refreshes them."""

    def __init__(self):
        stored = token_store.load(token_store.BOT_TOKEN_PATH)
        self.access_token = stored.get("access_token") or BOT_OAUTH_TOKEN
        self.refresh_token = stored.get("refresh_token") or BOT_REFRESH_TOKEN

    async def refresh(self) -> str | None:
        """Refresh the chat token and persist it. Returns the new token or None."""
        data = await oauth.refresh_tokens(self.refresh_token, CLIENT_ID, CLIENT_SECRET)
        if not data:
            return None
        self.access_token = data["access_token"]
        self.refresh_token = data.get("refresh_token", self.refresh_token)
        token_store.save(self.access_token, self.refresh_token, token_store.BOT_TOKEN_PATH)
        logger.info("Refreshed Twitch chat (bot) access token.")
        return self.access_token

    async def ensure_fresh(self) -> str | None:
        """Return a valid access token, refreshing first if needed.

        Falls back to the current (possibly static .env) token if no refresh
        token is configured yet, so the bot still starts before the bot account
        has been authorized via scripts/twitch_auth.py.
        """
        info = await oauth.validate(self.access_token)
        if info and info.get("expires_in", 0) > _REFRESH_MARGIN:
            logger.info(
                "Chat token valid for %s (expires in %ss).",
                info.get("login"), info.get("expires_in"),
            )
            return self.access_token

        if not self.refresh_token:
            logger.warning(
                "Chat token is missing/expiring and no bot refresh token is set. "
                "Run: uv run python scripts/twitch_auth.py bot  (logged in as the bot). "
                "Using the existing token as-is for now."
            )
            return self.access_token

        return await self.refresh() or self.access_token


async def chat_token_refresh_loop(bot, manager: "ChatTokenManager"):
    """Periodically refresh the chat token and propagate it into twitchio."""
    logger.info("Chat token refresh loop started.")
    while True:
        try:
            info = await oauth.validate(manager.access_token)
            expires_in = info.get("expires_in", 3600) if info else 0
            sleep_for = max(60, expires_in - _REFRESH_MARGIN)
            await asyncio.sleep(sleep_for)

            if not manager.refresh_token:
                # Nothing to refresh with yet; re-check on the next cycle.
                continue

            new_token = await manager.refresh()
            if new_token:
                # Update twitchio's in-memory token so the NEXT reconnect uses
                # it. The live connection is unaffected (Twitch doesn't drop an
                # established session on expiry).
                conn = getattr(bot, "_connection", None)
                if conn is not None and hasattr(conn, "_token"):
                    conn._token = new_token
                    logger.info("Propagated refreshed chat token to the connection.")
                else:
                    logger.warning(
                        "Could not find bot._connection._token to update; "
                        "reconnects may still use the old token."
                    )
        except asyncio.CancelledError:
            logger.info("Chat token refresh loop cancelled.")
            break
        except Exception:
            logger.exception("Error in chat token refresh loop")
            await asyncio.sleep(300)
