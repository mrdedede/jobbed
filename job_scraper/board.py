"""One job board, and the decision of how to scrape it.

Scraping tries strategies cheapest-first: feed (JSON), WordPress REST, sitemap
(JSON-LD), links (heuristic), inline JSON (a listing dumped in a `<script>`
tag instead of anchors), and -- only with a renderer -- a browser pass for
boards that build their listing in JS. Each Job records its source in the `via`
field, which ensures a misdetected ATS doesn't silently get scraped by the
wrong strategy.
"""

from __future__ import annotations

import re
from dataclasses import replace
from typing import Dict, List, Optional
from urllib.parse import urldefrag, urljoin, urlparse

from bs4 import BeautifulSoup

from job_scraper.detector import ATS_REGISTRY, ATSDetector, ATSName, Renderer
from job_scraper.fetching import fetch
from job_scraper.models import Job
from job_scraper.strategies import (
    FEEDS,
    VENDOR_SCRAPERS,
    scrape_feed,
    scrape_inline_json,
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


#: Platforms whose boards are not scraped at all: they refuse automated
#: access (notes.VENDOR_NOTES says how), and every strategy would only spend
#: requests -- and a browser slot in RenderBoards -- on a captcha page.
REFUSES_BOTS = frozenset({ATSName.APEC})


def refuses_bots(board_url: str) -> Optional[ATSName]:
    """The platform hosting this board if it refuses bots, else None.

    The board's own hostname, not its detected ATS: an employer page that
    links APEC offers is still the employer's board and is scraped as usual.
    """
    ats = ats_from_host(board_url)

    return ats if ats in REFUSES_BOTS else None


#: Distinguishes "board page not fetched yet" from "fetched and it failed".
#: A plain None-check cannot: None is the legitimate cached value for a dead
#: board, and would send every strategy after the first back out to re-fetch.
_UNFETCHED = object()

#: Absolute URLs anywhere in a page, not just in anchors and iframes:
#: Concentrix's Workday links exist only as "apply_url" in a script blob.
_URL_RE = re.compile(r"https?://[^\s\"'<>\\)]+")

#: Vendor-hosted files that are not boards: the Recruitee analytics script,
#: the SmartRecruiters widget bundle, logos.
_ASSET_RE = re.compile(
    r"\.(?:js|css|png|jpe?g|svg|gif|webp|pdf|woff2?)(?:$|\?)", re.I
)

#: Vendor CDN hosts, whose files often carry no extension at all: eXalt's
#: page leads with cdn-images.welcometothejungle.com/<hash>/... images.
_ASSET_HOST_RE = re.compile(r"^(?:cdn|static|assets?|images?|img)[\w-]*\.",
                            re.I)


def linked_ats_url(html: str, base_url: str) -> Optional[str]:
    """Find the first URL in a page that leads to another vendor's board.

    Args:
        html: The board page.
        base_url: Where the page was served from.

    Returns:
        The first URL on a vendor-owned host that names a path, is not an
        asset, and is not the vendor already hosting this board (teamtailor
        boards link app.teamtailor.com dashboards). None if there is none.
    """
    own = ats_from_host(base_url)

    for url in _URL_RE.findall(html):
        vendor = ats_from_host(url)
        parts = urlparse(url)

        # An empty path is a "powered by" homepage link, never a board.
        if (vendor and vendor != own and parts.path.strip("/")
                and not _ASSET_RE.search(url)
                and not _ASSET_HOST_RE.match(parts.hostname or "")):
            return url

    return None


#: Iframe hosts that never hold a listing: video players, consent banners,
#: trackers, share widgets, captchas. Substrings, so the numbered HubSpot
#: hosts (143801506.hs-sites-eu1.com) match too.
IFRAME_NOISE = (
    "googletagmanager.com", "doubleclick.net", "google.com", "recaptcha.net",
    "youtube.com", "youtube-nocookie.com", "vimeo.com", "cookiebot.com",
    "linkedin.com", "embedly.com", "addtoany.com", "stripe.com",
    "captcha-delivery.com", "company-target.com", "tree-nation.com",
    "hs-sites",
)

#: Listing iframes followed per board. Each costs a detection and a fetch.
MAX_IFRAMES = 3


def listing_iframes(html: str, base_url: str) -> List[str]:
    """The iframes on a page that could hold its job listing.

    A careers page that embeds its listing (Région Hauts-de-France's Gestmax
    search, selego's jobego widget) shows none of it to scrape_links: the
    postings live in the frame's own document, rendered or not.

    Args:
        html: The page.
        base_url: Where the page was served from.

    Returns:
        Absolute iframe URLs, in page order and deduplicated, leaving out
        IFRAME_NOISE hosts.
    """
    found: List[str] = []

    for tag in BeautifulSoup(html, "html.parser").find_all("iframe"):
        src = (tag.get("src") or tag.get("data-src") or "").strip()
        url = urljoin(base_url, src)
        host = urlparse(url).hostname or ""

        if (url.startswith("http") and host
                and not any(noise in host for noise in IFRAME_NOISE)
                and url not in found):
            found.append(url)

    return found


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

    def __init__(self, company_name: str, board_url: str, session,
                 render: Optional[Renderer] = None):
        """Initialize a Board scraper.

        Args:
            company_name: Hiring company name.
            board_url: Career board base URL.
            session: The run's shared httpx.AsyncClient (see
                fetching.new_client).
            render: Optional async browser renderer for JS-rendered boards.
                None disables browser rendering; keep this way if your
                machine has no browser.
        """
        self.company_name = company_name
        self.board_url = board_url
        self.render = render
        self.final_url = board_url
        self.ats: Optional[ATSName] = None
        self.board_jobs: List[Job] = []
        self._html: object = _UNFETCHED
        # What the browser pass returned, kept for diagnose: without it a
        # render that timed out and one that drew a page with no postings
        # read the same in no_jobs_rendered.csv.
        self.render_ran = False
        self.rendered_html: Optional[str] = None

        self.session = session

    @property
    def url(self) -> str:
        """The URL to scrape: wherever the detector landed, else the input."""
        return self.final_url or self.board_url

    @property
    def html(self) -> Optional[str]:
        """The board page if something has fetched it yet, else None.

        Read-only and never fetches -- a property cannot await. Strategies
        use `get_html()`.
        """
        return None if self._html is _UNFETCHED else self._html  # type: ignore[return-value]

    async def get_html(self) -> Optional[str]:
        """The board page, fetched at most once per Board.

        Four strategies want this same document -- comeet and njoyn read their
        bootstrap out of it, links parses its anchors, and wordpress only needs
        to know whether it is WordPress at all. Fetching per strategy meant
        asking the board for the same page up to four times.

        detect_ats() seeds this from the page it had to fetch anyway, so on the
        normal path it costs no request at all.
        """
        if self._html is _UNFETCHED:
            self._html = await fetch(self.session, self.url)

        return self._html  # type: ignore[return-value]

    async def detect_ats(self) -> Optional[ATSName]:
        """Detect the ATS powering this board.

        Runs HTTP analysis, with optional browser pass if a renderer is
        available. Updates self.final_url, self.ats and the cached page in
        place.

        Returns:
            Detected ATS name (ATSName enum), or None if unknown.
        """
        # Share the Board's session rather than letting the detector build its
        # own: the detector fetched this exact page, and without sharing the
        # board page was fetched twice per board.
        detected = await ATSDetector(
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

    async def scrape_board(self) -> List[Job]:
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
            Empty if every strategy came back empty, or if the board's host
            refuses bots (REFUSES_BOTS).
        """
        if refuses_bots(self.board_url):
            self.board_jobs = []

            return self.board_jobs

        for strategy in (self._feed, self._wordpress, self._sitemap,
                         self._links, self._inline_json, self._linked_ats,
                         self._iframes, self._rendered):
            jobs = await strategy()

            if jobs:
                self.board_jobs = dedupe(jobs)

                return self.board_jobs

        self.board_jobs = []

        return self.board_jobs

    async def _feed(self) -> List[Job]:
        """Try vendor-specific feed scraper, then generic ATS feed.

        Returns:
            List of Job results, or empty list if no feed.
        """
        if self.ats is None:
            return await self._feed_from_page()

        scraper = VENDOR_SCRAPERS.get(self.ats)

        if scraper is not None:
            return await scraper(self)

        feed = FEEDS.get(self.ats)

        return await scrape_feed(self, feed) if feed else []

    async def _feed_from_page(self) -> List[Job]:
        """Try every feed whose vendor the page references, first hit wins.

        A careers page that embeds its ATS as an iframe or widget (betclic's
        Breezy iframe, algolia's Greenhouse board) carries the tenant token
        but too little else for detect_ats to name the vendor -- and without
        a name the feed was never tried, so ~390 postings across a dozen
        boards came back empty. scrape_feed already reads the token off the
        page; this only supplies the vendor.

        Returns:
            Jobs from the first feed that produced any, with self.ats set to
            its vendor so the output names where they came from. Empty and
            self.ats left None if none did.
        """
        html = await self.get_html() or ""

        for vendor in ATS_REGISTRY:
            feed = FEEDS.get(vendor.name)

            # The host gate is what keeps this cheap and safe: Greenhouse's
            # `[?&]for=` token pattern alone would send any page with a
            # ?for= link off to the Greenhouse API.
            if feed is None or not any(host in html for host in vendor.hosts):
                continue

            jobs = await scrape_feed(self, feed)

            if jobs:
                self.ats = vendor.name

                return jobs

        return []

    async def _linked_ats(self) -> List[Job]:
        """The ATS the page links or embeds, read through its vendor path.

        A careers page that hands its listing to an ATS -- an iframe (sea
        tpi's nos-recrutements), a link (52entertainment's WTTJ page), or only
        an `apply_url` in inline JSON (Concentrix's Workday) -- names the
        board the vendor scrapers need, but they read only board.url. One
        hop, so a board costs at most one extra detection.

        Only the child's _feed runs, never its full chain: on a vendor host
        the sitemap is the vendor's whole site, and broadpeak's werecruit
        link once came back as 11,860 "jobs" that way. That also makes
        recursion impossible -- _feed never reaches this method.

        Returns:
            Jobs from the linked vendor, with self.ats set to it. Empty if the
            page links no vendor or the vendor path found nothing.
        """
        url = linked_ats_url(await self.get_html() or "", self.url)

        if url is None:
            return []

        child = Board(self.company_name, url, session=self.session)
        await child.detect_ats()
        jobs = await child._feed()

        if jobs:
            self.ats = child.ats

        return jobs

    async def _iframes(self) -> List[Job]:
        """The listing a careers page embeds as an iframe, read as a board.

        Returns:
            Jobs from the first listing iframe that gave any. Empty if the
            page embeds none.
        """
        return await self._iframe_jobs(await self.get_html() or "")

    async def _iframe_jobs(self, html: str) -> List[Job]:
        """Scrape each listing iframe in `html` as its own board.

        Only the child's feed, anchors and inline JSON run: not its sitemap
        or WordPress, which on a vendor host are the vendor's whole site --
        the trap _linked_ats documents. The child has no renderer and never
        reaches this method, so nothing recurses.

        Args:
            html: The page to read iframes from (served or rendered).

        Returns:
            Jobs from the first iframe that gave any, with self.ats set to
            that child's. Empty if none did.
        """
        for url in listing_iframes(html, self.url)[:MAX_IFRAMES]:
            child = Board(self.company_name, url, session=self.session)
            await child.detect_ats()

            for strategy in (child._feed, child._links, child._inline_json):
                jobs = await strategy()

                if jobs:
                    self.ats = child.ats

                    return jobs

        return []

    async def _wordpress(self) -> List[Job]:
        """Try WordPress REST API scraper.

        Returns:
            List of Job results, or empty list if not WordPress or API fails.
        """
        return await scrape_wordpress(self)

    async def _sitemap(self) -> List[Job]:
        """Try XML sitemap scraper.

        Returns:
            List of Job results, or empty list if no sitemap found.
        """
        return await scrape_sitemap(self)

    async def _links(self) -> List[Job]:
        """Try generic anchor-heuristic scraper.

        Returns:
            List of Job results, or empty list if no job-like anchors found.
        """
        return await scrape_links(self)

    async def _inline_json(self) -> List[Job]:
        """Try reading postings out of an inline `<script>` JSON blob.

        Cheaper than a browser pass and reads the same unrendered HTML
        `_links` already has -- the boards this reaches (Atos, Eviden) build
        their visible anchors from this exact blob client-side, so the data
        is already server-rendered, just not as markup `_links` can see.

        Returns:
            List of Job results, or empty list if no script on the page
            carries a title/url-shaped record.
        """
        return await scrape_inline_json(self)

    async def _rendered(self) -> List[Job]:
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

        html = await self.render(self.url)
        self.render_ran, self.rendered_html = True, html

        if not html:
            return []

        # `links` would claim these were scraped off the served page, which is
        # the one thing `via` exists to prevent -- a board that only works
        # under a browser has to stay visibly distinct from one that does not.
        jobs = [
            replace(job, via="rendered")
            for job in await scrape_links(self, html=html)
        ]

        # Some embeds exist only once scripts run (Lille Métropole's Gestmax
        # frame). The frame itself is fetched plainly, so the child's `via`
        # is the truthful one.
        return jobs or await self._iframe_jobs(html)
