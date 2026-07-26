import html
import json
import re

import aiohttp

from .logger import logger

# Supported problem sites for !st.
_LEETCODE_RE = re.compile(r'leetcode\.com/problems/([^/?#]+)', re.I)
_CSES_RE = re.compile(r'cses\.fi/problemset/task/(\d+)', re.I)
_EULER_RE = re.compile(r'projecteuler\.net/problem=(\d+)', re.I)
_CODEFORCES_RE = re.compile(
    r'codeforces\.com/(?:problemset/problem/(\d+)/([^/?#]+)'
    r'|(?:contest|gym)/(\d+)/problem/([^/?#]+))', re.I)

_HTML_TITLE_RE = re.compile(r'<title>(.*?)</title>', re.I | re.S)
_CF_TITLE_RE = re.compile(r'<div class="title">([^<]+)</div>')

_FETCH_TIMEOUT = aiohttp.ClientTimeout(total=6)
_HEADERS = {
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64; rv:128.0) "
                  "Gecko/20100101 Firefox/128.0",
}


def _codeforces_parts(m: re.Match) -> tuple[str, str]:
    contest = m.group(1) or m.group(3)
    index = (m.group(2) or m.group(4)).upper()
    return contest, index


def extract_problem_name(url: str) -> str | None:
    """Quick label for a supported problem URL (no network), or None if the
    URL isn't from LeetCode, CSES, Project Euler, or Codeforces."""
    m = _LEETCODE_RE.search(url)
    if m:
        return f"LeetCode: {m.group(1).replace('-', ' ').title()}"
    m = _CSES_RE.search(url)
    if m:
        return f"CSES #{m.group(1)}"
    m = _EULER_RE.search(url)
    if m:
        return f"Project Euler #{m.group(1)}"
    m = _CODEFORCES_RE.search(url)
    if m:
        contest, index = _codeforces_parts(m)
        return f"Codeforces #{contest}{index}"
    return None


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
    fallback = extract_problem_name(url)
    if fallback is None:
        return None

    try:
        async with aiohttp.ClientSession(timeout=_FETCH_TIMEOUT) as session:
            m = _LEETCODE_RE.search(url)
            if m:
                body = await _get_text(
                    session,
                    f"https://leetcode-api-pied.vercel.app/problem/{m.group(1)}",
                )
                if body:
                    data = json.loads(body)
                    title = data.get("title")
                    num = data.get("questionFrontendId")
                    if title and num:
                        return f"LeetCode #{num}: {title}"
                    if title:
                        return f"LeetCode: {title}"
                return fallback

            m = _CSES_RE.search(url)
            if m:
                body = await _get_text(
                    session, f"https://cses.fi/problemset/task/{m.group(1)}"
                )
                if body and (t := _HTML_TITLE_RE.search(body)):
                    # <title>CSES - Weird Algorithm</title>
                    name = html.unescape(t.group(1)).removeprefix("CSES - ").strip()
                    if name:
                        return f"CSES #{m.group(1)}: {name}"
                return fallback

            m = _EULER_RE.search(url)
            if m:
                body = await _get_text(
                    session, f"https://projecteuler.net/problem={m.group(1)}"
                )
                if body and (t := _HTML_TITLE_RE.search(body)):
                    # <title>#42 Coded Triangle Numbers - Project Euler</title>
                    name = html.unescape(t.group(1)).strip()
                    name = re.sub(r'^#\d+\s*', '', name)
                    name = name.removesuffix("- Project Euler").strip(" -")
                    if name:
                        return f"Project Euler #{m.group(1)}: {name}"
                return fallback

            m = _CODEFORCES_RE.search(url)
            if m:
                body = await _get_text(session, url)
                if body and (t := _CF_TITLE_RE.search(body)):
                    # <div class="title">A. Optimal Path</div>
                    name = html.unescape(t.group(1)).strip()
                    name = re.sub(r'^[A-Za-z0-9]+\.\s*', '', name)
                    contest, index = _codeforces_parts(m)
                    if name:
                        return f"Codeforces #{contest}{index}: {name}"
    except Exception:
        logger.exception("Problem title resolution failed for %s", url)

    return fallback
