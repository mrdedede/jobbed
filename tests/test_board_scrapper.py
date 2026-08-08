"""Board scraper tests using mocked HTTP.

Tests verify wiring, payload mapping, dispatch order, and via bookkeeping.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from job_scrapper.board_scrapper import (  # noqa: E402
    FEEDS,
    VENDOR_SCRAPERS,
    _JOB_URL_RE,
    Board,
    Feed,
    Job,
    _dedupe,
    _dig,
    _first_string,
    _nested_sitemaps,
    _title_from_url,
    _token,
    job_urls_from_sitemap,
    scrap_feed,
    scrap_links,
    scrap_sitemap,
    scrap_workday,
)
from job_scrapper.detector import ATS_NAMES, ATSName  # noqa: E402


def page(body: str, head: str = "") -> str:
    return f"<html><head>{head}</head><body>{body}</body></html>"


# ======================================================================
# Test doubles
# ======================================================================


class FakeResponse:
    def __init__(self, body):
        self.status_code = 200 if body is not None else 404
        self._body = (body or "").encode()
        self.encoding = "utf-8"
        self.raw = self

    def read(self, amount, decode_content=True):
        return self._body[:amount]

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class FakeSession:
    """Mock requests.Session for testing.

    Attributes:
        pages: URL -> body map. Body may be string or callable (for payloads).
        requested: List of requested URLs.
        headers: Request headers dict.
    """

    def __init__(self, pages: dict):
        self.pages = pages
        self.requested = []
        self.headers = {}

    def get(self, url, **kwargs):
        self.requested.append(url)

        return FakeResponse(self.pages.get(url))

    def post(self, url, json=None, **kwargs):
        self.requested.append(url)
        body = self.pages.get(url)

        return FakeResponse(body(json) if callable(body) else body)


def board(pages: dict, ats=None, url="https://acme.fr/jobs") -> Board:
    made = Board("acme", url, session=FakeSession(pages))
    made.ats = ats

    return made


# ======================================================================
# Sitemap parsing -- moved from test_detector.py unchanged
# ======================================================================


SITEMAP = """<?xml version="1.0"?><urlset>
  <url><loc>https://acme.fr/jobs/842306</loc></url>
  <url><loc>https://acme.fr/a-propos</loc></url>
  <url><loc>https://acme.fr/jobs/842306</loc></url>
  <url><loc>https://acme.fr/offres/dev-senior</loc></url>
  <url><loc>https://acme.fr/emplois/data-engineer</loc></url>
</urlset>"""

SITEMAP_INDEX = """<?xml version="1.0"?><sitemapindex>
  <sitemap><loc>https://acme.fr/sitemap-jobs.xml</loc></sitemap>
  <sitemap><loc>https://acme.fr/sitemap-stores.xml</loc></sitemap>
</sitemapindex>"""

POSTING = page(
    "<h1>Data Engineer</h1>",
    '<script type="application/ld+json">'
    '{"@type": "JobPosting", "title": "Data Engineer",'
    ' "jobLocation": {"address": {"addressLocality": "Lyon"}}}</script>',
)


def test_job_urls_from_sitemap_keeps_only_postings():
    assert job_urls_from_sitemap(SITEMAP) == [
        "https://acme.fr/jobs/842306",
        "https://acme.fr/offres/dev-senior",
        "https://acme.fr/emplois/data-engineer",
    ]


def test_nested_sitemaps_follows_only_job_children():
    assert _nested_sitemaps(SITEMAP_INDEX) == [
        "https://acme.fr/sitemap-jobs.xml"
    ]


# ======================================================================
# Strategy 2: sitemap + JSON-LD
# ======================================================================


def test_sitemap_reads_title_and_place_from_jsonld():
    jobs = scrap_sitemap(board({
        "https://acme.fr/sitemap.xml": SITEMAP,
        "https://acme.fr/jobs/842306": POSTING,
    }))

    assert len(jobs) == 3
    assert jobs[0] == Job(
        company="acme",
        title="Data Engineer",
        url="https://acme.fr/jobs/842306",
        place="Lyon",
        via="sitemap",
    )


def test_sitemap_falls_back_to_the_slug_when_a_posting_has_no_jsonld():
    """Enumeration still beats nothing; a title is never left blank."""
    jobs = scrap_sitemap(board({
        "https://acme.fr/sitemap.xml": SITEMAP,
        "https://acme.fr/offres/dev-senior": page("<h1>Dev</h1>"),
    }))

    by_url = {job.url: job for job in jobs}

    assert by_url["https://acme.fr/offres/dev-senior"].title == "Dev Senior"
    assert by_url["https://acme.fr/offres/dev-senior"].place is None


def test_sitemap_follows_an_index():
    """The postings live one level below the index -- as on leroymerlin."""
    jobs = scrap_sitemap(board({
        "https://acme.fr/sitemap.xml": SITEMAP_INDEX,
        "https://acme.fr/sitemap-jobs.xml": SITEMAP,
        "https://acme.fr/jobs/842306": POSTING,
    }))

    assert len(jobs) == 3


def test_sitemap_needs_more_than_a_couple_of_links():
    thin = """<urlset><url><loc>https://acme.fr/jobs/1</loc></url>
    <url><loc>https://acme.fr/jobs/2</loc></url></urlset>"""

    assert scrap_sitemap(board({"https://acme.fr/sitemap.xml": thin})) == []


