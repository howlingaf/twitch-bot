"""Running totals about the rest of the category, for finding people to network with.

The anonymous reader in emotewatch.py already sees every live channel in our
category: its chat, and every 3 minutes a Helix listing of who is live with
how many viewers. This keeps what that is worth knowing over time:

  category_streams      one row per broadcast seen: when it ran, its peak and
                        average viewers, its title -- schedule and size
  category_chat_daily   per channel per day: messages, distinct chatters, how
                        often the streamer types, and how many messages touch
                        systems topics or AI -- what the channel is about and
                        how conversational it is
  category_regulars     per channel per day: messages from each of OUR
                        regulars -- where our people already are, i.e. who
                        we have a natural introduction to

Totals only. Nobody's message text is stored here: counting is all the
questions need, and a copy of ~200 channels' conversations isn't something to
keep lying around.

Counts accumulate in memory and are written every few minutes as cumulative
rows for the day, so a message costs a dict update, not a database write.
After a restart the day's rows are read back and added to, except distinct
chatters, which can only be recounted from that point -- the stored figure
is kept if it's higher.
"""

import re
import time
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone

from .config import BOT_LOGIN, BROADCASTER_LOGIN
from .logger import logger

_CST = timezone(timedelta(hours=-5))
FLUSH_SECONDS = 300

SYSTEMS = re.compile(
    r"\b(c\+\+|cpp|rust|zig|odin|jai|assembly|asm|x86|arm64|risc-?v|kernel|compiler|interpreter|"
    r"allocator|malloc|pointers?|segfault|simd|emulator|bytecode|gdb|valgrind|syscalls?|"
    r"embedded|firmware|fpga|llvm|linker|lexer|parser|vulkan|opengl|raylib|bare.?metal|"
    r"k&r|in c|c code|memory leak|stack frame|cache miss)\b", re.I)
AI = re.compile(
    r"\b(ai|llms?|claude|chatgpt|gpt-?\d?|copilot|vibe.?cod\w*|openai|anthropic|gemini|codex|"
    r"agentic|ai agents?|prompt engineer\w*)\b", re.I)

SCHEMA = """
CREATE TABLE IF NOT EXISTS category_streams (
    login        TEXT NOT NULL,
    started_at   TEXT NOT NULL,      -- Helix started_at, ISO-8601 UTC
    last_seen    INTEGER NOT NULL,   -- unix seconds of the last poll it was live in
    title        TEXT,
    peak_viewers INTEGER NOT NULL DEFAULT 0,
    viewer_sum   INTEGER NOT NULL DEFAULT 0,
    samples      INTEGER NOT NULL DEFAULT 0,   -- avg viewers = viewer_sum / samples
    PRIMARY KEY (login, started_at)
);
CREATE TABLE IF NOT EXISTS category_chat_daily (
    login          TEXT NOT NULL,
    day            TEXT NOT NULL,    -- YYYY-MM-DD, US Central
    msgs           INTEGER NOT NULL DEFAULT 0,
    chatters       INTEGER NOT NULL DEFAULT 0,
    streamer_msgs  INTEGER NOT NULL DEFAULT 0,
    systems_msgs   INTEGER NOT NULL DEFAULT 0,
    ai_msgs        INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (login, day)
);
CREATE TABLE IF NOT EXISTS category_regulars (
    login    TEXT NOT NULL,          -- whose channel
    day      TEXT NOT NULL,
    regular  TEXT NOT NULL,          -- which of our regulars
    msgs     INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (login, day, regular)
);
"""


def _day(ts: float) -> str:
    return datetime.fromtimestamp(ts, _CST).strftime("%Y-%m-%d")


