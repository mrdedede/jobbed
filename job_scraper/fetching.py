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

import asyncio
import contextlib
import json
import re
import time
import weakref
import xml.etree.ElementTree as ET
from typing import Awaitable, Callable, Dict, List, Optional, Tuple, TypeVar
from urllib.parse import urlparse

import httpx

#: A browser UA, not a bot string: several boards (devoteam, edf, hermes) 403
#: or 502 a self-identifying bot and serve a plain browser fine. Accept-
#: Language matters too -- some of these boards branch their listing by
#: locale before a single job link is emitted.
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                  "AppleWebKit/537.36 (KHTML, like Gecko) "
                  "Chrome/126.0.0.0 Safari/537.36",
    "Accept": "application/json, text/html;q=0.9, */*;q=0.8",
    "Accept-Language": "fr-FR,fr;q=0.9,en-US;q=0.8,en;q=0.7",
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

#: Minimum gap between two requests to the same host.
#:
# ponytail: one flat gap per host plus a per-host in-flight cap, not a token
# bucket. A host sees at most PER_HOST requests at once and one new request
# per REQUEST_DELAY. If a host ever complains, the upgrade is a real
# per-host limiter, not a longer delay.
REQUEST_DELAY = 0.2

#: Requests in flight against one host, and across the whole run. The global
#: cap is what keeps nested fan-out (a board plus its sitemap detail pass)
#: from multiplying load.
PER_HOST = 4
GLOBAL_CONCURRENCY = 100

#: Retry budget for a transient upstream. 429 is in the list because boards
#: behind a CDN rate-limit rather than refuse, and backing off is the whole
#: correct response to being told to slow down.
RETRY_STATUSES = (429, 500, 502, 503, 504)
RETRY_TOTAL = 3
RETRY_BACKOFF = 0.5

#: Ceiling on a server's Retry-After, so one host cannot park a worker.
MAX_RETRY_AFTER = 30

DEFAULT_TIMEOUT = 20

#: A two-letter language code, optionally with a region: the leading path
#: segment boards insert for localised listings ("/fr-fr/careers/...").
LOCALE_RE = re.compile(r"^[a-z]{2}(?:-[a-z]{2})?$", re.I)


def new_client(headers: Optional[Dict[str, str]] = None) -> httpx.AsyncClient:
    """Build the one client a run shares.

    Retrying lives in `request`, not here: httpx has no adapter-level retry.

    Args:
        headers: Headers to apply. Defaults to HEADERS.

    Returns:
        An AsyncClient that follows redirects and pools connections across
        hosts. Close it with `async with` or `aclose()`.
    """
    return httpx.AsyncClient(
        headers=HEADERS if headers is None else headers,
        follow_redirects=True,
        max_redirects=10,
        timeout=DEFAULT_TIMEOUT,
        limits=httpx.Limits(max_connections=GLOBAL_CONCURRENCY,
                            max_keepalive_connections=20),
    )


class _Host:
    """Politeness state for one host."""

    def __init__(self) -> None:
        self.slots = asyncio.Semaphore(PER_HOST)
        self.next_at = 0.0


class _Gate:
    """Limiter state for one event loop."""

    def __init__(self) -> None:
        self.total = asyncio.Semaphore(GLOBAL_CONCURRENCY)
        self.hosts: Dict[str, _Host] = {}


# Keyed on the running loop: asyncio primitives belong to one loop, and every
# test (and every asyncio.run) gets a fresh one.
_gates: "weakref.WeakKeyDictionary" = weakref.WeakKeyDictionary()


def _gate() -> _Gate:
    loop = asyncio.get_running_loop()
    found = _gates.get(loop)

    if found is None:
        found = _gates[loop] = _Gate()

    return found


@contextlib.asynccontextmanager
async def _polite(url: str):
    """Hold a per-host slot and the global one, spacing hosts' requests out."""
    gate = _gate()
    host = gate.hosts.setdefault(urlparse(url).hostname or "", _Host())

    # Host first: a worker queued behind a busy host must not sit on one of
    # the global slots while other hosts could use it.
    async with host.slots, gate.total:
        now = time.monotonic()
        wait = host.next_at - now
        host.next_at = max(now, host.next_at) + REQUEST_DELAY

        if wait > 0:
            await asyncio.sleep(wait)

        yield


def _retry_wait(response: httpx.Response, attempt: int) -> float:
    """Seconds to back off: the server's Retry-After if sane, else exponential."""
    header = response.headers.get("Retry-After", "")

    if header.isdigit():
        return min(int(header), MAX_RETRY_AFTER)

    return RETRY_BACKOFF * 2 ** attempt


