"""Tests for job_scraper/strategies/inline_json.py.

Atos and Eviden (same in-house WordPress plugin) print their whole listing as
`window['atosjobs_<hash>'] = {"results": [...]}` inside a `<script>` tag and
build the visible anchors from it client-side -- so `links.py`'s anchor scan
finds nothing, even though the data is already sitting in the fetched HTML.
"""

import pytest

from job_scraper.board import Board
from job_scraper.strategies.inline_json import scrape_inline_json

pytestmark = pytest.mark.anyio


def board(html: str, url: str = "https://acme.fr/careers") -> Board:
    made = Board("acme", url, session=None)
    made._html = html

    return made


ATOS_SHAPE = """<html><body>
<script>window['atosjobs_g3i7Ejtt']={"page":1,"total":2,"results":[
  {"id":"550272","title":"Testeur QA (F\\/H)","date":"Aug 1, 2026",
   "url":"https:\\/\\/jobs.acme.fr\\/job\\/Testeur-QA\\/550272\\/"},
  {"id":"550188","title":"Consulting Manager","date":"Aug 2, 2026",
   "url":"https:\\/\\/jobs.acme.fr\\/job\\/Consulting-Manager\\/550188\\/"}
]};</script>
</body></html>"""


async def test_reads_postings_out_of_a_window_assignment():
    jobs = await scrape_inline_json(board(ATOS_SHAPE))

    assert len(jobs) == 2
    assert jobs[0].title == "Testeur QA (F/H)"
    assert jobs[0].url == "https://jobs.acme.fr/job/Testeur-QA/550272/"
    assert jobs[0].company == "acme"
    assert jobs[0].via == "inline_json"


async def test_var_assignment_shape_also_matches():
    html = """<script>var listing = {"jobs": [
      {"name": "Backend Engineer", "href": "/jobs/backend-engineer"}
    ]};</script>"""

    jobs = await scrape_inline_json(board(html))

    assert len(jobs) == 1
    assert jobs[0].title == "Backend Engineer"
    assert jobs[0].url == "https://acme.fr/jobs/backend-engineer"


async def test_relative_urls_are_resolved_against_the_board():
    jobs = await scrape_inline_json(board(ATOS_SHAPE, url="https://acme.fr/careers"))

    assert all(job.url.startswith("https://") for job in jobs)


async def test_json_ld_is_not_mistaken_for_a_listing():
    """schema.org WebPage carries the same name+url pair as a posting record
    but is never one -- detector.py already reads JSON-LD for ATS
    fingerprinting, and this module would otherwise double-count it as a
    single spurious job on every board that has any JSON-LD at all."""
    html = ('<script type="application/ld+json">'
            '{"@type": "WebPage", "name": "Careers", "url": "https://acme.fr/"}'
            '</script>')

    assert await scrape_inline_json(board(html)) == []


async def test_a_blob_with_no_title_and_url_pair_yields_nothing():
    html = '<script>var config = {"theme": "dark", "locale": "fr"};</script>'

    assert await scrape_inline_json(board(html)) == []


async def test_malformed_script_does_not_break_the_page():
    """One broken inline script (truncated JSON, hand-written JS) must not
    take down every other script tag on the same page."""
    html = ATOS_SHAPE + "<script>this is not json at all {</script>"

    jobs = await scrape_inline_json(board(html))

    assert len(jobs) == 2


async def test_no_html_returns_nothing():
    made = Board("acme", "https://acme.fr/careers", session=None)
    made._html = None

    assert await scrape_inline_json(made) == []


async def test_a_record_missing_either_key_is_skipped():
    html = """<script>var listing = [
      {"title": "No URL Here"},
      {"url": "/jobs/no-title"},
      {"title": "Complete", "url": "/jobs/complete"}
    ];</script>"""

    jobs = await scrape_inline_json(board(html))

    assert len(jobs) == 1
    assert jobs[0].title == "Complete"
