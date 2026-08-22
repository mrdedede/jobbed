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
import re
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict
from html import unescape
from pathlib import Path
from typing import Callable, Dict, List, Optional

import requests
from bs4 import BeautifulSoup

from job_scraper import paths
from job_scraper.fetching import (
    dig,
    fetch,
    fetch_json,
    first_string,
    jobposting_place,
    jsonld_nodes,
    new_session,
    walk_jobpostings,
    workday_endpoint,
)
from job_scraper.models import Job

FILTERED_JOBS = paths.FIRST_FILTERED_CSV
DETAILED_JOBS = paths.DETAILED_CSV

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


_local = threading.local()


def session() -> requests.Session:
    """The calling thread's session.

    requests.Session is not documented thread-safe, and one per worker keeps
    connection pooling -- which matters here because a board's postings all
    share a host.
    """
    found = getattr(_local, "session", None)

    if found is None:
        found = new_session()
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
    located = workday_endpoint(url)

    if located is None:
        return None

    root, tenant, segments = located

    # The whole path, not just the site: everything after it identifies the
    # individual posting.
    return f"{root}/wday/cxs/{tenant}/" + "/".join(segments)


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

    info = dig(fetch_json(session(), endpoint), "jobPostingInfo")

    if not isinstance(info, dict):
        return None

    description = _clean(info.get("jobDescription"))

    if not description:
        return None

    return {
        "title": first_string(info.get("title")),
        "description": description,
        "place": first_string(info.get("location")),
        "via": "workday",
    }


def _from_jsonld(soup: BeautifulSoup) -> Optional[dict]:
    """Read the page's JSON-LD JobPosting.

    Args:
        soup: Parsed posting page.

    Returns:
        Field dict, or None if the page has no JobPosting with a description.
    """
    for node in walk_jobpostings(jsonld_nodes(soup)):
        description = _clean(node.get("description"))

        if not description:
            continue

        return {
            "title": first_string(node.get("title")),
            "description": description,
            "company": first_string(dig(node, "hiringOrganization.name")),
            "place": jobposting_place(node),
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
        row: A first_filtered_file.csv row -- company, title, url, place, ats.

    Returns:
        A Job. Never raises: a dead page yields via="none" with an empty
        description, so one bad posting cannot stop a 8000-row run.
    """
    url = row["url"]
    found = None

    if (row.get("ats") or "") == "workday":
        found = _from_workday(url)
    elif (row.get("ats") or "") == "wttj":
        # Posting pages sit behind the same WAF wall as the board page; the
        # board stage already pulled the full posting from Algolia, so there
        # is nothing left here to fetch.
        found = {
            "title": row.get("title", ""),
            "description": row.get("description", ""),
            "via": "wttj",
        }
    else:
        html = fetch(session(), url)

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


def scrape_details(input_file: Optional[Path] = None,
                   output_file: Optional[Path] = None,
                   limit: int = 0,
                   workers: int = DEFAULT_WORKERS,
                   resume: bool = True,
                   on_progress: Optional[Callable[[str], None]] = None,
                   rows: Optional[List[Dict[str, str]]] = None) -> dict:
    """Fetch each filtered posting's own page and write the detail CSV.

    Args:
        input_file: The first filter's output. Defaults to
            paths.FIRST_FILTERED_CSV, resolved at call time so a caller that
            repoints `paths` is actually followed. Ignored when `rows` is given.
        output_file: Where to write postings with descriptions.
        limit: Only fetch the first N postings; 0 means all of them.
        workers: Thread pool size. See fetching.REQUEST_DELAY for what this
            implies about request rate.
        resume: Skip URLs the output already holds and append to it. False
            rewrites the file from scratch.
        on_progress: Optional callback given progress lines.
        rows: Postings to fetch, as the dicts the input CSV would have held.
            Lets a caller that already filtered in memory -- the Streamlit
            pages -- skip writing first_filtered_file.csv just to read it back.

    Returns:
        Dict with `pending`, `skipped` and per-extractor `counts`, plus the
        `output` path.
    """
    input_file = input_file or paths.FIRST_FILTERED_CSV
    output_file = output_file or paths.DETAILED_CSV

    if rows is None:
        with input_file.open(newline="", encoding="utf-8") as handle:
            rows = list(csv.DictReader(handle))

    if limit:
        rows = rows[:limit]

    done = already_done(output_file) if resume else set()
    pending = [row for row in rows if row["url"] not in done]

    if on_progress:
        on_progress(f"{len(pending)} postings to fetch "
                    f"({len(rows) - len(pending)} already done)")

    counts: Dict[str, int] = {}
    fresh = not resume or not output_file.exists()

    with output_file.open(
        "w" if fresh else "a", newline="", encoding="utf-8"
    ) as output:
        writer = csv.DictWriter(output, fieldnames=FIELDNAMES,
                                extrasaction="ignore")

        if fresh:
            writer.writeheader()

        with ThreadPoolExecutor(max_workers=workers) as pool:
            for index, (row, job) in enumerate(
                zip(pending, pool.map(fetch_job, pending)), start=1
            ):
                # pool.map yields in this thread, in order, so no lock is
                # needed. Flushed per row: a crash at 8000 keeps the first
                # 7999, and the next run resumes from exactly there.
                writer.writerow({**asdict(job), "ats": row.get("ats", "")})
                output.flush()

                counts[job.via] = counts.get(job.via, 0) + 1

                if on_progress and (index % 50 == 0 or index == len(pending)):
                    on_progress(
                        f"  {index}/{len(pending)}  "
                        + "  ".join(f"{via}={n}"
                                    for via, n in sorted(counts.items()))
                    )

    return {
        "pending": len(pending),
        "skipped": len(rows) - len(pending),
        "counts": counts,
        "output": output_file,
    }


def main() -> int:
    """Scrape every filtered posting's page into the detail CSV.

    Returns:
        0 on success.
    """
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

    stats = scrape_details(
        input_file=args.input,
        output_file=args.output,
        limit=args.limit,
        workers=args.workers,
        resume=not args.no_resume,
        on_progress=print,
    )

    print(f"\n-> {stats['output']}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