def test_sitemap_falls_back_to_robots_txt():
    jobs = scrap_sitemap(board({
        "https://acme.fr/robots.txt": "Sitemap: https://acme.fr/sm-jobs.xml",
        "https://acme.fr/sm-jobs.xml": SITEMAP,
    }))

    assert len(jobs) == 3


@pytest.mark.parametrize("path,is_posting", [
    ("/jobs/842306-chef-de-projet", True),
    ("/emplois/data-engineer", True),
    # French boards write the compound three ways; whize.fr uses the first,
    # and the pattern this was moved from matched none of them.
    ("/offre-emploi/1234-developpeur-backend", True),
    ("/offres-demploi/dev-senior", True),
    ("/offres-d-emploi/dev-senior", True),
    # Job word, but a content section rather than a posting collection.
    ("/career-advice/how-to-write-a-cv", False),
    ("/jobs-blog/our-culture", False),
    # The listing page itself is not a posting.
    ("/offres-demploi/", False),
    ("/about/team", False),
])
def test_generic_job_url_shape(path, is_posting):
    assert bool(_JOB_URL_RE.search(path)) is is_posting


@pytest.mark.parametrize("url,expected", [
    ("https://acme.fr/jobs/842306-chef-de-projet", "Chef De Projet"),
    ("https://acme.fr/offres/dev-senior", "Dev Senior"),
    ("https://acme.fr/jobs/lead_data_engineer/", "Lead Data Engineer"),
])
def test_title_from_url_deslugifies(url, expected):
    assert _title_from_url(url) == expected


# ======================================================================
# Strategy 1: the feed engine
# ======================================================================


GREENHOUSE_BODY = json.dumps({"jobs": [
    {
        "title": "Account Executive",
        "absolute_url": "https://job-boards.greenhouse.io/acme/jobs/1",
        "location": {"name": "Remote, Italy"},
    },
    {"title": "No URL here", "location": {"name": "Paris"}},
]})


def test_feed_maps_a_greenhouse_payload():
    made = board(
        {"https://boards-api.greenhouse.io/v1/boards/acme/jobs":
            GREENHOUSE_BODY},
        ats=ATSName.GREENHOUSE,
        url="https://boards.greenhouse.io/acme",
    )

    jobs = scrap_feed(made, FEEDS[ATSName.GREENHOUSE])

    assert jobs == [Job(
        company="acme",
        title="Account Executive",
        url="https://job-boards.greenhouse.io/acme/jobs/1",
        place="Remote, Italy",
        via="feed",
    )]


def test_feed_builds_the_url_when_the_payload_omits_one():
    """SmartRecruiters returns an internal API ref, never the public URL."""
    made = board(
        {"https://api.smartrecruiters.com/v1/companies/Visa/postings"
         "?limit=100": json.dumps({"content": [
             {"id": "744000133907678", "name": "Sr. Manager",
              "location": {"fullLocation": "Austin, TX"}},
         ]})},
        ats=ATSName.SMARTRECRUITERS,
        url="https://jobs.smartrecruiters.com/Visa",
    )

    jobs = scrap_feed(made, FEEDS[ATSName.SMARTRECRUITERS])

    assert jobs[0].url == (
        "https://jobs.smartrecruiters.com/Visa/744000133907678"
    )


def test_feed_returns_nothing_rather_than_raising_when_the_endpoint_is_down():
    made = board({}, ats=ATSName.GREENHOUSE,
                 url="https://boards.greenhouse.io/acme")

    assert scrap_feed(made, FEEDS[ATSName.GREENHOUSE]) == []


