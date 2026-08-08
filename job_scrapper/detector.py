"""Detect which Applicant Tracking System serves a job page.

The detector asks "how many independent, vendor-specific fingerprints agree?"
rather than "how often does the vendor's name appear?". Three things follow
from that:

  * Signals are kept apart by *source* (hostname, script URL, DOM attribute,
    visible text, ...). A vendor domain in a `<script src>` is real evidence;
    the same word in a job description is nearly worthless.
  * Each fingerprint scores once, and each source is capped, so one fact
    cannot be counted from five angles to manufacture confidence.
  * A generic URL shape (`/job/x`, `/jobs/123`, `/apply`) only counts when the
    page is already on that vendor's hostname. Ungated, those shapes match a
    large share of ordinary career sites.

Fingerprints live in ATS_REGISTRY as data; detect() is a fixed engine over it.
Adding an ATS is a dict entry.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from enum import StrEnum, auto
from typing import Callable, Dict, List, Optional, Sequence, Set, Tuple
from urllib.parse import parse_qs, urljoin, urlparse

import requests
from bs4 import BeautifulSoup

RULESET_VERSION = "2026.08.1"

#: Maximum points one source may contribute to one ATS. Stops a single fact
#: (a vendor domain appearing in six places) from stacking.
SOURCE_CAP = 70

#: Maximum total points for one ATS, for the same reason across sources.
ATS_CAP = 100

#: Bound on DOM traversal for pathological documents.
MAX_ELEMENTS = 20_000

#: Bound on collected outbound links.
MAX_ANCHORS = 500

#: URLs inside inline script payloads.
_SCRIPT_URL_RE = re.compile(r"""https?://[^\s"'<>\\)]+""")

TIER_DEFINITIVE = 1
TIER_STRONG = 2
TIER_SUPPORTING = 3


# ======================================================================
# PAGE
# ======================================================================


@dataclass
class Page:
    """Everything the rules are allowed to look at, split by source."""

    input_url: str = ""
    final_url: str = ""
    redirect_chain: List[str] = field(default_factory=list)

    host: str = ""
    path: str = ""
    query: Dict[str, List[str]] = field(default_factory=dict)

    canonical_urls: List[str] = field(default_factory=list)
    script_srcs: List[str] = field(default_factory=list)
    iframe_srcs: List[str] = field(default_factory=list)
    link_hrefs: List[str] = field(default_factory=list)
    form_actions: List[str] = field(default_factory=list)
    anchor_urls: List[str] = field(default_factory=list)
    script_urls: List[str] = field(default_factory=list)

    data_attrs: Dict[str, Set[str]] = field(default_factory=dict)
    classes: Set[str] = field(default_factory=set)
    ids: Set[str] = field(default_factory=set)

    meta: Dict[str, str] = field(default_factory=dict)
    jsonld: List[object] = field(default_factory=list)
    headers: Dict[str, str] = field(default_factory=dict)

    script_text: str = ""
    text: str = ""
    html: str = ""

    is_job_page: bool = False

    def urls_for(self, source: str) -> List[str]:
        return getattr(self, URL_SOURCES[source][0], [])


#: source name -> (Page attribute, points, tier, min_hits) for the rules that
#: look for a vendor-owned domain in a URL. The ordering encodes the priority
#: every analysis converged on: where the browser ended up beats what the page
#: links to, which beats what it says.
#:
#: `min_hits` is the discriminator for the two weakest sources. Anything can
#: mention a vendor URL once; a career site whose postings *systematically*
#: point at one ATS is running that ATS. An aggregator linking to several
#: vendors trips the conflict check instead.
URL_SOURCES: Dict[str, Tuple[str, int, int, int]] = {
    "redirect": ("redirect_chain", 55, TIER_DEFINITIVE, 1),
    "canonical": ("canonical_urls", 50, TIER_DEFINITIVE, 1),
    "script_src": ("script_srcs", 45, TIER_STRONG, 1),
    "iframe_src": ("iframe_srcs", 45, TIER_STRONG, 1),
    "form_action": ("form_actions", 45, TIER_STRONG, 1),
    "link_href": ("link_hrefs", 40, TIER_STRONG, 1),
    "anchor": ("anchor_urls", 35, TIER_DEFINITIVE, 3),
    "script_url": ("script_urls", 35, TIER_DEFINITIVE, 3),
}


# ======================================================================
# RESULT
# ======================================================================


@dataclass
class Evidence:
    ats: ATSName
    points: int
    tier: int
    source: str
    signal_id: str
    reason: str
    matched: Optional[str] = None


@dataclass
class DetectionResult:
    input_url: str
    final_url: str
    detected_ats: Optional[ATSName]
    #: Ranking score in [0, 1]. NOT a calibrated probability -- it orders
    #: results by evidence quality, nothing more.
    confidence: float
    scores: Dict[ATSName, int]
    status: str = "unknown"          # detected | ambiguous | unknown
    evidence: List[Evidence] = field(default_factory=list)
    conflicts: List[ATSName] = field(default_factory=list)
    redirect_chain: List[str] = field(default_factory=list)
    source_scores: Dict[ATSName, Dict[str, int]] = field(default_factory=dict)
    needs_rendering: bool = False
    status_code: Optional[int] = None
    error: Optional[str] = None
    ruleset_version: str = RULESET_VERSION


# ======================================================================
# MATCHERS
# ======================================================================

#: A matcher receives the page and the ATS definition it belongs to, and
#: returns the matched value (for the evidence trail) or None.
Matcher = Callable[[Page, "ATS"], Optional[str]]


