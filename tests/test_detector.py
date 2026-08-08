"""Detector regression and unit tests.

Uses saved corpus (if available) and adversarial test cases.
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
    _is_infra,
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

    # ------------------------------------------------------------------
    # The corpus reaches 13 of 28 platforms. Everything below has no saved
    # fixture, so without these the rules are shipped untested -- which is
    # how qparam("jobReqId") stayed dead code through a whole rewrite.
    # ------------------------------------------------------------------

    (
        "successfactors on an employer domain",
        "https://careers.example.com/sf/recruiting/?jobReqId=778899",
        page("<h1>Engineer</h1>"),
        "successfactors",
    ),
    (
        "talentsoft front office API path",
        "https://careers.example.com/api/v1/offersummaries",
        page("<h1>Offres</h1>"),
        "talentsoft",
    ),
    (
        "jobvite embedded on an employer domain",
        "https://careers.example.com/careers/",
        page(
            "<div class='jv-careersite'></div>",
            "<script src='https://jobs.jobvite.com/careersite/public/"
            "iframe.js'></script>",
        ),
        "jobvite",
    ),
    (
        "digitalrecruiters on an employer domain",
        "https://joinus.example.com/offre/1234",
        page(
            "<h1>Offre</h1>",
            "<script src='https://api.digitalrecruiters.com/w.js'></script>"
            "<link rel='canonical' "
            "href='https://api.digitalrecruiters.com/o/1234'>",
        ),
        "digitalrecruiters",
    ),
    (
        "personio hosted board",
        "https://jobs.personio.de/job/1234567",
        page("<h1>Entwickler</h1>"),
        "personio",
    ),
    (
        "pinpoint postings endpoint",
        "https://acme.pinpointhq.com/postings.json",
        page("<h1>Jobs</h1>"),
        "pinpoint",
    ),
    (
        "jazzhr hosted apply page",
        "https://acme.applytojob.com/apply/abc123",
        page("<h1>Apply</h1>"),
        "jazzhr",
    ),
    (
        "bamboohr hosted careers page",
        "https://acme.bamboohr.com/careers/42",
        page("<h1>Engineer</h1>"),
        "bamboohr",
    ),
    (
        "breezy hosted position",
        "https://acme.breezy.hr/p/abc123def",
        page("<h1>Engineer</h1>"),
        "breezy",
    ),
    (
        "comeet hosted job",
        "https://www.comeet.co/jobs/acme/12.34/engineer/A1.B2",
        page("<h1>Engineer</h1>"),
        "comeet",
    ),
    (
        "onlyfy hosted board",
        "https://acme.onlyfy.jobs/job/abc123",
        page("<h1>Entwickler</h1>"),
        "onlyfy",
    ),
    (
        "talentlyft hosted board",
        "https://acme.talentlyft.com/jobs/engineer",
        page("<h1>Engineer</h1>"),
        "talentlyft",
    ),
    (
        "softgarden hosted board",
        "https://acme.career.softgarden.de/vacancies/1234",
        page("<h1>Entwickler</h1>"),
        "softgarden",
    ),
    (
        "avature hosted board",
        "https://acme.avature.net/careers/JobDetail/1234",
        page("<h1>Engineer</h1>"),
        "avature",
    ),
    (
        "hibob hosted board",
        "https://acme.careers.hibob.com/jobs/abc-123/apply",
        page("<h1>Engineer</h1>"),
        "hibob",
    ),
    (
        "njoyn hosted board",
        "https://acme.njoyn.com/corp/xweb/xweb.asp?clid=1&Page=joblisting",
        page("<h1>Engineer</h1>"),
        "njoyn",
    ),
    (
        "phenom on an employer domain",
        "https://careers.example.com/global/en/job/1234",
        page(
            "<h1>Engineer</h1>",
            "<script src='https://cdn.phenompeople.com/x.js'></script>"
            "<link rel='stylesheet' "
            "href='https://cdn.phenompeople.com/a.css'>",
        ),
        "phenom",
    ),
    (
        "radancy on an employer domain",
        "https://careers.example.com/job/paris/engineer/1234/567",
        page(
            "<h1>Engineer</h1>",
            "<script src='https://tbcdn.talentbrew.com/js/head.js'></script>"
            "<link rel='stylesheet' "
            "href='https://tbcdn.talentbrew.com/css/x.css'>",
        ),
        "radancy",
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
    """Verify SOURCE_CAP and ATS_CAP are enforced."""
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
    """Verify all signal_ids are unique."""
    for name, (_, rules) in COMPILED.items():
        ids = [item.signal_id for item in rules]

        assert len(ids) == len(set(ids)), f"{name} has duplicate signal ids"


LEVER_EMBED = (
    "<script src='https://jobs.lever.co/x.js'></script>"
    "<iframe src='https://jobs.lever.co/acme'></iframe>"
    "<form action='https://jobs.lever.co/acme'></form>"
)


def test_hostname_beats_embedded_references(detector):
    """Hostname evidence outranks embedded references."""
    result = detector.detect_html(
        page(LEVER_EMBED),
        "https://boards.greenhouse.io/acme/jobs/12345",
    )

    assert result.detected_ats == "greenhouse"


def test_two_qualified_vendors_are_ambiguous(detector):
    """Ambiguous when two platforms have equal real evidence."""
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
    """Vendor terminology alone does not trigger detection."""
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


# ======================================================================
# Regressions
# ======================================================================


def test_query_parameter_name_is_case_insensitive(detector):
    """Query parameter matching is case-insensitive."""
    result = detector.detect_html(
        page("<h1>Engineer</h1>"),
        "https://careers.example.com/careers?jobReqId=12345",
    )

    assert result.scores["successfactors"] > 0


def test_no_evidence_reports_zero_confidence(detector):
    """No evidence results in zero confidence."""
    result = detector.detect_html(
        page("<p>Hello.</p>"),
        "https://example.com/",
    )

    assert result.status == "unknown"
    assert result.confidence == 0.0


def test_render_fallback_runs_only_for_js_shells():
    """Renderer is invoked only for JS-shell pages."""
    calls = []

    def fake_render(url: str) -> str:
        calls.append(url)
        return page(
            "<div data-automation-id='jobPostingHeader'>CTO</div>",
            "<script src='https://acme.wd3.myworkdayjobs.com/app.js'></script>",
        )
    url = "https://careers.example.com/jobs"
    shell = ATSDetector(render=fake_render)

    empty = shell.detect_html(
        page("<div id='root'></div>", "<script src='/app.js'></script>"),
        url,
    )
    assert empty.needs_rendering is True
    assert empty.detected_ats is None

    rendered = shell._maybe_render(empty, url, url)

    assert calls == [url]
    assert rendered.detected_ats == "workday"


def test_render_fallback_keeps_first_pass_when_renderer_adds_nothing():
    """Renderer output doesn't erase first pass evidence."""
    detector = ATSDetector(render=lambda url: "<html><body></body></html>")

    first = detector.detect_html(
        page("<div id='root'></div>", "<script src='/app.js'></script>"),
        "https://careers.example.com/jobs",
    )

    assert detector._maybe_render(first, first.final_url, first.input_url) is first


