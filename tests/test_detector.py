"""Detector regression tests.

Two halves:

  * the saved corpus (run tests/fetch_fixtures.py first) -- skipped if absent;
  * hand-written adversarial pages, which need no network and guard the
    false-positive patterns that the pre-rewrite detector fell for.
"""

from __future__ import annotations

import csv
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from job_scrapper.detector import (  # noqa: E402
    ATS_CAP,
    COMPILED,
    SOURCE_CAP,
    ATSDetector,
)

FIXTURES = Path(__file__).resolve().parent / "fixtures"


@pytest.fixture(scope="module")
def detector() -> ATSDetector:
    return ATSDetector()


def load_corpus() -> list[tuple[str, str, str]]:
    labels = FIXTURES / "labels.csv"

    if not labels.exists():
        return []

    with labels.open(newline="", encoding="utf-8") as handle:
        return [
            (row["fixture"], row["url"], row["expected"])
            for row in csv.DictReader(handle)
            if (FIXTURES / row["fixture"]).exists()
        ]


CORPUS = load_corpus()


# ======================================================================
# Corpus
# ======================================================================


@pytest.mark.skipif(not CORPUS, reason="no fixtures; run fetch_fixtures.py")
@pytest.mark.parametrize(
    "fixture,url,expected",
    [pytest.param(*row, id=row[0][:60]) for row in CORPUS],
)
def test_corpus(detector, fixture, url, expected):
    html = (FIXTURES / fixture).read_text(encoding="utf-8")
    result = detector.detect_html(html, url)

    assert result.detected_ats == (expected or None)


# ======================================================================
# Adversarial: the patterns that produced the old detector's 18 false
# positives. Each of these must stay unknown.
# ======================================================================


def page(body: str, head: str = "") -> str:
    return f"<html><head>{head}</head><body>{body}</body></html>"


ADVERSARIAL = [
    (
        "two-segment path is not Ashby",
        "https://www.sully-group.com/fr/carrieres",
        page("<h1>Nos carrieres</h1><p>Rejoignez-nous</p>"),
    ),
    (
        "generic /jobs/<id> path is not iCIMS",
        "https://example.com/en/jobs/1234",
        page("<h1>Software Engineer</h1>"),
    ),
    (
        "vendor name in a job description is not the vendor",
        "https://example.com/careers/data-engineer",
        page(
            "<h1>Data Engineer</h1>"
            "<p>Workday experience preferred. Familiarity with Greenhouse "
            "and Lever is a plus. We also use BambooHR internally.</p>"
        ),
    ),
    (
        "bare /apply is not Lever",
        "https://example.com/careers/engineer/apply",
        page("<h1>Apply</h1><form><input name='name'></form>"),
    ),
    (
        "UUID job path off-host is not Lever",
        "https://lity.so/jobs/8e3bd2ee-2fd7-4ac5-9662-04498ec711ec/cto/",
        page("<h1>CTO</h1>"),
    ),
    (
        "/jobs/<id>-<slug> off-host is not Teamtailor",
        "https://example.com/jobs/8142223-chef-de-projet",
        page("<h1>Chef de projet</h1>"),
    ),
    (
        "/api/token is not Talentsoft",
        "https://example.com/api/token",
        page("<p>ok</p>"),
    ),
    (
        "generic /careers/ path is not BambooHR",
        "https://example.com/careers/",
        page("<h1>Careers</h1>"),
    ),
    (
        "uuid/applyUrl field names are not SmartRecruiters",
        "https://example.com/offres-demploi/",
        page(
            "<script>var config = {uuid: 'abc', applyUrl: '/apply', "
            "refNumber: 'R-1'};</script>"
        ),
    ),
    (
        "gh_jid in documentation text is not Greenhouse",
        "https://example.com/blog/ats-integrations",
        page(
            "<p>Pass the gh_jid= parameter to the board API to fetch "
            "a posting.</p>"
        ),
    ),
    (
        "generic /job/<slug>/ path is not Workday",
        "https://careers.soprasteria.fr/job/ingenieur-data-in-lyon/",
        page("<h1>Ingenieur Data</h1>"),
    ),
    (
        "a JobPosting schema alone identifies no vendor",
        "https://example.com/careers/backend",
        page(
            "<h1>Backend</h1>",
            '<script type="application/ld+json">'
            '{"@type":"JobPosting","title":"Backend",'
            '"hiringOrganization":{"@type":"Organization","name":"Acme"}}'
            "</script>",
        ),
    ),
]


@pytest.mark.parametrize(
    "name,url,html",
    [pytest.param(*row, id=row[0]) for row in ADVERSARIAL],
)
def test_adversarial_stays_unknown(detector, name, url, html):
    result = detector.detect_html(html, url)

    assert result.detected_ats is None, (
        f"{name}: falsely detected {result.detected_ats} "
        f"({result.scores.get(result.detected_ats)} pts)"
    )