def _normalize_host(host: str) -> str:
    return (host or "").strip().lower().rstrip(".").split(":")[0]


def _host_hit(host: str, domains: Sequence[str]) -> Optional[str]:
    """Exact or subdomain match. Never a substring match.

    `notreallytaleo.net` must not match `taleo.net`.
    """
    host = _normalize_host(host)

    for domain in domains:
        if host == domain or host.endswith("." + domain):
            return host

    return None


def on_vendor_host(page: Page, ats: "ATS") -> bool:
    return bool(_host_hit(page.host, ats.hosts))


def host_is() -> Matcher:
    """The page is served from a vendor-owned hostname."""
    def test(page: Page, ats: "ATS") -> Optional[str]:
        return _host_hit(page.host, ats.hosts)

    return test


def url_host_in(source: str, min_hits: int = 1) -> Matcher:
    """A vendor-owned domain appears in one of the page's URL sources.

    This is what identifies custom career domains: the page is
    careers.example.com but its assets, canonical URL or apply links point at
    the ATS.
    """
    def test(page: Page, ats: "ATS") -> Optional[str]:
        domains = ats.hosts + ats.assets
        hits: Set[str] = set()

        for url in page.urls_for(source):
            host = _normalize_host(urlparse(url).hostname or "")

            # A URL on the page's own host tells us nothing the hostname rule
            # has not already scored. Counting it again would be the same
            # fact wearing a second source's hat.
            if host == page.host:
                continue

            if _host_hit(host, domains):
                # Distinct URLs, so one link repeated in a nav bar cannot
                # satisfy a multi-hit threshold on its own.
                hits.add(url)

                if len(hits) >= min_hits:
                    return sorted(hits)[0]

        return None

    return test


def path_re(pattern: str, gated: bool = True) -> Matcher:
    """Match the URL path.

    `gated` (the default) requires the page to be on a vendor hostname. Only
    pass gated=False for a path that no other platform plausibly serves --
    `/careersection/`, not `/jobs/`.
    """
    compiled = re.compile(pattern, re.I)

    def test(page: Page, ats: "ATS") -> Optional[str]:
        if gated and not on_vendor_host(page, ats):
            return None

        found = compiled.search(page.path)

        return found.group(0) if found else None

    return test


def qparam(name: str, value_pattern: Optional[str] = None,
           gated: bool = False) -> Matcher:
    """Match a query parameter, parsed rather than regexed out of the string."""
    compiled = re.compile(value_pattern, re.I) if value_pattern else None

    def test(page: Page, ats: "ATS") -> Optional[str]:
        if gated and not on_vendor_host(page, ats):
            return None

        for value in page.query.get(name, []):
            if compiled is None or compiled.fullmatch(value):
                return f"{name}={value}"

        return None

    return test


def in_text(source: str, needle: str, gated: bool = False) -> Matcher:
    """Substring match inside one named text source."""
    lowered = needle.lower()

    def test(page: Page, ats: "ATS") -> Optional[str]:
        if gated and not on_vendor_host(page, ats):
            return None

        haystack = getattr(page, source, "")

        if isinstance(haystack, str) and lowered in haystack.lower():
            return needle

        return None

    return test


def in_urls(source: str, needle: str) -> Matcher:
    """Substring match against one of the URL collections."""
    lowered = needle.lower()

    def test(page: Page, ats: "ATS") -> Optional[str]:
        for url in page.urls_for(source):
            if lowered in url.lower():
                return url

        return None

    return test


def has_class(name: str) -> Matcher:
    lowered = name.lower()

    def test(page: Page, ats: "ATS") -> Optional[str]:
        return lowered if lowered in page.classes else None

    return test


def has_id(name: str) -> Matcher:
    lowered = name.lower()

    def test(page: Page, ats: "ATS") -> Optional[str]:
        return lowered if lowered in page.ids else None

    return test


def has_data_attr(name: str,
                  value_pattern: Optional[str] = None) -> Matcher:
    """Match a data-* attribute, optionally constraining its value.

    Distinguishes data-automation-id="jobPosting" from the mere presence of
    the attribute name somewhere in the markup.
    """
    lowered = name.lower()
    compiled = re.compile(value_pattern, re.I) if value_pattern else None

    def test(page: Page, ats: "ATS") -> Optional[str]:
        values = page.data_attrs.get(lowered)

        if values is None:
            return None

        if compiled is None:
            return lowered

        for value in values:
            if compiled.search(value):
                return f"{lowered}={value}"

        return None

    return test


def in_meta(needle: str) -> Matcher:
    lowered = needle.lower()

    def test(page: Page, ats: "ATS") -> Optional[str]:
        for key, value in page.meta.items():
            if lowered in f"{key} {value}".lower():
                return f"{key}={value}"[:120]

        return None

    return test


def any_of(matchers) -> Matcher:
    """First matcher that hits wins. Keeps synonyms one signal, not many."""
    options = tuple(matchers)

    def test(page: Page, ats: "ATS") -> Optional[str]:
        for option in options:
            found = option(page, ats)

            if found:
                return found

        return None

    return test


def jsonld_url_host() -> Matcher:
    """A vendor domain appears in a URL-valued JSON-LD field.

    Structured data is often written by the employer's SEO tooling, so the
    vendor *name* in JSON-LD proves nothing -- but a posting URL on the
    vendor's domain does.
    """
    def test(page: Page, ats: "ATS") -> Optional[str]:
        domains = ats.hosts + ats.assets

        for value in _walk_strings(page.jsonld):
            if "//" not in value:
                continue

            if _host_hit(urlparse(value).hostname or "", domains):
                return value[:160]

        return None

    return test