def test_unknown_vendor_names_the_platform_we_cannot_identify(detector):
    """Unknown vendor is reported as a registry discovery lead."""
    result = detector.detect_html(
        page(
            "<h1>Engineer</h1>",
            "<script src='https://cdn.notanats.example/app.js'></script>"
            "<link rel='stylesheet' href='https://cdn.notanats.example/x.css'>"
        ),
        "https://careers.example.com/jobs/1",
    )

    assert result.status == "unknown"
    assert result.unknown_vendor == "notanats.example"


def test_unknown_vendor_ignores_infrastructure_and_single_hits(detector):
    """Infrastructure and single-hit domains are ignored."""
    result = detector.detect_html(
        page(
            "<h1>Engineer</h1>",
            "<script src='https://www.googletagmanager.com/gtm.js'></script>"
            "<script src='https://cdn.jsdelivr.net/x.js'></script>"
            "<script src='https://consent.cookiebot.com/uc.js'></script>"
            "<link rel='stylesheet' href='https://seen.once.example/a.css'>"
        ),
        "https://careers.example.com/jobs/1",
    )

    assert result.unknown_vendor is None


def test_infra_denylist_respects_domain_boundaries():
    """Infrastructure denylist respects domain label boundaries."""
    assert _is_infra("x.com")
    assert _is_infra("www.x.com")
    assert _is_infra("www.googletagmanager.com")

    assert not _is_infra("phoenix.com")
    assert not _is_infra("careers.phoenix.com")
    assert not _is_infra("acme.avature.net")


def test_unknown_vendor_is_absent_when_detected(detector):
    """Unknown vendor is empty when ATS is detected."""
    result = detector.detect_html(
        page("<h1>CTO</h1>"),
        "https://jobs.lever.co/acme/8e3bd2ee-2fd7-4ac5-9662-04498ec711ec",
    )

    assert result.detected_ats == "lever"
    assert result.unknown_vendor is None


def test_partial_evidence_outranks_no_evidence(detector):
    """Partial evidence has higher confidence than no evidence."""
    nothing = detector.detect_html(page("<p>Hello.</p>"), "https://example.com/")

    something = detector.detect_html(
        page("", "<script src='https://boards.greenhouse.io/a.js'></script>"),
        "https://careers.example.com/x",
    )

    assert something.status == nothing.status == "unknown"
    assert something.confidence > nothing.confidence
