"""WeRecruit (careers.werecruit.io), read from the offers it embeds in the page.

One host serves every tenant, so the host-root sitemap lists all of them --
the generic sitemap strategy used to return ~12,000 other companies' postings
for each board. The board page itself carries only the tenant's own offers, as
`window.allOffers = [...]` for its client-side widget.
"""

from __future__ import annotations

import json
import re
from typing import TYPE_CHECKING, List, Optional
from urllib.parse import urljoin

from job_scraper.fetching import first_string
from job_scraper.models import Job
from job_scraper.strategies.inline_json import _matching_bracket

if TYPE_CHECKING:
    from job_scraper.board import Board

_ALL_OFFERS_RE = re.compile(r"window\.allOffers\s*=\s*(\[)")


def _title(offer: dict) -> Optional[str]:
    return (first_string(offer.get("TitleTranslated"))
            or first_string(offer.get("Title")))


def scrape_werecruit(board: "Board") -> List[Job]:
    """Scrape a WeRecruit board out of its inline `window.allOffers` array.

    Args:
        board: Board to scrape.

    Returns:
        List of jobs, or empty list if the page carries no offers array.
    """
    html = board.html
    match = _ALL_OFFERS_RE.search(html or "")

    if not match:
        return []

    blob = _matching_bracket(html, match.start(1))

    try:
        offers = json.loads(blob) if blob else []
    except ValueError:
        return []

    jobs = []

    for offer in offers if isinstance(offers, list) else []:
        if not isinstance(offer, dict):
            continue

        title = _title(offer)
        url = first_string(offer.get("Url"))

        if not title or not url:
            continue

        jobs.append(Job(
            company=board.company_name,
            title=title,
            url=urljoin(board.url, url),
            place=first_string(offer.get("Address_City")),
            via="werecruit",
        ))

    return jobs
