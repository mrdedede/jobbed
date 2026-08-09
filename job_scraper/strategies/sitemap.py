"""Strategy 3: enumerate postings from the site's sitemap, then read each one.

This carries 71% of every row scraped, and it is also the expensive path: the
sitemap gives URLs and nothing else, so a title costs one request per posting.
"""

from __future__ import annotations

import re
from concurrent.futures import ThreadPoolExecutor
from typing import TYPE_CHECKING, Dict, List, Optional, Tuple
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup

from job_scraper.fetching import (
    fetch,
    jobposting_place,
    jsonld_nodes,
    walk_jobpostings,
    first_string,
)
from job_scraper.models import Job
from job_scraper.urls import JOB_URL_RE, title_from_url

if TYPE_CHECKING:
    from job_scraper.board import Board

# Budget for sitemap discovery requests.
MAX_SITEMAP_REQUESTS = 4

# Minimum job URLs to treat as a valid sitemap.
MIN_JOB_URLS = 3

# Maximum detail postings to fetch individually (one request each).
MAX_DETAIL = 200

#: Threads for the detail pass. Lower than post_scraper's 8 on purpose: every
#: request here goes to one host, where post_scraper's are spread across all of
#: them. See fetching.REQUEST_DELAY for the resulting ceiling.
SITEMAP_WORKERS = 4

LOC_RE = re.compile(r"<loc>\s*([^<\s]+)\s*</loc>", re.I)


def job_urls_from_sitemap(xml: str) -> List[str]:
    """Extract job posting URLs from sitemap XML.

    Args:
        xml: Sitemap XML content.

    Returns:
        List of posting URLs matching job URL pattern.
    """
    seen: Dict[str, None] = {}

    for url in LOC_RE.findall(xml):
        if JOB_URL_RE.search(urlparse(url).path or ""):
            seen.setdefault(url, None)

    return list(seen)


def _nested_sitemaps(xml: str) -> List[str]:
    """Find job-related child sitemaps from sitemap index.

    Args:
        xml: Sitemap index XML content.

    Returns:
        List of child sitemap URLs mentioning jobs.
    """
    return [
        url
        for url in LOC_RE.findall(xml)
        if url.lower().endswith(".xml")
        and re.search(r"job|offre|emploi|vacanc|career|stellen", url, re.I)
    ]


def _find_sitemap_jobs(session, url: str) -> Optional[Tuple[str, List[str]]]:
    """Discover and crawl sitemaps for job URLs.

    Args:
        session: Requests session.
        url: Starting URL to search for sitemaps.

    Returns:
        Tuple of (sitemap_url, job_urls) or None if no jobs found.
    """
    parsed = urlparse(url)
    root = f"{parsed.scheme}://{parsed.netloc}"
    budget = MAX_SITEMAP_REQUESTS

    queue = [
        urljoin(root, "/sitemap.xml"),
        urljoin(root, "/sitemap_index.xml"),
    ]
    # Children of a sitemap index. Drained before robots.txt is tried: a
    # `sitemap-jobs.xml` we already know about beats re-reading robots only to
    # be pointed back at the index we just parsed.
    nested: List[str] = []
    seen: set = set()
    robots_tried = False

    while budget > 0:
        if not queue:
            if nested:
                queue, nested = nested, []
            elif not robots_tried:
                robots_tried = True
                budget -= 1
                robots = fetch(session, urljoin(root, "/robots.txt"))
                queue = [
                    urljoin(root, line)
                    for line in re.findall(
                        r"(?im)^\s*sitemap:\s*(\S+)", robots or ""
                    )
                ]
            else:
                return None

            continue

        target = queue.pop(0)

        if target in seen:
            continue

        seen.add(target)
        budget -= 1
        xml = fetch(session, target)

        if not xml:
            continue

        jobs = job_urls_from_sitemap(xml)

        if len(jobs) >= MIN_JOB_URLS:
            return target, jobs

        nested.extend(_nested_sitemaps(xml))

    return None


def posting_fields(html: str) -> Tuple[Optional[str], Optional[str]]:
    """Extract title and place from a posting page's JobPosting JSON-LD.

    Args:
        html: Posting HTML.

    Returns:
        Tuple of (title, place), or (None, None) if the page publishes no
        JobPosting with a title.
    """
    soup = BeautifulSoup(html, "html.parser")

    for node in walk_jobpostings(jsonld_nodes(soup)):
        title = first_string(node.get("title"))

        if title:
            return title, jobposting_place(node)

    return None, None


def _detail(session, url: str) -> Tuple[Optional[str], Optional[str]]:
    """Fetch one posting and read its structured fields.

    Args:
        session: Requests session.
        url: Posting URL.

    Returns:
        Tuple of (title, place); both None if the page is dead or bare.
    """
    html = fetch(session, url)

    return posting_fields(html) if html else (None, None)


def scrape_sitemap(board: "Board") -> List[Job]:
    """Enumerate postings from a sitemap, then read each one's JSON-LD.

    The detail pass runs on a small thread pool. It was serial, one blocking
    request per posting across every sitemap board, which made it the dominant
    wall-clock cost of an entire run -- up to ~6,400 round trips end to end.
    `pool.map` yields in the calling thread and in submission order, so the
    result still follows sitemap order and needs no lock.

    Args:
        board: Board instance with session and URL.

    Returns:
        List of Job results, or empty list if no sitemap found.
    """
    found = _find_sitemap_jobs(board.session, board.url)

    if found is None:
        return []

    _, job_urls = found

    # One request per posting, so the cap is the real cost ceiling. Past it we
    # still emit the job, just with a slug title.
    detailed = job_urls[:MAX_DETAIL]

    if detailed:
        with ThreadPoolExecutor(max_workers=SITEMAP_WORKERS) as pool:
            fields = list(pool.map(
                lambda url: _detail(board.session, url), detailed
            ))
    else:
        fields = []

    # Pad so the zip below covers every URL, detailed or not.
    fields.extend((None, None) for _ in range(len(job_urls) - len(fields)))

    return [
        Job(
            company=board.company_name,
            title=title or title_from_url(url),
            url=url,
            place=place,
            via="sitemap",
        )
        for url, (title, place) in zip(job_urls, fields)
    ]
