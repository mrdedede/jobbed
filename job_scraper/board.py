"""One job board, and the decision of how to scrape it.

Scraping tries strategies cheapest-first: feed (JSON), WordPress REST, sitemap
(JSON-LD), links (heuristic), and -- only with a renderer -- a browser pass for
boards that build their listing in JS. Each Job records its source in the `via`
field, which ensures a misdetected ATS doesn't silently get scraped by the
wrong strategy.
"""

from __future__ import annotations

from dataclasses import replace
from typing import Dict, List, Optional
from urllib.parse import urldefrag

from job_scraper.detector import ATSDetector, ATSName, Renderer
from job_scraper.fetching import fetch, new_session
from job_scraper.models import Job
from job_scraper.strategies import (
    FEEDS,
    VENDOR_SCRAPERS,
    scrape_feed,
    scrape_links,
    scrape_sitemap,
    scrape_wordpress,
)
from job_scraper.urls import ats_from_host


def dedupe(jobs: List[Job]) -> List[Job]:
    """Deduplicate jobs by URL, preserving order and first title.

    Args:
        jobs: List of Job results, possibly with duplicate URLs.

    Returns:
        List with one row per unique posting URL. If a URL appears multiple
        times, the first Job result is kept; subsequent duplicates are
        discarded.
    """
    seen: Dict[str, Job] = {}

    for job in jobs:
        seen.setdefault(urldefrag(job.url).url.rstrip("/"), job)

    return list(seen.values())


#: Distinguishes "board page not fetched yet" from "fetched and it failed".
#: A plain None-check cannot: None is the legitimate cached value for a dead
#: board, and would send every strategy after the first back out to re-fetch.
_UNFETCHED = object()


class Board:
    """Orchestrates scraping of a single job board with ATS detection.

    Attributes:
        company_name: Hiring company name.
        board_url: Career board base URL.
        render: Optional Playwright browser renderer for JS-rendered boards.
        final_url: Resolved URL after HTTP redirects (set by detect_ats).
        ats: Detected ATS name, if any.
        board_jobs: List of scraped Job results.
    """

    def __init__(self, company_name: str, board_url: str, session=None,
                 render: Optional[Renderer] = None):
        """Initialize a Board scraper.

        Args:
            company_name: Hiring company name.
            board_url: Career board base URL.
            session: Optional requests.Session for HTTP requests. If None,
                a retrying session is created with default headers.
            render: Optional Playwright browser renderer for JS-rendered
                boards. None disables browser rendering; keep this way if your
                machine has no browser.
        """
        self.company_name = company_name
        self.board_url = board_url
        self.render = render
        self.final_url = board_url
        self.ats: Optional[ATSName] = None
        self.board_jobs: List[Job] = []
        self._html: object = _UNFETCHED

        self.session = session or new_session()

    @property
    def url(self) -> str:
        """The URL to scrape: wherever the detector landed, else the input."""
        return self.final_url or self.board_url

    @property
    def html(self) -> Optional[str]:
        """The board page, fetched at most once per Board.

        Four strategies want this same document -- comeet and njoyn read their
        bootstrap out of it, links parses its anchors, and wordpress only needs
        to know whether it is WordPress at all. Fetching per strategy meant
        asking the board for the same page up to four times.

        detect_ats() seeds this from the page it had to fetch anyway, so on the
        normal path it costs no request at all.
        """
        if self._html is _UNFETCHED:
            self._html = fetch(self.session, self.url)

        return self._html  # type: ignore[return-value]

    def detect_ats(self) -> Optional[ATSName]:
        """Detect the ATS powering this board.

        Runs HTTP analysis, with optional browser pass if a renderer is
        available. Updates self.final_url, self.ats and the cached page in
        place.

        Returns:
            Detected ATS name (ATSName enum), or None if unknown.
        """
        # Share the Board's session rather than letting the detector build its
        # own: the detector fetched this exact page, and without sharing the
        # board page was fetched twice per board and a whole connection pool
        # was built and thrown away each time.
        detected = ATSDetector(
            session=self.session, render=self.render
        ).detect(self.board_url)

        self.final_url = detected.final_url or self.board_url
        self.ats = detected.detected_ats or ats_from_host(self.board_url)

        # Only when the detector landed where the strategies will look. On a
        # redirect self.url has moved, and the wrong page cached here is worse
        # than the fetch it saves.
        if detected.html and self.url == detected.final_url:
            self._html = detected.html

        return self.ats

    def scrape_board(self) -> List[Job]:
        """Feed, then WordPress REST, then sitemap, then anchors, then browser.

        A feed knows field semantics the other two have to infer, so it always
        wins -- but only if there is one for this ATS and its token is in the
        URL. Each strategy returns [] rather than raising, so a board never
        dies on its best path.

        The browser runs last and only when one was supplied: it is the most
        expensive strategy by orders of magnitude, and the ~15 boards a cheap
        strategy already serves must never pay for it.

        Returns:
            The jobs from the first strategy that produced any, deduplicated.
            Empty if every strategy came back empty.
        """
        for strategy in (self._feed, self._wordpress,
                         self._sitemap, self._links, self._rendered):
            jobs = strategy()

            if jobs:
                self.board_jobs = dedupe(jobs)

                return self.board_jobs

        self.board_jobs = []

        return self.board_jobs

    def _feed(self) -> List[Job]:
        """Try vendor-specific feed scraper, then generic ATS feed.

        Returns:
            List of Job results, or empty list if no feed.
        """
        scraper = VENDOR_SCRAPERS.get(self.ats)

        if scraper is not None:
            return scraper(self)

        feed = FEEDS.get(self.ats)

        return scrape_feed(self, feed) if feed else []

    def _wordpress(self) -> List[Job]:
        """Try WordPress REST API scraper.

        Returns:
            List of Job results, or empty list if not WordPress or API fails.
        """
        return scrape_wordpress(self)

    def _sitemap(self) -> List[Job]:
        """Try XML sitemap scraper.

        Returns:
            List of Job results, or empty list if no sitemap found.
        """
        return scrape_sitemap(self)

    def _links(self) -> List[Job]:
        """Try generic anchor-heuristic scraper.

        Returns:
            List of Job results, or empty list if no job-like anchors found.
        """
        return scrape_links(self)

    def _rendered(self) -> List[Job]:
        """Anchors again, but on HTML a browser finished drawing.

        The ~13 boards left uncovered all fail for one reason: the listing is
        built client-side, so the fetched HTML holds either no anchors at all
        (kicklox, coexya, sully-group) or only marketing ones (datadoghq,
        equans, amazon.jobs). Rendering is the single lever that reaches them,
        and once rendered they are ordinary anchor pages -- hence scrape_links
        rather than a parser of its own.

        Returns:
            List of Job results, or empty list if no renderer was supplied or
            the browser produced nothing.
        """
        if self.render is None:
            return []

        html = self.render(self.url)

        if not html:
            return []

        # `links` would claim these were scraped off the served page, which is
        # the one thing `via` exists to prevent -- a board that only works
        # under a browser has to stay visibly distinct from one that does not.
        return [
            replace(job, via="rendered")
            for job in scrape_links(self, html=html)
        ]