@pytest.mark.parametrize("place", [
    "Paris",
    {"name": "Paris"},
    [{"city": "Paris"}],
])
def test_feed_place_handles_string_dict_and_list_shapes(place):
    """One vendor uses all three, sometimes in the same payload."""
    feed = Feed(url="https://x/{token}", token=(r"//(\w+)\.x",),
                items="jobs", link="url", place="location")

    made = board(
        {"https://x/acme": json.dumps({"jobs": [
            {"title": "Dev", "url": "https://x/1", "location": place},
        ]})},
        url="https://acme.x",
    )

    assert scrap_feed(made, feed)[0].place == "Paris"


@pytest.mark.parametrize("ats,url,expected", [
    (ATSName.GREENHOUSE, "https://boards.greenhouse.io/gitlab", "gitlab"),
    (ATSName.GREENHOUSE,
     "https://boards.greenhouse.io/embed/job_board?for=gitlab", "gitlab"),
    (ATSName.LEVER, "https://jobs.lever.co/scaleway/", "scaleway"),
    (ATSName.ASHBY, "https://jobs.ashbyhq.com/nabla?departmentId=8b", "nabla"),
    (ATSName.RECRUITEE, "https://aneo.recruitee.com", "aneo"),
    (ATSName.WORKABLE, "https://apply.workable.com/exotec/", "exotec"),
])
def test_token_regexes_match_real_board_urls(ats, url, expected):
    """The embed form is why `token` is a tuple: Python's alternation picks
    the leftmost position, so a single pattern captures "embed" instead."""
    assert _token(url, FEEDS[ats].token) == expected


def test_dig_tolerates_missing_keys_and_list_wrappers():
    assert _dig({"a": {"b": "c"}}, "a.b") == "c"
    assert _dig({"a": [{"b": "c"}]}, "a.b") == "c"
    assert _dig({"a": {}}, "a.b.c") is None
    assert _dig({"a": "flat"}, "a.b") is None
    assert _dig({"a": 1}, "") == {"a": 1}


def test_first_string_ignores_blanks():
    assert _first_string({"a": "", "b": "  ", "c": "Paris"}) == "Paris"
    assert _first_string({}) is None


# ======================================================================
# Strategy 1b: Workday
# ======================================================================


def workday_pages(payload):
    """Two full pages then a short one -- and total=0 after the first.

    That quirk is real: only Workday's first response reports a total, so a
    scraper that trusts it on later pages stops at 40 of 83.
    """
    offset = payload["offset"]
    limit = payload["limit"]
    remaining = max(0, 45 - offset)
    count = min(limit, remaining)

    return json.dumps({
        "total": 45 if offset == 0 else 0,
        "jobPostings": [
            {
                "title": f"Dev {offset + n}",
                "externalPath": f"/job/Paris/Dev_{offset + n}",
                "locationsText": "Paris",
            }
            for n in range(count)
        ],
    })


def test_workday_pages_past_the_zeroed_total():
    made = board(
        {"https://acme.wd3.myworkdayjobs.com/wday/cxs/acme/careers/jobs":
            workday_pages},
        ats=ATSName.WORKDAY,
        url="https://acme.wd3.myworkdayjobs.com/fr-FR/careers",
    )

    jobs = scrap_workday(made)

    assert len(jobs) == 45
    assert jobs[0] == Job(
        company="acme",
        title="Dev 0",
        url=("https://acme.wd3.myworkdayjobs.com/fr-FR/careers"
             "/job/Paris/Dev_0"),
        place="Paris",
        via="workday",
    )


def test_workday_ignores_a_missing_locale_segment():
    """`/Visa` has no locale; `/fr-FR/careers` does. Both must resolve."""
    made = board(
        {"https://visa.wd5.myworkdayjobs.com/wday/cxs/visa/Visa/jobs":
            workday_pages},
        ats=ATSName.WORKDAY,
        url="https://visa.wd5.myworkdayjobs.com/Visa",
    )

    assert len(scrap_workday(made)) == 45


# ======================================================================
# Strategy 3: anchor links
# ======================================================================


BOARD_PAGE = page(
    "<a href='/jobs/8142223-chef-de-projet'>Chef de projet</a>"
    "<a href='/jobs/8142224-dev-senior'>Dev senior</a>"
    "<a href='/about'>About us</a>"
    "<a href='/jobs/8142223-chef-de-projet'>&gt;</a>"
)


def test_links_filters_by_the_ats_job_pattern():
    jobs = scrap_links(board(
        {"https://acme.fr/jobs": BOARD_PAGE}, ats=ATSName.TEAMTAILOR
    ))

    assert [job.title for job in jobs] == ["Chef de projet", "Dev senior"]
    assert all(job.via == "links" and job.place is None for job in jobs)


