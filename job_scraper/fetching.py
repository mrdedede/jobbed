"""Shared HTTP fetching and JSON-shape helpers.

These lived in ``board_scraper`` behind underscore names, and ``post_scraper``
imported six of them anyway. A private name that two modules import is not
private -- it is shared infrastructure that was never given a home. This is the
home, and the names are public.

Everything here is best-effort by design: a fetch that fails returns ``None``
rather than raising, so one dead board cannot take down a run of a hundred.

Named ``fetching`` rather than the obvious ``http``: running a script by path
(``python job_scraper/main_scraper.py``, which this project supports and
documents) puts ``job_scraper/`` on sys.path, and an ``http`` module there
shadows the standard library package of that name. urllib3 does
``from http.client import ...`` at import, so requests stops importing and
every entry point dies before it starts.
"""

import json
import re
import time
import xml.etree.ElementTree as ET
from typing import Dict, List, Optional, Tuple
from urllib.parse import urlparse

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; JobResearchBot/1.0)",
    "Accept": "application/json, text/html;q=0.9, */*;q=0.8",
}

MAX_FETCH_BYTES = 2_000_000

# Feeds are whole boards in one response and run far larger than a page: a
# single JazzHR export measured 2.7 MB. At MAX_FETCH_BYTES the body is cut
# mid-document, the parse fails, and the board silently falls through to the
# sitemap looking like it had no feed at all.
FEED_MAX_BYTES = 20_000_000

#: Content types worth handing to a parser. Without this a PDF reaches
#: BeautifulSoup, which does not refuse it -- it returns a page of binary
#: noise that then scores as a job description.
TEXTUAL_TYPES = (
    "text/",
    "application/xhtml",
    "application/xml",
    "application/json",
    "+xml",
    "+json",
)

#: Pause before each request. Deliberately crude.
#:
# ponytail: one flat sleep, not a per-host token bucket. Every caller that runs
# concurrently does so against a single host, so N workers cap that host at
# N/REQUEST_DELAY requests per second -- 20/s at the shipped 4 workers. If a
# host ever complains, the upgrade is a per-host limiter, not a longer sleep.
REQUEST_DELAY = 0.2

#: Retry budget for a transient upstream. 429 is in the list because boards
#: behind a CDN rate-limit rather than refuse, and backing off is the whole
#: correct response to being told to slow down.
RETRY_STATUSES = (429, 500, 502, 503, 504)
RETRY_TOTAL = 3
RETRY_BACKOFF = 0.5

#: A two-letter language code, optionally with a region: the leading path
#: segment boards insert for localised listings ("/fr-fr/careers/...").
LOCALE_RE = re.compile(r"^[a-z]{2}(?:-[a-z]{2})?$", re.I)


def new_session(headers: Optional[Dict[str, str]] = None) -> requests.Session:
    """Build a session that retries transient failures with backoff.

    Args:
        headers: Headers to apply. Defaults to HEADERS.

    Returns:
        A requests Session with a retrying adapter mounted on http and https.
    """
    session = requests.Session()
    session.headers.update(HEADERS if headers is None else headers)

    retry = Retry(
        total=RETRY_TOTAL,
        backoff_factor=RETRY_BACKOFF,
        status_forcelist=RETRY_STATUSES,
        allowed_methods=frozenset({"GET", "POST"}),
        raise_on_status=False,
    )
    adapter = HTTPAdapter(max_retries=retry)
    session.mount("http://", adapter)
    session.mount("https://", adapter)

    return session


def is_textual(content_type: str) -> bool:
    """Report whether a Content-Type is something a parser should be given.

    An absent Content-Type is accepted: some boards send none, and refusing
    them would cost real coverage for a header that is merely advisory.

    Args:
        content_type: Raw Content-Type header value, possibly empty.

    Returns:
        True if the body is worth parsing.
    """
    if not content_type:
        return True

    lowered = content_type.lower()

    return any(kind in lowered for kind in TEXTUAL_TYPES)


