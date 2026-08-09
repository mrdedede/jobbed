"""Scrape job postings from a board using ATS-specific logic.

Scraping tries strategies cheapest-first: feed (JSON), WordPress REST, sitemap
(JSON-LD), links (heuristic), and -- only with --render -- a browser pass for
boards that build their listing in JS. Each Job records its source in the `via`
field, which ensures a misdetected ATS doesn't silently get scraped by the
wrong strategy.

Usage:
    python -m job_scrapper.board_scraper <url> [--company NAME] [--show N]
                                                [--render]

Run it as a module, not by path: the imports below are absolute, so
`python job_scrapper/board_scraper.py` puts this directory on sys.path
instead of the repo root and fails before __main__ is reached.
"""

from __future__ import annotations

import json
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass, replace
from html import unescape
from typing import Dict, List, Optional, Tuple
from urllib.parse import urldefrag, urljoin, urlparse

import requests
from bs4 import BeautifulSoup

from job_scrapper.detector import (
    ATS_REGISTRY,
    ATSDetector,
    ATSName,
    Renderer,
    _host_hit,
    _walk_strings,
    extract,
)

HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; JobResearchBot/1.0)",
    "Accept": "application/json, text/html;q=0.9, */*;q=0.8",
}

MAX_FETCH_BYTES = 2_000_000

# Feeds are whole boards in one response and run far larger than a page: a
# single JazzHR export measured 2.7 MB. At MAX_FETCH_BYTES the body is cut
# mid-document, the parse fails, and the board silently falls through to the
# sitemap looking like it had no feed at all.
FEED_MAX_BYTES = 20_000_000

# Budget for sitemap discovery requests.
MAX_SITEMAP_REQUESTS = 4

# Minimum job URLs to treat as a valid sitemap.
MIN_JOB_URLS = 3

# Maximum detail postings to fetch individually (one request each).
MAX_DETAIL = 200

# Safety stop for a paged feed whose total never resolves. At the 100/page the
# vendors use this is 10k postings -- larger than any single board seen.
FEED_MAX_PAGES = 100


@dataclass(frozen=True)
class Job:
    """Job posting with source metadata.

    Attributes:
        company: Company name.
        title: Job title.
        url: Job posting URL.
        place: Location (may be None).
        via: Source strategy. "feed" for a FEEDS row, the vendor's own name
            for a scraper that needed its own logic (workday, comeet), or
            wordpress/sitemap/links for the generic fallbacks. "rendered"
            means it took a browser -- the board is unscrapable without one.
    """

    company: str
    title: str
    url: str
    place: Optional[str] = None
    via: str = ""


