"""Reports over the chat store: who the regulars are, what chat uses.

Every report takes an open sqlite3 connection and returns plain text, so the
same code serves scripts/viewer_stats.py on the box and the `viewers`
console command in Discord. Keep output monospace-friendly: it's shown in a
code block either way.
"""

import json
import re
from collections import Counter, defaultdict
from datetime import datetime, timezone

_WORD = re.compile(r"\S+")

SEVENTV_SET_URL = "https://7tv.io/v3/users/twitch/{broadcaster_id}"


def seventv_names(payload: dict) -> set[str]:
    """Emote names from a 7TV /v3/users/twitch/<id> response."""
    return {e["name"] for e in payload.get("emote_set", {}).get("emotes", [])}


def since_ts(s: str | None) -> int:
    if not s:
        return 0
    return int(datetime.fromisoformat(s).replace(tzinfo=timezone.utc).timestamp())


def _iso(ts: int) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).isoformat()


def _day(ts) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%d") if ts else "-"


def _table(rows, headers) -> str:
    if not rows:
        return "(nothing yet)"
    widths = [max(len(str(h)), *(len(str(r[i])) for r in rows)) for i, h in enumerate(headers)]
    lines = ["  ".join(str(h).ljust(w) for h, w in zip(headers, widths)).rstrip()]
    lines += ["  ".join(str(c).ljust(w) for c, w in zip(r, widths)).rstrip() for r in rows]
    return "\n".join(lines)