T = TypeVar("T")


async def request(client: httpx.AsyncClient, url: str,
                  reader: Callable[[httpx.Response], Awaitable[T]],
                  method: str = "get", timeout: float = DEFAULT_TIMEOUT,
                  **kwargs) -> T:
    """Polite, retrying request; `reader` consumes the body while it is open.

    Retries RETRY_STATUSES and transport errors with backoff. A status that
    is still retryable after the budget is handed to `reader` like any other
    response, so the caller decides what a 503 means.

    Args:
        client: Shared AsyncClient.
        url: URL to request.
        reader: Coroutine given the streamed response.
        method: HTTP method.
        timeout: Per-phase timeout in seconds.
        **kwargs: Passed to client.stream (headers, data, json, params).

    Returns:
        Whatever `reader` returns.

    Raises:
        httpx.HTTPError: Transport failure after the retry budget.
    """
    for attempt in range(RETRY_TOTAL + 1):
        last = attempt == RETRY_TOTAL

        try:
            async with _polite(url), client.stream(
                method.upper(), url, timeout=timeout, **kwargs
            ) as response:
                if response.status_code not in RETRY_STATUSES or last:
                    return await reader(response)

                wait = _retry_wait(response, attempt)
        except httpx.TransportError:
            if last:
                raise

            wait = RETRY_BACKOFF * 2 ** attempt

        # Outside the slot, so a backing-off request does not hold it.
        await asyncio.sleep(wait)

    raise AssertionError("unreachable")  # pragma: no cover


async def read_body(response: httpx.Response, max_bytes: int) -> bytes:
    """Read at most `max_bytes` of a streamed, already-decoded body."""
    chunks: List[bytes] = []
    size = 0

    async for chunk in response.aiter_bytes():
        chunks.append(chunk)
        size += len(chunk)

        if size >= max_bytes:
            break

    return b"".join(chunks)[:max_bytes]


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
        response: The httpx Response the bytes came from.
        raw: Body bytes already read off the wire.

    Returns:
        Decoded text, with undecodable bytes replaced rather than raising.
    """
    declared = "charset=" in response.headers.get("Content-Type", "").lower()
    encoding = response.encoding if declared else "utf-8"

    return raw.decode(encoding or "utf-8", errors="replace")


async def fetch(session, url: str, timeout: int = DEFAULT_TIMEOUT,
                method: str = "get", max_bytes: int = MAX_FETCH_BYTES,
                **kwargs) -> Optional[str]:
    """Best-effort HTTP request returning text or None.

    Args:
        session: Shared httpx.AsyncClient.
        url: URL to fetch.
        timeout: Request timeout in seconds.
        method: HTTP method (get, post, etc).
        max_bytes: Maximum body size to read.
        **kwargs: Additional arguments to pass to the request.

    Returns:
        Response body as text, or None on any error, a non-200 status, or a
        body that is not textual.
    """
    async def read(response: httpx.Response) -> Optional[str]:
        if response.status_code != 200:
            return None

        if not is_textual(response.headers.get("Content-Type", "")):
            return None

        return decode_response(response, await read_body(response, max_bytes))

    try:
        return await request(session, url, read, method=method,
                             timeout=timeout, **kwargs)
    except (httpx.HTTPError, httpx.InvalidURL):
        return None


async def fetch_json(session, url: str, **kwargs) -> Optional[object]:
    """Fetch a URL and parse the body as JSON.

    Args:
        session: Shared httpx.AsyncClient.
        url: URL to fetch.
        **kwargs: Additional arguments passed to fetch.

    Returns:
        The decoded JSON value, or None if the fetch failed or the body was
        not valid JSON.
    """
    body = await fetch(session, url, **kwargs)

    if not body:
        return None

    try:
        return json.loads(body)
    except ValueError:
        return None


async def fetch_xml_items(session, url: str, tag: str,
                          **kwargs) -> Optional[List[dict]]:
    """Fetch an XML feed and flatten each posting element into a dict.

    Personio and JazzHR publish XML where every posting is one element whose
    children are flat text fields. Flattening those to dicts lets the whole
    JSON feed engine -- `dig`, `first_string`, `link_url` -- read them
    unchanged, which is why neither vendor needs a scraper function.

    Args:
        session: Shared httpx.AsyncClient.
        url: Feed URL.
        tag: Element name holding one posting (e.g. "position", "item").
        **kwargs: Additional arguments passed to the fetch.

    Returns:
        List of dicts (one per posting), or None on fetch/parse failure.
    """
    body = await fetch(session, url, **kwargs)

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
