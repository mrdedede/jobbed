"""Postings a board embeds as inline JSON instead of anchors.

Some boards render their listing entirely client-side from a data blob the
server already sent -- Atos and Eviden (same in-house WordPress plugin) print
the whole search result as ``window['atosjobs_<hash>'] = {"results": [...]}``
in a `<script>` tag, then build the visible `<a>` tags from it after load.
`links.py` never sees these: it only reads anchors already in the markup, and
this blob renders none. The data is real and server-rendered -- no browser
needed -- just never exposed the way `links.py` looks for it.

This is deliberately generic (title/url pair inside nested JSON), not an
Atos-specific parser: the same shape -- an in-house listing widget dumping its
API response into a script tag -- is common enough to be worth reading once.
"""

from __future__ import annotations

import json
import re
from typing import TYPE_CHECKING, List, Optional
from urllib.parse import urljoin

from bs4 import BeautifulSoup

from job_scraper.models import Job

if TYPE_CHECKING:
    from job_scraper.board import Board

#: Same discipline as detector.py's MAX_ELEMENTS/MAX_ANCHORS: a cap on how
#: much of a pathological blob gets walked, not a tuned value.
MAX_DEPTH = 12
MAX_RECORDS = 2_000

_TITLE_KEYS = ("title", "name", "jobtitle", "job_title", "posting_title")
_URL_KEYS = ("url", "href", "link", "joburl", "job_url", "permalink")

#: `var x = {...}`, `window.x = {...}`, or `window['x'] = {...}` -- the shapes
#: an inline listing widget bootstraps itself with. Captures the RHS greedily
#: to the end of the statement; json.loads then either parses it whole or, on
#: trailing script content past the object/array, is retried on a
#: bracket-matched slice (see _extract_json).
_ASSIGNMENT_RE = re.compile(
    r"(?:var\s+\w+|window(?:\.\w+|\[[\"\']\w+[\"\']\]))\s*=\s*([\[{])",
    re.I,
)


def _matching_bracket(text: str, start: int) -> Optional[str]:
    """Slice out a balanced {...} or [...] starting at `start`.

    A script tag routinely has trailing statements after the object literal
    (`;doSomething();`), so `json.loads` on everything from the assignment to
    the end of the tag usually fails. Bracket-counting finds exactly where the
    literal ends instead of guessing at a regex for "the rest of the JS".

    Args:
        text: Script text.
        start: Index of the opening bracket.

    Returns:
        The balanced substring including both brackets, or None if the
        brackets never close (truncated script, not real JSON).
    """
    open_ch = text[start]
    close_ch = "}" if open_ch == "{" else "]"
    depth = 0
    in_string = False
    escape = False

    for index in range(start, len(text)):
        char = text[index]

        if in_string:
            if escape:
                escape = False
            elif char == "\\":
                escape = True
            elif char == '"':
                in_string = False

            continue

        if char == '"':
            in_string = True
        elif char == open_ch:
            depth += 1
        elif char == close_ch:
            depth -= 1

            if depth == 0:
                return text[start:index + 1]

    return None


def _extract_json(script_text: str):
    """Pull the first parseable JSON object/array out of a script's text.

    Args:
        script_text: Raw text of one inline `<script>` tag.

    Returns:
        Parsed JSON (dict or list), or None if nothing in the tag parses.
    """
    stripped = script_text.strip()

    if stripped[:1] in "{[":
        try:
            return json.loads(stripped)
        except (ValueError, TypeError):
            pass

    match = _ASSIGNMENT_RE.search(script_text)

    if not match:
        return None

    blob = _matching_bracket(script_text, match.start(1))

    if not blob:
        return None

    try:
        return json.loads(blob)
    except (ValueError, TypeError):
        return None


def _find_records(node: object, depth: int = 0, budget: Optional[list] = None):
    """Walk parsed JSON for dicts that look like a title/url posting record.

    Args:
        node: JSON-like object (dict, list, or scalar).
        depth: Current recursion depth.
        budget: One-element list used as a mutable counter, capping how many
            records a single blob can yield.

    Yields:
        Dicts carrying both a title-like and a url-like key.
    """
    if budget is None:
        budget = [MAX_RECORDS]

    if depth > MAX_DEPTH or budget[0] <= 0:
        return

    if isinstance(node, dict):
        title = next((node[key] for key in _TITLE_KEYS if node.get(key)), None)
        url = next((node[key] for key in _URL_KEYS if node.get(key)), None)

        if isinstance(title, str) and isinstance(url, str) and title and url:
            budget[0] -= 1
            yield node

            return

        for value in node.values():
            yield from _find_records(value, depth + 1, budget)
    elif isinstance(node, list):
        for item in node:
            yield from _find_records(item, depth + 1, budget)


def scrape_inline_json(board: "Board", html: Optional[str] = None) -> List[Job]:
    """Extract jobs from a listing dumped as inline JSON in a `<script>` tag.

    Args:
        board: Board to scrape.
        html: Optional board page HTML to parse. If None, fetches from
            board.html.

    Returns:
        List of Job results, or empty list if no script on the page carries a
        title/url-shaped record.
    """
    html = html or board.html

    if not html:
        return []

    jobs: List[Job] = []

    for tag in BeautifulSoup(html, "html.parser").find_all("script", src=False):
        # JSON-LD is a separate, already-handled shape: detector.py reads it
        # for ATS fingerprinting, and its schema.org WebPage/WebSite/
        # BreadcrumbList records carry the exact same "name"+"url" pair this
        # module looks for without being a posting -- every one of them is a
        # false positive here.
        if (tag.get("type") or "").lower() == "application/ld+json":
            continue

        text = tag.get_text()

        if "{" not in text and "[" not in text:
            continue

        parsed = _extract_json(text)

        if parsed is None:
            continue

        for record in _find_records(parsed):
            jobs.append(Job(
                company=board.company_name,
                title=record[next(k for k in _TITLE_KEYS if record.get(k))],
                url=urljoin(
                    board.url,
                    record[next(k for k in _URL_KEYS if record.get(k))],
                ),
                via="inline_json",
            ))

    return jobs
