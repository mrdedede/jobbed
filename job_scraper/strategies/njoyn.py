"""njoyn, which publishes no feed but does publish a real listing table."""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, List
from urllib.parse import urljoin

from bs4 import BeautifulSoup

from job_scraper.models import Job

if TYPE_CHECKING:
    from job_scraper.board import Board

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
