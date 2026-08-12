"""Reading meaning out of a posting URL when the page will not say it.

A URL is the one thing every strategy has. When a board publishes no title --
no JSON-LD, no anchor text, nothing but a link in a sitemap -- the path is all
there is to work with, and how well it is read decides whether the row can be
filtered at all or has to be fetched blind.
"""

from __future__ import annotations

import re
from typing import Optional
from urllib.parse import unquote, urlparse

from job_scraper.detector import ATS_REGISTRY, ATSName, _host_hit

_JOB_WORD = (
    r"(?:jobs?|offres?|offers?|emplois?|postes?|annonces?|"
    r"vacanc(?:y|ies)|positions?|openings?|opportunit(?:y|ies)|"
    r"careers?|carrieres?|stellen?|recrutement)"
)

#: The posting segment. A real posting slug is either hyphenated/underscored
#: ("data-engineer", "ajusteuse_22660") or a bare id ("842306"); a single bare
#: word is a category or listing page. This is what stops the `[\w]*-?` prefix
#: below from swallowing /nos-offres/localisations along with
#: /nos-offres/dev-senior.
_JOB_SLUG = r"(?:[^/?#]*[-_][^/?#]*|\d{3,})"

#: `(?:[\w]+-)?` is the prefix French boards need: without it the job word has
#: to sit directly after a slash, so /nos-offres/... and /offres-emploi/...
#: match nothing and whole boards (Davidson, Crédit Agricole) come back empty.
#: The hyphen is required (not `[\w]*-?`, which let the prefix run straight
#: into the job word with nothing between them -- "external" + "jobs" then
#: read as one compound segment and let /externaljobs/JobDetail/498916, an
#: Avature marketing link, complete a match through the intermediate-segment
#: allowance below).
#:
#: `(?![a-zA-Z])` after the job word closes the same gap on the other side:
#: without it "jobs" inside "externaljobs" still matches, just from a
#: different starting position, and the marketing link comes back. It also
#: rules out an open-ended suffix after the job word: /career-advice/... and
#: /jobs-blog/... are content sections, not postings, and a `[\w-]*` tail
#: greedy enough to reach airfrance's /offre-de-emploi/..._22660.aspx also
#: swallowed "-advice" and "-blog" here. Left as a known gap rather than
#: solved: compound segments like /offre-de-emploi/ or michelin's
#: /job-offer-result-list/ still miss.
#:
#: Measured and rejected, so it is not re-tried:
#: - "join-us"/"nous-rejoindre" as job words. Buys Extia's 20 real postings
#:   and costs 10 false ones -- Atos and Equans both file /join-us/life-at-atos
#:   and /nous-rejoindre/faq-candidats under the same prefix, and the slug
#:   guard cannot tell those from a posting since they are hyphenated too.
#: - Grouping anchors by repeated path shape. On this corpus only Extia clears
#:   a useful threshold, and Equans' /votre-activite/ group clears it too, so
#:   the heuristic nets one real board and one wrong one.
#: Both boards need the API tier instead.
#: `(?:/[^/?#]+){0,2}` lets up to two path segments sit between the job word
#: and the slug -- kering files postings at
#: /en/talent/job-offers/asia/<slug>, mirakl at
#: /company/careers/jobs/mirakl/<id>. Capped at two: wider risks matching a
#: job word that is only in the path for navigation, with the actual posting
#: several sections away.
JOB_URL_RE = re.compile(
    rf"/(?:[\w]+-)?{_JOB_WORD}(?:-d?-?{_JOB_WORD})?(?![a-zA-Z])"
    rf"(?:/[^/?#]+){{0,2}}/{_JOB_SLUG}",
    re.I,
)

#: A path segment that is only an identifier, carrying no words to read. The
#: hex arm needs {8,} rather than a shorter bound: at 4 it eats real English
#: and French words that happen to spell in hex -- "cafe", "dead", "added".
_ID_SEGMENT = re.compile(r"^(?:\d+|[0-9a-f]{8,})$", re.I)

