"""Read each filtered posting's own page for title, description and place.

The board stage answers "what postings exist?"; this one answers "what does
this page say?". The URL is already known, so there is no enumeration -- just a
per-page ladder, cheapest first:

    workday -> JSON-LD JobPosting -> <main> -> de-chromed <body>

Measured against one live posting per ATS and per company in the corpus:
JSON-LD covers ~68% of rows, <main> covers most of the rest (successfactors,
radancy, avature, hibob), and Workday needs its own API call because the page
is a JS shell. The <body> rung exists for the plain WordPress/AEM career sites
(inetum, alteca, sibylone, danone, benin digital) that have neither -- they
share no class or id worth matching, so the whole de-chromed page is the
honest answer. Class/id heuristics ("job-description", "posting", ...) matched
nothing on any of these boards and are deliberately absent.

Usage:
    python job_scraper/post_scraper.py [--limit N] [--workers N] [--no-resume]
    python -m job_scraper.post_scraper [--limit N] [--workers N] [--no-resume]

Both work, and the file is named post_scraper rather than job_scraper for the
first one's sake: running a file puts its own directory on sys.path, so a
module sharing its package's name shadows that package and the absolute
imports below resolve back to this very file. By-path running also needs the
project installed -- `pip install -e .` -- since the repo root is not on
sys.path either way.
"""

from __future__ import annotations

import csv
import json
import re
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass
from html import unescape
from pathlib import Path
from typing import Dict, Optional
from urllib.parse import urlparse

import requests
from bs4 import BeautifulSoup

from job_scraper.board_scraper import (
    HEADERS,
    _LOCALE_RE,
    _dig,
    _fetch,
    _fetch_json,
    _first_string,
    _walk_jobpostings,
)

ROOT = Path(__file__).resolve().parent.parent
FILTERED_JOBS = ROOT / "temp" / "filtered_file.csv"
DETAILED_JOBS = ROOT / "temp" / "detailed_jobs.csv"

FIELDNAMES = ["company", "title", "description", "url", "place", "via", "ats"]

# Phenom's JSON-LD description came back 296k characters -- that is the whole
# page's furniture, not a posting. Everything genuine measured under 16k.
MAX_DESCRIPTION = 20_000

DEFAULT_WORKERS = 8

#: Page furniture, dropped before any text-level extraction. Removing it is
#: what makes the <body> rung usable at all, and it also trims the "Rechercher
#: les offres | Postuler" banner off the <main> boards.
CHROME_TAGS = ("script", "style", "noscript", "nav", "header", "footer",
               "aside", "form", "svg")

_BLANK_LINES = re.compile(r"\n{3,}")


@dataclass(frozen=True)
class Job:
    """One posting as its own page describes it.

    Attributes:
        company: Hiring company.
        title: Job title.
        description: Plain-text posting body.
        url: Posting URL.
        place: Location, if the page named one.
        via: Which extractor produced this row -- workday, jsonld, main, or
            none. Same purpose as board_scraper.Job.via: an extractor that
            starts returning nav furniture has to stay visible in the output.
    """

    company: str
    title: str
    description: str
    url: str
    place: Optional[str] = None
    via: str = ""


_local = threading.local()


def session() -> requests.Session:
    """The calling thread's session.

    requests.Session is not documented thread-safe, and one per worker keeps
    connection pooling -- which matters here because a board's postings all
    share a host.
    """
    found = getattr(_local, "session", None)

    if found is None:
        found = requests.Session()
        found.headers.update(HEADERS)
        _local.session = found

    return found


def _clean(fragment: object) -> str:
    """Turn a description field into plain text.

    JSON-LD and Workday both hand back HTML in a JSON string, sometimes
    double-escaped, so this unescapes before parsing rather than after.

    Args:
        fragment: Raw description value from a feed or JSON-LD node.

    Returns:
        Plain text, capped at MAX_DESCRIPTION.
    """
    if not isinstance(fragment, str) or not fragment.strip():
        return ""

    text = BeautifulSoup(unescape(fragment), "html.parser").get_text(
        "\n", strip=True
    )

    return _BLANK_LINES.sub("\n\n", text)[:MAX_DESCRIPTION]


