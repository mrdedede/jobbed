"""Job-page scraper tests using mocked HTTP.

Tests verify the extractor ladder, field precedence, description cleaning and
resume bookkeeping. Same test-double style as test_board_scraper.py.
"""

from __future__ import annotations

import asyncio
import csv
import json

import pytest

from job_scraper import post_scraper
from job_scraper.post_scraper import (
    MAX_DESCRIPTION,
    _clean,
    _workday_api,
    already_done,
    fetch_job,
)
from job_scraper.models import Job
from tests.test_board_scraper import FakeSession, page


pytestmark = pytest.mark.anyio


@pytest.fixture
def fake(monkeypatch):
    """One FakeSession the test can inspect, also handed to scrape_details."""
    made = FakeSession({})

    monkeypatch.setattr(post_scraper, "new_client", lambda: made)

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


async def test_jsonld_fills_every_field(fake):
    fake.pages[row()["url"]] = jsonld(
        title="Senior Go Engineer",
        description="<p>Build things.</p>",
        hiringOrganization={"@type": "Organization", "name": "Acme SA"},
        jobLocation={"address": {"addressLocality": "Lille"}},
    )

    job = (await fetch_job(row(), fake))

    assert job.via == "jsonld"
    assert job.title == "Senior Go Engineer"
    assert job.company == "Acme SA"
    assert job.place == "Lille"
    assert job.description == "Build things."


async def test_jsonld_place_falls_back_to_region(fake):
    fake.pages[row()["url"]] = jsonld(
        title="Dev",
        description="text",
        jobLocation={"address": {"addressRegion": "Hauts-de-France"}},
    )

    assert (await fetch_job(row(), fake)).place == "Hauts-de-France"


async def test_jsonld_description_is_unescaped_and_stripped(fake):
    fake.pages[row()["url"]] = jsonld(
        title="Dev",
        description="&lt;p&gt;R&amp;D team&lt;/p&gt;&lt;ul&gt;&lt;li&gt;Go"
                    "&lt;/li&gt;&lt;/ul&gt;",
    )

    job = (await fetch_job(row(), fake))

    assert "<" not in job.description
    assert "R&D team" in job.description
    assert "Go" in job.description


async def test_description_is_capped(fake):
    fake.pages[row()["url"]] = jsonld(title="Dev", description="x" * 300_000)

    assert len((await fetch_job(row(), fake)).description) == MAX_DESCRIPTION


async def test_jsonld_without_description_falls_through_to_main(fake):
    fake.pages[row()["url"]] = page(
        "<main>The real posting body.</main>",
        head='<script type="application/ld+json">'
             '{"@type": "JobPosting", "title": "Ignored"}</script>',
    )

    job = (await fetch_job(row(), fake))

    assert job.via == "main"
    assert job.description == "The real posting body."


# ======================================================================
# <main> fallback
# ======================================================================


async def test_main_fallback_keeps_row_company_and_place(fake):
    fake.pages[row()["url"]] = page(
        '<h1>Ingénieur Backend</h1><main>Missions: du Go.</main>'
    )

    job = (await fetch_job(row(company="engie", place="Nanterre", ats="radancy"), fake))

    assert job.via == "main"
    assert job.title == "Ingénieur Backend"
    assert job.description == "Missions: du Go."
    # <main> names neither, so the board stage's values stand.
    assert job.company == "engie"
    assert job.place == "Nanterre"


async def test_main_prefers_og_title_over_h1(fake):
    fake.pages[row()["url"]] = page(
        "<h1>Careers</h1><main>Body.</main>",
        head='<meta property="og:title" content="Data Engineer F/H">',
    )

    assert (await fetch_job(row(), fake)).title == "Data Engineer F/H"


async def test_role_main_is_accepted(fake):
    fake.pages[row()["url"]] = page('<div role="main">Body text.</div>')

    assert (await fetch_job(row(), fake)).via == "main"


async def test_main_drops_page_chrome(fake):
    fake.pages[row()["url"]] = page(
        "<main><nav>Rechercher les offres</nav>"
        "<p>Missions.</p><footer>Mentions légales</footer></main>"
    )

    assert (await fetch_job(row(), fake)).description == "Missions."


# ======================================================================
# <body> last resort
# ======================================================================


async def test_body_fallback_when_there_is_no_main(fake):
    # The plain WordPress/AEM shape: no JobPosting JSON-LD, no <main>.
    fake.pages[row()["url"]] = page(
        '<script type="application/ld+json">{"@type": "WebPage"}</script>'
        "<nav>Cybersecurity Data AI</nav>"
        '<div class="job-content"><h1>DevOps H/F</h1>'
        "<p>Chez Alteca.</p></div>"
    )

    job = (await fetch_job(row(), fake))

    assert job.via == "body"
    assert "Chez Alteca." in job.description
    # Chrome is gone, which is what makes this rung usable at all.
    assert "Cybersecurity" not in job.description
    assert job.title == "DevOps H/F"


