"""Detect which Applicant Tracking System serves a job page.

This module scores vendor-specific fingerprints to identify ATSs. It asks
"how many independent signals agree?" rather than "how often does the vendor's
name appear?". Fingerprints live in ATS_REGISTRY as data; detect() is a fixed
engine over it. Adding an ATS is a dict entry.
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

from job_scraper.fetching import decode_response, new_session, walk_strings

RULESET_VERSION = "2026.08.1"

# Maximum points one source may contribute to one ATS. Stops a single fact
# (a vendor domain in six places) from stacking.
SOURCE_CAP = 70

# Maximum total points for one ATS, for the same reason across sources.
ATS_CAP = 100

MAX_ELEMENTS = 20_000
MAX_ANCHORS = 500

_SCRIPT_URL_RE = re.compile(r"""https?://[^\s"'<>\\)]+""")

# Generic web infrastructure: CDNs, analytics, consent, social. Used only to
# keep the unknown-vendor report readable; never affects scoring.
INFRA_DENYLIST: Tuple[str, ...] = (
    "google", "gstatic", "googleapis", "doubleclick", "gtm", "bing",
    "facebook", "linkedin", "twitter", "x.com", "bsky.app", "instagram",
    "tiktok", "youtube", "vimeo", "apple.com", "amazon.com", "aws.amazon",
    "cloudflare", "cloudfront", "akamai", "fastly", "azurefd", "b-cdn.net",
    "jsdelivr", "unpkg", "bootstrapcdn", "jquery", "icomoon", "typography",
    "typekit", "fontawesome", "weglot",
    "hubspot", "hs-scripts", "hs-analytics", "hsforms", "hubapi",
    "segment", "sentry", "newrelic", "hotjar", "matomo", "piwik", "posthog",
    "clarity.ms", "atinternet", "aticdn", "elfsight", "tidio", "wp.com",
    "usercentrics", "trustarc", "cookieyes", "onetrust", "tarteaucitron",
    "axeptio", "cookiebot", "hcaptcha", "recaptcha", "cookielaw", "iubenda",
    # Site builders and generic object storage: never an ATS.
    "website-editor.net", "website-files.com", "windows.net", "bugherd",
    "cr-relay.com", "s81c.com",
    "cloudinary", "adobeaemcloud", "brightcove", "mux.com",
    "indeed.com", "glassdoor", "welcometothejungle", "jobijoba",
    "hellowork",
)

TIER_DEFINITIVE = 1
TIER_STRONG = 2
TIER_SUPPORTING = 3

# Preview/truncation lengths for evidence display
EVIDENCE_PREVIEW_LEN = 200
QUERY_PARAM_PREVIEW_LEN = 120
URL_PREVIEW_LEN = 160
MATCH_DISPLAY_LEN = 100


@dataclass
class Page:
    """Page structure split by evidence source."""

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


# source -> (Page attribute, points, tier, min_hits). Ordered by priority:
# hostname beats links beats text. min_hits discriminates weaker sources.
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


@dataclass
class Evidence:
    """One fired rule and its score."""

    ats: ATSName
    points: int
    tier: int
    source: str
    signal_id: str
    reason: str
    matched: Optional[str] = None


@dataclass
class DetectionResult:
    """Detection verdict and supporting evidence."""

    input_url: str
    final_url: str
    detected_ats: Optional[ATSName]
    confidence: float
    scores: Dict[ATSName, int]
    status: str = "unknown"
    evidence: List[Evidence] = field(default_factory=list)
    conflicts: List[ATSName] = field(default_factory=list)
    redirect_chain: List[str] = field(default_factory=list)
    source_scores: Dict[ATSName, Dict[str, int]] = field(default_factory=dict)
    needs_rendering: bool = False
    unknown_vendor: Optional[str] = None
    status_code: Optional[int] = None
    error: Optional[str] = None
    ruleset_version: str = RULESET_VERSION
    #: The page detection actually read. Handed back so a caller that needs
    #: the same document -- Board does, for four of its strategies -- can use
    #: this one instead of fetching it a second time.
    html: Optional[str] = None


# Matcher: (Page, ATS) -> Optional[str]. Returns matched value or None.
Matcher = Callable[[Page, "ATS"], Optional[str]]