def _workday_api(url: str) -> Optional[str]:
    """The /wday/cxs/ endpoint serving one Workday posting.

    Args:
        url: Public posting URL.

    Returns:
        API URL, or None if the path carries no site segment.
    """
    parsed = urlparse(url)
    tenant = parsed.hostname.split(".")[0] if parsed.hostname else ""
    segments = [part for part in parsed.path.split("/") if part]

    if segments and _LOCALE_RE.match(segments[0]):
        segments = segments[1:]

    if not tenant or not segments:
        return None

    return (f"{parsed.scheme}://{parsed.hostname}/wday/cxs/{tenant}/"
            + "/".join(segments))


def _from_workday(url: str) -> Optional[dict]:
    """Read a Workday posting from its JSON endpoint.

    The rendered page is a 158-character shell, so this runs instead of the
    page fetch rather than after it.

    Args:
        url: Public posting URL.

    Returns:
        Field dict, or None if the endpoint gave nothing usable.
    """
    endpoint = _workday_api(url)

    if not endpoint:
        return None

    info = _dig(_fetch_json(session(), endpoint), "jobPostingInfo")

    if not isinstance(info, dict):
        return None

    description = _clean(info.get("jobDescription"))

    if not description:
        return None

    return {
        "title": _first_string(info.get("title")),
        "description": description,
        "place": _first_string(info.get("location")),
        "via": "workday",
    }


def _from_jsonld(soup: BeautifulSoup) -> Optional[dict]:
    """Read the page's JSON-LD JobPosting.

    Args:
        soup: Parsed posting page.

    Returns:
        Field dict, or None if the page has no JobPosting with a description.
    """
    nodes = []

    for tag in soup.find_all("script", attrs={"type": "application/ld+json"}):
        try:
            nodes.append(json.loads(tag.get_text(strip=True)))
        except (ValueError, TypeError):
            continue

    for node in _walk_jobpostings(nodes):
        description = _clean(node.get("description"))

        if not description:
            continue

        return {
            "title": _first_string(node.get("title")),
            "description": description,
            "company": _first_string(_dig(node, "hiringOrganization.name")),
            # Same pair board_scraper._posting_fields reads: locality is what
            # the feeds report, region is what is left when a board omits it.
            "place": (
                _first_string(
                    _dig(node, "jobLocation.address.addressLocality")
                )
                or _first_string(
                    _dig(node, "jobLocation.address.addressRegion")
                )
            ),
            "via": "jsonld",
        }

    return None


def _text(node) -> str:
    """One element's text, blank runs collapsed and capped."""
    return _BLANK_LINES.sub(
        "\n\n", node.get_text("\n", strip=True)
    )[:MAX_DESCRIPTION]


def _page_title(soup: BeautifulSoup) -> Optional[str]:
    """The posting's title from og:title, else the first <h1>."""
    meta = soup.find("meta", attrs={"property": "og:title"})

    if meta and meta.get("content"):
        return meta["content"]

    heading = soup.find("h1")

    return heading.get_text(" ", strip=True) if heading else None


def _from_main(soup: BeautifulSoup, title: Optional[str]) -> Optional[dict]:
    """Read the posting out of the page's <main> landmark.

    The fallback for boards publishing no JSON-LD. Measured clean on
    successfactors, radancy, avature and hibob; no company or place, so those
    stay with whatever the board stage recorded.

    Args:
        soup: Parsed posting page, already de-chromed.
        title: Title read before de-chroming.

    Returns:
        Field dict, or None if the page has no <main> with text.
    """
    main = soup.find("main") or soup.find(attrs={"role": "main"})

    if main is None or not _text(main):
        return None

    return {"title": title, "description": _text(main), "via": "main"}


def _from_body(soup: BeautifulSoup, title: Optional[str]) -> Optional[dict]:
    """Last resort: the whole de-chromed page.

    For the plain WordPress/AEM career sites with no JSON-LD and no <main>.
    They share no container class or id, so narrowing further would mean one
    selector per employer; the nav and cookie banner are already gone, and
    via="body" keeps these rows distinguishable downstream.

    Args:
        soup: Parsed posting page, already de-chromed.
        title: Title read before de-chroming.

    Returns:
        Field dict, or None if the page has no text at all.
    """
    body = soup.body or soup

    if not _text(body):
        return None

    return {"title": title, "description": _text(body), "via": "body"}


