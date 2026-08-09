"""Board scraper tests using mocked HTTP.

Tests verify wiring, payload mapping, dispatch order, and via bookkeeping.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from job_scraper.board import Board, dedupe as _dedupe
from job_scraper.fetching import (
    MAX_FETCH_BYTES,
    dig as _dig,
    fetch as _fetch,
    first_string as _first_string,
)
from job_scraper.models import Job
from job_scraper.strategies import (
    FEEDS,
    VENDOR_NOTES,
    VENDOR_SCRAPERS,
    Feed,
    scrape_comeet,
    scrape_feed,
    scrape_links,
    scrape_njoyn,
    scrape_sitemap,
    scrape_wordpress,
    scrape_workday,
)
from job_scraper.strategies.comeet import COMEET_API
from job_scraper.strategies.feed import _token
from job_scraper.strategies.sitemap import (
    _nested_sitemaps,
    job_urls_from_sitemap,
)
from job_scraper.urls import (
    JOB_URL_RE as _JOB_URL_RE,
    ats_from_host as _ats_from_host,
)
from job_scraper.detector import (
    ATS_NAMES,
    ATSName,
    DetectionResult,
)


def page(body: str, head: str = "") -> str:
    return f"<html><head>{head}</head><body>{body}</body></html>"


# ======================================================================
# Test doubles
# ======================================================================


class FakeResponse:
    def __init__(self, body, content_type="text/html; charset=utf-8"):
        self.status_code = 200 if body is not None else 404
        self._body = (body or "").encode()
        self.encoding = "utf-8"
        self.headers = {"Content-Type": content_type}
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


def board(pages: dict, ats=None, url="https://acme.fr/jobs",
          render=None) -> Board:
    made = Board("acme", url, session=FakeSession(pages), render=render)
    made.ats = ats

    return made


FEED_FIXTURES = Path(__file__).resolve().parent / "fixtures" / "feeds"


def recorded(name: str) -> str:
    """Read a vendor payload captured from the live endpoint.

    These are the only record of each feed's real field names, which is why
    they are committed while the rest of fixtures/ is ignored.
    """
    return (FEED_FIXTURES / name).read_text(encoding="utf-8")


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
    jobs = scrape_sitemap(board({
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
    jobs = scrape_sitemap(board({
        "https://acme.fr/sitemap.xml": SITEMAP,
        "https://acme.fr/offres/dev-senior": page("<h1>Dev</h1>"),
    }))

    by_url = {job.url: job for job in jobs}

    assert by_url["https://acme.fr/offres/dev-senior"].title == "Dev Senior"
    assert by_url["https://acme.fr/offres/dev-senior"].place is None


def test_sitemap_follows_an_index():
    """The postings live one level below the index -- as on leroymerlin."""
    jobs = scrape_sitemap(board({
        "https://acme.fr/sitemap.xml": SITEMAP_INDEX,
        "https://acme.fr/sitemap-jobs.xml": SITEMAP,
        "https://acme.fr/jobs/842306": POSTING,
    }))

    assert len(jobs) == 3


def test_sitemap_needs_more_than_a_couple_of_links():
    thin = """<urlset><url><loc>https://acme.fr/jobs/1</loc></url>
    <url><loc>https://acme.fr/jobs/2</loc></url></urlset>"""

    assert scrape_sitemap(board({"https://acme.fr/sitemap.xml": thin})) == []