def _walk_strings(node: object, depth: int = 0):
    """Yield every string in a nested JSON structure."""
    if depth > 12:
        return

    if isinstance(node, str):
        yield node
    elif isinstance(node, dict):
        for value in node.values():
            yield from _walk_strings(value, depth + 1)
    elif isinstance(node, list):
        for value in node:
            yield from _walk_strings(value, depth + 1)


# ======================================================================
# RULES
# ======================================================================


@dataclass(frozen=True)
class Rule:
    signal_id: str          # dedup key: scored at most once
    source: str             # cap bucket
    points: int
    tier: int
    reason: str
    test: Matcher


class ATSName(StrEnum):
    """The platforms this detector knows about.

    StrEnum, so members compare and serialize as their lowercase name and
    every existing string consumer keeps working unchanged.
    """

    WORKDAY = auto()
    SMARTRECRUITERS = auto()
    GREENHOUSE = auto()
    ICIMS = auto()
    SUCCESSFACTORS = auto()
    TALEO = auto()
    LEVER = auto()
    TEAMTAILOR = auto()
    RECRUITEE = auto()
    ASHBY = auto()
    WORKABLE = auto()
    BAMBOOHR = auto()
    TALENTSOFT = auto()
    COMEET = auto()
    ONLYFY = auto()
    BREEZY = auto()
    TALENTLYFT = auto()
    PERSONIO = auto()
    PINPOINT = auto()
    JOBVITE = auto()
    JAZZHR = auto()


@dataclass(frozen=True)
class ATS:
    name: ATSName
    #: Hostnames the vendor itself serves. A page on one of these is that ATS.
    hosts: Tuple[str, ...] = ()
    #: Vendor infrastructure (CDNs, APIs, apply domains) that custom career
    #: domains reference. Not page hosts, but vendor-owned.
    assets: Tuple[str, ...] = ()
    #: Fingerprints specific to this platform.
    rules: Tuple[Rule, ...] = ()
    #: Words identifying the vendor in prose. Deliberately near-worthless.
    terms: Tuple[str, ...] = ()


def rule(signal_id: str, source: str, points: int, tier: int,
         reason: str, test: Matcher) -> Rule:
    return Rule(signal_id, source, points, tier, reason, test)


def _expand(ats: ATS) -> List[Rule]:
    """Build the full rule list for one ATS: generic + bespoke."""
    rules: List[Rule] = []

    if ats.hosts:
        rules.append(rule(
            f"{ats.name}.hostname",
            "hostname",
            60,
            TIER_DEFINITIVE,
            f"Served from a {ats.name} hostname",
            host_is(),
        ))

    if ats.hosts or ats.assets:
        for source, (_, points, tier, min_hits) in URL_SOURCES.items():
            rules.append(rule(
                f"{ats.name}.{source}",
                source,
                points,
                tier,
                f"{ats.name} domain in {source}",
                url_host_in(source, min_hits=min_hits),
            ))

        rules.append(rule(
            f"{ats.name}.jsonld",
            "jsonld",
            25,
            TIER_STRONG,
            f"{ats.name} URL in JSON-LD",
            jsonld_url_host(),
        ))

    rules.extend(ats.rules)

    if ats.terms:
        # One rule for all synonyms: "the vendor is named somewhere" is a
        # single fact however many spellings it has. Tier 3 and 2 points --
        # enough to break a tie, never enough to decide.
        rules.append(rule(
            f"{ats.name}.text",
            "text",
            2,
            TIER_SUPPORTING,
            f"{ats.name} mentioned in page text",
            any_of(in_text("text", term) for term in ats.terms),
        ))

        rules.append(rule(
            f"{ats.name}.meta",
            "meta",
            2,
            TIER_SUPPORTING,
            f"{ats.name} mentioned in metadata",
            any_of(in_meta(term) for term in ats.terms),
        ))

    return rules


# ======================================================================
# FINGERPRINT REGISTRY
# ======================================================================

