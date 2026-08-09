"""Job-page scraper tests using mocked HTTP.

Tests verify the extractor ladder, field precedence, description cleaning and
resume bookkeeping. Same test-double style as test_board_scraper.py.
"""

from __future__ import annotations

import csv
import json

import pytest

from job_scraper import job_scraper
from job_scraper.job_scraper import (
    MAX_DESCRIPTION,
    _clean,
    _workday_api,
    already_done,
    fetch_job,
)
from tests.test_board_scraper import FakeSession, page


@pytest.fixture
def fake(monkeypatch):
    """Route every session() call to one FakeSession the test can inspect."""
    made = FakeSession({})

    monkeypatch.setattr(job_scraper, "session", lambda: made)

    return made


def row(**overrides) -> dict:
    base = {
        "company": "acme",
        "title": "dev-senior-h-f",
        "url": "https://acme.fr/jobs/842306",
        "place": "",
        "ats": "teamtailor",
    }
    base.update(overrides)

    return base


def jsonld(**fields) -> str:
    node = {"@context": "https://schema.org", "@type": "JobPosting"}
    node.update(fields)

    return page(
        "<p>body</p>",
        head=f'<script type="application/ld+json">{json.dumps(node)}</script>',
    )


# ======================================================================
# JSON-LD
# ======================================================================


def test_jsonld_fills_every_field(fake):
    fake.pages[row()["url"]] = jsonld(
        title="Senior Go Engineer",
        description="<p>Build things.</p>",
        hiringOrganization={"@type": "Organization", "name": "Acme SA"},
        jobLocation={"address": {"addressLocality": "Lille"}},
    )

    job = fetch_job(row())

    assert job.via == "jsonld"
    assert job.title == "Senior Go Engineer"
    assert job.company == "Acme SA"
    assert job.place == "Lille"
    assert job.description == "Build things."


def test_jsonld_place_falls_back_to_region(fake):
    fake.pages[row()["url"]] = jsonld(
        title="Dev",
        description="text",
        jobLocation={"address": {"addressRegion": "Hauts-de-France"}},
    )

    assert fetch_job(row()).place == "Hauts-de-France"


def test_jsonld_description_is_unescaped_and_stripped(fake):
    fake.pages[row()["url"]] = jsonld(
        title="Dev",
        description="&lt;p&gt;R&amp;D team&lt;/p&gt;&lt;ul&gt;&lt;li&gt;Go"
                    "&lt;/li&gt;&lt;/ul&gt;",
    )

    job = fetch_job(row())

    assert "<" not in job.description
    assert "R&D team" in job.description
    assert "Go" in job.description


def test_description_is_capped(fake):
    fake.pages[row()["url"]] = jsonld(title="Dev", description="x" * 300_000)

    assert len(fetch_job(row()).description) == MAX_DESCRIPTION


def test_jsonld_without_description_falls_through_to_main(fake):
    fake.pages[row()["url"]] = page(
        "<main>The real posting body.</main>",
        head='<script type="application/ld+json">'
             '{"@type": "JobPosting", "title": "Ignored"}</script>',
    )

    job = fetch_job(row())

    assert job.via == "main"
    assert job.description == "The real posting body."


# ======================================================================
# <main> fallback
# ======================================================================


def test_main_fallback_keeps_row_company_and_place(fake):
    fake.pages[row()["url"]] = page(
        '<h1>Ingénieur Backend</h1><main>Missions: du Go.</main>'
    )

    job = fetch_job(row(company="engie", place="Nanterre", ats="radancy"))

    assert job.via == "main"
    assert job.title == "Ingénieur Backend"
    assert job.description == "Missions: du Go."
    # <main> names neither, so the board stage's values stand.
    assert job.company == "engie"
    assert job.place == "Nanterre"


