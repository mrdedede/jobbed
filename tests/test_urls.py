"""Tests for job_scraper/urls.py.

`title_from_url` is the highest-leverage function in the project. When it
returns "", the row has no title, the filter cannot judge it, the ID-exemption
passes it through unfiltered, and the detail stage pays a fetch for it. One
regex bug in here produced 7,779 titleless rows -- 39% of everything scraped.
"""

import pytest

from job_scraper.urls import JOB_URL_RE, ats_from_host, title_from_url

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


#: Each case is (path, why). All real postings measured live and missed by
#: JOB_URL_RE before it was widened for English/intermediate-segment shapes.
JOB_URL_HITS = [
    ("/job-offers/gucci-client-advisor-55/", "English 'offer(s)' was missing from the vocabulary"),
    ("/poste/developpeur-java-3/", "French 'poste' was missing from the vocabulary"),
    ("/fr/annonce/3571653-technicienne-atelier/", "'annonce' was missing from the vocabulary"),
    ("/opportunities/data-engineer/", "'opportunity/-ies' was missing from the vocabulary"),
    ("/en/talent/job-offers/asia/gucci-client-advisor-55/",
     "two segments between the job word and the slug -- kering's shape"),
    ("/company/careers/jobs/mirakl/5678655004/",
     "two segments between the job word and the slug -- mirakl's shape"),
    ("/global/en/careers/offers/detail/744000142576829",
     "two segments, bare numeric id slug -- talan's shape"),
    ("/careers/data_engineer_h_f", "underscore-joined slug, not just hyphenated"),
    ("/fr/details-doffre/data-engineer-7485457", "French elision 'd'offre' "
     "glued onto the job word with no separating hyphen -- Deezer's shape"),
]

#: Shapes that must keep missing: a category/listing page, not a posting.
JOB_URL_MISSES = [
    ("/jobs/", "no slug at all"),
    ("/careers/", "no slug at all"),
    ("/join-us/life-at-atos", "measured and rejected -- costs more false "
     "positives (Atos, Equans) than it buys (Extia)"),
    ("/career-advice/how-to-write-a-cv",
     "a content section, not a posting collection"),
    ("/jobs-blog/our-culture", "a content section, not a posting collection"),
    ("/en_US/externaljobs/JobDetail/498916",
     "Avature's marketing link -- 'jobs' must not match mid-word inside "
     "'externaljobs'; the vendor-specific JOB_PATH override handles the "
     "real posting shape instead"),
    ("/offre-de-emploi/emploi-ajusteuse-ajusteur-f-h_22660.aspx",
     "known gap: a compound job-word segment with trailing garbage before "
     "the next slash (airfrance's shape) is not solved by this pass -- "
     "solving it reopened the career-advice/jobs-blog false positives"),
]


@pytest.mark.parametrize("path,why", JOB_URL_HITS,
                         ids=[c[0] for c in JOB_URL_HITS])
def test_job_url_re_matches(path, why):
    assert JOB_URL_RE.search(path), why


@pytest.mark.parametrize("path,why", JOB_URL_MISSES,
                         ids=[c[0] for c in JOB_URL_MISSES])
def test_job_url_re_rejects(path, why):
    assert not JOB_URL_RE.search(path), why


class TestAtsFromHost:
    """Vendor-owned hostnames only -- an employer's own careers domain says
    nothing about the ATS behind it."""

    def test_vendor_host_is_named(self):
        assert ats_from_host("https://boards.greenhouse.io/acme") is not None

    def test_employer_host_is_not_guessed(self):
        assert ats_from_host("https://careers.acme.com/jobs") is None

    def test_junk_does_not_raise(self):
        assert ats_from_host("not a url") is None