def _normalize_host(host: str) -> str:
    return (host or "").strip().lower().rstrip(".").split(":")[0]


def _host_hit(host: str, domains: Sequence[str]) -> Optional[str]:
    """Check if host exactly or subdomains match any domain in list.

    Args:
        host: Hostname to check.
        domains: List of domains to match against.

    Returns:
        The normalized host if matched, None otherwise.
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
    """Match vendor-owned domain in a URL source.

    Args:
        source: URL source name (script_src, link_href, etc).
        min_hits: Minimum distinct URLs required to match.

    Returns:
        A matcher function.
    """
    def test(page: Page, ats: "ATS") -> Optional[str]:
        domains = ats.hosts + ats.assets
        hits: Set[str] = set()

        for url in page.urls_for(source):
            host = _normalize_host(urlparse(url).hostname or "")

            if host == page.host:
                continue

            if _host_hit(host, domains):
                hits.add(url)

                if len(hits) >= min_hits:
                    return sorted(hits)[0]

        return None

    return test


def path_re(pattern: str, gated: bool = True) -> Matcher:
    """Match URL path by regex.

    Args:
        pattern: Regex pattern to search in path.
        gated: If True, only match on vendor-owned hostname.

    Returns:
        A matcher function.
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
    """Match query parameter by name and optional value pattern.

    Args:
        name: Parameter name.
        value_pattern: Optional regex to match parameter value.
        gated: If True, only match on vendor-owned hostname.

    Returns:
        A matcher function.
    """
    compiled = re.compile(value_pattern, re.I) if value_pattern else None
    lowered = name.lower()

    def test(page: Page, ats: "ATS") -> Optional[str]:
        if gated and not on_vendor_host(page, ats):
            return None

        for value in page.query.get(lowered, []):
            if compiled is None or compiled.fullmatch(value):
                return f"{name}={value}"

        return None

    return test


def in_text(source: str, needle: str, gated: bool = False) -> Matcher:
    """Substring match in text source.

    Args:
        source: Text source attribute name (text, script_text, etc).
        needle: Substring to find.
        gated: If True, only match on vendor-owned hostname.

    Returns:
        A matcher function.
    """
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
    """Substring match in URL collection.

    Args:
        source: URL source name.
        needle: Substring to find.

    Returns:
        A matcher function.
    """
    lowered = needle.lower()

    def test(page: Page, ats: "ATS") -> Optional[str]:
        for url in page.urls_for(source):
            if lowered in url.lower():
                return url

        return None

    return test


def has_class(name: str) -> Matcher:
    """Match if element class exists.

    Args:
        name: CSS class name.

    Returns:
        A matcher function.
    """
    lowered = name.lower()

    def test(page: Page, ats: "ATS") -> Optional[str]:
        return lowered if lowered in page.classes else None

    return test


def has_id(name: str) -> Matcher:
    """Match if element ID exists.

    Args:
        name: Element ID.

    Returns:
        A matcher function.
    """
    lowered = name.lower()

    def test(page: Page, ats: "ATS") -> Optional[str]:
        return lowered if lowered in page.ids else None

    return test