ATS_REGISTRY: Tuple[ATS, ...] = (
    ATS(
        name=ATSName.WORKDAY,
        hosts=("myworkdayjobs.com",),
        assets=("myworkdayjobs.com", "workday.com"),
        terms=("workday",),
        rules=(
            rule(
                "workday.automation_id", "data_attr", 30, TIER_STRONG,
                "Workday data-automation-id on a job element",
                has_data_attr(
                    "data-automation-id",
                    r"job(?:Posting|Title|PostingHeader)|applyButton",
                ),
            ),
            rule(
                "workday.job_path", "path", 10, TIER_SUPPORTING,
                "Workday-style job path",
                path_re(r"/job/[^/?#]+/"),
            ),
        ),
    ),
    ATS(
        name=ATSName.SMARTRECRUITERS,
        hosts=("smartrecruiters.com",),
        assets=("smartrecruiters.com",),
        terms=("smartrecruiters",),
        rules=(
            rule(
                "smartrecruiters.api", "script_text", 20, TIER_SUPPORTING,
                "SmartRecruiters API referenced in script",
                in_text("script_text", "api.smartrecruiters.com"),
            ),
        ),
    ),
    ATS(
        name=ATSName.GREENHOUSE,
        hosts=("greenhouse.io",),
        assets=("greenhouse.io",),
        terms=("greenhouse",),
        rules=(
            rule(
                "greenhouse.gh_jid", "query", 60, TIER_DEFINITIVE,
                "Greenhouse gh_jid job identifier",
                qparam("gh_jid", r"\d+"),
            ),
            rule(
                "greenhouse.gh_jid_embed", "dom", 25, TIER_STRONG,
                "Greenhouse gh_jid identifier in markup",
                in_text("html", "gh_jid="),
            ),
            rule(
                "greenhouse.board_path", "path", 30, TIER_STRONG,
                "Greenhouse job board path",
                path_re(r"^/[^/?#]+/jobs/\d+"),
            ),
        ),
    ),
    ATS(
        name=ATSName.ICIMS,
        hosts=("icims.com",),
        assets=("icims.com",),
        terms=("icims",),
        rules=(
            rule(
                "icims.job_path", "path", 45, TIER_DEFINITIVE,
                "iCIMS job URL structure",
                path_re(r"/jobs/\d+/[^/?#]+/job"),
            ),
            rule(
                "icims.footer", "text", 35, TIER_STRONG,
                "'Software Powered by iCIMS' footer",
                in_text("text", "powered by icims"),
            ),
        ),
    ),
    ATS(
        name=ATSName.SUCCESSFACTORS,
        hosts=("jobs.hr.cloud.sap", "jobs.hr.sapcloud.cn"),
        assets=("jobs.hr.cloud.sap", "jobs.hr.sapcloud.cn",
                "successfactors.com", "successfactors.eu"),
        terms=("successfactors",),
        rules=(
            # SuccessFactors supports custom domains, so this path carries
            # the detection on employer-branded career sites.
            rule(
                "successfactors.recruiting_path", "path", 45, TIER_DEFINITIVE,
                "SuccessFactors /sf/recruiting/ endpoint",
                path_re(r"/sf/recruiting/", gated=False),
            ),
            rule(
                "successfactors.job_req", "query", 30, TIER_STRONG,
                "SuccessFactors jobReqId parameter",
                qparam("jobReqId"),
            ),
            rule(
                "successfactors.career_site", "script_text", 20,
                TIER_SUPPORTING,
                "SuccessFactors career site builder reference",
                in_text("script_text", "careersitebuilder"),
            ),
        ),
    ),
    ATS(
        name=ATSName.TALEO,
        hosts=("taleo.net",),
        assets=("taleo.net",),
        terms=("taleo",),
        rules=(
            # Taleo's .ftl endpoints and /careersection/ are unique enough to
            # identify the platform on an employer's own domain.
            rule(
                "taleo.careersection", "path", 55, TIER_DEFINITIVE,
                "Taleo /careersection/ path",
                path_re(r"/careersection/", gated=False),
            ),
            rule(
                "taleo.jobdetail", "path", 55, TIER_DEFINITIVE,
                "Taleo jobdetail.ftl endpoint",
                path_re(r"jobdetail\.ftl", gated=False),
            ),
            rule(
                "taleo.other_ftl", "path", 25, TIER_STRONG,
                "Taleo .ftl career endpoint",
                path_re(r"(?:jobsearch|jobrefer|careersection)\.ftl",
                        gated=False),
            ),
            rule(
                "taleo.job_param", "query", 20, TIER_STRONG,
                "Taleo job query parameter",
                qparam("job", gated=True),
            ),
        ),
    ),
    ATS(
        name=ATSName.LEVER,
        hosts=("jobs.lever.co",),
        assets=("lever.co",),
        terms=("lever",),
        rules=(
            rule(
                "lever.posting_uuid", "path", 40, TIER_STRONG,
                "Lever posting UUID in job path",
                path_re(
                    r"^/[^/?#]+/[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}"
                    r"-[0-9a-f]{4}-[0-9a-f]{12}",
                ),
            ),
        ),
    ),
    ATS(
        name=ATSName.TEAMTAILOR,
        hosts=("teamtailor.com",),
        assets=("teamtailor-cdn.com", "teamtailor.com"),
        terms=("teamtailor",),
        rules=(
            rule(
                "teamtailor.application", "path", 50, TIER_DEFINITIVE,
                "Teamtailor application route",
                path_re(r"/jobs/\d+-[^/?#]+/applications/new"),
            ),
            rule(
                "teamtailor.job_path", "path", 45, TIER_STRONG,
                "Teamtailor job URL structure",
                path_re(r"/(?:careers/)?jobs/\d+-[^/?#]+"),
            ),
            # Ungated the same shape is only a hint: plenty of platforms use
            # /jobs/<id>-<slug>. It supports, it never decides.
            rule(
                "teamtailor.job_path_generic", "path", 8, TIER_SUPPORTING,
                "Teamtailor-style job path on a non-Teamtailor host",
                path_re(r"/(?:careers/)?jobs/\d+-[^/?#]+", gated=False),
            ),
        ),
    ),
    ATS(
        name=ATSName.RECRUITEE,
        hosts=("recruitee.com",),
        assets=("recruitee.com",),
        terms=("recruitee",),
        rules=(
            rule(
                "recruitee.js_global", "script_text", 40, TIER_STRONG,
                "Recruitee JavaScript global",
                in_text("script_text", "window.recruitee"),
            ),
            rule(
                "recruitee.offers_api", "path", 30, TIER_STRONG,
                "Recruitee offers API/feed",
                path_re(r"/api/(?:feeds/)?offers"),
            ),
        ),
    ),
    ATS(
        name=ATSName.ASHBY,
        hosts=("jobs.ashbyhq.com", "ashbyhq.com"),
        assets=("ashbyhq.com",),
        terms=("ashbyhq",),
        rules=(
            # The old broad `/<a>/<b>` rule matched any two-segment path on
            # any site and was this detector's largest false-positive source.
            rule(
                "ashby.posting_uuid", "path", 30, TIER_STRONG,
                "Ashby posting UUID in job path",
                path_re(
                    r"^/[^/?#]+/[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}"
                    r"-[0-9a-f]{4}-[0-9a-f]{12}",
                ),
            ),
            rule(
                "ashby.embed", "script_text", 30, TIER_STRONG,
                "Ashby embed script",
                in_text("script_text", "ashby_embed"),
            ),
        ),
    ),
    ATS(
        name=ATSName.WORKABLE,
        hosts=("apply.workable.com", "workable.com"),
        assets=("workable.com",),
        terms=("workable",),
        rules=(
            rule(
                "workable.job_path", "path", 40, TIER_STRONG,
                "Workable job URL structure",
                path_re(r"^/[^/?#]+/j/[^/?#]+"),
            ),
        ),
    ),
    ATS(
        name=ATSName.BAMBOOHR,
        hosts=("bamboohr.com",),
        assets=("bamboohr.com", "bamboohr.co.uk"),
        terms=("bamboohr",),
        rules=(
            rule(
                "bamboohr.job_path", "path", 30, TIER_STRONG,
                "BambooHR careers job path",
                path_re(r"/careers/\d+"),
            ),
        ),
    ),
    ATS(
        name=ATSName.TALENTSOFT,
        hosts=("talentsoft.com", "cegid-hr.com"),
        assets=("talentsoft.com", "cegid.com", "cegid-hr.com"),
        terms=("talentsoft", "cegid talent"),
        rules=(
            # The named endpoints are Talentsoft's Front Office API. The old
            # `/api/token` rule was dropped: that path is everywhere.
            rule(
                "talentsoft.api", "path", 50, TIER_DEFINITIVE,
                "Talentsoft Front Office API endpoint",
                path_re(
                    r"/api/v\d+/(?:offersummaries|offers/getoffer|applicants)",
                    gated=False,
                ),
            ),
        ),
    ),
    ATS(
        name=ATSName.COMEET,
        hosts=("comeet.co",),
        assets=("comeet.co",),
        terms=("comeet",),
        rules=(
            rule(
                "comeet.careers_api", "script_text", 40, TIER_STRONG,
                "Comeet Careers API reference",
                in_text("script_text", "careers-api/2.0"),
            ),
            rule(
                "comeet.identifiers", "script_text", 35, TIER_STRONG,
                "Comeet position/company identifiers",
                in_text("script_text", "url_comeet_hosted_page"),
            ),
            rule(
                "comeet.job_path", "path", 40, TIER_STRONG,
                "Comeet hosted job URL structure",
                path_re(r"^/jobs/[^/?#]+/[^/?#]+/[^/?#]+/[^/?#]+"),
            ),
        ),
    ),
    ATS(
        name=ATSName.ONLYFY,
        hosts=("onlyfy.jobs",),
        assets=("onlyfy.jobs", "jobbase.io"),
        terms=("onlyfy", "prescreen"),
        rules=(),
    ),
    ATS(
        name=ATSName.BREEZY,
        hosts=("breezy.hr",),
        assets=("breezy.hr",),
        terms=("breezy hr",),
        rules=(
            rule(
                "breezy.position_path", "path", 45, TIER_STRONG,
                "Breezy position URL structure",
                path_re(r"/p/[0-9a-f]+"),
            ),
        ),
    ),
    ATS(
        name=ATSName.TALENTLYFT,
        hosts=("talentlyft.com",),
        assets=("talentlyft.com",),
        terms=("talentlyft",),
        rules=(
            rule(
                "talentlyft.api_fields", "script_text", 25, TIER_STRONG,
                "TalentLyft job API fields",
                in_text("script_text", "shortlinkurl"),
            ),
        ),
    ),
    ATS(
        name=ATSName.PERSONIO,
        hosts=("jobs.personio.de", "jobs.personio.com"),
        assets=("personio.de", "personio.com"),
        terms=("personio",),
        rules=(
            rule(
                "personio.iframe", "dom", 40, TIER_STRONG,
                "Personio iframe integration element",
                has_id("personio-iframe"),
            ),
            rule(
                "personio.job_path", "path", 45, TIER_STRONG,
                "Personio job URL structure",
                path_re(r"^/job/\d+"),
            ),
            rule(
                "personio.xml_feed", "path", 40, TIER_STRONG,
                "Personio XML career feed",
                path_re(r"^/xml(?:/|$)"),
            ),
        ),
    ),
    ATS(
        name=ATSName.PINPOINT,
        hosts=("pinpointhq.com",),
        assets=("pinpointhq.com",),
        terms=("pinpointhq",),
        rules=(
            rule(
                "pinpoint.job_path", "path", 45, TIER_STRONG,
                "Pinpoint job URL structure",
                path_re(r"^/(?:[a-z]{2}(?:-[a-z]{2})?/)?jobs/\d+/?$"),
            ),
            rule(
                "pinpoint.postings_json", "path", 45, TIER_STRONG,
                "Pinpoint postings.json endpoint",
                path_re(r"/postings\.json"),
            ),
            rule(
                "pinpoint.api", "path", 45, TIER_STRONG,
                "Pinpoint jobs API endpoint",
                path_re(r"^/api/v1/jobs(?:/|$)"),
            ),
            rule(
                "pinpoint.jobs_block", "dom", 35, TIER_STRONG,
                "Pinpoint careers jobs-block element",
                has_class("js-careers-jobs-block"),
            ),
        ),
    ),
    ATS(
        name=ATSName.JOBVITE,
        hosts=("jobs.jobvite.com", "jobvite.com"),
        assets=("jobvite.com",),
        terms=("jobvite",),
        rules=(
            rule(
                "jobvite.iframe_js", "script_src", 45, TIER_STRONG,
                "Jobvite career-site iframe script",
                in_urls("script_src", "careersite/public/iframe.js"),
            ),
            rule(
                "jobvite.careersite_class", "dom", 40, TIER_STRONG,
                "Jobvite jv-careersite element",
                has_class("jv-careersite"),
            ),
            rule(
                "jobvite.careersite_attr", "data_attr", 40, TIER_STRONG,
                "Jobvite data-careersite attribute",
                has_data_attr("data-careersite"),
            ),
            rule(
                "jobvite.job_path", "path", 40, TIER_STRONG,
                "Jobvite job URL structure",
                path_re(r"^/[^/?#]+/job/[a-z0-9_-]+"),
            ),
        ),
    ),
    ATS(
        name=ATSName.JAZZHR,
        hosts=("applytojob.com", "app.jazz.co"),
        assets=("applytojob.com", "jazz.co"),
        terms=("jazzhr",),
        rules=(
            rule(
                "jazzhr.widget", "script_src", 40, TIER_STRONG,
                "JazzHR widget script",
                in_urls("script_src", "jazz.co/widgets/buttons/create/"),
            ),
            rule(
                "jazzhr.apply_path", "path", 40, TIER_STRONG,
                "JazzHR hosted apply URL",
                path_re(r"^/apply/[a-z0-9]+"),
            ),
            rule(
                "jazzhr.feed", "path", 40, TIER_STRONG,
                "JazzHR job feed endpoint",
                path_re(r"/feeds/export/jobs/"),
            ),
        ),
    ),
)

