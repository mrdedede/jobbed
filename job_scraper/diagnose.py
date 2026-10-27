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

import asyncio
import re
from typing import Optional
from urllib.parse import urljoin, urlparse

import httpx
from bs4 import BeautifulSoup

from job_scraper.detector import ATS_REGISTRY
from job_scraper.fetching import decode_response, is_textual, request
from job_scraper.strategies.feed import FEEDS, _token
from job_scraper.strategies.links import JOB_PATH, _in_chrome, _same_page
from job_scraper.urls import _JOB_WORD, job_href_matches

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

#: A redirect target can carry a whole search state in its query string
#: (facet filters, refinement lists) -- one board's final_url ran past 2,000
#: characters and made its no_jobs.csv row unreadable. The reason line is for
#: a human, so the note is cut to a length one still reads as a URL.
MAX_REDIRECT_LEN = 120


async def explain(board, exc: Optional[BaseException] = None) -> str:
    """Say why this board yielded nothing.

    Args:
        board: The Board that came back empty, already scraped.
        exc: The exception that ended the scrape, if one did.

    Returns:
        One line naming the most likely cause.
    """
    return await _cause(board, exc) + _context(board)


async def _cause(board, exc: Optional[BaseException]) -> str:
    """The primary reason, before any board-level context is appended."""
    if exc is not None:
        return f"{type(exc).__name__}: {exc}"

    html = await board.get_html()

    # The page is in hand, so nothing about HTTP is left to explain: whatever
    # went wrong went wrong in the parsing.
    if html is not None:
        return await asyncio.to_thread(_analyse, html, board.ats, board.url)

    return await _probe(board)


async def _probe(board) -> str:
    """Re-request a board whose fetch returned None, to learn why it did."""
    async def read(response: httpx.Response) -> str:
        if response.status_code != 200:
            return f"http {response.status_code}"

        content_type = response.headers.get("Content-Type", "")

        if not is_textual(content_type):
            return f"non-textual body: {content_type}"

        # 200 and textual, yet fetch() returned None: the body was empty, or it
        # exceeded the fetch cap and came back truncated past parsing.
        raw = await response.aread()

        if not raw:
            return "empty body"

        return await asyncio.to_thread(
            _analyse, decode_response(response, raw), board.ats, board.url
        )

    try:
        return await request(board.session, board.url, read)
    except httpx.HTTPError as err:
        return f"fetch failed: {type(err).__name__}"


def _analyse(html: str, ats=None, base_url: str = "") -> str:
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

    # A job-shaped link already on the page outranks the SPA-marker guess:
    # the marker only *predicts* that postings arrive after render, and a
    # real match on this same fetch disproves that prediction outright.
    # Same-page anchors (nav skip-links, empty CTAs) are excluded first --
    # scrape_links already does this, so a diagnosis that skipped it could
    # flag a page as "job-shaped links present" over links no strategy would
    # ever have read either. Menu, banner and footer links likewise: left in,
    # Capgemini's menus read as "links present", a reason RenderBoards skips.
    if anchors and any(
        job_href_matches(urljoin(base_url, tag["href"]), ats, JOB_PATH)
        for tag in soup.find_all("a", href=True)
        if not _same_page(urljoin(base_url, tag["href"]), base_url)
        and not _in_chrome(tag)
    ):
        return (f"job-shaped links present but no strategy read them ({shape})")

    if marker or (len(anchors) < FEW_ANCHORS and scripts >= MANY_SCRIPTS):
        return f"likely javascript-rendered ({shape})"

    if not anchors:
        return f"no links on the page ({shape})"

    return f"no job-shaped links ({shape})"


#: A job word anywhere in the path, unanchored: wide on purpose, since its job
#: is to surface the links JOB_URL_RE turned down, not to judge them.
_JOB_WORD_ANYWHERE = re.compile(_JOB_WORD, re.I)

#: Pages that say outright they have nothing open -- an empty board, not a bug.
_NO_OPENINGS = re.compile(
    r"aucune offre|no (?:open )?(?:positions|openings|jobs) (?:available|open)|"
    r"there are (?:currently )?no (?:open )?(?:positions|jobs|openings)",
    re.I,
)

#: Near-miss paths listed per row; enough to see the shape, short enough to read.
MAX_NEAR_MISSES = 3


async def evidence(board) -> str:
    """The facts behind a miss, for whoever triages no_jobs.csv.

    `explain` names one cause; this lists what the page holds that a fix
    would start from: which ATS it references (the listing usually lives
    there), the tenant token each feed would read off it (a wrong one --
    Greenhouse's "v1" -- shows up right here), and which job-looking links
    JOB_URL_RE turned down. Reads only the cached page: never a request.

    Args:
        board: The Board that came back empty, already scraped.

    Returns:
        "; "-joined facts, or "" when there is no page or nothing to say.
    """
    html = board.html

    if not html:
        return ""

    return await asyncio.to_thread(_evidence, html, board.ats, board.url)


def _evidence(html: str, ats=None, base_url: str = "") -> str:
    """Synchronous body of `evidence`, run off the event loop."""
    soup = BeautifulSoup(html, "html.parser")
    facts = []

    # Iframes and scripts count too: a widget embed is the commonest way a
    # careers page hands its listing to an ATS without linking to it.
    refs = " ".join(
        tag.get(attr) or ""
        for tag, attr in [(a, "href") for a in soup.find_all("a", href=True)]
        + [(t, "src") for t in soup.find_all(["iframe", "script"], src=True)]
    ).lower()
    vendors = [str(vendor.name) for vendor in ATS_REGISTRY
               if any(host in refs for host in vendor.hosts)]

    if vendors:
        facts.append(f"ats refs: {', '.join(vendors)}")

    tokens = [f"{name}={token}" for name, feed in FEEDS.items()
              if (token := _token(html, feed.token))]

    if tokens:
        facts.append(f"feed tokens: {', '.join(tokens)}")

    hrefs = [tag["href"] for tag in soup.find_all("a", href=True)]
    misses = []

    for href in hrefs:
        url = urljoin(base_url, href)
        path = urlparse(url).path

        if (url.startswith("http") and _JOB_WORD_ANYWHERE.search(path)
                and not _same_page(url, base_url)
                and not job_href_matches(url, ats, JOB_PATH)
                and path not in misses):
            misses.append(path)

    if misses:
        facts.append(f"near-miss paths: {', '.join(misses[:MAX_NEAR_MISSES])}"
                     + (f" (+{len(misses) - MAX_NEAR_MISSES})"
                        if len(misses) > MAX_NEAR_MISSES else ""))

    # Postings that are on the page but not as web links: no anchor strategy
    # will ever read these, so they need saying apart from a regex gap.
    mailto = sum(href.startswith("mailto:") and "subject=" in href
                 for href in hrefs)
    pdfs = sum(href.lower().endswith(".pdf") for href in hrefs)

    if mailto:
        facts.append(f"{mailto} mailto postings")

    if pdfs:
        facts.append(f"{pdfs} pdf links")

    if _NO_OPENINGS.search(soup.get_text(" ")):
        facts.append("page says no openings")

    return "; ".join(facts)


def _context(board) -> str:
    """Facts about the board itself that change how a cause reads."""
    notes = []

    if board.final_url and board.final_url != board.board_url:
        url = board.final_url

        if len(url) > MAX_REDIRECT_LEN:
            url = url[:MAX_REDIRECT_LEN] + "..."

        notes.append(f"redirected to {url}")

    if board.render is not None:
        notes.append("renderer ran and still found nothing")

    return f" ({'; '.join(notes)})" if notes else ""