def test_sitemap_falls_back_to_robots_txt():
    jobs = scrape_sitemap(board({
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

    # A hyphenated prefix before the job word. Without this Davidson and
    # Crédit Agricole return nothing at all -- the job word is never the
    # first thing after a slash on a French board.
    ("/nos-offres/acheteur-industrie-electronique", True),
    ("/fr/nos-offres-emploi/577-170470-401-analyste", True),
    # French "carrieres", which the English-only vocabulary missed (Inetum).
    ("/fr/accueil/carrieres/ingenieur-devops-h-f", True),
    ("/fr/accueil/carrieres/004ad9d7-7526-46b7-a966-ea4d9796a7ef", True),

    # The cost of that prefix, and what the slug guard has to hold back: a
    # single bare word after the job word is a filter or category page.
    ("/fr/nos-offres/localisations", False),
    ("/nos-offres/candidature-spontanee", True),
    ("/en/company/jobs/faq.html", False),
    # Section landing pages under a job word.
    ("/carrieres/", False),
    ("/nos-offres/", False),
])
def test_generic_job_url_shape(path, is_posting):
    assert bool(_JOB_URL_RE.search(path)) is is_posting


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

    jobs = scrape_feed(made, FEEDS[ATSName.GREENHOUSE])

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

    jobs = scrape_feed(made, FEEDS[ATSName.SMARTRECRUITERS])

    assert jobs[0].url == (
        "https://jobs.smartrecruiters.com/Visa/744000133907678"
    )


SR_ENDPOINT = (
    "https://api.smartrecruiters.com/v1/companies/acme/postings?limit=100"
)


def smartrecruiters_pages(total: int):
    """Serve `total` postings 100 at a time, keyed by the offset in the URL."""
    pages = {}

    for offset in range(0, total + 100, 100):
        chunk = [
            {"id": str(n), "name": f"Dev {n}",
             "location": {"fullLocation": "Paris"}}
            for n in range(offset, min(offset + 100, total))
        ]
        # Page one keeps the bare endpoint: offset=0 is the default, so it is
        # never appended and unpaged vendors see no change at all.
        key = SR_ENDPOINT if offset == 0 else f"{SR_ENDPOINT}&offset={offset}"
        pages[key] = json.dumps(
            {"totalFound": total, "offset": offset, "limit": 100,
             "content": chunk}
        )

    return pages


def test_feed_pages_past_the_vendor_response_cap():
    """SmartRecruiters caps a response at 100 and reports the real total.

    Without paging a 250-posting board silently returned its first 100 and
    looked complete.
    """
    made = board(smartrecruiters_pages(250), ats=ATSName.SMARTRECRUITERS,
                 url="https://jobs.smartrecruiters.com/acme")

    jobs = scrape_feed(made, FEEDS[ATSName.SMARTRECRUITERS])

    assert len(jobs) == 250
    assert jobs[249].url == "https://jobs.smartrecruiters.com/acme/249"
    assert made.session.requested == [
        SR_ENDPOINT,
        f"{SR_ENDPOINT}&offset=100",
        f"{SR_ENDPOINT}&offset=200",
    ]


def test_feed_stops_at_the_total_instead_of_fetching_an_empty_page():
    """An exact multiple of the page size must not cost a wasted request."""
    made = board(smartrecruiters_pages(200), ats=ATSName.SMARTRECRUITERS,
                 url="https://jobs.smartrecruiters.com/acme")

    assert len(scrape_feed(made, FEEDS[ATSName.SMARTRECRUITERS])) == 200
    assert f"{SR_ENDPOINT}&offset=200" not in made.session.requested


def test_an_unpaged_feed_still_makes_exactly_one_request():
    """Greenhouse and friends return the whole board; paging must stay off."""
    made = board(
        {"https://boards-api.greenhouse.io/v1/boards/acme/jobs":
            GREENHOUSE_BODY},
        ats=ATSName.GREENHOUSE,
        url="https://boards.greenhouse.io/acme",
    )

    scrape_feed(made, FEEDS[ATSName.GREENHOUSE])

    assert made.session.requested == [
        "https://boards-api.greenhouse.io/v1/boards/acme/jobs"
    ]


def test_feed_returns_nothing_rather_than_raising_when_the_endpoint_is_down():
    made = board({}, ats=ATSName.GREENHOUSE,
                 url="https://boards.greenhouse.io/acme")

    assert scrape_feed(made, FEEDS[ATSName.GREENHOUSE]) == []


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

    assert scrape_feed(made, feed)[0].place == "Paris"


# ======================================================================
# Strategy 1c: feeds mapped against payloads recorded from the live vendor
#
# Every row below was captured from a real tenant, so these assert the field
# names the vendor actually ships -- not the ones its docs claim.
# ======================================================================


#: ats -> (recorded payload, board URL, endpoint the row should derive).
RECORDED_FEEDS = {
    ATSName.BREEZY: (
        "breezy_breezy.json",
        "https://breezy.breezy.hr/",
        "https://breezy.breezy.hr/json",
    ),
    ATSName.BAMBOOHR: (
        "bamboohr_skedulo.json",
        "https://skedulo.bamboohr.com/careers",
        "https://skedulo.bamboohr.com/careers/list",
    ),
    ATSName.PINPOINT: (
        "pinpoint_workwithus.json",
        "https://workwithus.pinpointhq.com/",
        "https://workwithus.pinpointhq.com/postings.json",
    ),
    ATSName.PERSONIO: (
        "personio_personio.xml",
        "https://personio.jobs.personio.de/",
        "https://personio.jobs.personio.de/xml",
    ),
    ATSName.JAZZHR: (
        "jazzhr_healthforce.xml",
        "https://healthforce.applytojob.com/apply",
        "https://app.jazz.co/feeds/export/jobs/healthforce",
    ),
}


@pytest.mark.parametrize("ats", sorted(RECORDED_FEEDS))
def test_recorded_feed_derives_its_endpoint_and_yields_jobs(ats):
    """The token regex must reach the endpoint the payload came from."""
    fixture, board_url, endpoint = RECORDED_FEEDS[ats]
    made = board({endpoint: recorded(fixture)}, ats=ats, url=board_url)

    jobs = scrape_feed(made, FEEDS[ats])

    assert made.session.requested == [endpoint]
    assert jobs, f"{ats} mapped no jobs from its recorded payload"
    assert all(job.via == "feed" for job in jobs)
    # A title that fell back to something non-empty but wrong (an id, a blank
    # CDATA) is the failure these feeds actually have.
    assert all(len(job.title) > 3 and job.url.startswith("http")
               for job in jobs)


@pytest.mark.parametrize("ats,expected", [
    (ATSName.BREEZY, Job(
        company="acme",
        title="Employee #12",
        url="https://breezy.breezy.hr/p/98323abf2296-employee-12",
        place="Chaos",
        via="feed",
    )),
    # No public URL in the payload -- built from the id via link_url.
    (ATSName.BAMBOOHR, Job(
        company="acme",
        title="Solution Consultant (Pre-Sales Engineer)",
        url="https://skedulo.bamboohr.com/careers/101",
        place="Denver",
        via="feed",
    )),
    (ATSName.PINPOINT, Job(
        company="acme",
        title="Product Reliability Engineer",
        url=("https://workwithus.pinpointhq.com/en/postings/"
             "0e968d34-78ce-4d50-bc33-1f3f28e816c6"),
        place="London",
        via="feed",
    )),
    # XML, and likewise URL-less: only <id> is published.
    (ATSName.PERSONIO, Job(
        company="acme",
        title="Staff Software Engineer, Data Platform",
        url="https://personio.jobs.personio.de/job/1834171",
        place="Munich",
        via="feed",
    )),
    # XML wrapped in CDATA, which ElementTree unwraps for us.
    (ATSName.JAZZHR, Job(
        company="acme",
        title="Travel Registered Nurse PACU Job",
        url=("http://healthforce.applytojob.com/apply/jU66d1wbId/"
             "Travel-Registered-Nurse-PACU-Job"),
        place="Lancaster",
        via="feed",
    )),
])
def test_recorded_feed_maps_every_field(ats, expected):
    fixture, board_url, endpoint = RECORDED_FEEDS[ats]
    made = board({endpoint: recorded(fixture)}, ats=ats, url=board_url)

    assert scrape_feed(made, FEEDS[ats])[0] == expected


@pytest.mark.parametrize("ats", [ATSName.BREEZY, ATSName.PINPOINT])
def test_nested_location_reports_city_not_first_string_in_dict(ats):
    """Why these rows carry a dotted `place` path rather than bare "location".

    Both vendors nest location. Breezy's dict leads with country and Pinpoint's
    with a numeric id, so `_first_string` would label every Pinpoint posting
    "283" and every Breezy one by its country.
    """
    fixture, board_url, endpoint = RECORDED_FEEDS[ats]
    made = board({endpoint: recorded(fixture)}, ats=ats, url=board_url)

    places = [job.place for job in scrape_feed(made, FEEDS[ats])]

    assert places[0] not in ("283", "United States")
    assert any(places)


def test_a_feed_larger_than_the_page_cap_is_not_truncated():
    """JazzHR's real export is 2.7 MB.

    Read at MAX_FETCH_BYTES it gets cut mid-document, the XML parse fails, and
    the board falls through to the sitemap looking like it never had a feed.
    """
    head, _, tail = recorded("jazzhr_healthforce.xml").rpartition("</job>")
    padding = "<!--" + "x" * (MAX_FETCH_BYTES + 1000) + "-->"
    endpoint = "https://app.jazz.co/feeds/export/jobs/healthforce"

    made = board(
        {endpoint: head + "</job>" + padding + tail},
        ats=ATSName.JAZZHR,
        url="https://healthforce.applytojob.com/apply",
    )

    assert len(scrape_feed(made, FEEDS[ATSName.JAZZHR])) == 3


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
# Strategy 1d: Comeet -- discovery, then feed
# ======================================================================


COMEET_BOARD_URL = "https://www.comeet.com/jobs/team8/61.003"
COMEET_API_URL = COMEET_API.format(
    uid="61.003", token="16358C6EF2C61631639B558C0429"
)


def comeet_board(**overrides):
    pages = {
        COMEET_BOARD_URL: recorded("comeet_team8_board.html"),
        COMEET_API_URL: recorded("comeet_team8_positions.json"),
    }
    pages.update(overrides)

    return board(pages, ats=ATSName.COMEET, url=COMEET_BOARD_URL)


def test_comeet_lifts_the_uid_and_token_off_the_board_page():
    """Both values, not just the UID.

    The careers API answers a UID on its own with "Token is missing", so a
    scraper that reads only the UID gets nothing back.
    """
    made = comeet_board()

    jobs = scrape_comeet(made)

    assert made.session.requested == [COMEET_BOARD_URL, COMEET_API_URL]
    assert jobs[0] == Job(
        company="acme",
        title="AI Platform- Senior Full Stack Developer",
        url=("https://www.comeet.com/jobs/team8/61.003/"
             "ai-platform--senior-full-stack-developer/1E.A5F"),
        place="Tel Aviv-Yafo",
        via="comeet",
    )


def test_comeet_gives_up_quietly_when_the_bootstrap_json_is_absent():
    """A rendered-only board must fall through, not raise -- `_feed` only
    catches NotImplementedError, so anything else would kill the board."""
    assert scrape_comeet(comeet_board(**{
        COMEET_BOARD_URL: page("<h1>Careers</h1>"),
    })) == []


def test_comeet_via_is_not_feed_so_a_misdetect_stays_visible():
    """Same reason Workday tags its own name rather than "feed"."""
    assert {job.via for job in scrape_comeet(comeet_board())} == {"comeet"}


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

    jobs = scrape_workday(made)

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

    assert len(scrape_workday(made)) == 45


# ======================================================================
# Strategy 1e: njoyn -- the title lives in the row, not the anchor
# ======================================================================


NJOYN_URL = (
    "https://cgi.njoyn.com/corp/xweb/xweb.asp?clid=21001&Page=joblisting"
)

#: Shape taken from the CGI board: a header row, then one row per posting
#: linked twice -- by requisition id and by "View Job Details". Neither anchor
#: is the title.
NJOYN_BOARD = page("""
<table>
  <tr><th>Position ID</th><th>Title</th><th>Category</th><th>City</th></tr>
  <tr>
    <td><a href="xweb.asp?clid=21001&Page=JobDetails&Jobid=J0626-0971">
      J0626-0971</a></td>
    <td>SW Supply Chain Functional SME - Senior Software Engineer</td>
    <td>Software Development</td>
    <td>Bangalore</td>
  </tr>
  <tr>
    <td><a href="xweb.asp?clid=21001&Page=JobDetails&Jobid=J0826-0407">
      J0826-0407</a></td>
    <td>Director, Service Delivery</td>
    <td>Consulting</td>
    <td>Sherbrooke</td>
  </tr>
</table>
""")


def test_njoyn_reads_the_title_from_the_row_not_the_link_text():
    jobs = scrape_njoyn(board({NJOYN_URL: NJOYN_BOARD}, ats=ATSName.NJOYN,
                              url=NJOYN_URL))

    assert jobs == [
        Job(
            company="acme",
            title="SW Supply Chain Functional SME - Senior Software Engineer",
            url=("https://cgi.njoyn.com/corp/xweb/xweb.asp?clid=21001"
                 "&Page=JobDetails&Jobid=J0626-0971"),
            place="Bangalore",
            via="njoyn",
        ),
        Job(
            company="acme",
            title="Director, Service Delivery",
            url=("https://cgi.njoyn.com/corp/xweb/xweb.asp?clid=21001"
                 "&Page=JobDetails&Jobid=J0826-0407"),
            place="Sherbrooke",
            via="njoyn",
        ),
    ]


def test_njoyn_locates_columns_by_header_not_position():
    """The board is localised, so column order is not guaranteed."""
    swapped = page("""
    <table>
      <tr><th>City</th><th>Title</th><th>Position ID</th></tr>
      <tr>
        <td>Lyon</td>
        <td>Data Engineer</td>
        <td><a href="xweb.asp?Page=JobDetails&Jobid=J1">J1</a></td>
      </tr>
    </table>
    """)

    jobs = scrape_njoyn(board({NJOYN_URL: swapped}, ats=ATSName.NJOYN,
                              url=NJOYN_URL))

    assert [(job.title, job.place) for job in jobs] == [
        ("Data Engineer", "Lyon")
    ]


def test_njoyn_ignores_tables_that_are_not_the_listing():
    """Layout tables are everywhere on a classic ASP board."""
    assert scrape_njoyn(board(
        {NJOYN_URL: page("<table><tr><td>nav</td></tr></table>")},
        ats=ATSName.NJOYN, url=NJOYN_URL,
    )) == []


def test_njoyn_job_path_still_covers_the_board_if_the_scraper_finds_nothing():
    """The fallback that made these postings visible in the first place.

    Matching the path alone found nothing: njoyn routes every posting through
    the same xweb.asp, so only the query tells a job from the board itself.
    """
    made = board({NJOYN_URL: NJOYN_BOARD}, ats=ATSName.NJOYN, url=NJOYN_URL)

    assert len(scrape_links(made)) == 2

    generic = board({NJOYN_URL: NJOYN_BOARD}, url=NJOYN_URL)

    assert scrape_links(generic) == []


# ======================================================================
# Strategy 1f: WordPress REST -- a platform, not an ATS
# ======================================================================


WP_ROOT = "https://carrieres.acme.com"
WP_TYPES = f"{WP_ROOT}/wp-json/wp/v2/types"

#: Any real WordPress page ships its theme assets out of wp-content. That
#: marker is what opts a board into the REST probe, so the double has to carry
#: it -- without a board page at all there is nothing to recognise.
WP_BOARD_PAGE = page(
    '<a href="/nos-offres/">Nos offres</a>',
    '<link rel="stylesheet" href="/wp-content/themes/acme/style.css">',
)


def wordpress_board(type_name="job", count=2, **extra):
    posts = [
        {"link": f"{WP_ROOT}/{type_name}/dev-{n}-h-f/",
         "title": {"rendered": f"D&#233;veloppeur {n} &#8211; Lyon"}}
        for n in range(count)
    ]
    pages = {
        f"{WP_ROOT}/": WP_BOARD_PAGE,
        WP_TYPES: json.dumps({"post": {}, "page": {}, type_name: {}}),
        f"{WP_ROOT}/wp-json/wp/v2/{type_name}?per_page=100&page=1":
            json.dumps(posts),
    }
    pages.update(extra)

    return board(pages, url=f"{WP_ROOT}/")


def test_wordpress_discovers_the_post_type_rather_than_guessing_it():
    """The type name is the site owner's choice: "job" here, "offres" on
    leboncoin. Both are real boards in the corpus."""
    made = wordpress_board(type_name="offres")

    jobs = scrape_wordpress(made)

    assert WP_TYPES in made.session.requested
    assert jobs[0].url == f"{WP_ROOT}/offres/dev-0-h-f/"
    assert jobs[0].via == "wordpress"


def test_wordpress_unescapes_the_rendered_title():
    """WP escapes entities; raw these read "Go developer &#8211; Team"."""
    jobs = scrape_wordpress(wordpress_board())

    assert jobs[0].title == "Développeur 0 – Lyon"


def test_wordpress_ignores_a_site_with_no_job_shaped_post_type():
    made = board(
        {f"{WP_ROOT}/": WP_BOARD_PAGE,
         WP_TYPES: json.dumps({"post": {}, "page": {}, "attachment": {}})},
        url=f"{WP_ROOT}/",
    )

    assert scrape_wordpress(made) == []


def test_wordpress_is_skipped_when_the_site_is_not_wordpress():
    assert scrape_wordpress(board({}, url=f"{WP_ROOT}/")) == []


def test_wordpress_probe_is_skipped_when_the_page_has_no_wp_marker():
    """The probe is a request spent on every board, and 25 of the 35 in the
    corpus are not WordPress. A page with no wp- marker never gets one."""
    made = board(
        {f"{WP_ROOT}/": page("<a href='/jobs/dev-senior'>Dev</a>"),
         WP_TYPES: json.dumps({"post": {}, "job": {}})},
        url=f"{WP_ROOT}/",
    )

    assert scrape_wordpress(made) == []
    assert WP_TYPES not in made.session.requested


def test_wordpress_runs_before_the_sitemap_so_postings_cost_one_request():
    """Both strategies recover a real title; the sitemap pays one request per
    posting to do it, so the cheaper one has to win."""
    made = wordpress_board(**{
        f"{WP_ROOT}/sitemap.xml": SITEMAP,
    })

    jobs = made.scrape_board()

    assert [job.via for job in jobs] == ["wordpress", "wordpress"]
    assert f"{WP_ROOT}/sitemap.xml" not in made.session.requested


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
    jobs = scrape_links(board(
        {"https://acme.fr/jobs": BOARD_PAGE}, ats=ATSName.TEAMTAILOR
    ))

    assert [job.title for job in jobs] == ["Chef de projet", "Dev senior"]
    assert all(job.via == "links" and job.place is None for job in jobs)


def test_links_falls_back_to_the_generic_job_shape_for_an_unknown_ats():
    jobs = scrape_links(board({"https://acme.fr/jobs": BOARD_PAGE}))

    assert len(jobs) == 2


#: URL shapes taken from real boards in the fixture corpus. Reproduced inline
#: because the HTML corpus itself is gitignored -- only feeds/ is committed.
AVATURE_PAGE = page(
    "<a href='/en_US/externaljobs/JobDetail/498916'>Architecte Cyber f/h</a>"
    "<a href='/de_DE/externaljobs/JobDetail/512004'>Data Engineer m/w</a>"
    "<a href='/en_US/externaljobs/SearchJobs/'>Search jobs</a>"
    "<a href='https://www.siemens.com/global/en/company/jobs/faq.html'>"
    "FAQs &amp; Support</a>"
)

RADANCY_PAGE = page(
    "<a href='/job/villeurbanne/sr-staff-engineer-f-m/44408/95396147344'>"
    "Sr Staff Engineer</a>"
    "<a href='/search-jobs'>Search jobs</a>"
    "<a href='/software-engineering-jobs'>Software engineering</a>"
    "<a href='/saved-jobs'>Saved jobs</a>"
)


def test_avature_postings_are_missed_entirely_without_its_job_path():
    """The generic shape cannot reach an Avature posting at all.

    "externaljobs" is not a job word even with the prefix allowance, so the
    generic pass finds nothing here. It used to return the marketing FAQ page
    instead -- worse than nothing, since the board then looked scraped -- and
    the slug guard now rejects that too ("faq.html" is neither hyphenated nor
    an id).
    """
    generic = scrape_links(board({"https://jobs.siemens.com/x": AVATURE_PAGE},
                                 url="https://jobs.siemens.com/x"))

    assert generic == []

    tuned = scrape_links(board({"https://jobs.siemens.com/x": AVATURE_PAGE},
                               ats=ATSName.AVATURE,
                               url="https://jobs.siemens.com/x"))

    assert [job.url for job in tuned] == [
        "https://jobs.siemens.com/en_US/externaljobs/JobDetail/498916",
        "https://jobs.siemens.com/de_DE/externaljobs/JobDetail/512004",
    ]


def test_radancy_job_path_keeps_listing_pages_out():
    """/search-jobs and /software-engineering-jobs are collections, not
    postings, and the generic shape lets the last one through."""
    jobs = scrape_links(board({"https://careers.synopsys.com/x": RADANCY_PAGE},
                              ats=ATSName.RADANCY,
                              url="https://careers.synopsys.com/x"))

    assert [job.url for job in jobs] == [
        "https://careers.synopsys.com/job/villeurbanne/"
        "sr-staff-engineer-f-m/44408/95396147344"
    ]


def test_links_skips_anchors_whose_label_is_not_a_title():
    """The `>` chevron points at a real posting and must still be dropped."""
    jobs = scrape_links(board(
        {"https://acme.fr/jobs": BOARD_PAGE}, ats=ATSName.TEAMTAILOR
    ))

    assert ">" not in [job.title for job in jobs]


#: A posting page linking back to itself by fragment, as Radancy boards do.
POSTING_WITH_SECTION_NAV = page(
    "<a href='#anchor-overview'>Overview</a>"
    "<a href='#anchor-benefits'>Benefits</a>"
    "<a href='/job/lyon/dev-senior/44408/95675646064'>Career Areas</a>"
    "<a href='/job/paris/data-engineer/44408/98232395056'>Data Engineer</a>"
)


def test_links_ignores_anchors_pointing_at_the_current_page():
    """Section jumps are navigation, and _dedupe strips the fragment.

    Without this the whole in-page nav collapses onto the posting being read
    and the first label wins, so a real job ends up titled "Career Areas".
    """
    here = "https://careers.synopsys.com/job/lyon/dev-senior/44408/95675646064"

    jobs = _dedupe(scrape_links(board(
        {here: POSTING_WITH_SECTION_NAV}, ats=ATSName.RADANCY, url=here,
    )))

    assert [job.title for job in jobs] == ["Data Engineer"]


@pytest.mark.parametrize("label", [
    "Skip to main content", "Learn more", "En savoir plus", "Postuler",
    "APPLY NOW", "Voir l'offre", "Lire la suite",
])
def test_links_falls_back_to_the_slug_when_the_label_is_boilerplate(label):
    """The posting is kept; only its label is distrusted.

    Inetum labels all 1620 of its postings "Lire la suite", so dropping the
    anchor would lose the entire board rather than just its titles.
    """
    jobs = scrape_links(board(
        {"https://acme.fr/jobs": page(
            f"<a href='/jobs/8142223-chef-de-projet'>{label}</a>"
        )},
        ats=ATSName.TEAMTAILOR,
    ))

    assert [job.title for job in jobs] == ["Chef De Projet"]


#: Inetum's shape: the anchor says "Lire la suite", the title is a heading
#: beside it, and the slug is a bare UUID.
CARD_BOARD = page(
    "<div class='card'>"
    "  <h3>Senior Data Engineer</h3>"
    "  <span>Warsaw</span>"
    "  <a href='/fr/accueil/carrieres/c7d3cf7c-3fa8-43bf-b34b-91ff69500ce6"
    ".html'>Lire la suite</a>"
    "</div>"
)


def test_links_takes_the_card_heading_when_the_label_is_boilerplate():
    """The slug is a UUID here, so without the heading these 1442 Inetum
    rows read "C7d3cf7c 3fa8 43bf B34b 91ff69500ce6"."""
    jobs = scrape_links(board({"https://www.inetum.com/x": CARD_BOARD},
                              url="https://www.inetum.com/x"))

    assert [job.title for job in jobs] == ["Senior Data Engineer"]


def test_links_still_prefers_the_anchor_label_over_the_card_heading():
    """A card heading may be a section title; the link's own text wins."""
    marked = page(
        "<div><h3>Nos offres</h3>"
        "<a href='/offres/data-engineer-h-f'>Data Engineer H/F</a></div>"
    )

    jobs = scrape_links(board({"https://acme.fr/x": marked},
                              url="https://acme.fr/x"))

    assert [job.title for job in jobs] == ["Data Engineer H/F"]


def test_links_prefers_a_real_label_over_the_slug():
    """Both anchors point at one posting; _dedupe keeps the first.

    Boards put the card heading before the "read more" button, so the real
    title is the one that survives.
    """
    jobs = _dedupe(scrape_links(board(
        {"https://acme.fr/jobs": page(
            "<a href='/jobs/8142223-chef-de-projet'>Chef de projet</a>"
            "<a href='/jobs/8142223-chef-de-projet'>Learn more</a>"
        )},
        ats=ATSName.TEAMTAILOR,
    )))

    assert [job.title for job in jobs] == ["Chef de projet"]


# ======================================================================
# Strategy 4: browser render -- opt-in, last resort
# ======================================================================


class FakeRenderer:
    """A Renderer that counts its calls. No browser is ever launched here.

    Same signature as job_scraper.render.render and detector.Renderer, so the
    suite exercises the wiring without Playwright installed.
    """

    def __init__(self, html=None):
        self.html = html
        self.calls = []

    def __call__(self, url: str):
        self.calls.append(url)

        return self.html


#: A JS shell: the anchors the board really has arrive only after rendering.
SHELL_PAGE = page("<div id='root'></div>")


def test_rendering_is_off_unless_a_renderer_is_supplied():
    """The default Board stays byte-identical on a browser-less machine."""
    made = board({"https://acme.fr/jobs": SHELL_PAGE})

    assert made.render is None
    assert made.scrape_board() == []


def test_the_browser_runs_only_after_every_cheaper_strategy_is_empty():
    """It is the most expensive strategy by orders of magnitude. A board the
    anchors already cover must never pay for it."""
    renderer = FakeRenderer(BOARD_PAGE)
    made = board({"https://acme.fr/jobs": BOARD_PAGE}, render=renderer)

    jobs = made.scrape_board()

    assert [job.via for job in jobs] == ["links", "links"]
    assert renderer.calls == []


def test_the_browser_recovers_a_board_whose_listing_is_drawn_in_js():
    """The ~13 uncovered boards in the corpus all fail this way: the served
    HTML is a shell, and the postings exist only after the JS runs."""
    renderer = FakeRenderer(BOARD_PAGE)
    made = board({"https://acme.fr/jobs": SHELL_PAGE}, render=renderer)

    jobs = made.scrape_board()

    assert renderer.calls == ["https://acme.fr/jobs"]
    assert [job.title for job in jobs] == ["Chef de projet", "Dev senior"]


def test_rendered_via_is_not_links_so_a_browser_only_board_stays_visible():
    """Same reason `via` distinguishes feed from sitemap: a board that cannot
    be scraped without a browser must not read like an ordinary anchor page."""
    made = board(
        {"https://acme.fr/jobs": SHELL_PAGE},
        render=FakeRenderer(BOARD_PAGE),
    )

    assert all(job.via == "rendered" for job in made.scrape_board())


def test_a_failed_render_yields_nothing_rather_than_raising():
    """render() returns None for a timeout, a crashed page, or no browser at
    all -- none of which may kill a hundred-board run."""
    made = board({"https://acme.fr/jobs": SHELL_PAGE}, render=FakeRenderer())

    assert made.scrape_board() == []


def test_the_renderer_reaches_the_detector_too(monkeypatch):
    """ATSDetector has its own rendered-retry path; handing it the same
    renderer can name an ATS on a shell page and so unlock a feed."""
    seen = {}

    class SpyDetector:
        def __init__(self, render=None, session=None):
            seen["render"] = render
            seen["session"] = session

        def detect(self, url):
            return DetectionResult(
                input_url=url, final_url=url,
                detected_ats=None, confidence=0.0, scores={},
            )

    monkeypatch.setattr("job_scraper.board.ATSDetector", SpyDetector)

    renderer = FakeRenderer()
    board({}, render=renderer).detect_ats()

    assert seen["render"] is renderer


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

    jobs = made.scrape_board()

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

    jobs = made.scrape_board()

    assert jobs and all(job.via == "links" for job in jobs)


def test_the_board_page_is_fetched_once_for_all_strategies():
    """wordpress, sitemap and links all want the same document. Before the
    Board cached it they each fetched their own copy."""
    url = "https://acme.fr/jobs"
    made = board({url: BOARD_PAGE}, url=url)

    made.scrape_board()

    assert made.session.requested.count(url) == 1


def test_a_dead_board_page_is_not_re_fetched_by_every_strategy():
    """None is a real cached value here, so a plain None-check would send each
    strategy in turn back out to the same broken board."""
    url = "https://acme.fr/jobs"
    made = board({}, url=url)

    assert made.scrape_board() == []
    assert made.html is None
    assert made.session.requested.count(url) == 1


def test_an_undeclared_charset_is_read_as_utf8_not_latin1():
    """requests defaults text/* with no charset to ISO-8859-1 (RFC 2616), which
    HTML5 does not. Trusting it turned "Développeur" into "DÃ©veloppeur" on
    Scalian and Sopra Steria, both of which declare utf-8 in a <meta> only."""
    class LatinGuess(FakeResponse):
        def __init__(self, body):
            super().__init__(body, content_type="text/html")
            # What requests actually reports for a charset-less text/html.
            self.encoding = "ISO-8859-1"

    class Undeclared(FakeSession):
        def get(self, url, **kwargs):
            self.requested.append(url)

            return LatinGuess(self.pages.get(url))

    url = "https://acme.fr/jobs"
    session = Undeclared({url: "<h1>Développeur</h1>"})

    assert "Développeur" in _fetch(session, url)


def test_a_declared_charset_is_still_honoured():
    """The fix must not override a board that says what it means."""
    class Latin(FakeResponse):
        def __init__(self, body):
            super().__init__(body, content_type="text/html; charset=latin-1")
            self._body = "Développeur".encode("latin-1")
            self.encoding = "latin-1"

    class Declared(FakeSession):
        def get(self, url, **kwargs):
            self.requested.append(url)

            return Latin(self.pages.get(url))

    url = "https://acme.fr/jobs"

    assert _fetch(Declared({url: "x"}), url) == "Développeur"


def test_every_detectable_ats_has_a_slot():
    """A new registry entry must not silently arrive with no scraping plan.

    Every ATS the detector can name is a FEEDS row, a VENDOR_SCRAPERS
    function, or a VENDOR_NOTES entry saying what was probed and why there is
    no scraper yet. The third is how a known gap stays a recorded decision
    instead of an oversight.
    """
    missing = [
        name for name in ATS_NAMES
        if name not in FEEDS
        and name not in VENDOR_SCRAPERS
        and name not in VENDOR_NOTES
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

    jobs = made.scrape_board()

    assert jobs and all(job.via == "sitemap" for job in jobs)


def test_vendor_notes_carry_their_lead():
    """The note is the point -- an empty entry teaches nothing.

    These used to be functions that raised NotImplementedError so the caller
    could catch it. The research survived the rewrite; the control flow did
    not need to.
    """
    assert "API key" in VENDOR_NOTES[ATSName.TEAMTAILOR]
    assert all(note.strip() for note in VENDOR_NOTES.values())


def test_notes_never_shadow_a_real_scraper():
    """A note is documentation. The moment one names an ATS that also has a
    scraper, the reader cannot tell which is live."""
    assert not set(VENDOR_NOTES) & set(VENDOR_SCRAPERS)
    assert not set(VENDOR_NOTES) & set(FEEDS)


def test_a_board_with_nothing_returns_no_jobs_rather_than_raising():
    made = board({}, ats=ATSName.GREENHOUSE,
                 url="https://boards.greenhouse.io/acme")

    assert made.scrape_board() == []


@pytest.mark.parametrize("url,expected", [
    # Personio blocks the detector's fetch with a 429 and a redirect to
    # marketing, so without this its working XML feed is never reached.
    ("https://personio.jobs.personio.de/", ATSName.PERSONIO),
    ("https://breezy.breezy.hr/", ATSName.BREEZY),
    ("https://acme.wd3.myworkdayjobs.com/careers", ATSName.WORKDAY),
    ("https://healthforce.applytojob.com/apply", ATSName.JAZZHR),
    # An employer's own careers domain says nothing about the ATS behind it --
    # guessing here is exactly what detector.py scores evidence to avoid.
    ("https://careers.acme.com/jobs", None),
    # Suffix, not substring: notpersonio.de is not personio.
    ("https://www.notpersonio.de/jobs", None),
])
def test_host_alone_names_the_ats_when_the_page_cannot_be_read(url, expected):
    assert _ats_from_host(url) == expected


def test_dedupe_collapses_fragments_and_trailing_slashes():
    jobs = [
        Job("acme", "Dev", "https://acme.fr/jobs/1"),
        Job("acme", "Dev again", "https://acme.fr/jobs/1/"),
        Job("acme", "Dev thrice", "https://acme.fr/jobs/1#apply"),
        Job("acme", "Other", "https://acme.fr/jobs/2"),
    ]

    assert [job.title for job in _dedupe(jobs)] == ["Dev", "Other"]