def _streams(db, since: int) -> list[tuple[str, int, int]]:
    """(id, start_ts, duration_minutes) for every stream in the window, oldest
    first. A stream still running (or one whose end was never recorded) is
    measured to its last presence poll or message."""
    out = []
    for sid, started, ended in db.execute(
        "SELECT id, started_at, ended_at FROM streams WHERE started_at >= ? "
        "ORDER BY started_at", (_iso(since),)
    ):
        start = since_ts(started.replace("Z", "+00:00"))
        if ended:
            end = since_ts(ended.replace("Z", "+00:00"))
        else:
            end = max(
                db.execute("SELECT COALESCE(MAX(minute), 0) FROM presence WHERE stream_id = ?",
                           (sid,)).fetchone()[0],
                db.execute("SELECT COALESCE(MAX(ts), 0) FROM messages WHERE stream_id = ?",
                           (sid,)).fetchone()[0],
                start,
            )
        out.append((sid, start, max(1, (end - start) // 60)))
    return out


# --------------------------------------------------------------------------
# regulars
# --------------------------------------------------------------------------
# One number per viewer, 0-100, from what the store knows. Weights are a
# judgement call, written down so they can be argued with:
#
#   attendance  50   streams they showed up to / streams held
#   stay        20   of the streams they attended, how much of each they stayed
#   chat        15   messages per attended stream, saturating at CHAT_SATURATE
#   support     15   sub badge, or any sub / gift / bits / raid event
#
# then scaled by recency: halved for every RECENCY_HALF_LIFE streams missed
# since they were last seen, so a former regular fades instead of squatting
# on the top of the list forever.
CHAT_SATURATE = 10
RECENCY_HALF_LIFE = 4


def regulars(db, since: int = 0, top: int = 25) -> str:
    streams = _streams(db, since)
    if not streams:
        return "(no streams recorded yet)"
    n_streams = len(streams)
    index = {sid: i for i, (sid, _, _) in enumerate(streams)}
    duration = {sid: mins for sid, _, mins in streams}

    minutes = defaultdict(lambda: defaultdict(int))   # login -> stream -> minutes
    for login, sid, m in db.execute(
        "SELECT login, stream_id, COUNT(*) FROM presence WHERE minute >= ? "
        "GROUP BY login, stream_id", (since,)
    ):
        if sid in index:
            minutes[login][sid] = m

    msgs = defaultdict(lambda: defaultdict(int))
    subs = set()
    for login, sid, c, sub in db.execute(
        "SELECT login, stream_id, COUNT(*), MAX(is_sub) FROM messages "
        "WHERE ts >= ? AND stream_id != '' GROUP BY login, stream_id", (since,)
    ):
        if sid in index:
            msgs[login][sid] = c
        if sub:
            subs.add(login)

    supporters = {login for (login,) in db.execute(
        "SELECT DISTINCT login FROM events WHERE ts >= ?", (since,))}

    rows = []
    for login in set(minutes) | set(msgs):
        attended = set(minutes[login]) | set(msgs[login])
        attendance = len(attended) / n_streams
        # Stay is only measurable where presence ran; a stream known from
        # messages alone (VOD backfill, pre-scope) counts as unknown, not 0.
        stays = [min(1.0, minutes[login][s] / duration[s]) for s in minutes[login]]
        stay = sum(stays) / len(stays) if stays else 0.0
        total_msgs = sum(msgs[login].values())
        chat = min(1.0, (total_msgs / len(attended)) / CHAT_SATURATE)
        support = 1.0 if (login in subs or login in supporters) else 0.0
        last_idx = max(index[s] for s in attended)
        missed = n_streams - 1 - last_idx
        recency = 0.5 ** (missed / RECENCY_HALF_LIFE)
        score = (50 * attendance + 20 * stay + 15 * chat + 15 * support) * recency
        rows.append((
            round(score), login, f"{len(attended)}/{n_streams}",
            f"{int(stay * 100)}%" if stays else "-",
            f"{sum(minutes[login].values()) / 60:.1f}",
            total_msgs,
            "sub" if login in subs else ("supporter" if login in supporters else ""),
            _day(streams[last_idx][1]),
        ))
    rows.sort(key=lambda r: (-r[0], r[1]))
    head = (f"Regulars — {n_streams} stream(s)"
            f"{' since ' + _day(since) if since else ''}, top {top}\n"
            f"score = 50·attendance + 20·stay + 15·chat + 15·support, "
            f"halved per {RECENCY_HALF_LIFE} streams missed\n")
    return head + "\n" + _table(rows[:top], (
        "score", "viewer", "streams", "stay", "hours", "msgs", "", "last seen"))


# --------------------------------------------------------------------------
# viewers (raw leaderboard)
# --------------------------------------------------------------------------
def viewers(db, since: int = 0, top: int = 25) -> str:
    total = len(_streams(db, since)) or 1
    rows = db.execute(
        """
        WITH m AS (
            SELECT login, COUNT(*) AS msgs, COUNT(DISTINCT stream_id) AS chat_streams,
                   MIN(ts) AS first_ts, MAX(ts) AS last_ts, MAX(is_sub) AS sub
            FROM messages WHERE ts >= ? AND stream_id != '' GROUP BY login
        ),
        p AS (
            SELECT login, COUNT(*) AS minutes, COUNT(DISTINCT stream_id) AS seen
            FROM presence WHERE minute >= ? GROUP BY login
        )
        SELECT COALESCE(m.login, p.login), COALESCE(p.seen, m.chat_streams, 0),
               COALESCE(p.minutes, 0), COALESCE(m.msgs, 0), m.first_ts, m.last_ts,
               COALESCE(m.sub, 0)
        FROM m LEFT JOIN p ON p.login = m.login
        UNION
        SELECT p.login, p.seen, p.minutes, 0, NULL, NULL, 0
        FROM p LEFT JOIN m ON m.login = p.login WHERE m.login IS NULL
        ORDER BY 2 DESC, 3 DESC, 4 DESC LIMIT ?
        """, (since, since, top),
    ).fetchall()
    out = [(login, f"{st}/{total}", f"{mins / 60:.1f}", n,
            f"{n / st:.1f}" if st else "-", _day(first), _day(last), "sub" if sub else "")
           for login, st, mins, n, first, last, sub in rows]
    return (f"Top {top} viewers across {total} stream(s)\n\n"
            + _table(out, ("viewer", "streams", "hours", "msgs", "msgs/stream",
                           "first chat", "last chat", "")))


# --------------------------------------------------------------------------
# emotes
# --------------------------------------------------------------------------
def emotes(db, seventv: set[str], since: int = 0, top: int = 25) -> str:
    twitch, sev, by_user = Counter(), Counter(), Counter()
    for login, content, em in db.execute(
        "SELECT login, content, emotes FROM messages WHERE ts >= ?", (since,)
    ):
        names = json.loads(em)
        hits = [w for w in _WORD.findall(content) if w in seventv]
        twitch.update(names)
        sev.update(hits)
        by_user[login] += len(names) + len(hits)
    return "\n\n".join([
        f"Twitch emotes (top {top})\n\n" + _table(twitch.most_common(top), ("emote", "uses")),
        f"7TV emotes (top {top}, {len(seventv)} in set)\n\n"
        + _table(sev.most_common(top), ("emote", "uses")),
        f"Heaviest emote users (top {top})\n\n" + _table(by_user.most_common(top), ("viewer", "emotes")),
    ])


# --------------------------------------------------------------------------
# streams
# --------------------------------------------------------------------------
def streams(db, since: int = 0, top: int = 25) -> str:
    rows = db.execute(
        """
        SELECT s.id, substr(s.started_at, 1, 16), s.game, s.title,
               (SELECT COUNT(*) FROM messages m WHERE m.stream_id = s.id),
               (SELECT COUNT(DISTINCT login) FROM messages m WHERE m.stream_id = s.id),
               (SELECT COUNT(DISTINCT login) FROM presence p WHERE p.stream_id = s.id),
               (SELECT COUNT(*) FROM messages m WHERE m.stream_id = s.id AND is_first),
               (SELECT COUNT(*) FROM events e WHERE e.stream_id = s.id)
        FROM streams s WHERE s.started_at >= ? ORDER BY s.started_at DESC LIMIT ?
        """, (_iso(since), top),
    ).fetchall()
    return _table(
        [(i, st, g or "", (t or "")[:36], m, c, p, n, e) for i, st, g, t, m, c, p, n, e in rows],
        ("stream", "started", "game", "title", "msgs", "chatters", "present", "new", "events"))


# --------------------------------------------------------------------------
# user
# --------------------------------------------------------------------------
def user(db, login: str, since: int = 0, top: int = 25) -> str:
    login = login.lower().lstrip("@")
    rows = db.execute(
        """
        SELECT s.id, substr(s.started_at, 1, 10),
               (SELECT COUNT(*) FROM messages m WHERE m.stream_id = s.id AND m.login = ?),
               (SELECT COUNT(*) FROM presence p WHERE p.stream_id = s.id AND p.login = ?)
        FROM streams s WHERE s.started_at >= ? ORDER BY s.started_at DESC LIMIT ?
        """, (login, login, _iso(since), top),
    ).fetchall()
    rows = [r for r in rows if r[2] or r[3]]
    parts = [f"{login}: {len(rows)} stream(s)\n\n" + _table(rows, ("stream", "date", "msgs", "minutes"))]
    fav = Counter()
    for (em,) in db.execute("SELECT emotes FROM messages WHERE login = ?", (login,)):
        fav.update(json.loads(em))
    if fav:
        parts.append("Favourite Twitch emotes: " + ", ".join(f"{e} x{n}" for e, n in fav.most_common(5)))
    ev = db.execute(
        "SELECT kind, COUNT(*), SUM(amount) FROM events WHERE login = ? GROUP BY kind", (login,)
    ).fetchall()
    if ev:
        parts.append("Support: " + ", ".join(f"{k} x{c} ({a})" for k, c, a in ev))
    return "\n\n".join(parts)


# --------------------------------------------------------------------------
# end-of-stream summary
# --------------------------------------------------------------------------
def stream_report(db, stream_id: str, top: int = 10) -> str:
    """What just happened, then where the regulars stand. Posted to the
    console channel when a stream ends."""
    row = db.execute(
        "SELECT title, game, started_at, ended_at FROM streams WHERE id = ?", (stream_id,)
    ).fetchone()
    if not row:
        return f"(no record of stream {stream_id})"
    title, game, started, ended = row
    mins = (since_ts(ended.replace("Z", "+00:00")) - since_ts(started.replace("Z", "+00:00"))) // 60 \
        if ended else 0
    msgs, chatters, newbies = db.execute(
        "SELECT COUNT(*), COUNT(DISTINCT login), SUM(is_first) FROM messages WHERE stream_id = ?",
        (stream_id,)).fetchone()
    present = db.execute(
        "SELECT COUNT(DISTINCT login) FROM presence WHERE stream_id = ?", (stream_id,)).fetchone()[0]
    events = db.execute(
        "SELECT kind, COUNT(*), SUM(amount) FROM events WHERE stream_id = ? GROUP BY kind",
        (stream_id,)).fetchall()

    head = [f"Stream report — {title or 'untitled'}" + (f" [{game}]" if game else ""),
            f"{mins // 60}h{mins % 60:02d}m · {present} in chat · {chatters} chatted · "
            f"{msgs} messages · {newbies or 0} first-timers"]
    if events:
        head.append("support: " + ", ".join(f"{k} x{c} ({a})" for k, c, a in events))

    chatty = db.execute(
        """
        SELECT m.login, COUNT(*) AS n,
               (SELECT COUNT(*) FROM presence p WHERE p.stream_id = m.stream_id AND p.login = m.login)
        FROM messages m WHERE m.stream_id = ? GROUP BY m.login ORDER BY n DESC LIMIT ?
        """, (stream_id, top)).fetchall()
    return "\n".join(head) + "\n\nThis stream (top chatters)\n\n" \
        + _table(chatty, ("viewer", "msgs", "minutes")) + "\n\n" + regulars(db, 0, top)


REPORTS = {"regulars": regulars, "viewers": viewers, "emotes": emotes,
           "streams": streams, "user": user}


def run(db, report: str, *, seventv: set[str] | None = None, name: str = "",
        since: int = 0, top: int = 25) -> str:
    """Dispatch by name. `seventv` is only needed for the emotes report."""
    if report == "emotes":
        return emotes(db, seventv or set(), since, top)
    if report == "user":
        if not name:
            raise ValueError("user report needs a login")
        return user(db, name, since, top)
    fn = REPORTS.get(report)
    if not fn:
        raise ValueError(f"unknown report {report!r}; one of {', '.join(REPORTS)}")
    return fn(db, since, top)