def decode_response(response, raw: bytes) -> str:
    """Decode a response body, preferring the document's own charset.

    response.encoding is ISO-8859-1 for any text/* that declares no charset --
    RFC 2616's default, which requests still honours and HTML5 does not.
    Trusting it turned Scalian's and Sopra Steria's accented titles into
    "DÃ©veloppeur"; both declare utf-8 in a <meta> tag the header never
    mentions.

    Args:
        response: The requests Response the bytes came from.
        raw: Body bytes already read off the wire.

    Returns:
        Decoded text, with undecodable bytes replaced rather than raising.
    """
    declared = "charset=" in response.headers.get("Content-Type", "").lower()
    encoding = response.encoding if declared else "utf-8"

    return raw.decode(encoding or "utf-8", errors="replace")


def fetch(session, url: str, timeout: int = 20,
          method: str = "get", max_bytes: int = MAX_FETCH_BYTES,
          **kwargs) -> Optional[str]:
    """Best-effort HTTP request returning text or None.

    Args:
        session: Requests session.
        url: URL to fetch.
        timeout: Request timeout in seconds.
        method: HTTP method (get, post, etc).
        max_bytes: Maximum body size to read.
        **kwargs: Additional arguments to pass to session method.

    Returns:
        Response body as text, or None on any error, a non-200 status, or a
        body that is not textual.
    """
    time.sleep(REQUEST_DELAY)

    try:
        with getattr(session, method)(
            url, timeout=timeout, allow_redirects=True, stream=True, **kwargs
        ) as response:
            if response.status_code != 200:
                return None

            if not is_textual(response.headers.get("Content-Type", "")):
                return None

            raw = response.raw.read(max_bytes, decode_content=True)

            return decode_response(response, raw)
    except requests.RequestException:
        return None


def fetch_json(session, url: str, **kwargs) -> Optional[object]:
    """Fetch a URL and parse the body as JSON.

    Args:
        session: Requests session.
        url: URL to fetch.
        **kwargs: Additional arguments passed to fetch.

    Returns:
        The decoded JSON value, or None if the fetch failed or the body was
        not valid JSON.
    """
    body = fetch(session, url, **kwargs)

    if not body:
        return None

    try:
        return json.loads(body)
    except ValueError:
        return None


def fetch_xml_items(session, url: str, tag: str,
                    **kwargs) -> Optional[List[dict]]:
    """Fetch an XML feed and flatten each posting element into a dict.

    Personio and JazzHR publish XML where every posting is one element whose
    children are flat text fields. Flattening those to dicts lets the whole
    JSON feed engine -- `dig`, `first_string`, `link_url` -- read them
    unchanged, which is why neither vendor needs a scraper function.

    Args:
        session: Requests session.
        url: Feed URL.
        tag: Element name holding one posting (e.g. "position", "item").
        **kwargs: Additional arguments passed to the fetch.

    Returns:
        List of dicts (one per posting), or None on fetch/parse failure.
    """
    body = fetch(session, url, **kwargs)

    if not body:
        return None

    try:
        root = ET.fromstring(body)
    except ET.ParseError:
        return None

    # ponytail: leaf text only, so a nested wrapper (Personio's
    # <additionalOffices>) flattens to "". The primary <office> is what `place`
    # reads -- revisit only if a feed puts a needed field behind a wrapper.
    return [
        {child.tag: (child.text or "").strip() for child in node}
        for node in root.iter(tag)
    ]


def dig(node: object, path: str) -> object:
    """Navigate nested dict by dotted path, handling list wrappers.

    Args:
        node: JSON-like object to traverse.
        path: Dotted path (e.g., "items.0.name").

    Returns:
        Value at path, or None if path doesn't exist.
    """
    if not path:
        return node

    for key in path.split("."):
        if isinstance(node, list):
            node = node[0] if node else None

        if not isinstance(node, dict):
            return None

        node = node.get(key)

    return node