def has_data_attr(name: str,
                  value_pattern: Optional[str] = None) -> Matcher:
    """Match data-* attribute with optional value constraint.

    Args:
        name: Attribute name (without data- prefix).
        value_pattern: Optional regex to match attribute value.

    Returns:
        A matcher function.
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
    """Substring match in meta tags.

    Args:
        needle: Substring to find.

    Returns:
        A matcher function.
    """
    lowered = needle.lower()

    def test(page: Page, ats: "ATS") -> Optional[str]:
        for key, value in page.meta.items():
            if lowered in f"{key} {value}".lower():
                return f"{key}={value}"[:QUERY_PARAM_PREVIEW_LEN]

        return None

    return test


def any_of(matchers) -> Matcher:
    """Try matchers in order; return first match.

    Args:
        matchers: Sequence of matcher functions.

    Returns:
        A matcher function that returns first non-None match.
    """
    options = tuple(matchers)

    def test(page: Page, ats: "ATS") -> Optional[str]:
        for option in options:
            found = option(page, ats)

            if found:
                return found

        return None

    return test


def jsonld_url_host() -> Matcher:
    """Match vendor domain in JSON-LD URL-valued field.

    Returns:
        A matcher function.
    """
    def test(page: Page, ats: "ATS") -> Optional[str]:
        domains = ats.hosts + ats.assets

        for value in walk_strings(page.jsonld):
            if "//" not in value:
                continue

            if _host_hit(urlparse(value).hostname or "", domains):
                return value[:URL_PREVIEW_LEN]

        return None

    return test


@dataclass(frozen=True)
class Rule:
    """A single fingerprint rule."""

    signal_id: str
    source: str
    points: int
    tier: int
    reason: str
    test: Matcher


class ATSName(StrEnum):
    """Supported ATS platforms."""

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

    # Enterprise career-site platforms. These almost always run on the
    # employer's own domain, so they are identified by vendor infrastructure
    # in `assets` rather than by a hostname of their own.
    PHENOM = auto()
    RADANCY = auto()
    AVATURE = auto()
    HIBOB = auto()
    SOFTGARDEN = auto()
    NJOYN = auto()
    DIGITALRECRUITERS = auto()


@dataclass(frozen=True)
class ATS:
    """ATS platform definition."""

    name: ATSName
    hosts: Tuple[str, ...] = ()
    assets: Tuple[str, ...] = ()
    rules: Tuple[Rule, ...] = ()
    terms: Tuple[str, ...] = ()


def rule(signal_id: str, source: str, points: int, tier: int,
         reason: str, test: Matcher) -> Rule:
    """Create a Rule."""
    return Rule(signal_id, source, points, tier, reason, test)


def _expand(ats: ATS) -> List[Rule]:
    """Build complete rule list: generic + platform-specific."""
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
                "successfactors.com", "successfactors.eu",
                "sapsf.com", "sapsf.eu", "sapsf.cn"),
        terms=("successfactors",),
        rules=(
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

    # ------------------------------------------------------------------
    # Enterprise career-site platforms.
    #
    # These serve the employer's own domain, so there is usually no vendor
    # hostname to match -- the giveaway is their CDN or API in a script_src
    # or link_href. Domains only, deliberately: the whole point is that the
    # page URL looks like an ordinary corporate careers site, so any path
    # shape we invented here would match half the web.
    # ------------------------------------------------------------------

    ATS(
        name=ATSName.PHENOM,
        assets=("phenompeople.com",),
        terms=("phenom people",),
    ),
    ATS(
        name=ATSName.RADANCY,
        assets=("talentbrew.com", "tmpwebeng.com",
                "radancy.net", "radancy.eu"),
        terms=("radancy", "talentbrew"),
    ),
    ATS(
        name=ATSName.AVATURE,
        hosts=("avature.net",),
        assets=("avature.net", "avacdn.net"),
        terms=("avature",),
    ),
    ATS(
        name=ATSName.HIBOB,
        hosts=("careers.hibob.com",),
        assets=("hibob.com",),
        terms=("hibob",),
    ),
    ATS(
        name=ATSName.SOFTGARDEN,
        hosts=("softgarden.de", "softgarden.io"),
        assets=("softgarden.de", "softgarden.io"),
        terms=("softgarden",),
    ),
    ATS(
        name=ATSName.NJOYN,
        hosts=("njoyn.com",),
        assets=("njoyn.com",),
        terms=("njoyn",),
    ),
    ATS(
        name=ATSName.DIGITALRECRUITERS,
        hosts=("digitalrecruiters.com",),
        assets=("digitalrecruiters.com",),
        terms=("digitalrecruiters",),
    ),
)

ATS_NAMES: Tuple[ATSName, ...] = tuple(ats.name for ats in ATS_REGISTRY)

COMPILED: Dict[ATSName, Tuple[ATS, List[Rule]]] = {
    ats.name: (ats, _expand(ats))
    for ats in ATS_REGISTRY
}

KNOWN_DOMAINS: frozenset = frozenset(
    domain
    for ats in ATS_REGISTRY
    for domain in ats.hosts + ats.assets
)

# The registry's signal_ids must stay unique -- see
# tests/test_detector.py::test_no_duplicate_signal_ids. That check used to be
# a module-level `assert` here, which python -O deletes and which, on the one
# occasion it would matter, kills import with a bare AssertionError instead of
# failing a test run.


Renderer = Callable[[str], Optional[str]]


class ATSDetector:
    """Fetch a job page and identify the ATS behind it.

    Args:
        timeout: Request timeout in seconds.
        max_bytes: Maximum response body size.
        max_redirects: Maximum redirect chain length.
        render: Optional browser renderer for JS-rendered pages.
        session: Optional requests.Session to fetch through. Pass one when the
            caller will also want the page -- otherwise the same URL is
            fetched twice and a connection pool is built and discarded per
            call. Defaults to a private retrying session.
    """

    def __init__(self, timeout: int = 15, max_bytes: int = 5_000_000,
                 max_redirects: int = 5,
                 render: Optional[Renderer] = None,
                 session=None):
        self.timeout = timeout
        self.max_bytes = max_bytes
        self.render = render

        self.session = session or new_session({
            "User-Agent": (
                "Mozilla/5.0 (compatible; ATSDetector/2.0; "
                "+https://example.com/ats-detector)"
            ),
            "Accept": (
                "text/html,application/xhtml+xml,"
                "application/xml;q=0.9,*/*;q=0.8"
            ),
        })
        self.session.max_redirects = max_redirects

    def detect(self, url: str) -> DetectionResult:
        """Fetch a URL and detect its ATS.

        Args:
            url: Job posting URL.

        Returns:
            DetectionResult with verdict and evidence.
        """
        try:
            with self.session.get(
                url,
                timeout=self.timeout,
                allow_redirects=True,
                stream=True,
            ) as response:
                response.raise_for_status()

                content_type = response.headers.get(
                    "Content-Type", ""
                ).lower()

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

                # Same decode the scrapers use, and it has to stay the same:
                # Board reuses this exact string as its board page, and
                # trusting response.encoding turned Scalian's and Sopra
                # Steria's accented titles into "DÃ©veloppeur".
                html = decode_response(response, raw)

                redirects = [item.url for item in response.history]

                result = self.detect_html(
                    html,
                    response.url,
                    headers=dict(response.headers),
                    input_url=url,
                    redirect_chain=redirects + [response.url],
                    status_code=response.status_code,
                )

                result.html = html

                return self._maybe_render(result, response.url, url)

        except requests.RequestException as exc:
            return self._failed(url, url, str(exc), None)

    def _maybe_render(self, result: DetectionResult, final_url: str,
                      input_url: str) -> DetectionResult:
        """Retry rendering if page appears to be JS-rendered.

        Args:
            result: Initial detection result.
            final_url: Final URL after redirects.
            input_url: Original input URL.

        Returns:
            Updated DetectionResult or original if rendering not needed.
        """
        if self.render is None or not result.needs_rendering:
            return result

        rendered = self.render(final_url)

        if not rendered:
            return result

        second = self.detect_html(
            rendered,
            final_url,
            input_url=input_url,
            redirect_chain=result.redirect_chain,
            status_code=result.status_code,
        )
        # The rendered DOM, not the served shell -- if this result is the one
        # returned, it is also the page a caller should reuse.
        second.html = rendered

        return second if second.status != "unknown" else result

    def detect_html(self, html: str, url: str,
                    headers: Optional[Dict[str, str]] = None,
                    input_url: Optional[str] = None,
                    redirect_chain: Optional[List[str]] = None,
                    status_code: Optional[int] = None) -> DetectionResult:
        """Score already-fetched HTML.

        Args:
            html: HTML content.
            url: Document URL.
            headers: HTTP response headers.
            input_url: Original request URL.
            redirect_chain: Redirect URLs.
            status_code: HTTP status code.

        Returns:
            DetectionResult with verdict and evidence.
        """
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
        """Return failure result."""
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


def extract(html: str, url: str,
            headers: Optional[Dict[str, str]] = None,
            input_url: Optional[str] = None,
            redirect_chain: Optional[List[str]] = None) -> Page:
    """Extract signal sources from HTML, keeping them apart by type.

    Args:
        html: HTML content.
        url: Document URL.
        headers: HTTP response headers.
        input_url: Original request URL.
        redirect_chain: Redirect URLs.

    Returns:
        Page structure with all signal sources.
    """
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

    page.script_urls = list(dict.fromkeys(
        _SCRIPT_URL_RE.findall(page.script_text.replace("\\/", "/"))
    ))[:MAX_ANCHORS]

    page.text = soup.get_text(" ", strip=True)

    page.is_job_page = any(
        "jobposting" in value.lower()
        for value in walk_strings(page.jsonld)
    ) or bool(
        soup.find(attrs={"itemtype": re.compile("JobPosting", re.I)})
    )

    return page


def score(page: Page) -> Tuple[
    Dict[ATSName, int], List[Evidence], Dict[ATSName, Dict[str, int]]
]:
    """Run every rule, deduplicating and capping by source.

    Args:
        page: Extracted page signals.

    Returns:
        Tuple of (scores dict, evidence list, source_scores dict).
    """
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
                matched=str(matched)[:EVIDENCE_PREVIEW_LEN],
            ))

        scores[name] = min(scores[name], ATS_CAP)

    return scores, evidence, source_scores


def decide(page: Page, status_code: Optional[int] = None) -> DetectionResult:
    """Convert scores to verdict: detected, ambiguous, or unknown.

    Args:
        page: Extracted page signals.
        status_code: HTTP status code from fetch.

    Returns:
        DetectionResult with verdict and evidence.
    """
    scores, evidence, source_scores = score(page)

    ranked = sorted(scores.items(), key=lambda item: item[1], reverse=True)

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

    qualified = bool(tier1) or len(tier2) >= 2

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

    if winner_score <= 0:
        confidence = 0.0
    elif status == "detected":
        confidence = (
            0.40 * tier_quality
            + 0.30 * margin
            + 0.30 * diversity
        )
    else:
        confidence = 0.20 * tier_quality + 0.20 * diversity

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
        unknown_vendor=(
            _unknown_vendor(page) if status == "unknown" else None
        ),
        status_code=status_code,
    )


def _looks_unrendered(page: Page) -> bool:
    """Check if page appears to be a JS shell.

    Args:
        page: Extracted page signals.

    Returns:
        True if page has external scripts but little text or JS app markers.
    """
    if len(page.text) > 2000:
        return False

    return bool(page.script_srcs) and (
        len(page.text) < 500
        or bool({"root", "app", "__next"} & page.ids)
    )


_VENDOR_SOURCES = (
    "script_srcs", "iframe_srcs", "form_actions",
    "link_hrefs", "canonical_urls",
)


def _is_infra(host: str) -> bool:
    """Check if host is generic web infrastructure.

    Args:
        host: Hostname to check.

    Returns:
        True if host matches infrastructure denylist.
    """
    for marker in INFRA_DENYLIST:
        if "." in marker:
            if host == marker or host.endswith("." + marker):
                return True
        elif marker in host:
            return True

    return False


def _unknown_vendor(page: Page) -> Optional[str]:
    """Find the top third-party domain on an undetected page.

    Args:
        page: Extracted page signals.

    Returns:
        The most-referenced third-party domain, or None.
    """
    counts: Dict[str, Set[str]] = {}

    for source in _VENDOR_SOURCES:
        for url in getattr(page, source, []):
            host = _normalize_host(urlparse(url).hostname or "")

            if not host or host == page.host or host.endswith("." + page.host):
                continue

            if _is_infra(host):
                continue

            if _host_hit(host, tuple(KNOWN_DOMAINS)):
                continue

            domain = ".".join(host.split(".")[-2:])
            counts.setdefault(domain, set()).add(url)

    ranked = sorted(
        counts.items(),
        key=lambda item: (-len(item[1]), item[0]),
    )

    return next(
        (domain for domain, urls in ranked if len(urls) >= 2),
        None,
    )


def print_result(result: DetectionResult) -> None:
    """Print detection result in human-readable format.

    Args:
        result: DetectionResult to print.
    """
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

    if result.unknown_vendor:
        print(f"Unknown vendor: {result.unknown_vendor}  "
              f"(unrecognized platform -- candidate for the registry)")

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
            print(f"{'':24}matched: {item.matched[:MATCH_DISPLAY_LEN]}")


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(
        description="Detect the ATS behind a job posting URL"
    )
    parser.add_argument("url")

    args = parser.parse_args()

    print_result(ATSDetector().detect(args.url))
