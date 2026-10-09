"""Beetween career sites (<tenant>.nos-recrutements.fr), via their public API.

The site is a Vue single-page app: the served HTML is an empty shell, so no
HTML strategy sees a posting. Its own frontend POSTs `/api/job/list` (10 per
page, `{"page": n}`) and the answer already carries each posting's body, so --
as with WTTJ -- post_scraper uses that description instead of fetching a page
that is a shell too.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, List
from urllib.parse import urlsplit

from bs4 import BeautifulSoup

from job_scraper.fetching import FEED_MAX_BYTES, fetch_json, first_string
from job_scraper.models import Job

if TYPE_CHECKING:
    from job_scraper.board import Board

#: A tenant with more than this many pages is not a real board.
MAX_PAGES = 50

#: Matches post_scraper.MAX_DESCRIPTION; kept local like welcometothejungle.py.
_MAX_DESCRIPTION = 20_000

_DESCRIPTION_FIELDS = (
    "descriptionCompany", "descriptionMission", "descriptionProfile",
)


def _description(job: dict) -> str:
    parts = (
        BeautifulSoup(job[field], "html.parser").get_text("\n", strip=True)
        for field in _DESCRIPTION_FIELDS
        if isinstance(job.get(field), str)
    )

    return "\n\n".join(part for part in parts if part)[:_MAX_DESCRIPTION]


def scrape_beetween(board: "Board") -> List[Job]:
    """Scrape a Beetween board through its job-list API.

    Args:
        board: Board to scrape.

    Returns:
        List of jobs, or empty list if the API does not answer.
    """
    parts = urlsplit(board.url)
    api = f"{parts.scheme}://{parts.netloc}/api/job/list"
    jobs: List[Job] = []

    for page in range(1, MAX_PAGES + 1):
        data = fetch_json(
            board.session, api, method="post", json={"page": page},
            max_bytes=FEED_MAX_BYTES,
        )
        records = data.get("jobs") if isinstance(data, dict) else None

        if not isinstance(records, list) or not records:
            break

        for record in records:
            if not isinstance(record, dict):
                continue

            title = first_string(record.get("title"))
            url = first_string(record.get("url"))

            if not title or not url:
                continue

            jobs.append(Job(
                company=board.company_name,
                title=title,
                url=url,
                place=first_string(record.get("city")),
                via="beetween",
                description=_description(record),
            ))

        if len(jobs) >= data.get("numFound", 0):
            break

    return jobs
