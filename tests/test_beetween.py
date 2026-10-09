"""Tests for job_scraper/strategies/beetween.py.

<tenant>.nos-recrutements.fr serves an empty Vue shell; the postings, bodies
included, come from POST /api/job/list, 10 per page.
"""

import json

from job_scraper.detector import ATSName
from job_scraper.models import Job
from job_scraper.strategies import VENDOR_SCRAPERS
from job_scraper.strategies.beetween import scrape_beetween
from job_scraper.urls import ats_from_host
from tests.test_board_scraper import board

BOARD_URL = "https://acme.nos-recrutements.fr/jobs?page=1"
API = "https://acme.nos-recrutements.fr/api/job/list"


def record(n: int) -> dict:
    return {
        "id": str(n), "wid": f"w{n}", "title": f"Dev {n} (H/F)",
        "city": "Paris",
        "url": f"https://acme.nos-recrutements.fr/job/w{n}",
        "descriptionMission": "<p>Build <b>things</b>.</p>",
        "descriptionProfile": "<ul><li>Python</li></ul>",
    }


def paged(total: int, per_page: int = 2):
    def responder(payload):
        start = (payload["page"] - 1) * per_page
        rows = [record(n) for n in range(start, min(start + per_page, total))]

        return json.dumps({"numFound": total, "jobs": rows})

    return responder


def test_reads_every_page_and_carries_the_description():
    made = board({API: paged(5)}, ats=ATSName.BEETWEEN, url=BOARD_URL)

    jobs = scrape_beetween(made)

    assert len(jobs) == 5
    assert jobs[0] == Job(
        company="acme", title="Dev 0 (H/F)",
        url="https://acme.nos-recrutements.fr/job/w0", place="Paris",
        via="beetween", description="Build\nthings\n.\n\nPython",
    )
    assert made.session.requested.count(API) == 3


def test_dead_api_yields_nothing():
    assert scrape_beetween(board({}, url=BOARD_URL)) == []


def test_host_is_detected_and_wired_in():
    assert ats_from_host(BOARD_URL) == ATSName.BEETWEEN
    assert VENDOR_SCRAPERS[ATSName.BEETWEEN] is scrape_beetween