ATS_NAMES: Tuple[ATSName, ...] = tuple(ats.name for ats in ATS_REGISTRY)

#: ATS name -> (definition, expanded rules). Built once at import.
COMPILED: Dict[ATSName, Tuple[ATS, List[Rule]]] = {
    ats.name: (ats, _expand(ats))
    for ats in ATS_REGISTRY
}


# ======================================================================
# DETECTOR
# ======================================================================


class ATSDetector:
    """Fetch a job page and identify the ATS behind it."""

    def __init__(self, timeout: int = 15, max_bytes: int = 5_000_000,
                 max_redirects: int = 5):
        self.timeout = timeout
        self.max_bytes = max_bytes

        self.session = requests.Session()
        self.session.max_redirects = max_redirects
        self.session.headers.update({
            "User-Agent": (
                "Mozilla/5.0 (compatible; ATSDetector/2.0; "
                "+https://example.com/ats-detector)"
            ),
            "Accept": (
                "text/html,application/xhtml+xml,"
                "application/xml;q=0.9,*/*;q=0.8"
            ),
        })

    # ------------------------------------------------------------------
    # Fetch
    # ------------------------------------------------------------------

    def detect(self, url: str) -> DetectionResult:
        try:
            response = self.session.get(
                url,
                timeout=self.timeout,
                allow_redirects=True,
                stream=True,
            )

            response.raise_for_status()

            content_type = response.headers.get("Content-Type", "").lower()

            if content_type and not any(
                kind in content_type
                for kind in ("text/html", "application/xhtml")
            ):
                return self._failed(
                    url, response.url,
                    f"Non-HTML content type: {content_type}",
                    response.status_code,
                )

            declared = response.headers.get("Content-Length")

            if declared and declared.isdigit() and \
                    int(declared) > self.max_bytes:
                return self._failed(
                    url, response.url,
                    "Response exceeds maximum allowed size",
                    response.status_code,
                )

            # requests hands urllib3 decode_content=False, so reading `raw`
            # directly yields gzip/deflate framing unless we ask for the
            # decompressed body.
            raw = response.raw.read(
                self.max_bytes + 1,
                decode_content=True,
            )

            if len(raw) > self.max_bytes:
                return self._failed(
                    url, response.url,
                    "Response exceeds maximum allowed size",
                    response.status_code,
                )

            html = raw.decode(
                response.encoding or response.apparent_encoding or "utf-8",
                errors="replace",
            )

            redirects = [item.url for item in response.history]

            return self.detect_html(
                html,
                response.url,
                headers=dict(response.headers),
                input_url=url,
                redirect_chain=redirects + [response.url],
                status_code=response.status_code,
            )

        except requests.RequestException as exc:
            return self._failed(url, url, str(exc), None)

    # ------------------------------------------------------------------
    # Offline entry point
    # ------------------------------------------------------------------

    def detect_html(self, html: str, url: str,
                    headers: Optional[Dict[str, str]] = None,
                    input_url: Optional[str] = None,
                    redirect_chain: Optional[List[str]] = None,
                    status_code: Optional[int] = None) -> DetectionResult:
        """Score already-fetched HTML. Used by detect() and by the tests."""
        page = extract(
            html,
            url,
            headers=headers,
            input_url=input_url or url,
            redirect_chain=redirect_chain or [url],
        )

        return decide(page, status_code=status_code)

    @staticmethod
    def _failed(input_url: str, final_url: str, error: str,
                status_code: Optional[int]) -> DetectionResult:
        return DetectionResult(
            input_url=input_url,
            final_url=final_url,
            detected_ats=None,
            confidence=0.0,
            scores={name: 0 for name in ATS_NAMES},
            status="unknown",
            status_code=status_code,
            error=error,
        )


