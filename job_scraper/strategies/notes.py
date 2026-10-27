"""Research notes on the ATS platforms that still have no scraper.

These were 17 functions that raised NotImplementedError so that the caller
could catch it and return []. The raise-to-be-caught was doing nothing a
missing dictionary key does not already do -- but the notes were, and are, the
most valuable part: each records what was probed, on what date, and exactly
which wall it hit, so nobody re-runs a dead end.

Every note is preserved verbatim from those stubs.
"""

from typing import Dict

from job_scraper.detector import ATSName

#: ATS -> why it has no scraper yet, and what a future attempt should know.
#: Presence here is documentation only; it never affects control flow.
VENDOR_NOTES: Dict[ATSName, str] = {
    ATSName.TEAMTAILOR: (
        "Public API needs an API key. The hosted board is server-rendered, so "
        "sitemap/links already do well here -- low priority."
    ),
    ATSName.JOBVITE: (
        "Board HTML at /{token}/search; no public JSON feed. Probed "
        "2026-08-09: jobs.jobvite.com serves the same 'Job Seeker FAQs' page "
        "for any unknown tenant, so a wrong slug looks like a 200 rather than "
        "a 404 -- do not trust a non-empty response as proof the tenant "
        "exists. Blocked on capturing a real tenant board first."
    ),
    ATSName.TALENTLYFT: (
        "Board JSON is behind the widget; shape unconfirmed."
    ),
    ATSName.DIGITALRECRUITERS: (
        "Probed 2026-08-09. The board metadata endpoint is public -- "
        "api.digitalrecruiters.com/careers/v1/careers-sites/{board_host} "
        "returns 200 with the site config, keyed by the careers hostname "
        "rather than by any token in the path. The postings themselves are "
        "under /public/v1/careers-sites/{board_host}/job-ads, which answers "
        "403 'You're not allowed to access this resource'. The page's "
        "dr-lkey-token header and both inline tokens were tried against it "
        "and all still 403, so the credential is not published on the board "
        "page. Blocked until a browser session is captured; the generic path "
        "handles it meanwhile."
    ),
    ATSName.ONLYFY: (
        "No public feed found; hosted board is server-rendered."
    ),
    ATSName.SOFTGARDEN: "No public feed found.",
    ATSName.HIBOB: (
        "No public feed found, and no JOB_PATH row either: the corpus "
        "fixtures carry zero same-host job anchors, so the listing is "
        "rendered client-side and there is nothing for scrape_links to "
        "filter. Postings themselves are /jobs/{uuid}. Needs a renderer "
        "before either strategy can see them."
    ),

    # Enterprise platforms. These run on the employer's own domain with no
    # tenant token in the URL, so each needs its own discovery step before any
    # feed can be addressed. Expect real work, not a FEEDS row.
    ATSName.ICIMS: (
        "Paged HTML board; iCIMS exposes no public JSON feed."
    ),
    ATSName.SUCCESSFACTORS: (
        "OData API needs credentials; the public path is the career-site "
        "HTML. Probed 2026-08-09: jobs.hr.cloud.sap does not resolve on its "
        "own -- the registry hosts are suffixes for per-customer subdomains, "
        "not reachable boards, so this needs a real customer career site "
        "captured first."
    ),
    ATSName.TALEO: (
        "careersection HTML with POST-driven paging. Probed 2026-08-09: "
        "taleo tenants live on per-customer hosts ({tenant}.taleo.net) and "
        "none could be reached without one from a real board, so the paging "
        "contract is still unverified. JOB_PATH[TALEO] covers jobdetail.ftl "
        "links meanwhile."
    ),
    ATSName.TALENTSOFT: (
        "Probed 2026-08-09. /api/v1/offersummaries is NOT callable on the "
        "employer's domain -- Feu Vert (confirmed Talentsoft via its inline "
        "TALENTSOFT-FRONT-OFFICE config) serves its SPA shell for that path "
        "and every other unknown one, so the detector matching the path says "
        "only that the SPA calls it, not that we can. Cegid's own docs put "
        "the public contract at api/v2/offersummaries behind partner "
        "credentials. Needs a real Talentsoft-hosted board captured first; "
        "the *-careers.talentsoft.com pattern does not resolve."
    ),
    ATSName.AVATURE: (
        "Employer-hosted templates; no uniform feed. JOB_PATH[AVATURE] covers "
        "the board instead, and matters more than most: without it the "
        "generic shape matches no Avature posting at all and returns "
        "marketing pages instead."
    ),
    ATSName.PHENOM: (
        "Employer-hosted; ph-widget JSON varies per deployment. Like hibob, "
        "the corpus fixture has no same-host job anchors -- the results list "
        "is drawn client-side, so there is no JOB_PATH row worth adding."
    ),
    ATSName.RADANCY: (
        "TalentBrew employer-hosted; no uniform feed. Unlike the others here "
        "the board is server-rendered, so JOB_PATH[RADANCY] handles it: "
        "verified against the synopsys fixtures, which drop the /search-jobs "
        "style collection pages the generic shape lets through."
    ),
    ATSName.TALEEZ: (
        "Probed 2026-08-20 (gandi.taleez.com): plain curl fetch returns only "
        "an Angular app shell (main-*.js bundle), zero server-rendered "
        "anchors or embedded JSON, and no discoverable API endpoint "
        "referenced in the bundle's script srcs. Needs a captured browser "
        "session's XHR traffic to find the real job-search endpoint before "
        "any feed/JOB_PATH work is possible."
    ),
    ATSName.FLATCHR: (
        "Probed 2026-08-20 (careers.flatchr.io/company/divalto/): Next.js "
        "SSR page carries a __NEXT_DATA__ blob, but its pageProps is only "
        "i18n translation strings -- no job data server-side. The listing is "
        "fetched client-side after load. Falls through to --render "
        "meanwhile; once rendered, a JOB_PATH row may be enough since the "
        "page is otherwise ordinary anchor markup."
    ),
    ATSName.JOBPOSTINGPRO: (
        "Re-probed 2026-10-26: the company pages (societe-ippon+technologies, "
        "societe-act+digital+france, careers-meent+life+sciences) do list "
        "their postings server-side, as /emploi-<id>-<n> anchors (31 on "
        "Ippon, 46 on act digital) that the generic shape rejected because "
        "the job word and the id share one segment. No feed found; "
        "JOB_PATH[JOBPOSTINGPRO] reads them through scrape_links."
    ),
    ATSName.GESTMAX: (
        "Probed 2026-10-26 (recrutement.loire-atlantique.fr/search, "
        "recrutement.strasbourg.eu/search): server-rendered listing, every "
        "posting an absolute /<offer id>/<lang id>/<slug> anchor with no job "
        "word in it. No feed or API seen. JOB_PATH[GESTMAX] reads them; "
        "detection rests on the gestmax-container markup, since tenants run "
        "on their own domains."
    ),
    ATSName.KISSMYJOB: (
        "Probed 2026-08-20 (career.kissmyjob.com/1/jobs): plain curl fetch "
        "returns only an Angular app shell (main-*.js bundle), zero "
        "server-rendered anchors or embedded JSON. Same wall as Taleez; "
        "needs a captured browser session to find the real API."
    ),
    ATSName.ORACLE_FUSION: (
        "Probed 2026-08-20 (iadugs.fa.ocs.oraclecloud.com/hcmUI/"
        "CandidateExperience/...): pure Angular SPA (main-minimal.js from "
        "static.oracle.com), zero server-rendered content. Employer-hosted "
        "with a per-tenant subdomain, so there is no single vendor host to "
        "match -- registry entry is assets-only (oraclecloud.com) plus a "
        "path_re on the /hcmUI/CandidateExperience/ URL shape shared across "
        "tenants. Oracle Fusion Recruiting does have a real job-search REST "
        "API, but it needs a per-tenant finder/site ID that is not present "
        "in the static page -- same discovery-step shape as comeet.py, "
        "blocked until a live session capture provides one."
    ),
    ATSName.CORNERSTONE: (
        "Probed 2026-08-20 (hris-suez.csod.com/ux/ats/careersite/...): "
        "static fetch returns only the player-career-site JS bundle's asset "
        "links (CSS/JS), zero job content -- Cornerstone's careersite "
        "widget is fully client-rendered. Registry entry is assets-only "
        "(csod.com); no path shape confirmed yet since the /player-career-"
        "site/ path seen is the JS bundle's own asset path, not a posting "
        "URL. Needs a captured browser session before any feed/JOB_PATH "
        "work is possible."
    ),
    ATSName.APEC: (
        "Probed 2026-10-27: the search page and the JSON search service "
        "behind it (POST /cms/webservices/rechercheOffre) both answer 403 "
        "with a DataDome captcha, from curl and from fetching.new_client "
        "alike. No public API; APEC gives offer access to partners under an "
        "agreement only. Not scraped by decision, and not to be worked "
        "around: board.REFUSES_BOTS skips these boards and diagnose reports "
        "them as bot-protected. A reader needs a partner feed first -- or "
        "APEC offers via France Travail's API, if it republishes them."
    ),
}
