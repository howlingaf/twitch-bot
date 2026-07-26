import html
import json
import re
from dataclasses import dataclass
from typing import Callable

import aiohttp

from .logger import logger

_LEETCODE_RE = re.compile(r'leetcode\.com/problems/([^/?#]+)', re.I)
_CSES_RE = re.compile(r'cses\.fi/problemset/task/(\d+)', re.I)
_EULER_RE = re.compile(r'projecteuler\.net/problem=(\d+)', re.I)
_CODEFORCES_RE = re.compile(
    r'codeforces\.com/(?:problemset/problem/(\d+)/([^/?#]+)'
    r'|(?:contest|gym)/(\d+)/problem/([^/?#]+))', re.I)

_HTML_TITLE_RE = re.compile(r'<title>(.*?)</title>', re.I | re.S)
_CF_TITLE_RE = re.compile(r'<div class="title">([^<]+)</div>')
_EULER_HASH_RE = re.compile(r'^#\d+\s*')
_CF_INDEX_RE = re.compile(r'^[A-Za-z0-9]+\.\s*')

_FETCH_TIMEOUT = aiohttp.ClientTimeout(total=6)
_HEADERS = {
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64; rv:128.0) "
                  "Gecko/20100101 Firefox/128.0",
}


def _codeforces_parts(m: re.Match) -> tuple[str, str]:
    contest = m.group(1) or m.group(3)
    index = (m.group(2) or m.group(4)).upper()
    return contest, index


def _leetcode_label(m: re.Match) -> str:
    return f"LeetCode: {m.group(1).replace('-', ' ').title()}"


def _leetcode_title(m: re.Match, body: str) -> str | None:
    data = json.loads(body)
    title = data.get("title")
    num = data.get("questionFrontendId")
    if title and num:
        return f"LeetCode #{num}: {title}"
    if title:
        return f"LeetCode: {title}"
    return None


def _cses_title(m: re.Match, body: str) -> str | None:
    # <title>CSES - Weird Algorithm</title>
    t = _HTML_TITLE_RE.search(body)
    if not t:
        return None
    name = html.unescape(t.group(1)).removeprefix("CSES - ").strip()
    return f"CSES #{m.group(1)}: {name}" if name else None


def _euler_title(m: re.Match, body: str) -> str | None:
    # <title>#42 Coded Triangle Numbers - Project Euler</title>
    t = _HTML_TITLE_RE.search(body)
    if not t:
        return None
    name = _EULER_HASH_RE.sub('', html.unescape(t.group(1)).strip())
    name = name.removesuffix("- Project Euler").strip(" -")
    return f"Project Euler #{m.group(1)}: {name}" if name else None


def _codeforces_title(m: re.Match, body: str) -> str | None:
    # <div class="title">A. Optimal Path</div>
    t = _CF_TITLE_RE.search(body)
    if not t:
        return None
    name = _CF_INDEX_RE.sub('', html.unescape(t.group(1)).strip())
    contest, index = _codeforces_parts(m)
    return f"Codeforces #{contest}{index}: {name}" if name else None


@dataclass(frozen=True)
class _Site:
    display: str
    pattern: re.Pattern
    label: Callable[[re.Match], str]              # quick label, no network
    fetch_url: Callable[[re.Match, str], str]     # where the title lives
    title: Callable[[re.Match, str], str | None]  # full label from the body


_SITES = (
    _Site(
        "LeetCode", _LEETCODE_RE, _leetcode_label,
        lambda m, url: f"https://leetcode-api-pied.vercel.app/problem/{m.group(1)}",
        _leetcode_title,
    ),
    _Site(
        "Codeforces", _CODEFORCES_RE,
        lambda m: "Codeforces #{}{}".format(*_codeforces_parts(m)),
        lambda m, url: url,
        _codeforces_title,
    ),
    _Site(
        "CSES", _CSES_RE,
        lambda m: f"CSES #{m.group(1)}",
        lambda m, url: f"https://cses.fi/problemset/task/{m.group(1)}",
        _cses_title,
    ),
    _Site(
        "Project Euler", _EULER_RE,
        lambda m: f"Project Euler #{m.group(1)}",
        lambda m, url: f"https://projecteuler.net/problem={m.group(1)}",
        _euler_title,
    ),
)

# "LeetCode, Codeforces, CSES, or Project Euler" — for user-facing messages.
SUPPORTED_SITES = ", ".join(s.display for s in _SITES[:-1]) + f", or {_SITES[-1].display}"


def _match_site(url: str) -> tuple[_Site, re.Match] | tuple[None, None]:
    for site in _SITES:
        m = site.pattern.search(url)
        if m:
            return site, m
    return None, None


def extract_problem_name(url: str) -> str | None:
    """Quick label for a supported problem URL (no network), or None if the
    URL isn't from any site in _SITES."""
    site, m = _match_site(url)
    return site.label(m) if site else None


def leetcode_slug(url: str) -> str | None:
    """Problem slug from a LeetCode URL (e.g. 'two-sum'), or None."""
    m = _LEETCODE_RE.search(url)
    return m.group(1) if m else None


async def _get_text(session: aiohttp.ClientSession, url: str) -> str | None:
    try:
        async with session.get(url, headers=_HEADERS) as resp:
            if resp.status != 200:
                logger.warning(
                    "Problem title fetch failed: HTTP %s for %s",
                    resp.status, url,
                )
                return None
            return await resp.text()
    except Exception as e:
        logger.warning("Problem title fetch error for %s: %s", url, e)
        return None


async def resolve_problem_name(url: str) -> str | None:
    """Full label for a supported problem URL, fetching the official problem
    title from the site. Falls back to the ID-only label if the fetch fails;
    returns None for unsupported URLs."""
    site, m = _match_site(url)
    if site is None:
        return None

    try:
        async with aiohttp.ClientSession(timeout=_FETCH_TIMEOUT) as session:
            body = await _get_text(session, site.fetch_url(m, url))
            if body and (full := site.title(m, body)):
                return full
    except Exception:
        logger.exception("Problem title resolution failed for %s", url)

    return site.label(m)