# ======================================================================
# EXTRACTION
# ======================================================================


def extract(html: str, url: str,
            headers: Optional[Dict[str, str]] = None,
            input_url: Optional[str] = None,
            redirect_chain: Optional[List[str]] = None) -> Page:
    """Pull every signal source out of the document, keeping them apart."""
    soup = BeautifulSoup(html, "html.parser")
    parsed = urlparse(url)

    page = Page(
        input_url=input_url or url,
        final_url=url,
        redirect_chain=list(redirect_chain or [url]),
        host=_normalize_host(parsed.hostname or ""),
        path=parsed.path.lower(),
        query={
            key.lower(): values
            for key, values in parse_qs(
                parsed.query, keep_blank_values=True
            ).items()
        },
        html=html,
        headers={
            key.lower(): value
            for key, value in (headers or {}).items()
        },
    )

    def absolute(value: object) -> Optional[str]:
        text = str(value or "").strip()

        return urljoin(url, text) if text else None

    for tag in soup.find_all("script", src=True):
        found = absolute(tag.get("src"))

        if found:
            page.script_srcs.append(found)

    for tag in soup.find_all("iframe", src=True):
        found = absolute(tag.get("src"))

        if found:
            page.iframe_srcs.append(found)

    for tag in soup.find_all("form", action=True):
        found = absolute(tag.get("action"))

        if found:
            page.form_actions.append(found)

    for tag in soup.find_all("link", href=True):
        found = absolute(tag.get("href"))

        if not found:
            continue

        rel = " ".join(tag.get("rel") or []).lower()

        # canonical/alternate name the page's real home; other <link>s are
        # stylesheets and icons, which still carry vendor CDNs.
        if "canonical" in rel or "alternate" in rel:
            page.canonical_urls.append(found)
        else:
            page.link_hrefs.append(found)

    for tag in soup.find_all("a", href=True):
        if len(page.anchor_urls) >= MAX_ANCHORS:
            break

        found = absolute(tag.get("href"))

        if found:
            page.anchor_urls.append(found)

    for index, element in enumerate(soup.find_all(True)):
        if index >= MAX_ELEMENTS:
            break

        element_id = element.get("id")

        if element_id:
            page.ids.add(str(element_id).lower())

        for value in element.get("class", []) or []:
            page.classes.add(str(value).lower())

        for key, value in element.attrs.items():
            key = str(key).lower()

            if not key.startswith("data-"):
                continue

            if isinstance(value, list):
                value = " ".join(str(item) for item in value)

            page.data_attrs.setdefault(key, set()).add(str(value).lower())

    if soup.title:
        page.meta["title"] = soup.title.get_text(" ", strip=True)

    for tag in soup.find_all("meta"):
        key = tag.get("name") or tag.get("property") or tag.get("itemprop")
        value = tag.get("content")

        if key and value:
            page.meta[str(key).lower()] = str(value)

    og_url = page.meta.get("og:url")

    if og_url:
        page.canonical_urls.append(urljoin(url, og_url))

    for tag in soup.find_all(
        "script", attrs={"type": "application/ld+json"}
    ):
        try:
            page.jsonld.append(json.loads(tag.get_text(strip=True)))
        except (ValueError, TypeError):
            continue

    page.script_text = "\n".join(
        tag.get_text(" ", strip=False)
        for tag in soup.find_all("script")
        if not tag.get("src")
    )

    # URLs embedded in inline JSON/JS payloads -- an "applyUrl" pointing at
    # the vendor is how an employer-branded career page gives itself away.
    page.script_urls = list(dict.fromkeys(
        # JSON escapes slashes, so https:\/\/host has to be unescaped first.
        _SCRIPT_URL_RE.findall(page.script_text.replace("\\/", "/"))
    ))[:MAX_ANCHORS]

    page.text = soup.get_text(" ", strip=True)

    # A JobPosting confirms this is a job page. It says nothing about which
    # ATS built it -- every platform emits the same schema.
    page.is_job_page = any(
        "jobposting" in value.lower()
        for value in _walk_strings(page.jsonld)
    ) or bool(
        soup.find(attrs={"itemtype": re.compile("JobPosting", re.I)})
    )

    return page