def _fetch(session, url: str, timeout: int = 20,
           method: str = "get", max_bytes: int = MAX_FETCH_BYTES,
           **kwargs) -> Optional[str]:
    """Best-effort HTTP request returning text or None.

    Args:
        session: Requests session.
        url: URL to fetch.
        timeout: Request timeout in seconds.
        method: HTTP method (get, post, etc).
        max_bytes: Maximum body size to read.
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

            raw = response.raw.read(max_bytes, decode_content=True)

            return raw.decode(response.encoding or "utf-8", errors="replace")
    except requests.RequestException:
        return None


def _fetch_json(session, url: str, **kwargs) -> Optional[object]:
    body = _fetch(session, url, **kwargs)

    if not body:
        return None

    try:
        return json.loads(body)
    except ValueError:
        return None


def _fetch_xml_items(session, url: str, tag: str,
                     **kwargs) -> Optional[List[dict]]:
    """Fetch an XML feed and flatten each posting element into a dict.

    Personio and JazzHR publish XML where every posting is one element whose
    children are flat text fields. Flattening those to dicts lets the whole
    JSON feed engine -- `_dig`, `_first_string`, `link_url` -- read them
    unchanged, which is why neither vendor needs a scraper function.

    Args:
        session: Requests session.
        url: Feed URL.
        tag: Element name holding one posting (e.g. "position", "item").
        **kwargs: Additional arguments passed to the fetch.

    Returns:
        List of dicts (one per posting), or None on fetch/parse failure.
    """
    body = _fetch(session, url, **kwargs)

    if not body:
        return None

    try:
        root = ET.fromstring(body)
    except ET.ParseError:
        return None

    # ponytail: leaf text only, so a nested wrapper (Personio's
    # <additionalOffices>) flattens to "". The primary <office> is what `place`
    # reads -- revisit only if a feed puts a needed field behind a wrapper.
    return [
        {child.tag: (child.text or "").strip() for child in node}
        for node in root.iter(tag)
    ]


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
        item_tag: XML element holding one posting. Set = body is XML, not JSON.
        total: Dotted path to the count of postings on the whole board. Set
            together with page_param to page a feed that caps its response.
        page_param: Query parameter carrying the number of items already read.
    """

    url: str
    token: Tuple[str, ...]
    items: str = ""
    title: str = "title"
    place: str = "location"
    link: str = "url"
    link_url: str = ""
    item_tag: str = ""
    total: str = ""
    page_param: str = ""


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
        # 100 is this endpoint's ceiling, so a board any larger came back
        # silently truncated until these two were set.
        total="totalFound",
        page_param="offset",
    ),
    ATSName.BREEZY: Feed(
        url="https://{token}.breezy.hr/json",
        token=(r"//([\w-]+)\.breezy\.hr",),
        # Body is the list itself; _dig("") returns it unchanged.
        items="",
        title="name",
        # Not "location": that dict leads with country, so _first_string would
        # report every posting as its country instead of its city.
        place="location.city",
        link="url",
    ),
    ATSName.BAMBOOHR: Feed(
        url="https://{token}.bamboohr.com/careers/list",
        token=(r"//([\w-]+)\.bamboohr\.com",),
        items="result",
        title="jobOpeningName",
        # `location` exists but is routinely all-null; atsLocation is filled.
        place="atsLocation.city",
        link_url="https://{token}.bamboohr.com/careers/{id}",
    ),
    ATSName.PINPOINT: Feed(
        url="https://{token}.pinpointhq.com/postings.json",
        token=(r"//([\w-]+)\.pinpointhq\.com",),
        items="data",
        title="title",
        # Same trap as Breezy, worse: this dict leads with a numeric id, so
        # _first_string would report the location as "283".
        place="location.city",
        link="url",
    ),
    ATSName.PERSONIO: Feed(
        url="https://{token}.jobs.personio.de/xml",
        token=(r"//([\w-]+)\.jobs\.personio\.(?:de|com)",),
        item_tag="position",
        title="name",
        place="office",
        # The XML carries no URL at all, only an id.
        link_url="https://{token}.jobs.personio.de/job/{id}",
    ),
    ATSName.JAZZHR: Feed(
        # The export lives on app.jazz.co keyed by subdomain, not on the
        # tenant's own applytojob.com host -- that host 404s this path.
        url="https://app.jazz.co/feeds/export/jobs/{token}",
        token=(r"//([\w-]+)\.applytojob\.com", r"//([\w-]+)\.jazz\.co"),
        item_tag="job",
        title="title",
        place="city",
        link="url",
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


def _with_param(url: str, name: str, value: int) -> str:
    """Add a paging parameter, keeping any query the endpoint template has.

    Args:
        url: Endpoint, which may already carry a query (SmartRecruiters
            templates in its own `?limit=100`).
        name: Parameter name; empty means no paging, return url unchanged.
        value: Parameter value.

    Returns:
        The URL with the parameter appended.
    """
    # Offset 0 is the default everywhere, so leaving the first request
    # untouched keeps every unpaged vendor's traffic byte-identical.
    if not name or not value:
        return url

    return f"{url}{'&' if '?' in url else '?'}{name}={value}"


def _feed_jobs(board: "Board", feed: Feed, token: str,
               items: List[object]) -> List[Job]:
    """Map one page of feed items onto Jobs, skipping unusable rows.

    Args:
        board: Board being scraped.
        feed: Feed configuration.
        token: Tenant token, for link_url templates.
        items: Raw items from one response.

    Returns:
        Jobs for the items that carry both a title and a URL.
    """
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


def scrape_feed(board: "Board", feed: Feed) -> List[Job]:
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

    endpoint = feed.url.format(token=token)
    jobs: List[Job] = []
    seen = 0

    for _ in range(FEED_MAX_PAGES):
        target = _with_param(endpoint, feed.page_param, seen)

        if feed.item_tag:
            body = None
            items = _fetch_xml_items(
                board.session, target, feed.item_tag,
                max_bytes=FEED_MAX_BYTES,
            )
        else:
            body = _fetch_json(
                board.session, target, max_bytes=FEED_MAX_BYTES
            )
            items = _dig(body, feed.items)

        if not isinstance(items, list) or not items:
            break

        seen += len(items)
        jobs.extend(_feed_jobs(board, feed, token, items))

        # One page unless the vendor publishes a total to page against.
        if not feed.page_param:
            break

        total = _dig(body, feed.total)

        if not isinstance(total, int) or seen >= total:
            break

    return jobs


_LOCALE_RE = re.compile(r"^[a-z]{2}(?:-[a-z]{2})?$", re.I)

WORKDAY_PAGE = 20
WORKDAY_MAX_PAGES = 50


def scrape_workday(board: "Board") -> List[Job]:
    """Scrape Workday board via /wday/cxs/ JSON endpoint.

    Args:
        board: Board to scrape.

    Returns:
        List of jobs from board.
    """
    parsed = urlparse(board.url)
    tenant = parsed.hostname.split(".")[0] if parsed.hostname else ""
    segments = [part for part in parsed.path.split("/") if part]

    if segments and _LOCALE_RE.match(segments[0]):
        segments = segments[1:]

    if not tenant or not segments:
        return []

    site = segments[0]
    root = f"{parsed.scheme}://{parsed.hostname}"
    endpoint = f"{root}/wday/cxs/{tenant}/{site}/jobs"
    base = urljoin(board.url, parsed.path.rstrip("/"))

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


# The board page bootstraps itself with a JSON blob carrying both values.
_COMEET_UID = re.compile(r'"company_uid"\s*:\s*"([\w.]+)"')
_COMEET_TOKEN = re.compile(r'"token"\s*:\s*"([0-9A-Fa-f]{16,})"')

COMEET_API = (
    "https://www.comeet.co/careers-api/2.0/company/{uid}"
    "/positions?token={token}"
)


def scrape_comeet(board: "Board") -> List[Job]:
    """Scrape Comeet through its careers API.

    The API is keyed by a company UID that never appears in the board URL, and
    it rejects a UID on its own with "Token is missing" -- so both values have
    to be lifted from the board page's inline bootstrap JSON first. That extra
    request is why this cannot be a FEEDS row.

    Args:
        board: Board to scrape.

    Returns:
        List of jobs, or empty list if discovery or the API fails.
    """
    html = board.html

    if not html:
        return []

    uid = _COMEET_UID.search(html)
    token = _COMEET_TOKEN.search(html)

    if not uid or not token:
        return []

    items = _fetch_json(
        board.session,
        COMEET_API.format(uid=uid.group(1), token=token.group(1)),
        max_bytes=FEED_MAX_BYTES,
    )

    if not isinstance(items, list):
        return []

    jobs = []

    for item in items:
        if not isinstance(item, dict):
            continue

        title = _first_string(item.get("name"))
        url = _first_string(item.get("url_comeet_hosted_page"))

        if not title or not url:
            continue

        jobs.append(Job(
            company=board.company_name,
            title=title,
            url=url,
            # Sibling "name" is the full "Tel Aviv, Israel"; city is the field
            # the other feeds report, so stay consistent.
            place=_first_string(_dig(item, "location.city")),
            via="comeet",
        ))

    return jobs


_NJOYN_JOB = re.compile(r"Page=JobDetails", re.I)


def scrape_njoyn(board: "Board") -> List[Job]:
    """Scrape an njoyn board out of its listing table.

    njoyn publishes no feed, but the board page already carries every posting
    in a table with a real header row. A link-only pass cannot read it: each
    posting is linked twice, and neither anchor is its title -- one is the
    requisition id and the other says "View Job Details". The title is a
    sibling cell, so the row has to be read as a row.

    Columns are located by header name rather than position, since the board
    is localised and the column order is not guaranteed.

    Args:
        board: Board to scrape.

    Returns:
        List of jobs, or empty list if no listing table is present.
    """
    html = board.html

    if not html:
        return []

    base = board.url
    jobs = []

    for table in BeautifulSoup(html, "html.parser").find_all("table"):
        rows = table.find_all("tr")

        if not rows:
            continue

        header = [
            cell.get_text(" ", strip=True).casefold()
            for cell in rows[0].find_all(["th", "td"])
        ]

        if "title" not in header:
            continue

        title_at = header.index("title")
        city_at = header.index("city") if "city" in header else None

        for row in rows[1:]:
            link = row.find("a", href=_NJOYN_JOB)

            if not link:
                continue

            cells = [
                cell.get_text(" ", strip=True)
                for cell in row.find_all(["td", "th"])
            ]

            if title_at >= len(cells) or not cells[title_at]:
                continue

            place = None

            if city_at is not None and city_at < len(cells):
                place = cells[city_at] or None

            jobs.append(Job(
                company=board.company_name,
                title=cells[title_at],
                url=urljoin(base, link["href"].strip()),
                place=place,
                via="njoyn",
            ))

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


scrape_teamtailor = _todo(
    "Public API needs an API key. The hosted board is server-rendered, so "
    "sitemap/links already do well here -- low priority."
)
scrape_jobvite = _todo(
    "Board HTML at /{token}/search; no public JSON feed. Probed 2026-08-09: "
    "jobs.jobvite.com serves the same 'Job Seeker FAQs' page for any unknown "
    "tenant, so a wrong slug looks like a 200 rather than a 404 -- do not "
    "trust a non-empty response as proof the tenant exists. Blocked on "
    "capturing a real tenant board first."
)
scrape_talentlyft = _todo("Board JSON is behind the widget; shape unconfirmed.")
scrape_digitalrecruiters = _todo(
    "Probed 2026-08-09. The board metadata endpoint is public -- "
    "api.digitalrecruiters.com/careers/v1/careers-sites/{board_host} returns "
    "200 with the site config, keyed by the careers hostname rather than by "
    "any token in the path. The postings themselves are under "
    "/public/v1/careers-sites/{board_host}/job-ads, which answers 403 "
    "'You're not allowed to access this resource'. The page's dr-lkey-token "
    "header and both inline tokens were tried against it and all still 403, "
    "so the credential is not published on the board page. Blocked until a "
    "browser session is captured; the generic path handles it meanwhile."
)
scrape_onlyfy = _todo("No public feed found; hosted board is server-rendered.")
scrape_softgarden = _todo("No public feed found.")
scrape_hibob = _todo(
    "No public feed found, and no JOB_PATH row either: the corpus fixtures "
    "carry zero same-host job anchors, so the listing is rendered client-side "
    "and there is nothing for scrape_links to filter. Postings themselves are "
    "/jobs/{uuid}. Needs a renderer before either strategy can see them."
)

# Enterprise platforms. These run on the employer's own domain with no tenant
# token in the URL, so each needs its own discovery step before any feed can
# be addressed. Expect real work, not a FEEDS row.
scrape_icims = _todo("Paged HTML board; iCIMS exposes no public JSON feed.")
scrape_successfactors = _todo(
    "OData API needs credentials; the public path is the career-site HTML. "
    "Probed 2026-08-09: jobs.hr.cloud.sap does not resolve on its own -- the "
    "registry hosts are suffixes for per-customer subdomains, not reachable "
    "boards, so this needs a real customer career site captured first."
)
scrape_taleo = _todo(
    "careersection HTML with POST-driven paging. Probed 2026-08-09: taleo "
    "tenants live on per-customer hosts ({tenant}.taleo.net) and none could "
    "be reached without one from a real board, so the paging contract is "
    "still unverified. JOB_PATH[TALEO] covers jobdetail.ftl links meanwhile."
)
scrape_talentsoft = _todo(
    "Probed 2026-08-09. /api/v1/offersummaries is NOT callable on the "
    "employer's domain -- Feu Vert (confirmed Talentsoft via its inline "
    "TALENTSOFT-FRONT-OFFICE config) serves its SPA shell for that path and "
    "every other unknown one, so the detector matching the path says only "
    "that the SPA calls it, not that we can. Cegid's own docs put the public "
    "contract at api/v2/offersummaries behind partner credentials. Needs a "
    "real Talentsoft-hosted board captured first; the "
    "*-careers.talentsoft.com pattern does not resolve."
)
scrape_avature = _todo(
    "Employer-hosted templates; no uniform feed. JOB_PATH[AVATURE] covers the "
    "board instead, and matters more than most: without it the generic shape "
    "matches no Avature posting at all and returns marketing pages instead."
)
scrape_phenom = _todo(
    "Employer-hosted; ph-widget JSON varies per deployment. Like hibob, the "
    "corpus fixture has no same-host job anchors -- the results list is drawn "
    "client-side, so there is no JOB_PATH row worth adding."
)
scrape_radancy = _todo(
    "TalentBrew employer-hosted; no uniform feed. Unlike the others here the "
    "board is server-rendered, so JOB_PATH[RADANCY] handles it: verified "
    "against the synopsys fixtures, which drop the /search-jobs style "
    "collection pages the generic shape lets through."
)


#: ATS -> vendor scraper. Separate from FEEDS because these are functions, not
#: table rows: either they need logic a Feed cannot express (Workday), or they
#: are not written yet.
VENDOR_SCRAPERS = {
    ATSName.WORKDAY: scrape_workday,
    ATSName.COMEET: scrape_comeet,
    ATSName.TEAMTAILOR: scrape_teamtailor,
    ATSName.JOBVITE: scrape_jobvite,
    ATSName.TALENTLYFT: scrape_talentlyft,
    ATSName.ONLYFY: scrape_onlyfy,
    ATSName.SOFTGARDEN: scrape_softgarden,
    ATSName.HIBOB: scrape_hibob,
    ATSName.NJOYN: scrape_njoyn,
    ATSName.DIGITALRECRUITERS: scrape_digitalrecruiters,
    ATSName.ICIMS: scrape_icims,
    ATSName.SUCCESSFACTORS: scrape_successfactors,
    ATSName.TALEO: scrape_taleo,
    ATSName.TALENTSOFT: scrape_talentsoft,
    ATSName.AVATURE: scrape_avature,
    ATSName.PHENOM: scrape_phenom,
    ATSName.RADANCY: scrape_radancy,
}


_JOB_WORD = (
    r"(?:jobs?|offres?|emplois?|vacanc(?:y|ies)|positions?|openings?|"
    r"careers?|carrieres?|stellen?|recrutement)"
)

#: The posting segment. A real posting slug is either hyphenated
#: ("data-engineer") or a bare id ("842306"); a single bare word is a category
#: or listing page. This is what stops the `[\w]*-?` prefix below from
#: swallowing /nos-offres/localisations along with /nos-offres/dev-senior.
_JOB_SLUG = r"(?:[^/?#]*-[^/?#]*|\d{3,})"

#: `[\w]*-?` is the prefix French boards need: without it the job word has to
#: sit directly after a slash, so /nos-offres/... and /offres-emploi/... match
#: nothing and whole boards (Davidson, Crédit Agricole) come back empty.
#:
#: Measured and rejected, so it is not re-tried:
#: - "join-us"/"nous-rejoindre" as job words. Buys Extia's 20 real postings
#:   and costs 10 false ones -- Atos and Equans both file /join-us/life-at-atos
#:   and /nous-rejoindre/faq-candidats under the same prefix, and the slug
#:   guard cannot tell those from a posting since they are hyphenated too.
#: - Grouping anchors by repeated path shape. On this corpus only Extia clears
#:   a useful threshold, and Equans' /votre-activite/ group clears it too, so
#:   the heuristic nets one real board and one wrong one.
#: Both boards need the API tier instead.
_JOB_URL_RE = re.compile(
    rf"/[\w]*-?{_JOB_WORD}(?:-d?-?{_JOB_WORD})?/{_JOB_SLUG}",
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
    # Boards that serve postings as pages leave the extension on the slug,
    # which otherwise arrives as a "Html" word at the end of every title.
    slug = re.sub(r"\.(?:html?|aspx?|php|jsp)$", "", slug, flags=re.I)
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
    """Yield every dict in a JSON tree with @type matching JobPosting.

    Args:
        node: JSON object to traverse (dict, list, or scalar).
        depth: Current recursion depth; stops at 12 to avoid infinite loops.

    Yields:
        Dicts with @type containing "jobposting" (case-insensitive).
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


def scrape_sitemap(board: "Board") -> List[Job]:
    """Enumerate postings from a sitemap, then read each one's JSON-LD.

    Args:
        board: Board instance with session and URL.

    Returns:
        List of Job results, or empty list if no sitemap found.
    """
    found = _find_sitemap_jobs(board.session, board.url)

    if found is None:
        return []

    _, job_urls = found
    jobs = []

    for index, url in enumerate(job_urls):
        title = place = None

        # One request per posting, so the cap is the real cost ceiling. Past
        # it we still emit the job, just with a slug title.
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


# ======================================================================
# STRATEGY 3: ANCHOR LINKS
# ======================================================================

#: Job-URL shapes per ATS, for filtering anchors on a board page.
#:
#: These deliberately duplicate a handful of regexes that resemble ones in
#: detector.ATS_REGISTRY. The registry's are compiled into Matcher closures --
#: the pattern string is not reachable -- and they serve *gating* (is this
#: vendor's page?) rather than *filtering* (is this link a posting?). Wiring
#: them together would mean detector precision tuning silently changes scraper
#: output. Ten short regexes is the cheaper trade.
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
    # Locale-prefixed (/en_US/, /de_DE/). Worth a row even though these boards
    # have no feed: the generic shape needs a job word straight after a slash,
    # so "externaljobs" never matches it and Avature postings are missed
    # outright rather than merely scraped loosely.
    ATSName.AVATURE: r"/externaljobs/jobdetail/\d+",
    # Trailing ids are /{req}/{tracking} and both are numeric. Requiring only
    # the first also matched /44408/UnderstandingRecruitmentFraud, a policy
    # page sitting at a posting-shaped URL.
    ATSName.RADANCY: r"/job/[^/?#]+/[^/?#]+/\d+/\d+",
    # njoyn addresses every posting through one ASP endpoint, so the path is
    # identical for the board and its jobs and only the query tells them
    # apart. Containing "?" is what opts this row into query matching below.
    ATSName.NJOYN: r"xweb\.asp\?.*\bPage=JobDetails\b.*\bJobid=",
}

#: An anchor label shorter than this is a chevron or a badge, not a job title.
MIN_TITLE = 3
MAX_TITLE = 200

#: Labels that point at a posting but never name it. Boards link the same
#: posting from a skip-link, a card, and a "read more" button, and _dedupe
#: keeps whichever came first -- so without this a real job is titled
#: "Skip to main content". Dropped here rather than in _dedupe because these
#: are never a job title for any caller, whichever order they arrive in.
SKIP_TITLES = frozenset({
    "skip to main content", "skip to content", "skip to job description",
    "learn more", "read more", "see more", "show more", "view details",
    "view job", "view job details", "view all jobs", "apply", "apply now",
    "details",
    "en savoir plus", "voir l'offre", "voir loffre", "voir plus",
    "voir toutes les offres", "postuler", "en savoir +", "lire la suite",
    "mehr erfahren", "jetzt bewerben",
})


#: WordPress registers job boards as a custom post type, and the name is the
#: site owner's choice -- "job" on one board here, "offres" on another. The
#: type list is public, so the name is discovered rather than guessed.
_WP_TYPES = "/wp-json/wp/v2/types"

#: Every WordPress theme ships assets from wp-content, and most expose the REST
#: root as a wp-json link tag. Either is enough to justify the probe below.
_WP_MARKER = re.compile(r"wp-content|wp-json", re.I)
_WP_JOB_TYPE = re.compile(
    r"job|offre|emploi|career|carriere|poste|recrut|vacan", re.I
)

#: WordPress caps per_page at 100.
WP_PAGE = 100
WP_MAX_PAGES = 20


def scrape_wordpress(board: "Board") -> List[Job]:
    """Scrape a WordPress careers site through its REST API.

    Plenty of employer career sites are just WordPress, which publishes every
    custom post type at /wp-json/wp/v2/<type> with a real title and a public
    link. That beats both fallbacks: the sitemap strategy costs one request
    per posting to recover the same title, and the link strategy only ever
    sees whatever the theme happened to put in an anchor.

    Args:
        board: Board to scrape.

    Returns:
        List of jobs, or empty list if this is not WordPress or has no
        job-shaped post type.
    """
    # Costs nothing to check and saves a request on every board that is not
    # WordPress -- 25 of the 35 in the corpus. Nor can it hide a board: the
    # endpoint below is built from the board's own host, so a site whose REST
    # API lives elsewhere was already out of reach for this strategy.
    if not board.html or not _WP_MARKER.search(board.html):
        return []

    base = board.url
    parsed = urlparse(base)
    root = f"{parsed.scheme}://{parsed.netloc}"

    types = _fetch_json(board.session, urljoin(root, _WP_TYPES))

    if not isinstance(types, dict):
        return []

    name = next(
        (key for key in types if _WP_JOB_TYPE.search(key)),
        None,
    )

    if not name:
        return []

    endpoint = urljoin(root, f"/wp-json/wp/v2/{name}")
    jobs: List[Job] = []

    for page_number in range(1, WP_MAX_PAGES + 1):
        posts = _fetch_json(
            board.session,
            f"{endpoint}?per_page={WP_PAGE}&page={page_number}",
            max_bytes=FEED_MAX_BYTES,
        )

        if not isinstance(posts, list) or not posts:
            break

        for post in posts:
            if not isinstance(post, dict):
                continue

            title = _first_string(_dig(post, "title.rendered"))
            url = _first_string(post.get("link"))

            if not title or not url:
                continue

            jobs.append(Job(
                company=board.company_name,
                # Titles come back HTML-escaped ("Go developer &#8211; Team").
                title=unescape(title),
                url=url,
                via="wordpress",
            ))

        if len(posts) < WP_PAGE:
            break

    return jobs


#: How far up from the anchor to look for the card's heading. Three levels
#: covers the usual <div class=card><h3>title</h3>...<a>read more</a></div>
#: without drifting up into the page banner.
CARD_DEPTH = 3


def _card_title(tag) -> str:
    """The heading of the card an anchor sits in.

    Boards routinely label the link itself "Lire la suite" and put the real
    title in a heading beside it -- Inetum does this for all 1620 of its
    postings. Checked before the slug, which on that board is a bare UUID.

    Args:
        tag: The anchor element.

    Returns:
        Heading text, or "" if no usable heading is nearby.
    """
    parent = tag

    for _ in range(CARD_DEPTH):
        parent = parent.parent

        if parent is None:
            break

        heading = parent.find(["h1", "h2", "h3", "h4", "h5"])

        if heading is None:
            continue

        text = heading.get_text(" ", strip=True)

        if MIN_TITLE <= len(text) <= MAX_TITLE:
            return text

    return ""


def _same_page(url: str, base: str) -> bool:
    """Check if two URLs point to the same page, ignoring fragment and trailing slash.

    Args:
        url: URL to test.
        base: Base URL to compare against.

    Returns:
        True if url and base normalize to the same page URL.
    """
    return (urldefrag(url).url.rstrip("/")
            == urldefrag(base).url.rstrip("/"))


def scrape_links(board: "Board", html: Optional[str] = None) -> List[Job]:
    """Extract jobs from anchor tags using heuristic URL/title patterns.

    Args:
        board: Board to scrape.
        html: Optional board page HTML to parse. If None, fetches from board.html.
            Allows the rendered strategy to reuse this pipeline on browser output.

    Returns:
        List of Job results with URL and title, or empty list if no anchors match.
    """
    html = html or board.html

    if not html:
        return []

    shape = JOB_PATH.get(board.ats) or _JOB_URL_RE.pattern
    pattern = re.compile(shape, re.I)
    # Most boards put the posting id in the path, so matching the path alone
    # keeps a query string full of filters from creating false positives.
    # A row that spells out "?" is asking for the query too -- the only way to
    # express a vendor like njoyn, whose postings all share one path.
    with_query = "?" in shape
    base = board.url
    jobs = []

    # Not Page.anchor_urls: extract() collects hrefs but discards anchor text,
    # and growing a detector structure for a scraping need is the larger diff.
    for tag in BeautifulSoup(html, "html.parser").find_all("a", href=True):
        title = tag.get_text(" ", strip=True)

        if not (MIN_TITLE <= len(title) <= MAX_TITLE):
            continue

        # A boilerplate label is a reason to distrust the *title*, not to drop
        # the posting: Inetum labels all 1620 of its jobs "Lire la suite", so
        # skipping the anchor would lose the whole board. Fall back to the
        # slug, the same trade scrape_sitemap already makes.
        if title.casefold().rstrip(" .>→") in SKIP_TITLES:
            title = ""

        url = urljoin(base, tag["href"].strip())

        # A link back to the page we are already on is navigation, not a
        # posting. On a posting page these are the section jumps
        # (#anchor-overview, #anchor-benefits); _dedupe strips the fragment,
        # so they all collapse onto the real posting and the first label --
        # "Career Areas" -- wins over its actual title.
        if _same_page(url, base):
            continue

        parts = urlparse(url)
        target = (
            f"{parts.path}?{parts.query}" if with_query and parts.query
            else parts.path
        )

        if not pattern.search(target or ""):
            continue

        jobs.append(Job(
            company=board.company_name,
            # Anchor label, then the card's heading, then the slug. The slug
            # is genuinely last resort: it is a UUID on Inetum and a bare id
            # on plenty of others.
            title=title or _card_title(tag) or _title_from_url(url),
            url=url,
            via="links",
        ))

    return jobs


# ======================================================================
# BOARD
# ======================================================================


def _dedupe(jobs: List[Job]) -> List[Job]:
    """Deduplicate jobs by URL, preserving order and first title.

    Args:
        jobs: List of Job results, possibly with duplicate URLs.

    Returns:
        List with one row per unique posting URL. If a URL appears multiple times,
        the first Job result is kept; subsequent duplicates are discarded.
    """
    seen: Dict[str, Job] = {}

    for job in jobs:
        seen.setdefault(urldefrag(job.url).url.rstrip("/"), job)

    return list(seen.values())


def _ats_from_host(url: str) -> Optional[ATSName]:
    """Name the ATS from a vendor-owned hostname alone, without fetching.

    The detector reads the page, so a board that blocks it -- Personio answers
    429 and redirects to marketing -- comes back unknown, no feed is tried, and
    both fallbacks then fail on the same blocked HTML. Yet its XML feed serves
    fine, and the hostname already said which vendor it is.

    Deliberately only vendor-owned hosts: an employer's own careers domain says
    nothing about the ATS behind it, which is the whole reason detector.py
    scores evidence instead of matching URLs.

    Args:
        url: Board URL.

    Returns:
        The ATS owning the hostname, or None.
    """
    host = urlparse(url).hostname or ""

    for ats in ATS_REGISTRY:
        if ats.hosts and _host_hit(host, ats.hosts):
            return ats.name

    return None


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
                a new session is created with default headers.
            render: Optional Playwright browser renderer for JS-rendered boards.
                None disables browser rendering; keep this way if your machine
                has no browser.
        """
        self.company_name = company_name
        self.board_url = board_url
        self.render = render
        self.final_url = board_url
        self.ats: Optional[ATSName] = None
        self.board_jobs: List[Job] = []
        self._html: object = _UNFETCHED

        self.session = session or requests.Session()

        if session is None:
            self.session.headers.update(HEADERS)

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
        """
        if self._html is _UNFETCHED:
            self._html = _fetch(self.session, self.url)

        return self._html  # type: ignore[return-value]

    def detect_ats(self) -> Optional[ATSName]:
        """Detect the ATS powering this board.

        Runs HTTP analysis, with optional browser pass if a renderer is available.
        Updates self.final_url and self.ats in place.

        Returns:
            Detected ATS name (ATSName enum), or None if unknown.
        """
        detected = ATSDetector(render=self.render).detect(self.board_url)

        self.final_url = detected.final_url or self.board_url
        self.ats = detected.detected_ats or _ats_from_host(self.board_url)

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
        """
        for strategy in (self._feed, self._wordpress,
                         self._sitemap, self._links, self._rendered):
            jobs = strategy()

            if jobs:
                self.board_jobs = _dedupe(jobs)

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
            try:
                return scraper(self)
            except NotImplementedError:
                return []

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


# ======================================================================
# CLI
# ======================================================================


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(
        description="Scrape every posting off one job board"
    )
    parser.add_argument("url")
    parser.add_argument("--company", default="?")
    parser.add_argument("--show", type=int, default=10)
    parser.add_argument(
        "--render", action="store_true",
        help="last-resort browser pass for boards that build their listing "
             "in JS. Needs: pip install playwright && playwright install "
             "chromium",
    )

    args = parser.parse_args()

    renderer = None

    if args.render:
        # Imported here, not at module scope: Playwright is an opt-in extra.
        from job_scrapper.render import render as renderer

    board = Board(args.company, args.url, render=renderer)

    print(f"Board:    {args.url}")
    print(f"ATS:      {board.detect_ats() or 'unknown'}")
    print(f"Resolved: {board.final_url}")

    found = board.scrape_board()

    print(f"Strategy: {found[0].via if found else 'none'}")
    print(f"Jobs:     {len(found)}\n")

    for job in found[:args.show]:
        print(f"  {job.title}")
        print(f"    {job.url}")
        print(f"    place: {job.place or '-'}")