#: A build hash or record id glued to the front of an otherwise real slug, as
#: in /offres/4lxbw5bd1f-data-ingenieur-h-f/.
#:
#: The lookahead requiring a digit is load-bearing. Without it the character
#: class matches any long lowercase word, so the first real word of the title
#: is eaten instead: "associate-real-estate" became "Real Estate" and
#: "senior-python-dev" became "Python Dev" -- and "senior" is a blacklist term,
#: so dropping it silently defeated the filter it was supposed to feed.
_HASH_PREFIX = re.compile(r"^(?=[0-9a-z]*\d)[0-9a-z]{6,}[-_]", re.I)

#: Path segments that shape a URL without describing the posting. Reaching one
#: of these means the walk has climbed past the posting into its collection,
#: and there is nothing further up worth reading.
_GENERIC_SEGMENTS = frozenset({
    "job", "jobs", "offre", "offres", "emploi", "emplois", "career",
    "careers", "carriere", "carrieres", "vacancy", "vacancies", "position",
    "positions", "opening", "openings", "detail", "details", "posting",
    "postings", "stelle", "stellen", "recrutement", "vacature", "vacatures",
})

#: Boards that serve postings as pages leave the extension on the slug, which
#: otherwise arrives as a "Html" word at the end of every title.
_PAGE_EXTENSION = re.compile(r"\.(?:html?|aspx?|php|jsp)$", re.I)


def title_from_url(url: str) -> str:
    """Recover a job title from the posting's URL path.

    Walks the path right to left and returns the first segment that reads like
    a title. That fallback is the whole point: plenty of boards file postings
    at `/fr/jobs/associate-real-estate/76969`, where the last segment is a bare
    record id and the descriptive slug sits one level up.

    The previous implementation returned "" for exactly that shape -- it
    stripped a leading id out of `slug` in place, so its `or slug` fallback
    could only ever fall back to the string it had just emptied. That single
    line produced 39% of all scraped rows with no title at all (7,779 of
    19,793), every one of them from the sitemap strategy.

    Args:
        url: Job posting URL.

    Returns:
        A title in Title Case, or "" when the path genuinely carries no words
        -- `/jobs/843490` on Leroy Merlin and AXA really is all there is. An
        empty return is honest and downstream depends on it: it is what marks
        a row as unjudgeable before it has been fetched.
    """
    for segment in reversed([part for part in urlparse(url).path.split("/")
                             if part]):
        # Percent-decode first, or an escaped separator becomes title text:
        # "associate-real-estate-%26-regulatory" reads as "26" mid-title, and
        # accented French slugs arrive mojibaked.
        slug = _PAGE_EXTENSION.sub("", unquote(segment))

        if _ID_SEGMENT.match(slug):
            continue

        words = [word for word in re.split(r"[-_+\s]+",
                                           _HASH_PREFIX.sub("", slug)) if word]
        title = " ".join(word.capitalize() for word in words)

        if title and title.casefold() not in _GENERIC_SEGMENTS:
            return title

    return ""


def ats_from_host(url: str) -> Optional[ATSName]:
    """Name the ATS from a vendor-owned hostname alone, without fetching.

    The detector reads the page, so a board that blocks it -- Personio answers
    429 and redirects to marketing -- comes back unknown, no feed is tried, and
    both fallbacks then fail on the same blocked HTML. Yet its XML feed serves
    fine, and the hostname already said which vendor it is.

    Deliberately only vendor-owned hosts: an employer's own careers domain says
    nothing about the ATS behind it, which is the whole reason detector.py
    scores evidence instead of matching URLs.

    Args:
        url: Board URL.

    Returns:
        The ATS owning the hostname, or None.
    """
    host = urlparse(url).hostname or ""

    for ats in ATS_REGISTRY:
        if ats.hosts and _host_hit(host, ats.hosts):
            return ats.name

    return None
