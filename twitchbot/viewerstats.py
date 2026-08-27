"""Reports over the chat store: who the regulars are, what chat uses.

Every report takes an open sqlite3 connection and the same keyword
arguments, and returns plain text — so the same code serves
scripts/viewer_stats.py on the box and the `viewers` console command in
Discord. Output is monospace tables; the caller wraps it in a code block.
"""

import argparse
import json
import re
from collections import Counter, defaultdict
from datetime import datetime, timezone

import aiohttp

_WORD = re.compile(r"\S+")

SEVENTV_SET_URL = "https://7tv.io/v3/users/twitch/{broadcaster_id}"


def _ts(iso: str) -> int:
    """ISO-8601 (Helix's trailing Z included) or bare YYYY-MM-DD -> unix seconds."""
    return int(datetime.fromisoformat(iso).replace(
        tzinfo=timezone.utc if "T" not in iso else None).timestamp())


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


def _support_line(rows) -> str:
    return ", ".join(f"{k} x{c} ({a})" for k, c, a in rows)


async def fetch_seventv(broadcaster_id: str) -> set[str]:
    """Names in the channel's active 7TV emote set (empty if unreachable).
    7TV emotes are plain text in chat, so this is how they get counted."""
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10)) as s:
            async with s.get(SEVENTV_SET_URL.format(broadcaster_id=broadcaster_id)) as r:
                data = await r.json()
        return {e["name"] for e in data.get("emote_set", {}).get("emotes", [])}
    except Exception:
        return set()


