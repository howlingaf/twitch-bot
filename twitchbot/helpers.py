import re


def extract_problem_name(url: str) -> str:
    m = re.search(r'leetcode\.com/problems/([^/]+)', url)
    return m.group(1).replace('-', ' ').title() if m else "Problem"