# ======================================================================
# SCORING
# ======================================================================


def score(page: Page) -> Tuple[
    Dict[ATSName, int], List[Evidence], Dict[ATSName, Dict[str, int]]
]:
    """Run every rule, deduplicating signals and capping each source."""
    scores = {name: 0 for name in ATS_NAMES}
    source_scores: Dict[ATSName, Dict[str, int]] = {
        name: {} for name in ATS_NAMES
    }
    evidence: List[Evidence] = []
    fired: Set[str] = set()

    for name, (ats, rules) in COMPILED.items():
        for item in rules:
            # One fingerprint, one score -- however many ways it matches.
            if item.signal_id in fired:
                continue

            matched = item.test(page, ats)

            if not matched:
                continue

            fired.add(item.signal_id)

            bucket = source_scores[name]
            used = bucket.get(item.source, 0)
            allowed = max(0, min(item.points, SOURCE_CAP - used))

            if allowed <= 0:
                continue

            bucket[item.source] = used + allowed
            scores[name] += allowed

            evidence.append(Evidence(
                ats=name,
                points=allowed,
                tier=item.tier,
                source=item.source,
                signal_id=item.signal_id,
                reason=item.reason,
                matched=str(matched)[:200],
            ))

        scores[name] = min(scores[name], ATS_CAP)

    return scores, evidence, source_scores


