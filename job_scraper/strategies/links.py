"""Strategy 4: the last non-browser resort -- filter the page's own anchors."""

from __future__ import annotations

from typing import TYPE_CHECKING, Dict, List, Optional
from urllib.parse import urldefrag, urljoin

from bs4 import BeautifulSoup

from job_scraper.detector import ATSName
from job_scraper.models import Job
from job_scraper.urls import job_href_matches, title_from_url

if TYPE_CHECKING:
    from job_scraper.board import Board

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
    # "jobs" sits in the hostname (jobs.ashbyhq.com), not the path, so the
    # generic shape -- which requires a job word in the path -- never matches
    # a bare /{tenant}/{uuid}. Same UUID shape as the detector's own
    # ashby.posting_uuid rule (detector.py), kept in sync with it.
    ATSName.ASHBY: (
        r"^/[^/?#]+/[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}"
        r"-[0-9a-f]{12}"
    ),
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
    """Check if two URLs point to the same page, ignoring fragment and slash.

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
        html: Optional board page HTML to parse. If None, fetches from
            board.html. Allows the rendered strategy to reuse this pipeline on
            browser output.

    Returns:
        List of Job results with URL and title, or empty list if no anchors
        match.
    """
    html = html or board.html

    if not html:
        return []

    base = board.url
    jobs = []

    # Not Page.anchor_urls: extract() collects hrefs but discards anchor text,
    # and growing a detector structure for a scraping need is the larger diff.
    for tag in BeautifulSoup(html, "html.parser").find_all("a", href=True):
        title = tag.get_text(" ", strip=True)

        # An out-of-range length is a reason to distrust the *title*, not to
        # drop the posting -- same trade as the boilerplate check just below.
        # A card whose title lives in a nested element renders as "" here; a
        # card whose anchor wraps the whole tile renders as the entire tile's
        # text. Both still name a real job once the fallback below reads the
        # card heading or slug instead.
        if not (MIN_TITLE <= len(title) <= MAX_TITLE):
            title = ""

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

        if not job_href_matches(url, board.ats, JOB_PATH):
            continue

        jobs.append(Job(
            company=board.company_name,
            # Anchor label, then the card's heading, then the slug. The slug
            # is genuinely last resort: it is a UUID on Inetum and a bare id
            # on plenty of others.
            title=title or _card_title(tag) or title_from_url(url),
            url=url,
            via="links",
        ))

    return jobs
