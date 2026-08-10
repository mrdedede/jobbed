"""Explain why a board produced no postings.

`fetch()` is best-effort by design: a timeout, a 403, a PDF body and a page
full of marketing links all arrive at the scraper as the same empty result.
That is right for the run -- one dead board must not stop ninety-nine others --
and useless afterwards, when the question is which of those four happened.

This module answers that question for the boards that came back empty, at the
moment they came back empty, while the Board still holds its session, its
resolved URL and the page it already fetched. Only a board whose fetch actually
failed costs a second request.
"""

from __future__ import annotations

from typing import Optional

import requests
from bs4 import BeautifulSoup

from job_scraper.fetching import decode_response, is_textual
from job_scraper.urls import JOB_URL_RE

#: SPA bootstraps: the root element or state blob a client-side listing hangs
#: itself off. Their presence is the strongest single signal that the postings
#: exist but arrive after the document does.
SPA_MARKERS = (
    'id="root"', "id='root'",
    'id="app"', "id='app'",
    'id="__next"', "id='__next'",
    "__NEXT_DATA__",
    "__INITIAL_STATE__",
    "__NUXT__",
)

# ponytail: anchors-vs-scripts ratio, not a rendered diff. A page under this
# anchor count with scripts above the script floor is called javascript-built
# without proving it. Upgrade path is a Playwright pass comparing anchor counts
# before and after render -- worth it only if these verdicts start misleading.
FEW_ANCHORS = 15
MANY_SCRIPTS = 10


def explain(board, exc: Optional[BaseException] = None) -> str:
    """Say why this board yielded nothing.

    Args:
        board: The Board that came back empty, already scraped.
        exc: The exception that ended the scrape, if one did.

    Returns:
        One line naming the most likely cause.
    """
    return _cause(board, exc) + _context(board)


def _cause(board, exc: Optional[BaseException]) -> str:
    """The primary reason, before any board-level context is appended."""
    if exc is not None:
        return f"{type(exc).__name__}: {exc}"

    html = board.html

    # The page is in hand, so nothing about HTTP is left to explain: whatever
    # went wrong went wrong in the parsing.
    if html is not None:
        return _analyse(html)

    return _probe(board)


def _probe(board) -> str:
    """Re-request a board whose fetch returned None, to learn why it did."""
    try:
        response = board.session.get(
            board.url, timeout=20, allow_redirects=True, stream=True
        )
    except requests.RequestException as err:
        return f"fetch failed: {type(err).__name__}"

    with response:
        if response.status_code != 200:
            return f"http {response.status_code}"

        content_type = response.headers.get("Content-Type", "")

        if not is_textual(content_type):
            return f"non-textual body: {content_type}"

        # 200 and textual, yet fetch() returned None: the body was empty, or it
        # exceeded the fetch cap and came back truncated past parsing.
        raw = response.raw.read(decode_content=True)

        if not raw:
            return "empty body"

        return _analyse(decode_response(response, raw))


def _analyse(html: str) -> str:
    """Judge a page that was fetched fine but yielded no postings."""
    if not html.strip():
        return "empty body"

    soup = BeautifulSoup(html, "html.parser")
    anchors = [tag.get("href") for tag in soup.find_all("a", href=True)]
    scripts = len(soup.find_all("script"))
    marker = next((mark for mark in SPA_MARKERS if mark in html), "")

    shape = f"{len(anchors)} anchors, {scripts} scripts"

    if marker:
        shape += f", {marker}"

    if marker or (len(anchors) < FEW_ANCHORS and scripts >= MANY_SCRIPTS):
        return f"likely javascript-rendered ({shape})"

    if not anchors:
        return f"no links on the page ({shape})"

    if any(JOB_URL_RE.search(href) for href in anchors):
        return (f"job-shaped links present but no strategy read them ({shape})")

    return f"no job-shaped links ({shape})"


def _context(board) -> str:
    """Facts about the board itself that change how a cause reads."""
    notes = []

    if board.final_url and board.final_url != board.board_url:
        notes.append(f"redirected to {board.final_url}")

    if board.render is not None:
        notes.append("renderer ran and still found nothing")

    return f" ({'; '.join(notes)})" if notes else ""
