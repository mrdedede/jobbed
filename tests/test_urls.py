"""Tests for job_scraper/urls.py.

`title_from_url` is the highest-leverage function in the project. When it
returns "", the row has no title, the filter cannot judge it, the ID-exemption
passes it through unfiltered, and the detail stage pays a fetch for it. One
regex bug in here produced 7,779 titleless rows -- 39% of everything scraped.
"""

import pytest

from job_scraper.urls import ats_from_host, title_from_url

# Each case is (url, expected, why). The `why` is the point: these are the
# real shapes that were failing, not invented ones.
CASES = [
    (
        "https://x.com/fr/jobs/associate-real-estate/76969",
        "Associate Real Estate",
        "the descriptive segment is one level up from a bare record id -- "
        "this shape alone is most of the 39%",
    ),
    (
        "https://x.com/offres/4lxbw5bd1f-data-ingenieur-h-f/",
        "Data Ingenieur H F",
        "a hash prefix is stripped whole, not just its leading digit",
    ),
    (
        "https://wearesander.com/fr/jobs/boekhouder-%7C-chemische-sector/78977",
        "Boekhouder | Chemische Sector",
        "percent-encoding is decoded, or %7C reads as the word '7c'",
    ),
    (
        "https://x.com/fr/jobs/comptable-avec-exp%C3%A9rience/76050",
        "Comptable Avec Expérience",
        "accented French slugs survive decoding",
    ),
    (
        "https://x.com/jobs/senior-python-dev.html",
        "Senior Python Dev",
        "a page extension goes, and 'senior' -- a blacklist term -- stays",
    ),
    (
        "https://x.com/jobs/associate-consultant",
        "Associate Consultant",
        "a long leading word is not mistaken for a hash: it has no digit",
    ),
    (
        "https://x.com/cafe-manager",
        "Cafe Manager",
        "a real word that happens to spell in hex is not an id",
    ),
    (
        "https://x.com/careers/data_engineer_h_f",
        "Data Engineer H F",
        "underscores separate words too",
    ),
    (
        "https://acme.fr/jobs/842306-chef-de-projet",
        "Chef De Projet",
        "a leading numeric id on the slug itself is stripped",
    ),
    (
        "https://acme.fr/jobs/lead_data_engineer/",
        "Lead Data Engineer",
        "a trailing slash does not hide the slug",
    ),
    (
        "https://acme.fr/carrieres/dev-senior.html",
        "Dev Senior",
        "otherwise every title on such a board ends in 'Html'",
    ),
    (
        "https://acme.fr/jobs/chef-de-projet.aspx",
        "Chef De Projet",
        ".aspx too",
    ),
    (
        "https://x.com/jobs/843490",
        "",
        "AXA and Leroy Merlin really do publish nothing but an id; '' is the "
        "honest answer and is what marks the row unjudgeable downstream",
    ),
    (
        "https://x.com/job/12345",
        "",
        "a generic collection segment is not a title",
    ),
    ("https://x.com/", "", "no path at all"),
    ("https://x.com", "", "no path at all, without the trailing slash"),
]


@pytest.mark.parametrize("url,expected,why",
                         CASES, ids=[case[0][:60] for case in CASES])
def test_title_from_url(url, expected, why):
    assert title_from_url(url) == expected, why


def test_the_regression_that_started_this():
    """The one the audit named: this must not be empty."""
    assert title_from_url(
        "https://x.com/fr/jobs/associate-real-estate/76969"
    ) != ""


def test_no_url_shape_raises():
    """Called on every sitemap row, so it has to survive junk."""
    for url in ("", "not a url", "http://", "///", "https://x.com/%%%"):
        assert isinstance(title_from_url(url), str)


class TestAtsFromHost:
    """Vendor-owned hostnames only -- an employer's own careers domain says
    nothing about the ATS behind it."""

    def test_vendor_host_is_named(self):
        assert ats_from_host("https://boards.greenhouse.io/acme") is not None

    def test_employer_host_is_not_guessed(self):
        assert ats_from_host("https://careers.acme.com/jobs") is None

    def test_junk_does_not_raise(self):
        assert ats_from_host("not a url") is None
