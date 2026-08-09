"""Comeet, whose API key has to be lifted off the board page first."""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, List

from job_scraper.fetching import FEED_MAX_BYTES, dig, fetch_json, first_string
from job_scraper.models import Job

if TYPE_CHECKING:
    from job_scraper.board import Board

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

    items = fetch_json(
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

        title = first_string(item.get("name"))
        url = first_string(item.get("url_comeet_hosted_page"))

        if not title or not url:
            continue

        jobs.append(Job(
            company=board.company_name,
            title=title,
            url=url,
            # Sibling "name" is the full "Tel Aviv, Israel"; city is the field
            # the other feeds report, so stay consistent.
            place=first_string(dig(item, "location.city")),
            via="comeet",
        ))

    return jobs