class CategoryStats:
    def __init__(self, store):
        self.db = store.db
        self.db.executescript(SCHEMA)
        self._regulars: set[str] = set()
        self._regulars_at = 0.0
        self._chat: dict[tuple[str, str], dict] = {}
        self._chatters: dict[tuple[str, str], set[str]] = defaultdict(set)
        self._regs: Counter = Counter()          # (channel, day, regular) -> msgs
        self._last_flush = time.time()

    # ---- who counts as one of ours ----
    def regulars(self) -> set[str]:
        """People who chatted in our channel on at least 10 different days in
        the last 120. Recomputed daily, so newcomers graduate into it."""
        if time.time() - self._regulars_at > 86400:
            since = int(time.time()) - 120 * 86400
            self._regulars = {l for (l,) in self.db.execute(
                "SELECT login FROM messages WHERE ts >= ? AND login NOT IN (?, ?) "
                "GROUP BY login HAVING COUNT(DISTINCT date(ts, 'unixepoch', '-5 hours')) >= 10",
                (since, BROADCASTER_LOGIN.lower(), BOT_LOGIN.lower()))}
            self._regulars_at = time.time()
            logger.info("Category stats: tracking %d regulars across the category.", len(self._regulars))
        return self._regulars

    # ---- chat ----
    def on_message(self, channel: str, login: str, text: str) -> None:
        channel, login = channel.lower(), login.lower()
        if channel == BROADCASTER_LOGIN.lower():
            return      # our own chat is recorded properly elsewhere
        key = (channel, _day(time.time()))
        row = self._chat.get(key)
        if row is None:
            row = self._chat[key] = self._load(key)
        row["msgs"] += 1
        self._chatters[key].add(login)
        if login == channel:
            row["streamer_msgs"] += 1
        if SYSTEMS.search(text):
            row["systems_msgs"] += 1
        if AI.search(text):
            row["ai_msgs"] += 1
        if login in self.regulars():
            self._regs[(channel, key[1], login)] += 1
        if time.time() - self._last_flush >= FLUSH_SECONDS:
            self.flush()

    def _load(self, key: tuple[str, str]) -> dict:
        r = self.db.execute(
            "SELECT msgs, chatters, streamer_msgs, systems_msgs, ai_msgs FROM category_chat_daily "
            "WHERE login=? AND day=?", key).fetchone()
        base = dict(zip(("msgs", "chatters", "streamer_msgs", "systems_msgs", "ai_msgs"), r or (0,) * 5))
        base["stored_chatters"] = base.pop("chatters")
        return base

    def flush(self) -> None:
        self._last_flush = time.time()
        try:
            self.db.execute("BEGIN")
            for (channel, day), row in self._chat.items():
                chatters = max(row["stored_chatters"], len(self._chatters[(channel, day)]))
                self.db.execute(
                    "INSERT OR REPLACE INTO category_chat_daily VALUES (?,?,?,?,?,?,?)",
                    (channel, day, row["msgs"], chatters, row["streamer_msgs"],
                     row["systems_msgs"], row["ai_msgs"]))
            for (channel, day, regular), n in self._regs.items():
                self.db.execute(
                    "INSERT INTO category_regulars VALUES (?,?,?,?) "
                    "ON CONFLICT(login, day, regular) DO UPDATE SET msgs = msgs + excluded.msgs",
                    (channel, day, regular, n))
            self.db.execute("COMMIT")
            self._regs.clear()
        except Exception:
            self.db.execute("ROLLBACK")
            logger.exception("Category stats flush failed")
        # Yesterday's rows are final once written; drop them from memory.
        today = _day(time.time())
        for key in [k for k in self._chat if k[1] != today]:
            self._chat.pop(key, None)
            self._chatters.pop(key, None)

    # ---- schedule and size, from the category poll ----
    def on_poll(self, streams: list[dict]) -> None:
        now = int(time.time())
        try:
            self.db.execute("BEGIN")
            for s in streams:
                self.db.execute(
                    "INSERT INTO category_streams (login, started_at, last_seen, title, peak_viewers, "
                    "viewer_sum, samples) VALUES (?,?,?,?,?,?,1) "
                    "ON CONFLICT(login, started_at) DO UPDATE SET last_seen=excluded.last_seen, "
                    "title=excluded.title, peak_viewers=MAX(peak_viewers, excluded.peak_viewers), "
                    "viewer_sum=viewer_sum + excluded.viewer_sum, samples=samples + 1",
                    (s["user_login"].lower(), s["started_at"], now, s.get("title", ""),
                     s.get("viewer_count", 0), s.get("viewer_count", 0)))
            self.db.execute("COMMIT")
        except Exception:
            self.db.execute("ROLLBACK")
            logger.exception("Category stats poll write failed")