def fetch_job(row: Dict[str, str]) -> Job:
    """Read one posting's page, falling back to the row when a field is absent.

    Args:
        row: A filtered_file.csv row -- company, title, url, place, ats.

    Returns:
        A Job. Never raises: a dead page yields via="none" with an empty
        description, so one bad posting cannot stop a 8000-row run.
    """
    url = row["url"]
    found = None

    if (row.get("ats") or "") == "workday":
        found = _from_workday(url)
    else:
        html = _fetch(session(), url)

        if html:
            soup = BeautifulSoup(html, "html.parser")
            found = _from_jsonld(soup)

            if not found:
                # After the JSON-LD rung, never before: de-chroming drops the
                # <script> tags the ld+json blocks live in.
                title = _page_title(soup)

                for tag in soup.find_all(CHROME_TAGS):
                    tag.decompose()

                found = _from_main(soup, title) or _from_body(soup, title)

    found = found or {}

    def pick(key: str) -> str:
        # Page wins: a board-stage title is often a URL slug or a card label.
        # Unescaped twice because these fields arrive double-escaped often
        # enough to matter -- Quanteam's title reached "&#8211;" after one
        # pass. Descriptions already get two passes for free, since _clean
        # unescapes and then parses the result as HTML.
        return unescape(
            unescape(found.get(key) or row.get(key) or "")
        ).strip()

    return Job(
        company=pick("company"),
        title=pick("title"),
        description=found.get("description", ""),
        url=url,
        place=pick("place") or None,
        via=found.get("via", "none"),
    )


def already_done(path: Path) -> set:
    """URLs an earlier run already wrote.

    Args:
        path: Output CSV, which may not exist.

    Returns:
        Set of URLs to skip.
    """
    if not path.exists():
        return set()

    with path.open(newline="", encoding="utf-8") as handle:
        return {row["url"] for row in csv.DictReader(handle) if row.get("url")}


def main() -> int:
    """Scrape every filtered posting's page into the detail CSV."""
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--limit", type=int, default=0,
                        help="only scrape the first N postings")
    parser.add_argument("--workers", type=int, default=DEFAULT_WORKERS)
    parser.add_argument("--input", type=Path, default=FILTERED_JOBS)
    parser.add_argument("--output", type=Path, default=DETAILED_JOBS)
    parser.add_argument("--no-resume", action="store_true",
                        help="rewrite the output instead of skipping URLs it "
                             "already holds")
    args = parser.parse_args()

    with args.input.open(newline="", encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle))

    if args.limit:
        rows = rows[:args.limit]

    done = set() if args.no_resume else already_done(args.output)
    pending = [row for row in rows if row["url"] not in done]

    print(f"{len(pending)} postings to fetch "
          f"({len(rows) - len(pending)} already done)")

    counts: Dict[str, int] = {}
    fresh = args.no_resume or not args.output.exists()

    with args.output.open(
        "w" if fresh else "a", newline="", encoding="utf-8"
    ) as output:
        writer = csv.DictWriter(output, fieldnames=FIELDNAMES)

        if fresh:
            writer.writeheader()

        with ThreadPoolExecutor(max_workers=args.workers) as pool:
            for index, (row, job) in enumerate(
                zip(pending, pool.map(fetch_job, pending)), start=1
            ):
                # pool.map yields in this thread, in order, so no lock is
                # needed. Flushed per row: a crash at 8000 keeps the first
                # 7999, and the next run resumes from exactly there.
                writer.writerow({**asdict(job), "ats": row.get("ats", "")})
                output.flush()

                counts[job.via] = counts.get(job.via, 0) + 1

                if index % 50 == 0 or index == len(pending):
                    print(f"  {index}/{len(pending)}  "
                          + "  ".join(f"{via}={n}"
                                      for via, n in sorted(counts.items())))

    print(f"\n-> {args.output}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
