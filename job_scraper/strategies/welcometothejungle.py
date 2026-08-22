"""Welcome to the Jungle, read through its public Algolia search index.

The board page itself is walled behind an AWS WAF JS challenge, and so is
every individual posting page -- but WTTJ's frontend is backed by a public,
non-secret Algolia "search-only" key that answers with full job data
(including the posting body) in one call, on a completely different origin
the WAF never touches. That means both the listing and the detail belong to
this one strategy; post_scraper.py carries the description straight through
instead of re-fetching a page it cannot reach.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, List, Optional

from job_scraper.fetching import FEED_MAX_BYTES, dig, fetch_json, first_string
from job_scraper.models import Job

if TYPE_CHECKING:
    from job_scraper.board import Board

ALGOLIA_URL = "https://csekhvms53-dsn.algolia.net/1/indexes/*/queries"
ALGOLIA_HEADERS = {
    "Content-Type": "application/json",
    "Referer": "https://www.welcometothejungle.com/",
    "X-Algolia-Application-Id": "CSEKHVMS53",
    "X-Algolia-API-Key": "4bd8f6215d0cc52b26430765769e65a0",
}
ALGOLIA_INDEX = "wttj_jobs_production_fr"

ORG_API = "https://api.welcometothejungle.com/api/v1/organizations/{slug}"

#: Board URLs look like /fr/companies-v1/<slug>/jobs or /fr/companies/<slug>.
_SLUG_RE = re.compile(r"/companies(?:-v1)?/([^/?#]+)")

#: 20_000, matching post_scraper.MAX_DESCRIPTION -- kept local so this module
#: does not import a private constant from a stage that runs after it.
_MAX_DESCRIPTION = 20_000


def _extract_slug(board_url: str) -> Optional[str]:
    match = _SLUG_RE.search(board_url)

    return match.group(1) if match else None


def _search(session, filters: str) -> List[dict]:
    payload = {
        "requests": [{
            "indexName": ALGOLIA_INDEX,
            "params": f"filters={filters}&hitsPerPage=1000",
        }]
    }
    data = fetch_json(
        session, ALGOLIA_URL, method="post", json=payload,
        headers=ALGOLIA_HEADERS, max_bytes=FEED_MAX_BYTES,
    )
    hits = dig(data, "results.hits")

    return hits if isinstance(hits, list) else []


def _resolve_org_name(session, slug: str) -> Optional[str]:
    """Look up the canonical org name for a slug WTTJ has since renamed.

    api.welcometothejungle.com is a separate, un-walled origin -- it accepts
    the old URL slug and still answers with the current organization, which
    Algolia's own records have already moved to (organization.name, not
    organization.slug).
    """
    data = fetch_json(session, ORG_API.format(slug=slug), max_bytes=FEED_MAX_BYTES)

    return first_string(dig(data, "organization.name"))


def _description(hit: dict) -> str:
    parts = [first_string(hit.get("summary")), first_string(hit.get("profile"))]
    missions = hit.get("key_missions")

    if isinstance(missions, list) and missions:
        parts.append("\n".join(f"- {m}" for m in missions if isinstance(m, str)))

    text = "\n\n".join(p for p in parts if p)

    return text[:_MAX_DESCRIPTION]


def _job_url(locale: str, org_slug: str, hit: dict) -> Optional[str]:
    slug = first_string(hit.get("slug"))

    if not slug:
        return None

    return f"https://www.welcometothejungle.com/{locale}/companies/{org_slug}/jobs/{slug}"


def scrape_wttj(board: "Board") -> List[Job]:
    """Scrape a Welcome to the Jungle company board through its Algolia index.

    Args:
        board: Board to scrape.

    Returns:
        List of jobs, or empty list if the slug or the API call fails.
    """
    org_slug = _extract_slug(board.url)

    if not org_slug:
        return []

    locale_match = re.search(r"welcometothejungle\.com/([a-z]{2})/", board.url)
    locale = locale_match.group(1) if locale_match else "en"

    hits = _search(board.session, f'organization.slug:"{org_slug}"')

    if not hits:
        # The URL can still carry a slug WTTJ has since renamed away from --
        # organization.slug in Algolia moves with the rename, the board URL
        # does not. Resolve through the org API and retry on the current name.
        org_name = _resolve_org_name(board.session, org_slug)

        if not org_name:
            return []

        hits = _search(board.session, f'organization.name:"{org_name}"')

        if not hits:
            return []

    jobs = []

    for hit in hits:
        if not isinstance(hit, dict):
            continue

        title = first_string(hit.get("name"))
        url = _job_url(locale, org_slug, hit)

        if not title or not url:
            continue

        jobs.append(Job(
            company=board.company_name,
            title=title,
            url=url,
            place=first_string(dig(hit, "offices.city")),
            via="wttj",
            description=_description(hit),
        ))

    return jobs