# --------------------------------------------------------------------------
# shared aggregation
# --------------------------------------------------------------------------
def _streams(db, since: int) -> list[tuple[str, int, int]]:
    """(id, start_ts, duration_minutes), oldest first. A stream with no
    recorded end is measured to its last presence poll or message."""
    last = defaultdict(int)
    for sid, t in db.execute("SELECT stream_id, MAX(minute) FROM presence GROUP BY stream_id"):
        last[sid] = t
    for sid, t in db.execute("SELECT stream_id, MAX(ts) FROM messages GROUP BY stream_id"):
        last[sid] = max(last[sid], t)
    out = []
    for sid, started, ended in db.execute(
        "SELECT id, started_at, ended_at FROM streams WHERE started_at >= ? ORDER BY started_at",
        (_iso(since),),
    ):
        start = _ts(started)
        end = _ts(ended) if ended else max(last[sid], start)
        out.append((sid, start, max(1, (end - start) // 60)))
    return out


def _per_viewer(db, since: int, streams: list) -> list[dict]:
    """One dict per login with everything the viewer reports need."""
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
    first, last, subs = {}, {}, set()
    for login, sid, c, sub, lo, hi in db.execute(
        "SELECT login, stream_id, COUNT(*), MAX(is_sub), MIN(ts), MAX(ts) FROM messages "
        "WHERE ts >= ? AND stream_id != '' GROUP BY login, stream_id", (since,)
    ):
        if sid not in index:
            continue
        msgs[login][sid] = c
        first[login] = min(first.get(login, lo), lo)
        last[login] = max(last.get(login, hi), hi)
        if sub:
            subs.add(login)

    supporters = {login for (login,) in db.execute(
        "SELECT DISTINCT login FROM events WHERE ts >= ?", (since,))}

    out = []
    for login in set(minutes) | set(msgs):
        attended = set(minutes[login]) | set(msgs[login])
        # Stay is only measurable where presence ran; a stream known from
        # messages alone (VOD backfill, pre-scope) is unknown, not 0.
        stays = [min(1.0, minutes[login][s] / duration[s]) for s in minutes[login]]
        last_idx = max(index[s] for s in attended)
        out.append({
            "login": login,
            "streams": len(attended),
            "minutes": sum(minutes[login].values()),
            "stay": sum(stays) / len(stays) if stays else None,
            "msgs": sum(msgs[login].values()),
            "sub": login in subs,
            "supporter": login in supporters,
            "first_ts": first.get(login),
            "last_ts": last.get(login),
            "last_idx": last_idx,
            "last_stream_ts": streams[last_idx][1],
        })
    return out


# --------------------------------------------------------------------------
# regulars
# --------------------------------------------------------------------------
# One number per viewer, 0-100. Weights are a judgement call, written down
# so they can be argued with:
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


def _score(v: dict, n_streams: int) -> float:
    attendance = v["streams"] / n_streams
    chat = min(1.0, (v["msgs"] / v["streams"]) / CHAT_SATURATE)
    support = 1.0 if (v["sub"] or v["supporter"]) else 0.0
    missed = n_streams - 1 - v["last_idx"]
    recency = 0.5 ** (missed / RECENCY_HALF_LIFE)
    return (50 * attendance + 20 * (v["stay"] or 0) + 15 * chat + 15 * support) * recency


def regulars(db, *, since=0, top=25, **_) -> str:
    streams = _streams(db, since)
    if not streams:
        return "(no streams recorded yet)"
    n = len(streams)
    rows = sorted(
        ((round(_score(v, n)), v) for v in _per_viewer(db, since, streams)),
        key=lambda r: (-r[0], r[1]["login"]),
    )
    head = (f"Regulars — {n} stream(s){' since ' + _day(since) if since else ''}, top {top}\n"
            f"score = 50·attendance + 20·stay + 15·chat + 15·support, "
            f"halved per {RECENCY_HALF_LIFE} streams missed\n")
    return head + "\n" + _table([(
        s, v["login"], f"{v['streams']}/{n}",
        f"{int(v['stay'] * 100)}%" if v["stay"] is not None else "-",
        f"{v['minutes'] / 60:.1f}", v["msgs"],
        "sub" if v["sub"] else ("supporter" if v["supporter"] else ""),
        _day(v["last_stream_ts"]),
    ) for s, v in rows[:top]], ("score", "viewer", "streams", "stay", "hours", "msgs", "", "last seen"))


def viewers(db, *, since=0, top=25, **_) -> str:
    """The raw leaderboard: same facts as `regulars`, no score, sorted by
    attendance then hours then messages."""
    streams = _streams(db, since)
    n = len(streams) or 1
    rows = sorted(_per_viewer(db, since, streams),
                  key=lambda v: (-v["streams"], -v["minutes"], -v["msgs"]))
    return f"Top {top} viewers across {n} stream(s)\n\n" + _table([(
        v["login"], f"{v['streams']}/{n}", f"{v['minutes'] / 60:.1f}", v["msgs"],
        f"{v['msgs'] / v['streams']:.1f}", _day(v["first_ts"]), _day(v["last_ts"]),
        "sub" if v["sub"] else "",
    ) for v in rows[:top]], ("viewer", "streams", "hours", "msgs", "msgs/stream",
                             "first chat", "last chat", ""))


def emotes(db, *, since=0, top=25, seventv=frozenset(), **_) -> str:
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


def streams(db, *, since=0, top=25, **_) -> str:
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


def user(db, *, name="", since=0, top=25, **_) -> str:
    login = name.lower().lstrip("@")
    if not login:
        raise ValueError("user report needs a login: viewers user NAME")
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
        parts.append("Support: " + _support_line(ev))
    return "\n\n".join(parts)


def stream_report(db, stream_id: str, top: int = 10) -> str:
    """What just happened, then where the regulars stand. Posted to the
    console channel when a stream ends."""
    row = db.execute("SELECT title, game FROM streams WHERE id = ?", (stream_id,)).fetchone()
    if not row:
        return f"(no record of stream {stream_id})"
    title, game = row
    mins = next((m for sid, _, m in _streams(db, 0) if sid == stream_id), 0)
    msgs, chatters, newbies = db.execute(
        "SELECT COUNT(*), COUNT(DISTINCT login), COALESCE(SUM(is_first), 0) FROM messages "
        "WHERE stream_id = ?", (stream_id,)).fetchone()
    present = db.execute(
        "SELECT COUNT(DISTINCT login) FROM presence WHERE stream_id = ?", (stream_id,)).fetchone()[0]
    events = db.execute(
        "SELECT kind, COUNT(*), SUM(amount) FROM events WHERE stream_id = ? GROUP BY kind",
        (stream_id,)).fetchall()
    head = [f"Stream report — {title or 'untitled'}" + (f" [{game}]" if game else ""),
            f"{mins // 60}h{mins % 60:02d}m · {present} in chat · {chatters} chatted · "
            f"{msgs} messages · {newbies} first-timers"]
    if events:
        head.append("support: " + _support_line(events))
    chatty = db.execute(
        """
        SELECT m.login, COUNT(*) AS n,
               (SELECT COUNT(*) FROM presence p WHERE p.stream_id = m.stream_id AND p.login = m.login)
        FROM messages m WHERE m.stream_id = ? GROUP BY m.login ORDER BY n DESC LIMIT ?
        """, (stream_id, top)).fetchall()
    return ("\n".join(head) + "\n\nThis stream (top chatters)\n\n"
            + _table(chatty, ("viewer", "msgs", "minutes")) + "\n\n" + regulars(db, top=top))


REPORTS = {"regulars": regulars, "viewers": viewers, "emotes": emotes,
           "streams": streams, "user": user}


def _top(s: str) -> int:
    return max(1, min(50, int(s)))


def arg_parser(**defaults) -> argparse.ArgumentParser:
    """The one grammar for both the CLI and the Discord console command:
    [report] [NAME] [--since YYYY-MM-DD] [--top N]."""
    p = argparse.ArgumentParser(prog="viewers", add_help=False, exit_on_error=False)
    p.add_argument("report", nargs="?", default="regulars", choices=sorted(REPORTS))
    p.add_argument("name", nargs="?", default="")
    p.add_argument("--since", type=_ts, default=0)
    p.add_argument("--top", type=_top, default=25)
    p.set_defaults(**defaults)
    return p