def decide(page: Page, status_code: Optional[int] = None) -> DetectionResult:
    """Turn scores into a verdict: detected, ambiguous or unknown."""
    scores, evidence, source_scores = score(page)

    ranked = sorted(scores.items(), key=lambda item: item[1], reverse=True)

    # Being served *from* a vendor's hostname settles it. Anything the page
    # embeds or links to is a weaker claim than where the page itself lives,
    # so a Greenhouse-hosted board that embeds a Lever widget is Greenhouse.
    # At most one ATS can own the hostname -- the domain lists are disjoint.
    host_owner = next(
        (item.ats for item in evidence if item.source == "hostname"),
        None,
    )

    if host_owner:
        ranked.sort(key=lambda item: (item[0] != host_owner, -item[1]))

    winner, winner_score = ranked[0]
    runner_up, second_score = ranked[1] if len(ranked) > 1 else (None, 0)

    def strong_sources(name: Optional[ATSName], max_tier: int) -> Set[str]:
        return {
            item.source
            for item in evidence
            if item.ats == name and item.tier <= max_tier
        }

    tier1 = strong_sources(winner, TIER_DEFINITIVE)
    tier2 = strong_sources(winner, TIER_STRONG)

    # Tier-3 evidence can never decide, however much of it there is: three
    # mentions of a vendor's name are not one vendor-owned hostname.
    qualified = bool(tier1) or len(tier2) >= 2

    # A competitor only creates doubt if it also holds structural evidence
    # and is close behind -- and never against the host owner.
    rival_qualified = (
        host_owner is None
        and bool(strong_sources(runner_up, TIER_STRONG))
        and second_score >= 0.6 * winner_score
    )

    conflicts: List[ATSName] = []

    if winner_score <= 0 or not qualified:
        status = "unknown"
    elif rival_qualified:
        status = "ambiguous"
        conflicts = [winner, runner_up]
    else:
        status = "detected"

    tier_quality = 1.0 if tier1 else (0.6 if tier2 else 0.2)
    margin = (
        (winner_score - second_score) / winner_score
        if winner_score > 0 else 0.0
    )
    diversity = min(len(source_scores.get(winner, {})) / 3.0, 1.0)

    confidence = (
        0.40 * tier_quality
        + 0.30 * margin
        + 0.30 * diversity
    ) if status == "detected" else (
        0.20 * tier_quality + 0.20 * diversity
    )

    return DetectionResult(
        input_url=page.input_url,
        final_url=page.final_url,
        detected_ats=winner if status == "detected" else None,
        confidence=round(max(0.0, min(1.0, confidence)), 3),
        scores=scores,
        status=status,
        evidence=sorted(
            evidence,
            key=lambda item: (item.tier, -item.points),
        ),
        conflicts=conflicts,
        redirect_chain=page.redirect_chain,
        source_scores={
            name: buckets
            for name, buckets in source_scores.items()
            if buckets
        },
        needs_rendering=(
            status == "unknown" and _looks_unrendered(page)
        ),
        status_code=status_code,
    )


def _looks_unrendered(page: Page) -> bool:
    """A JS shell: little text, external scripts, an empty app container."""
    if len(page.text) > 2000:
        return False

    return bool(page.script_srcs) and (
        len(page.text) < 500
        or bool({"root", "app", "__next"} & page.ids)
    )


# ======================================================================
# CLI
# ======================================================================


def print_result(result: DetectionResult) -> None:
    print(f"\nInput URL:    {result.input_url}")
    print(f"Final URL:    {result.final_url}")
    print(f"HTTP status:  {result.status_code}")

    if len(result.redirect_chain) > 1:
        print(f"Redirects:    {len(result.redirect_chain) - 1}")

        for step in result.redirect_chain:
            print(f"  -> {step}")

    if result.error:
        print(f"Error:        {result.error}")
        return

    print(f"Status:       {result.status}")
    print(f"Detected ATS: {result.detected_ats or 'unknown'}")
    print(f"Confidence:   {result.confidence:.1%}  (ranking score)")

    if result.conflicts:
        print(f"Conflicts:    {', '.join(result.conflicts)}")

    if result.needs_rendering:
        print("Note:         page looks JavaScript-rendered")

    scored = [
        (name, value)
        for name, value in result.scores.items()
        if value
    ]

    if not scored:
        print("\nNo ATS signals found.")
        return

    print("\nScores:")

    for name, value in sorted(
        scored, key=lambda item: item[1], reverse=True
    ):
        buckets = result.source_scores.get(name, {})

        breakdown = "  ".join(
            f"{source}={points}"
            for source, points in sorted(buckets.items())
        )

        print(f"  {name:16} {value:4}   {breakdown}")

    print("\nEvidence:")

    for item in result.evidence:
        print(
            f"  [{item.ats:16}] +{item.points:3} "
            f"T{item.tier} {item.source:12} {item.reason}"
        )

        if item.matched:
            print(f"{'':24}matched: {item.matched[:100]}")


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(
        description="Detect the ATS behind a job posting URL"
    )
    parser.add_argument("url")

    args = parser.parse_args()

    print_result(ATSDetector().detect(args.url))
