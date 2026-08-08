from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Dict, List
from urllib.parse import urlparse

import requests
from bs4 import BeautifulSoup


ATS_NAMES = (
    "workday",
    "smartrecruiters",
    "greenhouse",
    "icims",
    "successfactors",
    "taleo",
    "lever",
    "teamtailor",
    "recruitee",
    "ashby",
    "workable",
    "bamboohr",
    "talentsoft",
    "comeet",
    "onlyfy",
    "breezy",
    "talentlyft",
    "personio",
    "pinpoint",
    "jobvite",
    "jazzhr",
)

@dataclass
class Evidence:
    ats: str
    points: int
    reason: str
    tier: int


@dataclass
class DetectionResult:
    input_url: str
    final_url: str
    detected_ats: str | None
    confidence: float
    scores: Dict[str, int]
    evidence: List[Evidence] = field(default_factory=list)
    status_code: int | None = None
    error: str | None = None


class ATSDetector:
    """
    Tiered ATS detector.

    Signal tiers:

      Tier 1 - definitive
        * Canonical ATS hostname
        * ATS-specific endpoint
        * Unique platform identifier

      Tier 2 - strong
        * ATS-specific JS/API identifiers
        * Strong URL structures
        * Platform-specific DOM attributes

      Tier 3 - supporting
        * CSS classes / IDs
        * Metadata
        * Footer text
        * Generic platform terminology

    Detection requires:
      * minimum score
      * sufficient confidence
      * at least one Tier-1 or Tier-2 signal

    This prevents generic terminology or customizable CSS
    from producing high-confidence false positives.
    """

    def __init__(
        self,
        timeout: int = 15,
        max_bytes: int = 5_000_000,
    ):
        self.timeout = timeout
        self.max_bytes = max_bytes

        self.session = requests.Session()
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

    def detect(self, url: str) -> DetectionResult:
        scores = {ats: 0 for ats in ATS_NAMES}
        evidence: List[Evidence] = []

        try:
            response = self.session.get(
                url,
                timeout=self.timeout,
                allow_redirects=True,
                stream=True,
            )

            response.raise_for_status()

            content_length = response.headers.get("Content-Length")

            if content_length:
                try:
                    if int(content_length) > self.max_bytes:
                        return self._error_result(
                            url,
                            response.url,
                            response.status_code,
                            scores,
                            evidence,
                            "Response exceeds maximum allowed size",
                        )
                except ValueError:
                    pass

            raw = response.raw.read(self.max_bytes + 1)

            if len(raw) > self.max_bytes:
                return self._error_result(
                    url,
                    response.url,
                    response.status_code,
                    scores,
                    evidence,
                    "Response exceeds maximum allowed size",
                )

            html = raw.decode(
                response.encoding or "utf-8",
                errors="replace",
            )

            soup = BeautifulSoup(html, "html.parser")

            final_url = response.url
            parsed = urlparse(final_url)

            hostname = parsed.netloc.lower().split(":")[0]
            path = parsed.path.lower()
            query = parsed.query.lower()
            url_text = final_url.lower()
            html_lower = html.lower()

            classes: List[str] = []
            ids: List[str] = []

            for element in soup.find_all(True):
                element_id = element.get("id")

                if element_id:
                    ids.append(str(element_id).lower())

                element_classes = element.get("class", [])

                classes.extend(
                    str(x).lower()
                    for x in element_classes
                )

            class_text = " ".join(classes)
            id_text = " ".join(ids)

            metadata = self._extract_metadata(soup)

            script_text = "\n".join(
                script.get_text(" ", strip=False)
                for script in soup.find_all("script")
            ).lower()

            searchable = (
                html_lower
                + " "
                + script_text
                + " "
                + class_text
                + " "
                + id_text
            )

            # =========================================================
            # WORKDAY
            # =========================================================

            self._score(
                scores,
                evidence,
                "workday",
                60,
                "Hostname contains myworkdayjobs.com",
                "myworkdayjobs.com" in hostname,
                tier=1,
            )

            self._score(
                scores,
                evidence,
                "workday",
                30,
                "Workday job URL structure detected",
                bool(re.search(
                    r"/job/[^/?#]+/",
                    path,
                    re.I,
                )),
                tier=2,
            )

            self._score(
                scores,
                evidence,
                "workday",
                30,
                "Workday-specific data-automation-id detected",
                bool(re.search(
                    r"data-automation-id\s*=\s*['\"]"
                    r"(jobPosting|jobTitle|locations|"
                    r"jobPostingHeader|applyButton)",
                    html,
                    re.I,
                )),
                tier=2,
            )

            self._score(
                scores,
                evidence,
                "workday",
                15,
                "Workday data-automation-id attribute detected",
                "data-automation-id" in html_lower,
                tier=3,
            )

            self._score(
                scores,
                evidence,
                "workday",
                10,
                "Workday terminology detected",
                bool(re.search(
                    r"\bworkday\b|myworkdayjobs",
                    searchable,
                    re.I,
                )),
                tier=3,
            )

            # =========================================================
            # SMARTRECRUITERS
            # =========================================================

            self._score(
                scores,
                evidence,
                "smartrecruiters",
                60,
                "Hostname contains smartrecruiters.com",
                "smartrecruiters.com" in hostname,
                tier=1,
            )

            self._score(
                scores,
                evidence,
                "smartrecruiters",
                30,
                "SmartRecruiters URL/posting structure detected",
                bool(re.search(
                    r"smartrecruiters\.com/[^/?#]+/"
                    r"(?:\d+|[a-z0-9]+)-",
                    url_text,
                    re.I,
                )),
                tier=2,
            )

            self._score(
                scores,
                evidence,
                "smartrecruiters",
                30,
                "SmartRecruiters API URL detected",
                "api.smartrecruiters.com" in searchable,
                tier=2,
            )

            self._score(
                scores,
                evidence,
                "smartrecruiters",
                25,
                "SmartRecruiters identifiers detected",
                bool(re.search(
                    r"\b(?:uuid|refnumber|jobadurl|applyurl)\b",
                    searchable,
                    re.I,
                )),
                tier=2,
            )

            self._score(
                scores,
                evidence,
                "smartrecruiters",
                10,
                "SmartRecruiters terminology detected",
                "smartrecruiters" in searchable,
                tier=3,
            )

            # =========================================================
            # GREENHOUSE
            # =========================================================

            self._score(
                scores,
                evidence,
                "greenhouse",
                60,
                "Hostname contains boards.greenhouse.io",
                "boards.greenhouse.io" in hostname,
                tier=1,
            )

            self._score(
                scores,
                evidence,
                "greenhouse",
                60,
                "Greenhouse gh_jid identifier detected",
                "gh_jid" in query
                or re.search(
                    r"\bgh_jid\b",
                    searchable,
                ) is not None,
                tier=1,
            )

            self._score(
                scores,
                evidence,
                "greenhouse",
                30,
                "Greenhouse URL structure detected",
                bool(re.search(
                    r"boards\.greenhouse\.io/[^/?#]+/\d+",
                    url_text,
                    re.I,
                )),
                tier=2,
            )

            self._score(
                scores,
                evidence,
                "greenhouse",
                20,
                "Greenhouse job identifier detected",
                bool(re.search(
                    r"\bgh[_-]?(?:jid|job[_-]?id)\b",
                    searchable,
                    re.I,
                )),
                tier=2,
            )

            self._score(
                scores,
                evidence,
                "greenhouse",
                10,
                "Greenhouse terminology detected",
                "greenhouse" in searchable,
                tier=3,
            )

            # =========================================================
            # ICIMS
            # =========================================================

            self._score(
                scores,
                evidence,
                "icims",
                60,
                "Hostname contains icims.com",
                "icims.com" in hostname,
                tier=1,
            )

            self._score(
                scores,
                evidence,
                "icims",
                45,
                "Classic iCIMS job URL structure detected",
                bool(re.search(
                    r"/jobs/\d+/[^/?#]+/job",
                    path,
                    re.I,
                )),
                tier=1,
            )

            self._score(
                scores,
                evidence,
                "icims",
                30,
                "iCIMS career-site path detected",
                bool(re.search(
                    r"/(?:careers-home|jobs|career-site)",
                    path,
                    re.I,
                )),
                tier=2,
            )

            self._score(
                scores,
                evidence,
                "icims",
                20,
                "iCIMS software footer detected",
                "software powered by icims" in html_lower,
                tier=2,
            )

            self._score(
                scores,
                evidence,
                "icims",
                10,
                "iCIMS terminology detected",
                "icims" in searchable,
                tier=3,
            )

            # =========================================================
            # SAP SUCCESSFACTORS
            # =========================================================

            self._score(
                scores,
                evidence,
                "successfactors",
                60,
                "Hostname contains jobs.hr.cloud.sap",
                "jobs.hr.cloud.sap" in hostname,
                tier=1,
            )

            self._score(
                scores,
                evidence,
                "successfactors",
                60,
                "SuccessFactors-hosted career domain detected",
                bool(re.search(
                    r"(?:jobs\.hr\.cloud\.sap|"
                    r"jobs\.hr\.sapcloud\.cn)",
                    hostname,
                    re.I,
                )),
                tier=1,
            )

            self._score(
                scores,
                evidence,
                "successfactors",
                45,
                "SuccessFactors recruiting endpoint detected",
                bool(re.search(
                    r"/sf/recruiting/",
                    path,
                    re.I,
                )),
                tier=1,
            )

            self._score(
                scores,
                evidence,
                "successfactors",
                30,
                "SuccessFactors jobReqId detected",
                bool(re.search(
                    r"\bjobreqid\b",
                    searchable,
                    re.I,
                )),
                tier=2,
            )

            self._score(
                scores,
                evidence,
                "successfactors",
                30,
                "SuccessFactors jobPostingId detected",
                bool(re.search(
                    r"\bjobpostingid\b",
                    searchable,
                    re.I,
                )),
                tier=2,
            )

            self._score(
                scores,
                evidence,
                "successfactors",
                30,
                "SuccessFactors recruiting deep-link detected",
                bool(re.search(
                    r"/sf/recruiting/"
                    r"(?:jobreqsummary|jobposting)",
                    path,
                    re.I,
                )),
                tier=2,
            )

            self._score(
                scores,
                evidence,
                "successfactors",
                10,
                "SuccessFactors terminology detected",
                bool(re.search(
                    r"\bsuccessfactors\b|"
                    r"sap successfactors|"
                    r"success factors",
                    searchable,
                    re.I,
                )),
                tier=3,
            )

            # =========================================================
            # ORACLE TALEO
            # =========================================================

            self._score(
                scores,
                evidence,
                "taleo",
                60,
                "Hostname contains taleo.net",
                hostname.endswith(".taleo.net")
                or hostname == "taleo.net",
                tier=1,
            )

            self._score(
                scores,
                evidence,
                "taleo",
                60,
                "Taleo careersection path detected",
                "/careersection/" in path,
                tier=1,
            )

            self._score(
                scores,
                evidence,
                "taleo",
                60,
                "Taleo jobdetail.ftl endpoint detected",
                "jobdetail.ftl" in path,
                tier=1,
            )

            self._score(
                scores,
                evidence,
                "taleo",
                35,
                "Taleo job query parameter detected",
                bool(re.search(
                    r"(?:^|&)job=",
                    query,
                    re.I,
                )),
                tier=2,
            )

            self._score(
                scores,
                evidence,
                "taleo",
                20,
                "Taleo career-section URL detected",
                bool(re.search(
                    r"/careersection/[^/?#]+/",
                    path,
                    re.I,
                )),
                tier=2,
            )

            self._score(
                scores,
                evidence,
                "taleo",
                10,
                "Taleo terminology detected",
                bool(re.search(
                    r"\btaleo\b|oracle recruiting",
                    searchable,
                    re.I,
                )),
                tier=3,
            )

            # =========================================================
            # LEVER
            # =========================================================

            self._score(
                scores,
                evidence,
                "lever",
                60,
                "Hostname contains jobs.lever.co",
                hostname == "jobs.lever.co",
                tier=1,
            )

            self._score(
                scores,
                evidence,
                "lever",
                60,
                "Lever posting UUID detected in URL",
                bool(re.search(
                    r"/[0-9a-f]{8}-"
                    r"[0-9a-f]{4}-"
                    r"[0-9a-f]{4}-"
                    r"[0-9a-f]{4}-"
                    r"[0-9a-f]{12}",
                    path,
                    re.I,
                )),
                tier=1,
            )

            self._score(
                scores,
                evidence,
                "lever",
                40,
                "Lever job URL structure detected",
                bool(re.search(
                    r"^/[^/?#]+/"
                    r"[0-9a-f]{8}-"
                    r"[0-9a-f]{4}-"
                    r"[0-9a-f]{4}-"
                    r"[0-9a-f]{4}-"
                    r"[0-9a-f]{12}"
                    r"(?:/apply)?/?$",
                    path,
                    re.I,
                )),
                tier=2,
            )

            self._score(
                scores,
                evidence,
                "lever",
                30,
                "Lever application route detected",
                bool(re.search(
                    r"/apply/?$",
                    path,
                    re.I,
                )),
                tier=2,
            )

            self._score(
                scores,
                evidence,
                "lever",
                25,
                "Lever posting identifiers detected",
                bool(re.search(
                    r"\b(?:postingid|requisitioncodes|reqcode)\b",
                    searchable,
                    re.I,
                )),
                tier=2,
            )

            self._score(
                scores,
                evidence,
                "lever",
                10,
                "Lever terminology detected",
                bool(re.search(
                    r"\blever\b|"
                    r"\burls\.show\b|"
                    r"\burls\.apply\b",
                    searchable,
                    re.I,
                )),
                tier=3,
            )

            # =========================================================
            # TEAMTAILOR
            # =========================================================

            self._score(
                scores,
                evidence,
                "teamtailor",
                60,
                "Hostname contains teamtailor.com",
                hostname.endswith(".teamtailor.com")
                or hostname == "teamtailor.com",
                tier=1,
            )

            self._score(
                scores,
                evidence,
                "teamtailor",
                60,
                "Teamtailor application route detected",
                bool(re.search(
                    r"/jobs/\d+-[^/?#]+/applications/new",
                    path,
                    re.I,
                )),
                tier=1,
            )

            self._score(
                scores,
                evidence,
                "teamtailor",
                45,
                "Teamtailor job URL structure detected",
                bool(re.search(
                    r"/(?:careers/)?jobs/\d+-[^/?#]+",
                    path,
                    re.I,
                )),
                tier=2,
            )

            self._score(
                scores,
                evidence,
                "teamtailor",
                30,
                "Teamtailor job ID detected",
                bool(re.search(
                    r"/(?:careers/)?jobs/\d+-",
                    path,
                    re.I,
                )),
                tier=2,
            )

            self._score(
                scores,
                evidence,
                "teamtailor",
                25,
                "Teamtailor application iframe parameter detected",
                "iframe=true" in query,
                tier=2,
            )

            self._score(
                scores,
                evidence,
                "teamtailor",
                25,
                "Teamtailor API/job attributes detected",
                bool(re.search(
                    r"\b(?:apply-url|cover-image-url|"
                    r"remote-status|employment-type|"
                    r"employment-level)\b",
                    searchable,
                    re.I,
                )),
                tier=2,
            )

            self._score(
                scores,
                evidence,
                "teamtailor",
                10,
                "Teamtailor terminology detected",
                bool(re.search(
                    r"\bteamtailor\b",
                    searchable,
                    re.I,
                )),
                tier=3,
            )

            # =========================================================
            # RECRUITEE
            # =========================================================

            self._score(
                scores,
                evidence,
                "recruitee",
                60,
                "Hostname contains recruitee.com",
                hostname.endswith(".recruitee.com")
                or hostname == "recruitee.com",
                tier=1,
            )

            self._score(
                scores,
                evidence,
                "recruitee",
                45,
                "Recruitee careers-site URL detected",
                bool(re.search(
                    r"^[a-z0-9-]+\.recruitee\.com$",
                    hostname,
                    re.I,
                )),
                tier=1,
            )

            self._score(
                scores,
                evidence,
                "recruitee",
                30,
                "Recruitee careers API/feed URL detected",
                bool(re.search(
                    r"/api/(?:feeds/)?offers(?:\.xml)?/?$",
                    path,
                    re.I,
                )),
                tier=2,
            )

            self._score(
                scores,
                evidence,
                "recruitee",
                25,
                "Recruitee careers URL detected",
                bool(re.search(
                    r"\brecruitee\.com\b",
                    url_text,
                    re.I,
                )),
                tier=2,
            )

            self._score(
                scores,
                evidence,
                "recruitee",
                10,
                "Recruitee terminology detected",
                bool(re.search(
                    r"\brecruitee\b|"
                    r"\btellent recruitee\b",
                    searchable,
                    re.I,
                )),
                tier=3,
            )

            # =========================================================
            # ASHBY
            # =========================================================

            self._score(
                scores,
                evidence,
                "ashby",
                60,
                "Hostname is jobs.ashbyhq.com",
                hostname == "jobs.ashbyhq.com",
                tier=1,
            )

            self._score(
                scores,
                evidence,
                "ashby",
                45,
                "Ashby job-board URL structure detected",
                bool(re.search(
                    r"^/[^/?#]+/"
                    r"(?:[0-9a-f]{8}-[0-9a-f]{4}-"
                    r"[0-9a-f]{4}-[0-9a-f]{4}-"
                    r"[0-9a-f]{12}|[^/?#]+)"
                    r"(?:/application)?/?$",
                    path,
                    re.I,
                )),
                tier=2,
            )

            self._score(
                scores,
                evidence,
                "ashby",
                35,
                "Ashby application route detected",
                bool(re.search(
                    r"/application/?$",
                    path,
                    re.I,
                )),
                tier=2,
            )

            self._score(
                scores,
                evidence,
                "ashby",
                30,
                "Ashby hostname/reference detected",
                bool(re.search(
                    r"\bjobs\.ashbyhq\.com\b|"
                    r"\bashbyhq\.com\b",
                    searchable,
                    re.I,
                )),
                tier=2,
            )

            self._score(
                scores,
                evidence,
                "ashby",
                10,
                "Ashby terminology detected",
                bool(re.search(
                    r"\bashby\b|"
                    r"\bashbyhq\b",
                    searchable,
                    re.I,
                )),
                tier=3,
            )

            # =========================================================
            # WORKABLE
            # =========================================================

            self._score(
                scores,
                evidence,
                "workable",
                60,
                "Hostname is apply.workable.com",
                hostname == "apply.workable.com",
                tier=1,
            )

            self._score(
                scores,
                evidence,
                "workable",
                50,
                "Workable job URL structure detected",
                bool(re.search(
                    r"^/[^/?#]+/j/[^/?#]+/?$",
                    path,
                    re.I,
                )),
                tier=1,
            )

            self._score(
                scores,
                evidence,
                "workable",
                40,
                "Workable application route detected",
                bool(re.search(
                    r"^/[^/?#]+/j/[^/?#]+/apply/?$",
                    path,
                    re.I,
                )),
                tier=2,
            )

            self._score(
                scores,
                evidence,
                "workable",
                30,
                "Workable hostname/reference detected",
                bool(re.search(
                    r"\bapply\.workable\.com\b|"
                    r"\bworkable\.com\b",
                    searchable,
                    re.I,
                )),
                tier=2,
            )

            self._score(
                scores,
                evidence,
                "workable",
                10,
                "Workable terminology detected",
                bool(re.search(
                    r"\bworkable\b",
                    searchable,
                    re.I,
                )),
                tier=3,
            )

            # =========================================================
            # BAMBOOHR
            # =========================================================

            self._score(
                scores,
                evidence,
                "bamboohr",
                60,
                "Hostname contains bamboohr.com",
                hostname.endswith(".bamboohr.com")
                or hostname == "bamboohr.com",
                tier=1,
            )

            self._score(
                scores,
                evidence,
                "bamboohr",
                50,
                "BambooHR careers path detected",
                bool(re.search(
                    r"/careers(?:/|$)",
                    path,
                    re.I,
                ))
                and (
                    hostname.endswith(".bamboohr.com")
                    or "bamboohr.com" in searchable
                ),
                tier=1,
            )

            self._score(
                scores,
                evidence,
                "bamboohr",
                35,
                "BambooHR job URL structure detected",
                bool(re.search(
                    r"/careers/[^/?#]+",
                    path,
                    re.I,
                ))
                and (
                    hostname.endswith(".bamboohr.com")
                    or "bamboohr" in searchable
                ),
                tier=2,
            )

            self._score(
                scores,
                evidence,
                "bamboohr",
                30,
                "BambooHR reference detected",
                bool(re.search(
                    r"\bbamboohr\b|"
                    r"\bbamboo hr\b",
                    searchable,
                    re.I,
                )),
                tier=2,
            )

            self._score(
                scores,
                evidence,
                "bamboohr",
                10,
                "BambooHR terminology detected",
                bool(re.search(
                    r"\bbamboohr\b|"
                    r"\bbamboo hr\b",
                    searchable,
                    re.I,
                )),
                tier=3,
            )

            # =========================================================
            # TALENTSOFT / CEGID
            # =========================================================

            self._score(
                scores,
                evidence,
                "talentsoft",
                60,
                "Talentsoft/Cegid API endpoint detected",
                bool(re.search(
                    r"/api/v\d+/"
                    r"(?:offersummaries|offers/getoffer|"
                    r"applicants)\b",
                    url_text,
                    re.I,
                )),
                tier=1,
            )

            self._score(
                scores,
                evidence,
                "talentsoft",
                55,
                "Talentsoft token endpoint detected",
                bool(re.search(
                    r"/api/token(?:[/?#]|$)",
                    url_text,
                    re.I,
                )),
                tier=1,
            )

            self._score(
                scores,
                evidence,
                "talentsoft",
                40,
                "Talentsoft Front Office API reference detected",
                bool(re.search(
                    r"\b/api/v\d+/(?:offersummaries|"
                    r"offers/getoffer|applicants)\b",
                    searchable,
                    re.I,
                )),
                tier=2,
            )

            self._score(
                scores,
                evidence,
                "talentsoft",
                30,
                "Talentsoft offer reference detected",
                bool(re.search(
                    r"\b(?:offerreference|offer[_-]?reference|"
                    r"offercode|reference)\b",
                    searchable,
                    re.I,
                ))
                and bool(re.search(
                    r"\btalentsoft\b|\bcegid\b",
                    searchable,
                    re.I,
                )),
                tier=2,
            )

            self._score(
                scores,
                evidence,
                "talentsoft",
                25,
                "Talentsoft/Cegid hostname detected",
                bool(re.search(
                    r"\btalentsoft\b|\bcegid-hr\b|"
                    r"\bcegid\b",
                    hostname,
                    re.I,
                )),
                tier=1,
            )

            self._score(
                scores,
                evidence,
                "talentsoft",
                10,
                "Talentsoft terminology detected",
                bool(re.search(
                    r"\btalentsoft\b|"
                    r"\bcegid talent\b|"
                    r"\bcegid hr\b",
                    searchable,
                    re.I,
                )),
                tier=3,
            )

            # =========================================================
            # COMEET
            # =========================================================

            self._score(
                scores,
                evidence,
                "comeet",
                60,
                "Comeet hosted jobs domain detected",
                hostname == "www.comeet.co"
                or hostname == "comeet.co",
                tier=1,
            )

            self._score(
                scores,
                evidence,
                "comeet",
                60,
                "Comeet hosted job URL structure detected",
                bool(re.search(
                    r"^/jobs/[^/?#]+/"
                    r"[^/?#]+/"
                    r"[^/?#]+/"
                    r"[^/?#]+/?$",
                    path,
                    re.I,
                ))
                and (
                    hostname.endswith("comeet.co")
                    or "comeet.co" in url_text
                ),
                tier=1,
            )

            self._score(
                scores,
                evidence,
                "comeet",
                50,
                "Comeet Careers API endpoint detected",
                bool(re.search(
                    r"/careers-api/2\.0/"
                    r"company/[^/?#]+/"
                    r"(?:positions|positions/[^/?#]+)",
                    url_text,
                    re.I,
                )),
                tier=1,
            )

            self._score(
                scores,
                evidence,
                "comeet",
                35,
                "Comeet position/company identifiers detected",
                bool(re.search(
                    r"\b(?:url_comeet_hosted_page|"
                    r"url_active_page|"
                    r"is_published|"
                    r"position_uid|"
                    r"company_uid)\b",
                    searchable,
                    re.I,
                )),
                tier=2,
            )

            self._score(
                scores,
                evidence,
                "comeet",
                30,
                "Comeet Careers API reference detected",
                "careers-api/2.0" in searchable,
                tier=2,
            )

            self._score(
                scores,
                evidence,
                "comeet",
                20,
                "Comeet API token parameter detected",
                bool(re.search(
                    r"(?:^|[?&])token=",
                    query,
                    re.I,
                ))
                and (
                    "comeet" in searchable
                    or "careers-api" in searchable
                ),
                tier=2,
            )

            self._score(
                scores,
                evidence,
                "comeet",
                10,
                "Comeet terminology detected",
                bool(re.search(
                    r"\bcomeet\b",
                    searchable,
                    re.I,
                )),
                tier=3,
            )

            # =========================================================
            # ONLYFY / PRESCREEN
            # =========================================================

            self._score(
                scores,
                evidence,
                "onlyfy",
                70,
                "onlyfy.jobs hostname detected",
                hostname.endswith(".onlyfy.jobs")
                or hostname == "onlyfy.jobs",
                tier=1,
            )

            self._score(
                scores,
                evidence,
                "onlyfy",
                55,
                "onlyfy application-manager infrastructure detected",
                bool(re.search(
                    r"\bonlyfy\.jobs\b|"
                    r"\bjobbase\.io\b",
                    url_text,
                    re.I,
                )),
                tier=1,
            )

            self._score(
                scores,
                evidence,
                "onlyfy",
                40,
                "onlyfy/Perscreen infrastructure reference detected",
                bool(re.search(
                    r"\bonlyfy\b|"
                    r"\bprescreen\b|"
                    r"\bjobbase\.io\b",
                    searchable,
                    re.I,
                )),
                tier=2,
            )

            self._score(
                scores,
                evidence,
                "onlyfy",
                25,
                "onlyfy application-manager reference detected",
                bool(re.search(
                    r"\bonlyfy application manager\b|"
                    r"\bapplication manager\b",
                    searchable,
                    re.I,
                )),
                tier=2,
            )

            self._score(
                scores,
                evidence,
                "onlyfy",
                10,
                "onlyfy terminology detected",
                bool(re.search(
                    r"\bonlyfy\b|"
                    r"\bprescreen\b",
                    searchable,
                    re.I,
                )),
                tier=3,
            )

            # =========================================================
            # BREEZY HR
            # =========================================================

            self._score(
                scores,
                evidence,
                "breezy",
                70,
                "Breezy hosted careers domain detected",
                hostname.endswith(".breezy.hr")
                or hostname == "breezy.hr",
                tier=1,
            )

            self._score(
                scores,
                evidence,
                "breezy",
                60,
                "Breezy job URL structure detected",
                bool(re.search(
                    r"/p/[a-z0-9]+/?$",
                    path,
                    re.I,
                ))
                and (
                    hostname.endswith(".breezy.hr")
                    or "breezy.hr" in searchable
                ),
                tier=1,
            )

            self._score(
                scores,
                evidence,
                "breezy",
                50,
                "Breezy API endpoint detected",
                bool(re.search(
                    r"(?:api\.)?breezy\.hr/v3/"
                    r"company/[^/?#]+/positions",
                    url_text,
                    re.I,
                )),
                tier=1,
            )

            self._score(
                scores,
                evidence,
                "breezy",
                35,
                "Breezy source-tracking parameter detected",
                bool(re.search(
                    r"(?:^|&)source=",
                    query,
                    re.I,
                ))
                and (
                    "breezy" in searchable
                    or hostname.endswith(".breezy.hr")
                ),
                tier=2,
            )

            self._score(
                scores,
                evidence,
                "breezy",
                30,
                "Breezy API position state detected",
                bool(re.search(
                    r"\b(?:published|draft|archived|"
                    r"closed|pending)\b",
                    searchable,
                    re.I,
                ))
                and "breezy" in searchable,
                tier=2,
            )

            self._score(
                scores,
                evidence,
                "breezy",
                10,
                "Breezy terminology detected",
                bool(re.search(
                    r"\bbreezy\b|\bbreezy hr\b",
                    searchable,
                    re.I,
                )),
                tier=3,
            )

            # =========================================================
            # TALENTLYFT
            # =========================================================

            self._score(
                scores,
                evidence,
                "talentlyft",
                70,
                "TalentLyft hosted career domain detected",
                (
                    hostname == "talentlyft.com"
                    or hostname.endswith(".talentlyft.com")
                ),
                tier=1,
            )

            self._score(
                scores,
                evidence,
                "talentlyft",
                45,
                "TalentLyft /jobs/<friendly-slug> structure detected",
                bool(re.search(
                    r"^/jobs/[^/?#]+/?$",
                    path,
                    re.I,
                ))
                and (
                    hostname == "talentlyft.com"
                    or hostname.endswith(".talentlyft.com")
                ),
                tier=2,
            )

            self._score(
                scores,
                evidence,
                "talentlyft",
                35,
                "TalentLyft job URL structure detected",
                bool(re.search(
                    r"/jobs/[^/?#]+",
                    path,
                    re.I,
                ))
                and (
                    "talentlyft" in searchable
                    or "talentlyft.com" in url_text
                ),
                tier=2,
            )

            self._score(
                scores,
                evidence,
                "talentlyft",
                30,
                "TalentLyft job API field detected",
                bool(re.search(
                    r"\b(?:friendlyurl|relativeurl|"
                    r"absoluteurl|shortlinkurl)\b",
                    searchable,
                    re.I,
                )),
                tier=2,
            )

            self._score(
                scores,
                evidence,
                "talentlyft",
                30,
                "TalentLyft SEO metadata field detected",
                bool(re.search(
                    r"\b(?:metatagtitle|metatagdescription|"
                    r"metatagimageurl)\b",
                    searchable,
                    re.I,
                )),
                tier=2,
            )

            self._score(
                scores,
                evidence,
                "talentlyft",
                25,
                "TalentLyft public API reference detected",
                bool(re.search(
                    r"/v2/public/[^/?#]+/jobs",
                    searchable,
                    re.I,
                )),
                tier=2,
            )

            self._score(
                scores,
                evidence,
                "talentlyft",
                20,
                "TalentLyft API/job identifier detected",
                bool(re.search(
                    r"\b(?:talentlyft|jobrequisitionid|"
                    r"websiteid)\b",
                    searchable,
                    re.I,
                )),
                tier=2,
            )

            self._score(
                scores,
                evidence,
                "talentlyft",
                10,
                "TalentLyft terminology detected",
                bool(re.search(
                    r"\btalentlyft\b",
                    searchable,
                    re.I,
                )),
                tier=3,
            )

            # =========================================================
            # PERSONIO
            # =========================================================

            self._score(
                scores,
                evidence,
                "personio",
                70,
                "Personio hosted career domain detected",
                bool(re.search(
                    r"(?:^|[.])jobs\.personio\.(?:de|com)$",
                    hostname,
                    re.I,
                )),
                tier=1,
            )

            self._score(
                scores,
                evidence,
                "personio",
                60,
                "Personio job URL structure detected",
                bool(re.search(
                    r"^/job/\d+/?$",
                    path,
                    re.I,
                ))
                and (
                    "personio" in hostname
                    or "personio" in searchable
                ),
                tier=1,
            )

            self._score(
                scores,
                evidence,
                "personio",
                55,
                "Personio XML career feed detected",
                bool(re.search(
                    r"/xml(?:/|$|\?)",
                    path,
                    re.I,
                ))
                and (
                    "personio" in hostname
                    or "personio" in searchable
                ),
                tier=1,
            )

            self._score(
                scores,
                evidence,
                "personio",
                40,
                "Personio iframe integration identifier detected",
                "personio-iframe" in searchable,
                tier=2,
            )

            self._score(
                scores,
                evidence,
                "personio",
                35,
                "Personio language parameter detected",
                bool(re.search(
                    r"(?:^|&)language=[a-z-]+",
                    query,
                    re.I,
                ))
                and (
                    "personio" in hostname
                    or "personio" in searchable
                ),
                tier=2,
            )

            self._score(
                scores,
                evidence,
                "personio",
                30,
                "Personio career-page reference detected",
                bool(re.search(
                    r"\b(?:jobs\.personio\.(?:de|com)|"
                    r"personio-iframe|"
                    r"personio\.de|"
                    r"personio\.com)\b",
                    searchable,
                    re.I,
                )),
                tier=2,
            )

            self._score(
                scores,
                evidence,
                "personio",
                10,
                "Personio terminology detected",
                bool(re.search(
                    r"\bpersonio\b",
                    searchable,
                    re.I,
                )),
                tier=3,
            )

            # =========================================================
            # PINPOINT
            # =========================================================

            self._score(
                scores,
                evidence,
                "pinpoint",
                70,
                "Pinpoint hosted career domain detected",
                hostname.endswith(".pinpointhq.com")
                or hostname == "pinpointhq.com",
                tier=1,
            )

            self._score(
                scores,
                evidence,
                "pinpoint",
                60,
                "Pinpoint job URL structure detected",
                bool(re.search(
                    r"^/(?:[a-z]{2}(?:-[a-z]{2})?/)?"
                    r"jobs/\d+/?$",
                    path,
                    re.I,
                ))
                and "pinpointhq.com" in hostname,
                tier=1,
            )

            self._score(
                scores,
                evidence,
                "pinpoint",
                55,
                "Pinpoint postings JSON endpoint detected",
                bool(re.search(
                    r"/(?:[a-z]{2}(?:-[a-z]{2})?/)?"
                    r"postings\.json(?:/|$)",
                    path,
                    re.I,
                ))
                and "pinpointhq.com" in hostname,
                tier=1,
            )

            self._score(
                scores,
                evidence,
                "pinpoint",
                55,
                "Pinpoint API jobs endpoint detected",
                bool(re.search(
                    r"/api/v1/jobs(?:/|$)",
                    path,
                    re.I,
                ))
                and "pinpointhq.com" in hostname,
                tier=1,
            )

            self._score(
                scores,
                evidence,
                "pinpoint",
                35,
                "Pinpoint jobs-block anchor detected",
                "js-careers-jobs-block" in searchable,
                tier=2,
            )

            self._score(
                scores,
                evidence,
                "pinpoint",
                30,
                "Pinpoint referral parameter detected",
                bool(re.search(
                    r"(?:^|&)referred_by=",
                    query,
                    re.I,
                ))
                and (
                    "pinpointhq.com" in hostname
                    or "pinpoint" in searchable
                ),
                tier=2,
            )

            self._score(
                scores,
                evidence,
                "pinpoint",
                30,
                "Pinpoint API reference detected",
                bool(re.search(
                    r"\bpinpointhq\.com/api/v1/jobs\b|"
                    r"\bpostings\.json\b",
                    searchable,
                    re.I,
                )),
                tier=2,
            )

            self._score(
                scores,
                evidence,
                "pinpoint",
                10,
                "Pinpoint terminology detected",
                bool(re.search(
                    r"\bpinpoint\b|\bpinpointhq\b",
                    searchable,
                    re.I,
                )),
                tier=3,
            )

            # =========================================================
            # JOBVITE
            # =========================================================

            self._score(
                scores,
                evidence,
                "jobvite",
                70,
                "Jobvite hosted career domain detected",
                hostname == "jobs.jobvite.com"
                or hostname.endswith(".jobs.jobvite.com"),
                tier=1,
            )

            self._score(
                scores,
                evidence,
                "jobvite",
                60,
                "Jobvite job URL structure detected",
                bool(re.search(
                    r"^/[^/?#]+/job/[a-z0-9_-]+/?$",
                    path,
                    re.I,
                ))
                and (
                    hostname == "jobs.jobvite.com"
                    or "jobvite.com" in hostname
                ),
                tier=1,
            )

            self._score(
                scores,
                evidence,
                "jobvite",
                60,
                "Jobvite application URL structure detected",
                bool(re.search(
                    r"^/[^/?#]+/job/[a-z0-9_-]+/apply/?$",
                    path,
                    re.I,
                ))
                and (
                    hostname == "jobs.jobvite.com"
                    or "jobvite.com" in hostname
                ),
                tier=1,
            )

            self._score(
                scores,
                evidence,
                "jobvite",
                45,
                "Jobvite iframe career-site class detected",
                "jv-careersite" in searchable,
                tier=2,
            )

            self._score(
                scores,
                evidence,
                "jobvite",
                40,
                "Jobvite data-careersite attribute detected",
                bool(re.search(
                    r"\bdata-careersite\s*=",
                    html,
                    re.I,
                )),
                tier=2,
            )

            self._score(
                scores,
                evidence,
                "jobvite",
                40,
                "Jobvite data-page job route detected",
                bool(re.search(
                    r'data-page\s*=\s*["\']job/'
                    r'[^"\']+',
                    html,
                    re.I,
                )),
                tier=2,
            )

            self._score(
                scores,
                evidence,
                "jobvite",
                35,
                "Jobvite iframe JavaScript detected",
                bool(re.search(
                    r"jobs\.jobvite\.com/"
                    r"__assets__/scripts/"
                    r"careersite/public/iframe\.js",
                    searchable,
                    re.I,
                )),
                tier=1,
            )

            self._score(
                scores,
                evidence,
                "jobvite",
                30,
                "Jobvite source-tracking parameters detected",
                bool(re.search(
                    r"(?:^|&)"
                    r"__jvs[td]=",
                    query,
                    re.I,
                )),
                tier=2,
            )

            self._score(
                scores,
                evidence,
                "jobvite",
                10,
                "Jobvite terminology detected",
                bool(re.search(
                    r"\bjobvite\b",
                    searchable,
                    re.I,
                )),
                tier=3,
            )

            # =========================================================
            # JAZZHR
            # =========================================================

            self._score(
                scores,
                evidence,
                "jazzhr",
                70,
                "JazzHR applytojob.com domain detected",
                hostname.endswith(".applytojob.com")
                or hostname == "applytojob.com",
                tier=1,
            )

            self._score(
                scores,
                evidence,
                "jazzhr",
                70,
                "JazzHR hosted job URL detected",
                bool(re.search(
                    r"^/apply/[a-z0-9]+(?:/[^/?#]+)?/?$",
                    path,
                    re.I,
                ))
                and "applytojob.com" in hostname,
                tier=1,
            )

            self._score(
                scores,
                evidence,
                "jazzhr",
                65,
                "JazzHR feed endpoint detected",
                bool(re.search(
                    r"/feeds/export/jobs/[^/?#]+/?$",
                    path,
                    re.I,
                ))
                and (
                    "jazz.co" in hostname
                    or "jazzhr" in searchable
                ),
                tier=1,
            )

            self._score(
                scores,
                evidence,
                "jazzhr",
                60,
                "JazzHR app domain detected",
                hostname == "app.jazz.co"
                or hostname.endswith(".app.jazz.co"),
                tier=1,
            )

            self._score(
                scores,
                evidence,
                "jazzhr",
                50,
                "JazzHR widget script detected",
                bool(re.search(
                    r"(?:app\.)?jazz\.co/"
                    r"widgets/buttons/create/",
                    searchable,
                    re.I,
                )),
                tier=2,
            )

            self._score(
                scores,
                evidence,
                "jazzhr",
                35,
                "JazzHR job identifier detected",
                bool(re.search(
                    r"\bjob_[0-9]{8,}_[a-z0-9]+\b",
                    searchable,
                    re.I,
                )),
                tier=2,
            )

            self._score(
                scores,
                evidence,
                "jazzhr",
                30,
                "JazzHR publisher reference detected",
                bool(re.search(
                    r"<publisher>\s*jazz\s*</publisher>|"
                    r"\bpublisherurl\b[^<]*jazz\.co",
                    searchable,
                    re.I,
                )),
                tier=2,
            )

            self._score(
                scores,
                evidence,
                "jazzhr",
                10,
                "JazzHR terminology detected",
                bool(re.search(
                    r"\bjazzhr\b|\bjazz hr\b|\bjazz\.co\b",
                    searchable,
                    re.I,
                )),
                tier=3,
            )

            # =========================================================
            # GENERIC METADATA / JSON-LD
            # =========================================================

            self._score_structured_data(
                soup,
                scores,
                evidence,
            )

            self._score_metadata(
                metadata,
                scores,
                evidence,
            )

            # =========================================================
            # RANK
            # =========================================================

            ranked = sorted(
                scores.items(),
                key=lambda item: item[1],
                reverse=True,
            )

            winner, winner_score = ranked[0]
            second_score = ranked[1][1]

            winner_evidence = [
                item
                for item in evidence
                if item.ats == winner
            ]

            has_strong_signal = any(
                item.tier <= 2
                for item in winner_evidence
            )

            confidence = self._confidence(
                winner_score,
                second_score,
                has_strong_signal,
            )

            detected = (
                winner
                if winner_score >= 25
                and confidence >= 0.55
                and has_strong_signal
                else None
            )

            return DetectionResult(
                input_url=url,
                final_url=final_url,
                detected_ats=detected,
                confidence=confidence,
                scores=scores,
                evidence=sorted(
                    evidence,
                    key=lambda item: (
                        item.tier,
                        -item.points,
                    ),
                ),
                status_code=response.status_code,
            )

        except requests.RequestException as exc:
            return DetectionResult(
                input_url=url,
                final_url=url,
                detected_ats=None,
                confidence=0.0,
                scores=scores,
                evidence=evidence,
                error=str(exc),
            )

    # ================================================================
    # HELPERS
    # ================================================================

    @staticmethod
    def _error_result(
        input_url: str,
        final_url: str,
        status_code: int,
        scores: Dict[str, int],
        evidence: List[Evidence],
        error: str,
    ) -> DetectionResult:
        return DetectionResult(
            input_url=input_url,
            final_url=final_url,
            detected_ats=None,
            confidence=0.0,
            scores=scores,
            evidence=evidence,
            status_code=status_code,
            error=error,
        )

    @staticmethod
    def _score(
        scores: Dict[str, int],
        evidence: List[Evidence],
        ats: str,
        points: int,
        reason: str,
        condition: bool,
        tier: int,
    ) -> None:
        if not condition:
            return

        scores[ats] += points

        evidence.append(
            Evidence(
                ats=ats,
                points=points,
                reason=reason,
                tier=tier,
            )
        )

    @staticmethod
    def _extract_metadata(
        soup: BeautifulSoup,
    ) -> Dict[str, str]:
        metadata: Dict[str, str] = {}

        if soup.title:
            metadata["title"] = soup.title.get_text(
                " ",
                strip=True,
            )

        for tag in soup.find_all("meta"):
            key = (
                tag.get("name")
                or tag.get("property")
                or tag.get("itemprop")
            )

            value = tag.get("content")

            if key and value:
                metadata[str(key).lower()] = str(value)

        return metadata

    @staticmethod
    def _score_metadata(
        metadata: Dict[str, str],
        scores: Dict[str, int],
        evidence: List[Evidence],
    ) -> None:
        combined = " ".join(
            f"{key} {value}"
            for key, value in metadata.items()
        ).lower()

        signals = {
            "greenhouse": "greenhouse",
            "workday": "workday",
            "smartrecruiters": "smartrecruiters",
            "icims": "icims",
            "successfactors": "successfactors",
            "sap successfactors": "successfactors",
            "taleo": "taleo",
            "oracle taleo": "taleo",
            "lever": "lever",
            "teamtailor": "teamtailor",
            "recruitee": "recruitee",
            "tellent recruitee": "recruitee",
            "ashby": "ashby",
            "ashbyhq": "ashby",
            "workable": "workable",
            "bamboohr": "bamboohr",
            "bamboo hr": "bamboohr",
            "talentsoft": "talentsoft",
            "cegid": "talentsoft",
            "comeet": "comeet",
            "onlyfy": "onlyfy",
            "prescreen": "onlyfy",
            "jobbase.io": "onlyfy",
            "breezy": "breezy",
            "breezy hr": "breezy",
            "talentlyft": "talentlyft",
            "personio": "personio",
            "jobs.personio.de": "personio",
            "jobs.personio.com": "personio",
            "pinpoint": "pinpoint",
            "pinpointhq": "pinpoint",
            "jobvite": "jobvite",
            "jobs.jobvite.com": "jobvite",
            "jazzhr": "jazzhr",
            "jazz hr": "jazzhr",
            "jazz.co": "jazzhr",
            "applytojob.com": "jazzhr",
        }

        for needle, ats in signals.items():
            if needle not in combined:
                continue

            scores[ats] += 5

            evidence.append(
                Evidence(
                    ats=ats,
                    points=5,
                    reason=(
                        f"{needle} reference found in metadata"
                    ),
                    tier=3,
                )
            )

    @staticmethod
    def _score_structured_data(
        soup: BeautifulSoup,
        scores: Dict[str, int],
        evidence: List[Evidence],
    ) -> None:
        signals = {
            "greenhouse": "greenhouse",
            "workday": "workday",
            "smartrecruiters": "smartrecruiters",
            "icims": "icims",
            "successfactors": "successfactors",
            "taleo": "taleo",
            "lever": "lever",
            "teamtailor": "teamtailor",
            "recruitee": "recruitee",
            "ashby": "ashby",
            "workable": "workable",
            "bamboohr": "bamboohr",
            "talentsoft": "talentsoft",
            "cegid": "talentsoft",
            "comeet": "comeet",
            "onlyfy": "onlyfy",
            "prescreen": "onlyfy",
            "jobbase.io": "onlyfy",
            "breezy": "breezy",
            "talentlyft": "talentlyft",
            "personio": "personio",
            "jobs.personio.de": "personio",
            "jobs.personio.com": "personio",
            "pinpoint": "pinpoint",
            "pinpointhq": "pinpoint",
            "jobvite": "jobvite",
            "jobs.jobvite.com": "jobvite",
            "jazzhr": "jazzhr",
            "jazz hr": "jazzhr",
            "jazz.co": "jazzhr",
            "applytojob.com": "jazzhr",
        }

        for script in soup.find_all(
            "script",
            attrs={"type": "application/ld+json"},
        ):
            text = script.get_text(
                strip=True
            ).lower()

            for needle, ats in signals.items():
                if needle not in text:
                    continue

                scores[ats] += 5

                evidence.append(
                    Evidence(
                        ats=ats,
                        points=5,
                        reason=(
                            f"{needle} reference found in JSON-LD"
                        ),
                        tier=3,
                    )
                )

    @staticmethod
    def _confidence(
        winner_score: int,
        second_score: int,
        has_strong_signal: bool,
    ) -> float:
        if winner_score <= 0:
            return 0.0

        strength = min(
            winner_score / 100.0,
            1.0,
        )

        margin = (
            (winner_score - second_score)
            / max(winner_score, 1)
        )

        confidence = (
            0.60 * strength
            + 0.30 * margin
        )

        if has_strong_signal:
            confidence += 0.10

        return round(
            max(
                0.0,
                min(1.0, confidence),
            ),
            3,
        )


def print_result(result: DetectionResult) -> None:
    print(f"\nInput URL:    {result.input_url}")
    print(f"Final URL:    {result.final_url}")
    print(f"HTTP status:  {result.status_code}")

    if result.error:
        print(f"Error:        {result.error}")
        return

    print(
        f"Detected ATS: "
        f"{result.detected_ats or 'unknown'}"
    )

    print(
        f"Confidence:   "
        f"{result.confidence:.1%}"
    )

    print("\nScores:")

    for ats, score in sorted(
        result.scores.items(),
        key=lambda item: item[1],
        reverse=True,
    ):
        print(f"  {ats:16} {score}")

    print("\nEvidence:")

    for item in result.evidence:
        print(
            f"  [{item.ats:16}] "
            f"+{item.points:2} "
            f"[Tier {item.tier}] "
            f"{item.reason}"
        )


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(
        description="Detect ATS from a job posting URL"
    )

    parser.add_argument("url")

    args = parser.parse_args()

    detector = ATSDetector()
    result = detector.detect(args.url)

    print_result(result)
