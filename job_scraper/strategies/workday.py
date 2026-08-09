"""Workday, which needs a POST loop no Feed row can express."""

from __future__ import annotations

from typing import TYPE_CHECKING, List
from urllib.parse import urljoin, urlparse

from job_scraper.fetching import dig, fetch_json, workday_endpoint
from job_scraper.models import Job

if TYPE_CHECKING:
    from job_scraper.board import Board

WORKDAY_PAGE = 20
WORKDAY_MAX_PAGES = 50


def scrape_workday(board: "Board") -> List[Job]:
    """Scrape Workday board via /wday/cxs/ JSON endpoint.

    Args:
        board: Board to scrape.

    Returns:
        List of jobs from board.
    """
    located = workday_endpoint(board.url)

    if located is None:
        return []

    root, tenant, segments = located
    endpoint = f"{root}/wday/cxs/{tenant}/{segments[0]}/jobs"
    base = urljoin(board.url, urlparse(board.url).path.rstrip("/"))

    jobs: List[Job] = []

    for page in range(WORKDAY_MAX_PAGES):
        body = fetch_json(
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

        postings = dig(body, "jobPostings")

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
