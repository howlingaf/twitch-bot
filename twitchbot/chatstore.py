"""Persistent record of who was in chat and what they said.

Twitch keeps no per-viewer history you can query later — no emote counts, no
watch time per user, no message archive beyond the VOD's chat replay — so the
only way to know who the regulars are is to keep the record ourselves.

Four tables, all keyed by stream:

  streams   one row per broadcast (Helix stream id, title, start/end)
  messages  every chat line: who, when, what, which Twitch emotes it carried,
            and the sender's badges at the time
  presence  one row per (minute, user) from polling Get Chatters while live —
            the closest thing to watch time Twitch exposes (logged-in
            viewers with chat open; lurkers on the embed don't show)
  events    subs, resubs, gifts, bits, raids — the "support" signals that
            arrive as USERNOTICE / bits tags rather than as chat text, plus
            follows, which arrive nowhere and are polled at stream end

SQLite via the stdlib, WAL mode, one autocommit connection. Writes are
single-row inserts on the chat path and one transaction per presence poll,
both sub-millisecond, so they run inline on the event loop.
"""

import json
import sqlite3
import time
from pathlib import Path

from .logger import logger

_SCHEMA = """
CREATE TABLE IF NOT EXISTS streams (
    id          TEXT PRIMARY KEY,   -- Helix stream id, or local-<epoch> if unknown
    started_at  TEXT NOT NULL,      -- ISO-8601 UTC
    ended_at    TEXT,
    title       TEXT,
    game        TEXT
);

CREATE TABLE IF NOT EXISTS messages (
    id          TEXT PRIMARY KEY,   -- Twitch message id (dedups VOD backfill)
    ts          INTEGER NOT NULL,   -- unix seconds
    stream_id   TEXT NOT NULL,
    user_id     TEXT,
    login       TEXT NOT NULL,
    display     TEXT,
    content     TEXT NOT NULL,
    emotes      TEXT NOT NULL,      -- JSON list of Twitch emote names in the message
    is_sub      INTEGER NOT NULL DEFAULT 0,
    is_mod      INTEGER NOT NULL DEFAULT 0,
    is_vip      INTEGER NOT NULL DEFAULT 0,
    is_first    INTEGER NOT NULL DEFAULT 0,   -- first-time chatter in the channel
    source      TEXT NOT NULL DEFAULT 'live'  -- 'live' or 'vod' (backfilled)
);
CREATE INDEX IF NOT EXISTS messages_stream_login ON messages (stream_id, login);
CREATE INDEX IF NOT EXISTS messages_login_ts ON messages (login, ts);

CREATE TABLE IF NOT EXISTS presence (
    minute      INTEGER NOT NULL,   -- unix seconds, floored to the minute
    stream_id   TEXT NOT NULL,
    user_id     TEXT NOT NULL,
    login       TEXT NOT NULL,
    PRIMARY KEY (minute, user_id)
);
CREATE INDEX IF NOT EXISTS presence_stream_login ON presence (stream_id, login);

CREATE TABLE IF NOT EXISTS events (
    id          INTEGER PRIMARY KEY,
    ts          INTEGER NOT NULL,
    stream_id   TEXT NOT NULL,
    kind        TEXT NOT NULL,      -- sub | resub | subgift | submysterygift | bits | raid | follow
    user_id     TEXT,
    login       TEXT NOT NULL,
    amount      INTEGER NOT NULL DEFAULT 0,  -- months / gifts / bits / raid viewers
    tier        TEXT,               -- 1000 / 2000 / 3000 / Prime for sub kinds
    detail      TEXT                -- recipient login for a gift, resub message, ...
);
CREATE INDEX IF NOT EXISTS events_login ON events (login);
"""


def parse_emotes(content: str, emotes_tag: str | None) -> list[str]:
    """Emote names from an IRC `emotes` tag ("id:0-4,6-10/id2:12-15").

    Twitch sends positions, not names; the name is the slice of the message.
    Positions index code points, which is what Python str indexing does.
    """
    if not emotes_tag:
        return []
    names = []
    for entry in emotes_tag.split("/"):
        _, _, spans = entry.partition(":")
        for span in spans.split(","):
            start, _, end = span.partition("-")
            try:
                names.append(content[int(start):int(end) + 1])
            except ValueError:
                continue
    return names


class ChatStore:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.db = sqlite3.connect(self.path, isolation_level=None)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=NORMAL")
        self.db.executescript(_SCHEMA)
        logger.info("Chat store open at %s", self.path)

    # -------- streams --------
    def start_stream(self, stream_id: str, started_at: str,
                     title: str = "", game: str = "") -> None:
        self.db.execute(
            "INSERT INTO streams (id, started_at, title, game) VALUES (?, ?, ?, ?) "
            "ON CONFLICT(id) DO UPDATE SET title=excluded.title, game=excluded.game",
            (stream_id, started_at, title, game),
        )

    def end_stream(self, stream_id: str, ended_at: str) -> None:
        self.db.execute(
            "UPDATE streams SET ended_at = ? WHERE id = ? AND ended_at IS NULL",
            (ended_at, stream_id),
        )

    # -------- messages --------
    def add_message(self, *, id: str, ts: int, stream_id: str, user_id: str | None,
                    login: str, display: str | None, content: str,
                    emotes: list[str], is_sub=False, is_mod=False, is_vip=False,
                    is_first=False, source: str = "live") -> None:
        self.db.execute(
            "INSERT OR IGNORE INTO messages (id, ts, stream_id, user_id, login, "
            "display, content, emotes, is_sub, is_mod, is_vip, is_first, source) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (id, ts, stream_id, user_id, login.lower(), display, content,
             json.dumps(emotes), is_sub, is_mod, is_vip, is_first, source),
        )

    # -------- presence --------
    def add_presence(self, stream_id: str, chatters: list[tuple[str, str]]) -> None:
        """Record every (user_id, login) in `chatters` as present this minute."""
        minute = int(time.time()) // 60 * 60
        self.db.execute("BEGIN")
        self.db.executemany(
            "INSERT OR IGNORE INTO presence (minute, stream_id, user_id, login) "
            "VALUES (?, ?, ?, ?)",
            [(minute, stream_id, uid, login.lower()) for uid, login in chatters],
        )
        self.db.execute("COMMIT")

    # -------- events --------
    def add_event(self, *, ts: int, stream_id: str, kind: str, user_id: str | None,
                  login: str, amount: int = 0, tier: str | None = None,
                  detail: str | None = None) -> None:
        self.db.execute(
            "INSERT INTO events (ts, stream_id, kind, user_id, login, amount, tier, detail) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (ts, stream_id, kind, user_id, login.lower(), int(amount or 0), tier, detail),
        )

    def has_event(self, stream_id: str, kind: str, login: str) -> bool:
        """Whether this person already has an event of this kind on this stream.

        Follows are polled rather than pushed, so the same follow can be seen
        twice (a retry, a re-run); the events table has no natural key to lean on.
        """
        return bool(self.db.execute(
            "SELECT 1 FROM events WHERE stream_id=? AND kind=? AND login=? LIMIT 1",
            (stream_id, kind, login.lower())).fetchone())

    def close(self) -> None:
        self.db.close()