async def test_html_entities_are_unescaped_in_title_and_company(fake):
    fake.pages[row()["url"]] = jsonld(
        title="R&amp;D Engineer",
        description="text",
        hiringOrganization={"name": "IT &amp; Systèmes"},
    )

    job = (await fetch_job(row(), fake))

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


async def test_workday_reads_json_and_never_fetches_the_page(fake):
    fake.pages[WORKDAY_API] = json.dumps({
        "jobPostingInfo": {
            "title": "Développeur Full-Stack",
            "jobDescription": "<p>Kubernetes, IAM.</p>",
            "location": "Néosoft Rennes",
        }
    })

    job = (await fetch_job(row(url=WORKDAY_URL, ats="workday"), fake))

    assert job.via == "workday"
    assert job.title == "Développeur Full-Stack"
    assert job.description == "Kubernetes, IAM."
    assert job.place == "Néosoft Rennes"
    # The page itself is a JS shell; asking for it is wasted traffic.
    assert fake.requested == [WORKDAY_API]


# ======================================================================
# Failure
# ======================================================================


async def test_gone_page_yields_a_row_rather_than_raising(fake):
    job = (await fetch_job(row(), fake))

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
        writer = csv.DictWriter(handle, fieldnames=post_scraper.FIELDNAMES)
        writer.writeheader()
        writer.writerow({"url": "https://acme.fr/jobs/1", "title": "Dev"})

    assert already_done(target) == {"https://acme.fr/jobs/1"}


# ======================================================================
# scrape_details: concurrency, order, resume
# ======================================================================


def _write_input(path, urls):
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(
            handle, fieldnames=["company", "title", "url", "place", "ats"]
        )
        writer.writeheader()

        for url in urls:
            writer.writerow({"company": "acme", "title": "t", "url": url,
                             "place": "", "ats": "teamtailor"})


def _written_urls(path):
    with path.open(newline="", encoding="utf-8") as handle:
        return [r["url"] for r in csv.DictReader(handle)]


async def test_rows_are_written_in_input_order_whatever_finishes_first(
    tmp_path, fake, monkeypatch
):
    urls = [f"https://acme.fr/jobs/{n}" for n in range(6)]
    _write_input(tmp_path / "in.csv", urls)

    async def slow_first(row, client):
        # Earlier rows take longer, so completion order is the reverse.
        await asyncio.sleep(0.02 * (len(urls) - urls.index(row["url"])))

        return Job(company="acme", title="t", description="",
                   url=row["url"], via="none")

    monkeypatch.setattr(post_scraper, "fetch_job", slow_first)

    await post_scraper.scrape_details(
        input_file=tmp_path / "in.csv", output_file=tmp_path / "out.csv",
        workers=6,
    )

    assert _written_urls(tmp_path / "out.csv") == urls


async def test_resume_skips_urls_already_written(tmp_path, fake, monkeypatch):
    def fresh():
        # scrape_details closes its client, so each run needs its own.
        made = FakeSession(fake.pages)
        made.requested = fake.requested

        return made

    monkeypatch.setattr(post_scraper, "new_client", fresh)
    first, second = "https://acme.fr/jobs/1", "https://acme.fr/jobs/2"
    _write_input(tmp_path / "in.csv", [first, second])
    fake.pages[first] = jsonld(title="One", description="a")
    fake.pages[second] = jsonld(title="Two", description="b")

    await post_scraper.scrape_details(
        input_file=tmp_path / "in.csv", output_file=tmp_path / "out.csv",
        limit=1,
    )
    fake.requested.clear()
    stats = await post_scraper.scrape_details(
        input_file=tmp_path / "in.csv", output_file=tmp_path / "out.csv",
    )

    assert fake.requested == [second]
    assert stats["skipped"] == 1 and stats["pending"] == 1
    assert _written_urls(tmp_path / "out.csv") == [first, second]


def test_start_order_spreads_hosts_and_keeps_each_hosts_own_order():
    rows = [{"url": u} for u in (
        "https://a.fr/1", "https://a.fr/2", "https://a.fr/3",
        "https://b.fr/1", "https://c.fr/1", "https://b.fr/2",
    )]

    assert post_scraper.start_order(rows) == [0, 3, 4, 1, 5, 2]
    assert post_scraper.start_order([]) == []