# ======================================================================
# Positive fingerprints, hand-written so they do not depend on the corpus
# ======================================================================


POSITIVE = [
    (
        "lever hosted posting",
        "https://jobs.lever.co/acme/8e3bd2ee-2fd7-4ac5-9662-04498ec711ec",
        page("<h1>CTO</h1>"),
        "lever",
    ),
    (
        "workday hosted posting",
        "https://acme.wd3.myworkdayjobs.com/en-US/careers/job/Paris/Dev_R-1",
        page("<div data-automation-id='jobPostingHeader'>Dev</div>"),
        "workday",
    ),
    (
        "taleo on an employer domain",
        "https://careers.example.com/careersection/ex/jobdetail.ftl?job=1234",
        page("<h1>Engineer</h1>"),
        "taleo",
    ),
    (
        "greenhouse gh_jid parameter",
        "https://example.com/careers/apply?gh_jid=4567890",
        page("<h1>Engineer</h1>"),
        "greenhouse",
    ),
    (
        "teamtailor custom domain via CDN assets",
        "https://careers.example.com/jobs/8142223-developer",
        page(
            "<h1>Developer</h1>",
            "<link rel='stylesheet' "
            "href='https://assets-aws.teamtailor-cdn.com/assets/x.css'>"
            "<script src='https://assets-aws.teamtailor-cdn.com/p.js'>"
            "</script>",
        ),
    ),
    (
        "workable custom domain via alternate link",
        "https://careers.example.com/",
        page(
            "<h1>Openings</h1>",
            "<link rel='alternate' hreflang='en' "
            "href='https://apply.workable.com/exotec/?lng=en'>",
        ),
        "workable",
    ),
]


@pytest.mark.parametrize(
    "name,url,html,expected",
    [
        pytest.param(
            row[0], row[1], row[2],
            row[3] if len(row) > 3 else "teamtailor",
            id=row[0],
        )
        for row in POSITIVE
    ],
)
def test_positive_fingerprints(detector, name, url, html, expected):
    result = detector.detect_html(html, url)

    assert result.detected_ats == expected, f"{name}: {result.scores}"


# ======================================================================
# Structural invariants -- the score-inflation class made unrepresentable
# ======================================================================


def test_no_source_exceeds_cap(detector):
    """One source can never contribute more than SOURCE_CAP to one ATS."""
    url = "https://acme.recruitee.com/o/developer"

    html = page(
        "<h1>Dev</h1> recruitee recruitee recruitee",
        "<script src='https://acme.recruitee.com/a.js'></script>"
        "<link rel='canonical' href='https://acme.recruitee.com/o/developer'>",
    )

    result = detector.detect_html(html, url)

    for ats, buckets in result.source_scores.items():
        for source, points in buckets.items():
            assert points <= SOURCE_CAP, f"{ats}.{source} = {points}"

    assert max(result.scores.values()) <= ATS_CAP


def test_signal_ids_are_unique_per_ats():
    """A duplicated signal_id would silently swallow a fingerprint."""
    for name, (_, rules) in COMPILED.items():
        ids = [item.signal_id for item in rules]

        assert len(ids) == len(set(ids)), f"{name} has duplicate signal ids"


LEVER_EMBED = (
    "<script src='https://jobs.lever.co/x.js'></script>"
    "<iframe src='https://jobs.lever.co/acme'></iframe>"
    "<form action='https://jobs.lever.co/acme'></form>"
)


def test_hostname_beats_embedded_references(detector):
    """Where the page lives outranks whatever it embeds."""
    result = detector.detect_html(
        page(LEVER_EMBED),
        "https://boards.greenhouse.io/acme/jobs/12345",
    )

    assert result.detected_ats == "greenhouse"


def test_two_qualified_vendors_are_ambiguous(detector):
    """Never pick a winner when two platforms both have real evidence."""
    result = detector.detect_html(
        page(
            "<script src='https://jobs.lever.co/x.js'></script>"
            "<iframe src='https://jobs.lever.co/acme'></iframe>"
            "<form action='https://boards.greenhouse.io/acme'></form>"
            "<link rel='stylesheet' href='https://boards.greenhouse.io/s.css'>"
        ),
        "https://careers.example.com/j/1",
    )

    assert result.status == "ambiguous"
    assert result.detected_ats is None
    assert set(result.conflicts) == {"lever", "greenhouse"}


def test_js_shell_flags_needs_rendering(detector):
    result = detector.detect_html(
        page("<div id='root'></div><script src='/app.js'></script>"),
        "https://careers.example.com/jobs",
    )

    assert result.status == "unknown"
    assert result.needs_rendering is True


def test_terminology_alone_never_detects(detector):
    """Every vendor name at once must still resolve to unknown."""
    names = " ".join(
        term
        for ats, _ in COMPILED.values()
        for term in ats.terms
    )

    result = detector.detect_html(
        page(f"<p>We have used {names} over the years.</p>"),
        "https://example.com/about",
    )

    assert result.detected_ats is None
