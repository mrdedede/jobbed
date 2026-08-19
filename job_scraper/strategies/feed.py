"""Strategy 1: read a whole board out of its ATS vendor's public feed.

The cheapest and highest-fidelity path by a wide margin -- one request returns
every posting with the vendor's own field semantics, no inference required.
Most vendors need nothing but a table row here; the ones that need code have
their own module in this package.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Dict, List, Optional, Tuple

from job_scraper.detector import ATSName
from job_scraper.fetching import (
    FEED_MAX_BYTES,
    dig,
    fetch_json,
    fetch_xml_items,
    first_string,
)
from job_scraper.models import Job

if TYPE_CHECKING:
    from job_scraper.board import Board

# Safety stop for a paged feed whose total never resolves. At the 100/page the
# vendors use this is 10k postings -- larger than any single board seen.
FEED_MAX_PAGES = 100


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
        token=(
            # oneclick-ui URLs put the tenant after /company/, not right
            # after the hostname -- must be tried before the generic form.
            r"smartrecruiters\.com/oneclick-ui/company/([\w.-]+)",
            r"smartrecruiters\.com/([\w.-]+)",
        ),
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
        # Body is the list itself; dig("") returns it unchanged.
        items="",
        title="name",
        # Not "location": that dict leads with country, so first_string would
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
        # first_string would report the location as "283".
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

        title = first_string(dig(item, feed.title))

        if feed.link_url:
            url = feed.link_url.format(token=token, id=item.get("id"))
        else:
            url = first_string(dig(item, feed.link))

        if not title or not url:
            continue

        jobs.append(Job(
            company=board.company_name,
            title=title,
            url=url,
            place=first_string(dig(item, feed.place)),
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
    # A board that embeds the ATS as a widget rather than linking to it
    # (owkin's Ashby embed, `<div data-ashby-src="...ashbyhq.com/owkin/embed">`)
    # never puts the token in its own URL -- only in the page it serves. The
    # page is already cached on Board, so this costs no extra request.
    token = (
        _token(board.final_url, feed.token)
        or _token(board.board_url, feed.token)
        or _token(board.html or "", feed.token)
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
            items = fetch_xml_items(
                board.session, target, feed.item_tag,
                max_bytes=FEED_MAX_BYTES,
            )
        else:
            body = fetch_json(
                board.session, target, max_bytes=FEED_MAX_BYTES
            )
            items = dig(body, feed.items)

        if not isinstance(items, list) or not items:
            break

        seen += len(items)
        jobs.extend(_feed_jobs(board, feed, token, items))

        # One page unless the vendor publishes a total to page against.
        if not feed.page_param:
            break

        total = dig(body, feed.total)

        if not isinstance(total, int) or seen >= total:
            break

    return jobs