def walk_strings(node: object, depth: int = 0):
    """Recursively yield strings from nested JSON structure.

    Args:
        node: JSON-like object (dict, list, or string).
        depth: Current recursion depth.

    Yields:
        String values found in the structure.
    """
    if depth > 12:
        return

    if isinstance(node, str):
        yield node
    elif isinstance(node, dict):
        for value in node.values():
            yield from walk_strings(value, depth + 1)
    elif isinstance(node, list):
        for value in node:
            yield from walk_strings(value, depth + 1)


def first_string(node: object) -> Optional[str]:
    """Extract first non-empty string from nested structure.

    Args:
        node: JSON-like object to search.

    Returns:
        First non-empty string found, or None.
    """
    for value in walk_strings(node):
        text = value.strip()

        if text:
            return text

    return None


def walk_jobpostings(node: object, depth: int = 0):
    """Yield every dict in a JSON tree with @type matching JobPosting.

    Args:
        node: JSON object to traverse (dict, list, or scalar).
        depth: Current recursion depth; stops at 12 to avoid infinite loops.

    Yields:
        Dicts with @type containing "jobposting" (case-insensitive).
    """
    if depth > 12:
        return

    if isinstance(node, dict):
        kind = node.get("@type")

        if "jobposting" in str(kind).lower():
            yield node

        for value in node.values():
            yield from walk_jobpostings(value, depth + 1)
    elif isinstance(node, list):
        for value in node:
            yield from walk_jobpostings(value, depth + 1)


def jsonld_nodes(soup) -> List[object]:
    """Parse every JSON-LD script block on a page.

    This is the cheap way to reach a posting's structured data. The alternative
    -- `detector.extract()` -- BeautifulSoup-parses the page and then walks up
    to 20,000 elements collecting classes, ids, data-attrs, anchors and script
    URLs, all to read one JSON-LD block. That work is what detection needs and
    what an extractor does not.

    Args:
        soup: Parsed posting page.

    Returns:
        The decoded JSON value of each parseable ld+json block, in page order.
        Unparseable blocks are skipped -- a board with one broken script must
        not lose the others.
    """
    nodes = []

    for tag in soup.find_all("script", attrs={"type": "application/ld+json"}):
        try:
            nodes.append(json.loads(tag.get_text(strip=True)))
        except (ValueError, TypeError):
            continue

    return nodes


def jobposting_place(node: dict) -> Optional[str]:
    """Read a posting's location out of its JSON-LD node.

    Locality is what the feeds report; region is what is left when a board
    omits it.

    Args:
        node: A JobPosting JSON-LD node.

    Returns:
        The location string, or None if the node names neither.
    """
    return (
        first_string(dig(node, "jobLocation.address.addressLocality"))
        or first_string(dig(node, "jobLocation.address.addressRegion"))
    )


def workday_endpoint(url: str) -> Optional[Tuple[str, str, List[str]]]:
    """Derive a Workday tenant's JSON API location from a board or job URL.

    Workday serves every tenant from `/wday/cxs/{tenant}/{site}/`, both for the
    board listing and for individual postings, so the board scraper and the
    detail scraper need exactly the same parsing to find it. They differ only
    in how much of the path they keep: the board stops at the site, a posting
    carries its own trailing segments.

    Args:
        url: Any Workday URL under the tenant's host.

    Returns:
        Tuple of (root, tenant, segments), where segments is the path with any
        locale prefix removed and segments[0] is the site. None if the URL
        carries no tenant host or no path at all.
    """
    parsed = urlparse(url)
    tenant = parsed.hostname.split(".")[0] if parsed.hostname else ""
    segments = [part for part in parsed.path.split("/") if part]

    # Localised boards prefix the site with a language code that is not part
    # of the API path.
    if segments and LOCALE_RE.match(segments[0]):
        segments = segments[1:]

    if not tenant or not segments:
        return None

    return f"{parsed.scheme}://{parsed.hostname}", tenant, segments
