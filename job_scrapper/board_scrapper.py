"""Scrape job postings from a board using ATS-specific logic.

Scraping tries three strategies in order: feed (JSON), sitemap (JSON-LD), and
links (heuristic). Each Job records its source in the `via` field, which
ensures a misdetected ATS doesn't silently get scraped by the wrong strategy.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple
from urllib.parse import urldefrag, urljoin, urlparse

import requests
from bs4 import BeautifulSoup

from job_scrapper.detector import (
    ATSDetector,
    ATSName,
    _walk_strings,
    extract,
)

HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; JobResearchBot/1.0)",
    "Accept": "application/json, text/html;q=0.9, */*;q=0.8",
}

MAX_FETCH_BYTES = 2_000_000

# Budget for sitemap discovery requests.
MAX_SITEMAP_REQUESTS = 4

# Minimum job URLs to treat as a valid sitemap.
MIN_JOB_URLS = 3

# Maximum detail postings to fetch individually (one request each).
MAX_DETAIL = 200


@dataclass(frozen=True)
class Job:
    """Job posting with source metadata.

    Attributes:
        company: Company name.
        title: Job title.
        url: Job posting URL.
        place: Location (may be None).
        via: Source strategy (feed, workday, sitemap, or links).
    """

    company: str
    title: str
    url: str
    place: Optional[str] = None
    via: str = ""


def _fetch(session, url: str, timeout: int = 20,
           method: str = "get", **kwargs) -> Optional[str]:
    """Best-effort HTTP request returning text or None.

    Args:
        session: Requests session.
        url: URL to fetch.
        timeout: Request timeout in seconds.
        method: HTTP method (get, post, etc).
        **kwargs: Additional arguments to pass to session method.

    Returns:
        Response body decoded as UTF-8, or None on any error.
    """
    try:
        with getattr(session, method)(
            url, timeout=timeout, allow_redirects=True, stream=True, **kwargs
        ) as response:
            if response.status_code != 200:
                return None

            raw = response.raw.read(MAX_FETCH_BYTES, decode_content=True)

            return raw.decode(response.encoding or "utf-8", errors="replace")
    except requests.RequestException:
        return None


def _fetch_json(session, url: str, **kwargs) -> Optional[object]:
    """Fetch URL and parse as JSON.

    Args:
        session: Requests session.
        url: URL to fetch.
        **kwargs: Additional arguments to _fetch.

    Returns:
        Parsed JSON or None on error.
    """
    body = _fetch(session, url, **kwargs)

    if not body:
        return None

    try:
        return json.loads(body)
    except ValueError:
        return None


@dataclass(frozen=True)
class Feed:
    """ATS vendor feed endpoint configuration.

    Attributes:
        url: Endpoint template (formatted with tenant token).
        token: Regex tuple to extract token from board URL.
        items: Dotted path to jobs list (empty = body is the list).
        title: Dotted path to job title.
        place: Dotted path to location.
        link: Dotted path to job URL.
        link_url: Template for internal IDs that aren't public URLs.
    """

    url: str
    token: Tuple[str, ...]
    items: str = ""
    title: str = "title"
    place: str = "location"
    link: str = "url"
    link_url: str = ""


FEEDS: Dict[ATSName, Feed] = {
    ATSName.GREENHOUSE: Feed(
        url="https://boards-api.greenhouse.io/v1/boards/{token}/jobs",
        token=(r"[?&]for=([\w.-]+)", r"greenhouse\.io/(?:embed/)?([\w.-]+)"),
        items="jobs",
        title="title",
        place="location",
        link="absolute_url",
    ),
    ATSName.LEVER: Feed(
        url="https://api.lever.co/v0/postings/{token}?mode=json",
        token=(r"jobs\.lever\.co/([\w.-]+)",),
        title="text",
        place="categories.location",
        link="hostedUrl",
    ),
    ATSName.ASHBY: Feed(
        url="https://api.ashbyhq.com/posting-api/job-board/{token}",
        token=(r"ashbyhq\.com/([\w.-]+)",),
        items="jobs",
        title="title",
        place="location",
        link="jobUrl",
    ),
    ATSName.RECRUITEE: Feed(
        url="https://{token}.recruitee.com/api/offers/",
        token=(r"//([\w-]+)\.recruitee\.com",),
        items="offers",
        title="title",
        place="location",
        link="careers_url",
    ),
    ATSName.WORKABLE: Feed(
        url=(
            "https://apply.workable.com/api/v1/widget/accounts/"
            "{token}?details=true"
        ),
        token=(r"apply\.workable\.com/([\w.-]+)",
               r"//([\w-]+)\.workable\.com"),
        items="jobs",
        title="title",
        place="city",
        link="url",
    ),
    ATSName.SMARTRECRUITERS: Feed(
        url=(
            "https://api.smartrecruiters.com/v1/companies/{token}"
            "/postings?limit=100"
        ),
        token=(r"smartrecruiters\.com/([\w.-]+)",),
        items="content",
        title="name",
        place="location",
        # The feed returns an internal API ref, never the public posting URL.
        link_url="https://jobs.smartrecruiters.com/{token}/{id}",
    ),
}


def _dig(node: object, path: str) -> object:
    """Navigate nested dict by dotted path, handling list wrappers.

    Args:
        node: JSON-like object to traverse.
        path: Dotted path (e.g., "items.0.name").

    Returns:
        Value at path, or None if path doesn't exist.
    """
    if not path:
        return node

    for key in path.split("."):
        if isinstance(node, list):
            node = node[0] if node else None

        if not isinstance(node, dict):
            return None

        node = node.get(key)

    return node


def _first_string(node: object) -> Optional[str]:
    """Extract first non-empty string from nested structure.

    Args:
        node: JSON-like object to search.

    Returns:
        First non-empty string found, or None.
    """
    for value in _walk_strings(node):
        text = value.strip()

        if text:
            return text

    return None


def _token(url: str, patterns: Tuple[str, ...]) -> Optional[str]:
    """Extract tenant token from URL by regex patterns.

    Args:
        url: URL to search.
        patterns: Regex patterns to try (group 1 is the token).

    Returns:
        Token extracted from group 1, or None.
    """
    for pattern in patterns:
        found = re.search(pattern, url, re.I)

        if found:
            return found.group(1)

    return None


def scrap_feed(board: "Board", feed: Feed) -> List[Job]:
    """Scrape board from ATS vendor API endpoint.

    Args:
        board: Board to scrape.
        feed: Feed configuration.

    Returns:
        List of jobs, or empty list on any error.
    """
    token = _token(board.final_url, feed.token) or _token(
        board.board_url, feed.token
    )

    if not token:
        return []

    body = _fetch_json(board.session, feed.url.format(token=token))
    items = _dig(body, feed.items)

    if not isinstance(items, list):
        return []

    jobs = []

    for item in items:
        if not isinstance(item, dict):
            continue

        title = _first_string(_dig(item, feed.title))

        if feed.link_url:
            url = feed.link_url.format(token=token, id=item.get("id"))
        else:
            url = _first_string(_dig(item, feed.link))

        if not title or not url:
            continue

        jobs.append(Job(
            company=board.company_name,
            title=title,
            url=url,
            place=_first_string(_dig(item, feed.place)),
            via="feed",
        ))

    return jobs


_LOCALE_RE = re.compile(r"^[a-z]{2}(?:-[a-z]{2})?$", re.I)

WORKDAY_PAGE = 20
WORKDAY_MAX_PAGES = 50


def scrap_workday(board: "Board") -> List[Job]:
    """Scrape Workday board via /wday/cxs/ JSON endpoint.

    Args:
        board: Board to scrape.

    Returns:
        List of jobs from board.
    """
    parsed = urlparse(board.final_url or board.board_url)
    tenant = parsed.hostname.split(".")[0] if parsed.hostname else ""
    segments = [part for part in parsed.path.split("/") if part]

    if segments and _LOCALE_RE.match(segments[0]):
        segments = segments[1:]

    if not tenant or not segments:
        return []

    site = segments[0]
    root = f"{parsed.scheme}://{parsed.hostname}"
    endpoint = f"{root}/wday/cxs/{tenant}/{site}/jobs"
    base = urljoin(board.final_url or board.board_url, parsed.path.rstrip("/"))

    jobs: List[Job] = []

    for page in range(WORKDAY_MAX_PAGES):
        body = _fetch_json(
            board.session,
            endpoint,
            method="post",
            json={
                "appliedFacets": {},
                "limit": WORKDAY_PAGE,
                "offset": page * WORKDAY_PAGE,
                "searchText": "",
            },
        )

        postings = _dig(body, "jobPostings")

        if not isinstance(postings, list) or not postings:
            break

        for item in postings:
            if not isinstance(item, dict):
                continue

            title = (item.get("title") or "").strip()
            path = item.get("externalPath") or ""

            if not title or not path:
                continue

            jobs.append(Job(
                company=board.company_name,
                title=title,
                url=base + path,
                place=(item.get("locationsText") or "").strip() or None,
                via="workday",
            ))

        if len(postings) < WORKDAY_PAGE:
            break

    return jobs


def _todo(note: str):
    """Create stub scraper with implementation note in docstring.

    Args:
        note: Implementation note for the stub.

    Returns:
        A function that raises NotImplementedError with the note.
    """
    def scrape(board: "Board") -> List[Job]:
        raise NotImplementedError(note)

    scrape.__doc__ = note

    return scrape


scrap_personio = _todo(
    "XML feed at https://{token}.jobs.personio.de/xml -- needs an XML parse "
    "path in Feed, which nothing else uses yet."
)
scrap_breezy = _todo(
    "JSON board at https://{token}.breezy.hr/json (top-level list)."
)
scrap_bamboohr = _todo(
    "JSON board at https://{token}.bamboohr.com/careers/list; postings carry "
    "an id, so the URL needs Feed.link_url."
)
scrap_pinpoint = _todo(
    "JSON board at https://{token}.pinpointhq.com/postings.json."
)
scrap_comeet = _todo(
    "Careers API needs a company UID that is not in the board URL -- lift it "
    "from url_comeet_hosted_page in the page's inline script."
)
scrap_teamtailor = _todo(
    "Public API needs an API key. The hosted board is server-rendered, so "
    "sitemap/links already do well here -- low priority."
)
scrap_jazzhr = _todo(
    "RSS at /feeds/export/jobs/ rather than JSON; needs an XML parse path."
)
scrap_jobvite = _todo("Board HTML at /{token}/search; no public JSON feed.")
scrap_talentlyft = _todo("Board JSON is behind the widget; shape unconfirmed.")
scrap_onlyfy = _todo("No public feed found; hosted board is server-rendered.")
scrap_softgarden = _todo("No public feed found.")
scrap_hibob = _todo("No public feed found.")
scrap_njoyn = _todo("Classic ASP board; enumeration is via paged HTML only.")
scrap_digitalrecruiters = _todo(
    "api.digitalrecruiters.com serves the board; endpoint shape unconfirmed."
)

# Enterprise platforms. These run on the employer's own domain with no tenant
# token in the URL, so each needs its own discovery step before any feed can
# be addressed. Expect real work, not a FEEDS row.
scrap_icims = _todo("Paged HTML board; iCIMS exposes no public JSON feed.")
scrap_successfactors = _todo(
    "OData API needs credentials; the public path is the career-site HTML."
)
scrap_taleo = _todo("careersection HTML with POST-driven paging.")
scrap_talentsoft = _todo(
    "Front Office API at /api/v1/offersummaries on the employer's own domain "
    "-- the detector already matches this path, so the base URL is known."
)
scrap_avature = _todo("Employer-hosted templates; no uniform feed.")
scrap_phenom = _todo("Employer-hosted; ph-widget JSON varies per deployment.")
scrap_radancy = _todo("TalentBrew employer-hosted; no uniform feed.")


#: ATS -> vendor scraper. Separate from FEEDS because these are functions, not
#: table rows: either they need logic a Feed cannot express (Workday), or they
#: are not written yet.
VENDOR_SCRAPERS = {
    ATSName.WORKDAY: scrap_workday,
    ATSName.PERSONIO: scrap_personio,
    ATSName.BREEZY: scrap_breezy,
    ATSName.BAMBOOHR: scrap_bamboohr,
    ATSName.PINPOINT: scrap_pinpoint,
    ATSName.COMEET: scrap_comeet,
    ATSName.TEAMTAILOR: scrap_teamtailor,
    ATSName.JAZZHR: scrap_jazzhr,
    ATSName.JOBVITE: scrap_jobvite,
    ATSName.TALENTLYFT: scrap_talentlyft,
    ATSName.ONLYFY: scrap_onlyfy,
    ATSName.SOFTGARDEN: scrap_softgarden,
    ATSName.HIBOB: scrap_hibob,
    ATSName.NJOYN: scrap_njoyn,
    ATSName.DIGITALRECRUITERS: scrap_digitalrecruiters,
    ATSName.ICIMS: scrap_icims,
    ATSName.SUCCESSFACTORS: scrap_successfactors,
    ATSName.TALEO: scrap_taleo,
    ATSName.TALENTSOFT: scrap_talentsoft,
    ATSName.AVATURE: scrap_avature,
    ATSName.PHENOM: scrap_phenom,
    ATSName.RADANCY: scrap_radancy,
}


_JOB_WORD = (
    r"(?:jobs?|offres?|emplois?|vacanc(?:y|ies)|positions?|openings?|"
    r"careers?|stellen?)"
)

_JOB_URL_RE = re.compile(
    rf"/{_JOB_WORD}(?:-d?-?{_JOB_WORD})?/[^/?#]{{3,}}",
    re.I,
)

_LOC_RE = re.compile(r"<loc>\s*([^<\s]+)\s*</loc>", re.I)


def job_urls_from_sitemap(xml: str) -> List[str]:
    """Extract job posting URLs from sitemap XML.

    Args:
        xml: Sitemap XML content.

    Returns:
        List of posting URLs matching job URL pattern.
    """
    seen: Dict[str, None] = {}

    for url in _LOC_RE.findall(xml):
        if _JOB_URL_RE.search(urlparse(url).path or ""):
            seen.setdefault(url, None)

    return list(seen)


def _nested_sitemaps(xml: str) -> List[str]:
    """Find job-related child sitemaps from sitemap index.

    Args:
        xml: Sitemap index XML content.

    Returns:
        List of child sitemap URLs mentioning jobs.
    """
    return [
        url
        for url in _LOC_RE.findall(xml)
        if url.lower().endswith(".xml")
        and re.search(r"job|offre|emploi|vacanc|career|stellen", url, re.I)
    ]


def _find_sitemap_jobs(session, url: str) -> Optional[Tuple[str, List[str]]]:
    """Discover and crawl sitemaps for job URLs.

    Args:
        session: Requests session.
        url: Starting URL to search for sitemaps.

    Returns:
        Tuple of (sitemap_url, job_urls) or None if no jobs found.
    """
    parsed = urlparse(url)
    root = f"{parsed.scheme}://{parsed.netloc}"
    budget = MAX_SITEMAP_REQUESTS

    queue = [
        urljoin(root, "/sitemap.xml"),
        urljoin(root, "/sitemap_index.xml"),
    ]
    # Children of a sitemap index. Drained before robots.txt is tried: a
    # `sitemap-jobs.xml` we already know about beats re-reading robots only to
    # be pointed back at the index we just parsed.
    nested: List[str] = []
    seen: set = set()
    robots_tried = False

    while budget > 0:
        if not queue:
            if nested:
                queue, nested = nested, []
            elif not robots_tried:
                robots_tried = True
                budget -= 1
                robots = _fetch(session, urljoin(root, "/robots.txt"))
                queue = [
                    urljoin(root, line)
                    for line in re.findall(
                        r"(?im)^\s*sitemap:\s*(\S+)", robots or ""
                    )
                ]
            else:
                return None

            continue

        target = queue.pop(0)

        if target in seen:
            continue

        seen.add(target)
        budget -= 1
        xml = _fetch(session, target)

        if not xml:
            continue

        jobs = job_urls_from_sitemap(xml)

        if len(jobs) >= MIN_JOB_URLS:
            return target, jobs

        nested.extend(_nested_sitemaps(xml))

    return None


def _title_from_url(url: str) -> str:
    """Extract and clean job title from URL slug.

    Args:
        url: Job posting URL.

    Returns:
        Capitalized slug or full URL path if no slug found.
    """
    slug = urlparse(url).path.rstrip("/").rsplit("/", 1)[-1]
    slug = re.sub(r"^\d+[-_]?", "", slug)
    words = re.split(r"[-_+]+", slug)

    return " ".join(word.capitalize() for word in words if word) or slug


def _posting_fields(html: str, url: str) -> Tuple[Optional[str], Optional[str]]:
    """Extract title and place from JobPosting JSON-LD.

    Args:
        html: Posting HTML.
        url: Posting URL.

    Returns:
        Tuple of (title, place) or (None, None) if no JSON-LD found.
    """
    page = extract(html, url)

    if not page.is_job_page:
        return None, None

    for node in _walk_jobpostings(page.jsonld):
        title = _first_string(node.get("title"))
        place = _first_string(
            _dig(node, "jobLocation.address.addressLocality")
        ) or _first_string(_dig(node, "jobLocation.address.addressRegion"))

        if title:
            return title, place

    return None, None


def _walk_jobpostings(node: object, depth: int = 0):
    """Recursively yield JobPosting objects from nested JSON-LD.

    Args:
        node: JSON-LD node to traverse.
        depth: Current recursion depth.

    Yields:
        Dict objects with @type containing "jobposting".
    """
    if depth > 12:
        return

    if isinstance(node, dict):
        kind = node.get("@type")

        if "jobposting" in str(kind).lower():
            yield node

        for value in node.values():
            yield from _walk_jobpostings(value, depth + 1)
    elif isinstance(node, list):
        for value in node:
            yield from _walk_jobpostings(value, depth + 1)


def scrap_sitemap(board: "Board") -> List[Job]:
    """Scrape jobs from board sitemap and JSON-LD.

    Args:
        board: Board to scrape.

    Returns:
        List of jobs, empty if no sitemap found.
    """
    found = _find_sitemap_jobs(board.session, board.final_url or board.board_url)

    if found is None:
        return []

    _, job_urls = found
    jobs = []

    for index, url in enumerate(job_urls):
        title = place = None

        if index < MAX_DETAIL:
            html = _fetch(board.session, url)

            if html:
                title, place = _posting_fields(html, url)

        jobs.append(Job(
            company=board.company_name,
            title=title or _title_from_url(url),
            url=url,
            place=place,
            via="sitemap",
        ))

    return jobs


JOB_PATH: Dict[ATSName, str] = {
    ATSName.TEAMTAILOR: r"/(?:careers/)?jobs/\d+-[^/?#]+",
    ATSName.ICIMS: r"/jobs/\d+/[^/?#]+/job",
    ATSName.TALEO: r"jobdetail\.ftl",
    ATSName.BREEZY: r"/p/[0-9a-f]+",
    ATSName.BAMBOOHR: r"/careers/\d+",
    ATSName.PERSONIO: r"/job/\d+",
    ATSName.JAZZHR: r"/apply/[a-z0-9]+",
    ATSName.JOBVITE: r"/[^/?#]+/job/[a-z0-9_-]+",
    ATSName.COMEET: r"/jobs/[^/?#]+/[^/?#]+/[^/?#]+/[^/?#]+",
    ATSName.PINPOINT: r"/jobs/\d+/?$",
}

MIN_TITLE = 3
MAX_TITLE = 200


def scrap_links(board: "Board") -> List[Job]:
    """Scrape jobs from board page anchors by URL pattern.

    Args:
        board: Board to scrape.

    Returns:
        List of jobs from matching anchors.
    """
    html = _fetch(board.session, board.final_url or board.board_url)

    if not html:
        return []

    pattern = re.compile(
        JOB_PATH.get(board.ats) or _JOB_URL_RE.pattern, re.I
    )
    base = board.final_url or board.board_url
    jobs = []

    for tag in BeautifulSoup(html, "html.parser").find_all("a", href=True):
        title = tag.get_text(" ", strip=True)

        if not (MIN_TITLE <= len(title) <= MAX_TITLE):
            continue

        url = urljoin(base, tag["href"].strip())

        if not pattern.search(urlparse(url).path or ""):
            continue

        jobs.append(Job(
            company=board.company_name,
            title=title,
            url=url,
            via="links",
        ))

    return jobs


def _dedupe(jobs: List[Job]) -> List[Job]:
    """Deduplicate jobs by URL, preserving order.

    Args:
        jobs: Job list with possible duplicates.

    Returns:
        Deduplicated job list.
    """
    seen: Dict[str, Job] = {}

    for job in jobs:
        seen.setdefault(urldefrag(job.url).url.rstrip("/"), job)

    return list(seen.values())


class Board:
    """Board scraper for one job board URL.

    Attributes:
        company_name: Company name.
        board_url: Board URL.
        final_url: Resolved URL after redirects.
        ats: Detected ATS platform.
        session: Requests session.
    """

    def __init__(self, cn: str, bu: str, session=None):
        self.company_name = cn
        self.board_url = bu
        self.final_url = bu
        self.ats: Optional[ATSName] = None
        self.board_jobs: List[Job] = []

        self.session = session or requests.Session()

        if session is None:
            self.session.headers.update(HEADERS)

    def detect_ats(self) -> Optional[ATSName]:
        """Detect ATS platform for this board.

        Returns:
            ATSName if detected, None otherwise.
        """
        detected = ATSDetector().detect(self.board_url)

        self.final_url = detected.final_url or self.board_url
        self.ats = detected.detected_ats

        return self.ats

    def scrap_board(self) -> List[Job]:
        """Scrape jobs using best available strategy.

        Tries: vendor feed, sitemap + JSON-LD, then anchor links.

        Returns:
            List of jobs, deduplicated.
        """
        for strategy in (self._feed, self._sitemap, self._links):
            jobs = strategy()

            if jobs:
                self.board_jobs = _dedupe(jobs)

                return self.board_jobs

        self.board_jobs = []

        return self.board_jobs

    def _feed(self) -> List[Job]:
        """Try vendor scraper, then vendor feed."""
        scraper = VENDOR_SCRAPERS.get(self.ats)

        if scraper is not None:
            try:
                return scraper(self)
            except NotImplementedError:
                return []

        feed = FEEDS.get(self.ats)

        return scrap_feed(self, feed) if feed else []

    def _sitemap(self) -> List[Job]:
        """Scrape via sitemap strategy."""
        return scrap_sitemap(self)

    def _links(self) -> List[Job]:
        """Scrape via anchor link strategy."""
        return scrap_links(self)


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(
        description="Scrape every posting off one job board"
    )
    parser.add_argument("url")
    parser.add_argument("--company", default="?")
    parser.add_argument("--show", type=int, default=10)

    args = parser.parse_args()

    board = Board(args.company, args.url)

    print(f"Board:    {args.url}")
    print(f"ATS:      {board.detect_ats() or 'unknown'}")
    print(f"Resolved: {board.final_url}")

    found = board.scrap_board()

    print(f"Strategy: {found[0].via if found else 'none'}")
    print(f"Jobs:     {len(found)}\n")

    for job in found[:args.show]:
        print(f"  {job.title}")
        print(f"    {job.url}")
        print(f"    place: {job.place or '-'}")