def test_links_falls_back_to_the_generic_job_shape_for_an_unknown_ats():
    jobs = scrap_links(board({"https://acme.fr/jobs": BOARD_PAGE}))

    assert len(jobs) == 2


def test_links_skips_anchors_whose_label_is_not_a_title():
    """The `>` chevron points at a real posting and must still be dropped."""
    jobs = scrap_links(board(
        {"https://acme.fr/jobs": BOARD_PAGE}, ats=ATSName.TEAMTAILOR
    ))

    assert ">" not in [job.title for job in jobs]


# ======================================================================
# Dispatch -- the guard this design rests on
# ======================================================================


FEED_BOARD = {
    "https://boards-api.greenhouse.io/v1/boards/acme/jobs": GREENHOUSE_BODY,
    "https://boards.greenhouse.io/acme": BOARD_PAGE,
    "https://boards.greenhouse.io/sitemap.xml": SITEMAP,
}


def test_dispatch_prefers_the_feed_over_sitemap_and_links():
    made = board(FEED_BOARD, ats=ATSName.GREENHOUSE,
                 url="https://boards.greenhouse.io/acme")

    jobs = made.scrap_board()

    assert [job.via for job in jobs] == ["feed"]
    # The cheaper path won outright: no sitemap discovery was paid for.
    assert "https://boards.greenhouse.io/sitemap.xml" not in (
        made.session.requested
    )


def test_via_records_a_fallback_so_a_broken_feed_is_visible():
    """A named ATS scraped by anchors must not look like a normal result.

    This is the whole reason `via` exists -- otherwise "fallback" quietly
    becomes the bucket for everything and a real ATS is scraped badly forever.
    """
    made = board(
        {"https://boards.greenhouse.io/acme": BOARD_PAGE},
        ats=ATSName.GREENHOUSE,
        url="https://boards.greenhouse.io/acme",
    )

    jobs = made.scrap_board()

    assert jobs and all(job.via == "links" for job in jobs)


def test_every_detectable_ats_has_a_slot():
    """A new registry entry must not silently arrive with no scraping plan.

    Every ATS the detector can name is either a FEEDS row or a VENDOR_SCRAPERS
    function -- including the ones that only raise NotImplementedError, which
    is how a known gap stays a recorded decision instead of an oversight.
    """
    missing = [
        name for name in ATS_NAMES
        if name not in FEEDS and name not in VENDOR_SCRAPERS
    ]

    assert not missing, f"no scraping plan for: {missing}"


def test_no_ats_is_claimed_by_both_tables():
    """Two dispatch tables, disjoint -- otherwise one silently shadows the
    other and a working feed can be masked by an unwritten stub."""
    assert not set(FEEDS) & set(VENDOR_SCRAPERS)


def test_an_unwritten_vendor_scraper_falls_through_instead_of_crashing():
    """The reason NotImplementedError is caught.

    Teamtailor has no vendor scraper, but its boards scrape fine from a
    sitemap. Letting the stub propagate would take those from 51 jobs to a
    traceback.
    """
    made = board(
        {
            "https://acme.fr/sitemap.xml": SITEMAP,
            "https://acme.fr/jobs/842306": POSTING,
        },
        ats=ATSName.TEAMTAILOR,
    )

    jobs = made.scrap_board()

    assert jobs and all(job.via == "sitemap" for job in jobs)


def test_stub_scrapers_carry_their_lead_in_the_message():
    """The note is the point of the stub -- an empty `pass` teaches nothing."""
    with pytest.raises(NotImplementedError, match="API key"):
        VENDOR_SCRAPERS[ATSName.TEAMTAILOR](board({}))


def test_a_board_with_nothing_returns_no_jobs_rather_than_raising():
    made = board({}, ats=ATSName.GREENHOUSE,
                 url="https://boards.greenhouse.io/acme")

    assert made.scrap_board() == []


def test_dedupe_collapses_fragments_and_trailing_slashes():
    jobs = [
        Job("acme", "Dev", "https://acme.fr/jobs/1"),
        Job("acme", "Dev again", "https://acme.fr/jobs/1/"),
        Job("acme", "Dev thrice", "https://acme.fr/jobs/1#apply"),
        Job("acme", "Other", "https://acme.fr/jobs/2"),
    ]

    assert [job.title for job in _dedupe(jobs)] == ["Dev", "Other"]
