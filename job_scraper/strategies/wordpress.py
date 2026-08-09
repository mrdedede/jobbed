"""Strategy 2: read a WordPress careers site through its REST API."""

from __future__ import annotations

import re
from html import unescape
from typing import TYPE_CHECKING, List
from urllib.parse import urljoin, urlparse

from job_scraper.fetching import FEED_MAX_BYTES, dig, fetch_json, first_string
from job_scraper.models import Job

if TYPE_CHECKING:
    from job_scraper.board import Board

#: WordPress registers job boards as a custom post type, and the name is the
#: site owner's choice -- "job" on one board here, "offres" on another. The
#: type list is public, so the name is discovered rather than guessed.
_WP_TYPES = "/wp-json/wp/v2/types"

#: Every WordPress theme ships assets from wp-content, and most expose the REST
#: root as a wp-json link tag. Either is enough to justify the probe below.
_WP_MARKER = re.compile(r"wp-content|wp-json", re.I)
_WP_JOB_TYPE = re.compile(
    r"job|offre|emploi|career|carriere|poste|recrut|vacan", re.I
)

#: WordPress caps per_page at 100.
WP_PAGE = 100
WP_MAX_PAGES = 20


def scrape_wordpress(board: "Board") -> List[Job]:
    """Scrape a WordPress careers site through its REST API.

    Plenty of employer career sites are just WordPress, which publishes every
    custom post type at /wp-json/wp/v2/<type> with a real title and a public
    link. That beats both fallbacks: the sitemap strategy costs one request
    per posting to recover the same title, and the link strategy only ever
    sees whatever the theme happened to put in an anchor.

    Args:
        board: Board to scrape.

    Returns:
        List of jobs, or empty list if this is not WordPress or has no
        job-shaped post type.
    """
    # Costs nothing to check and saves a request on every board that is not
    # WordPress -- 25 of the 35 in the corpus. Nor can it hide a board: the
    # endpoint below is built from the board's own host, so a site whose REST
    # API lives elsewhere was already out of reach for this strategy.
    if not board.html or not _WP_MARKER.search(board.html):
        return []

    base = board.url
    parsed = urlparse(base)
    root = f"{parsed.scheme}://{parsed.netloc}"

    types = fetch_json(board.session, urljoin(root, _WP_TYPES))

    if not isinstance(types, dict):
        return []

    name = next(
        (key for key in types if _WP_JOB_TYPE.search(key)),
        None,
    )

    if not name:
        return []

    endpoint = urljoin(root, f"/wp-json/wp/v2/{name}")
    jobs: List[Job] = []

    for page_number in range(1, WP_MAX_PAGES + 1):
        posts = fetch_json(
            board.session,
            f"{endpoint}?per_page={WP_PAGE}&page={page_number}",
            max_bytes=FEED_MAX_BYTES,
        )

        if not isinstance(posts, list) or not posts:
            break

        for post in posts:
            if not isinstance(post, dict):
                continue

            title = first_string(dig(post, "title.rendered"))
            url = first_string(post.get("link"))

            if not title or not url:
                continue

            jobs.append(Job(
                company=board.company_name,
                # Titles come back HTML-escaped ("Go developer &#8211; Team").
                title=unescape(title),
                url=url,
                via="wordpress",
            ))

        if len(posts) < WP_PAGE:
            break

    return jobs