def test_main_prefers_og_title_over_h1(fake):
    fake.pages[row()["url"]] = page(
        "<h1>Careers</h1><main>Body.</main>",
        head='<meta property="og:title" content="Data Engineer F/H">',
    )

    assert fetch_job(row()).title == "Data Engineer F/H"


def test_role_main_is_accepted(fake):
    fake.pages[row()["url"]] = page('<div role="main">Body text.</div>')

    assert fetch_job(row()).via == "main"


def test_main_drops_page_chrome(fake):
    fake.pages[row()["url"]] = page(
        "<main><nav>Rechercher les offres</nav>"
        "<p>Missions.</p><footer>Mentions légales</footer></main>"
    )

    assert fetch_job(row()).description == "Missions."


# ======================================================================
# <body> last resort
# ======================================================================


def test_body_fallback_when_there_is_no_main(fake):
    # The plain WordPress/AEM shape: no JobPosting JSON-LD, no <main>.
    fake.pages[row()["url"]] = page(
        '<script type="application/ld+json">{"@type": "WebPage"}</script>'
        "<nav>Cybersecurity Data AI</nav>"
        '<div class="job-content"><h1>DevOps H/F</h1>'
        "<p>Chez Alteca.</p></div>"
    )

    job = fetch_job(row())

    assert job.via == "body"
    assert "Chez Alteca." in job.description
    # Chrome is gone, which is what makes this rung usable at all.
    assert "Cybersecurity" not in job.description
    assert job.title == "DevOps H/F"


def test_html_entities_are_unescaped_in_title_and_company(fake):
    fake.pages[row()["url"]] = jsonld(
        title="R&amp;D Engineer",
        description="text",
        hiringOrganization={"name": "IT &amp; Systèmes"},
    )

    job = fetch_job(row())

    assert job.title == "R&D Engineer"
    assert job.company == "IT & Systèmes"


# ======================================================================
# Workday
# ======================================================================


WORKDAY_URL = (
    "https://neosoft.wd3.myworkdayjobs.com/fr-FR/neo-soft/job/Rennes/Dev_R-1"
)
WORKDAY_API = (
    "https://neosoft.wd3.myworkdayjobs.com/wday/cxs/neosoft/neo-soft"
    "/job/Rennes/Dev_R-1"
)


def test_workday_api_url_strips_the_locale_segment():
    assert _workday_api(WORKDAY_URL) == WORKDAY_API


def test_workday_reads_json_and_never_fetches_the_page(fake):
    fake.pages[WORKDAY_API] = json.dumps({
        "jobPostingInfo": {
            "title": "Développeur Full-Stack",
            "jobDescription": "<p>Kubernetes, IAM.</p>",
            "location": "Néosoft Rennes",
        }
    })

    job = fetch_job(row(url=WORKDAY_URL, ats="workday"))

    assert job.via == "workday"
    assert job.title == "Développeur Full-Stack"
    assert job.description == "Kubernetes, IAM."
    assert job.place == "Néosoft Rennes"
    # The page itself is a JS shell; asking for it is wasted traffic.
    assert fake.requested == [WORKDAY_API]


# ======================================================================
# Failure
# ======================================================================


def test_gone_page_yields_a_row_rather_than_raising(fake):
    job = fetch_job(row())

    assert job.via == "none"
    assert job.description == ""
    # Everything the board stage knew survives.
    assert job.title == "dev-senior-h-f"
    assert job.url == row()["url"]


def test_clean_ignores_non_strings():
    assert _clean(None) == ""
    assert _clean({"@type": "Text"}) == ""
    assert _clean("   ") == ""


# ======================================================================
# Resume
# ======================================================================


def test_already_done_reads_written_urls(tmp_path):
    target = tmp_path / "detailed_jobs.csv"

    assert already_done(target) == set()

    with target.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=job_scraper.FIELDNAMES)
        writer.writeheader()
        writer.writerow({"url": "https://acme.fr/jobs/1", "title": "Dev"})

    assert already_done(target) == {"https://acme.fr/jobs/1"}
