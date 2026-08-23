"""Scrape all boards from job_boards.csv into jobs.csv.

Usage:
    python job_scraper/main_scraper.py [--limit N] [--render]

Outputs jobs.csv with detected ATS and scraping strategy (via) for each row.
This is stage one of the pipeline; `python main.py` runs it along with the
rest.
"""

from __future__ import annotations

import argparse
import csv
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Callable, List, Optional

import requests

from job_scraper import diagnose, paths
from job_scraper.board import Board
from job_scraper.fetching import new_session
from job_scraper.models import Job

INPUT_FILE = paths.BOARDS_CSV
OUTPUT_FILE = paths.JOBS_CSV

FIELDNAMES = ["company", "title", "url", "place", "via", "ats", "description"]
MISS_FIELDNAMES = ["company", "url", "reason"]

#: Boards do more work per item than a single post fetch (ATS detection +
#: multi-strategy scrape), so a lower default than post_scraper's 8 keeps a
#: hundred-board run from hammering that many hosts at once.
DEFAULT_WORKERS = 4

#: A render pass launches a whole headless Chromium instance per board; this
#: many at once is already a heavy concurrent load for one machine.
MAX_RENDER_WORKERS = 3

_local = threading.local()


def session() -> requests.Session:
    """The calling thread's session.

    Mirrors post_scraper.session(): requests.Session is not documented
    thread-safe, and one per worker keeps connection pooling, which matters
    here since a board's own page and its ATS-detection fetch share a host.
    """
    found = getattr(_local, "session", None)

    if found is None:
        found = new_session()
        _local.session = found

    return found


@dataclass
class BoardResult:
    """Everything the reporting loop needs about one scraped board.

    Kept side-effect free (no CSV writes, no callbacks) so it can run on any
    thread and be tested on its own.
    """

    company: str
    url: str
    board: Board
    ats: Optional[str]
    jobs: Optional[List[Job]]
    error: Optional[Exception]


def scrape_one_board(entry: dict, render: Optional[Callable] = None) -> BoardResult:
    """Detect and scrape a single board entry.

    Args:
        entry: One job_boards.csv row (company, url).
        render: Optional Playwright renderer for JS-built listings.

    Returns:
        A BoardResult carrying either the scraped jobs or the exception that
        stopped this board -- never raises, so one hostile board can't take
        down the pool.
    """
    company, url = entry["company"], entry["url"]
    board = Board(company, url, session=session(), render=render)

    try:
        ats = board.detect_ats()
        jobs = board.scrape_board()
    except Exception as exc:
        return BoardResult(company, url, board, ats=None, jobs=None, error=exc)

    return BoardResult(company, url, board, ats=ats, jobs=jobs, error=None)


def scrape_boards(input_file: Optional[Path] = None,
                  output_file: Optional[Path] = None,
                  limit: int = 0,
                  render: Optional[Callable] = None,
                  on_board: Optional[Callable[[str], None]] = None,
                  workers: int = DEFAULT_WORKERS) -> dict:
    """Scrape every board in the input CSV and write one row per posting.

    A board that raises is reported and skipped rather than allowed to end the
    run -- with a hundred boards, one hostile site must not cost the other 99.

    Args:
        input_file: CSV of company,url board rows. Defaults to
            paths.BOARDS_CSV, resolved at call time rather than baked into the
            signature -- a default argument is bound once at import, so a
            caller that repoints `paths` would silently keep getting the
            original file.
        output_file: Where to write the scraped postings.
        limit: Only scrape the first N boards; 0 means all of them.
        render: Optional Playwright renderer for JS-built listings.
        on_board: Optional callback given one progress line per board. Left to
            the caller so this function stays usable from something that is
            not a terminal.
        workers: Thread pool size. Boards are scraped concurrently; each
            worker gets its own session (see `session()` above).

    Returns:
        Dict with `boards`, `jobs` and `failed` counts, the `output` path, the
        `no_jobs` path holding one diagnosed row per board that produced
        nothing, and `per_board` mapping each board URL to how many postings it
        produced.
    """
    input_file = input_file or paths.BOARDS_CSV
    output_file = output_file or paths.JOBS_CSV

    with input_file.open(newline="", encoding="utf-8") as handle:
        boards = [row for row in csv.DictReader(handle)
                  if not row["company"].lstrip().startswith("#")]

    if limit:
        boards = boards[:limit]

    total_jobs = 0
    failed = 0
    # Keyed on URL, not company: company is not unique in this file (statera
    # appears four times at four different boards), so a per-company tally
    # silently merges boards and reports four as one.
    per_board: dict = {}

    with output_file.open("w", newline="", encoding="utf-8") as output, \
            paths.NO_JOBS_CSV.open("w", newline="",
                                   encoding="utf-8") as empties:
        # extrasaction: most strategies leave `description` blank -- WTTJ is
        # the one exception, since its posting pages are as WAF-walled as its
        # board page and everything it has comes from the board-stage call.
        writer = csv.DictWriter(output, fieldnames=FIELDNAMES,
                                extrasaction="ignore")
        writer.writeheader()

        misses = csv.DictWriter(empties, fieldnames=MISS_FIELDNAMES)
        misses.writeheader()

        def record_miss(board, company, url, exc=None) -> None:
            """Write one no_jobs row, never letting diagnosis end the run.

            Broad for the same reason the scrape loop below is: this is a
            reporting aid, and it has no business costing anyone a run of a
            hundred boards.
            """
            try:
                reason = diagnose.explain(board, exc)
            except Exception as err:
                reason = f"diagnosis failed: {type(err).__name__}: {err}"

            misses.writerow({"company": company, "url": url,
                             "reason": reason})

        with ThreadPoolExecutor(max_workers=workers) as pool:
            results = pool.map(
                lambda entry: scrape_one_board(entry, render=render), boards
            )

            # pool.map yields in input order on this thread, so every write
            # below -- writer, misses, on_board -- stays single-threaded and
            # in the same order as the sequential version, with no lock.
            for index, result in enumerate(results, start=1):
                company, url, board = result.company, result.url, result.board

                if result.error is not None:
                    failed += 1
                    per_board[url] = None
                    record_miss(board, company, url, result.error)

                    if on_board:
                        on_board(f"[{index}/{len(boards)}] FAIL   {company}: "
                                 f"{result.error}")

                    continue

                ats, jobs = result.ats, result.jobs
                via = jobs[0].via if jobs else "none"
                per_board[url] = len(jobs)

                if not jobs:
                    record_miss(board, company, url)

                if on_board:
                    on_board(f"[{index}/{len(boards)}] {len(jobs):4} jobs  "
                             f"{company:24} {ats or 'unknown':16} via {via}")

                for job in jobs:
                    writer.writerow({**asdict(job), "ats": ats or ""})
                    total_jobs += 1

    return {
        "boards": len(boards),
        "jobs": total_jobs,
        "failed": failed,
        "output": output_file,
        "no_jobs": paths.NO_JOBS_CSV,
        "per_board": per_board,
    }


def main() -> int:
    """Scrape all boards and write results to CSV.

    Returns:
        0 on success.
    """
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--limit", type=int, default=0,
                        help="only scrape the first N boards")
    parser.add_argument("--input", type=Path, default=INPUT_FILE)
    parser.add_argument("--output", type=Path, default=OUTPUT_FILE)
    parser.add_argument("--workers", type=int, default=DEFAULT_WORKERS)
    parser.add_argument(
        "--render", action="store_true",
        help="last-resort browser pass for boards that build their listing "
             "in JS. Costs seconds per board. Needs: pip install playwright "
             "&& playwright install chromium",
    )
    args = parser.parse_args()

    renderer = None
    workers = args.workers

    if args.render:
        # Imported here, not at module scope: Playwright is an opt-in extra
        # and this script has to keep running on a machine with no browser.
        from job_scraper.render import render as renderer
        # Each render is a full headless Chromium launch -- capped separately
        # from --workers so a general "run with more workers" bump doesn't
        # also mean "launch more browsers at once".
        workers = min(workers, MAX_RENDER_WORKERS)

    stats = scrape_boards(
        input_file=args.input,
        output_file=args.output,
        limit=args.limit,
        render=renderer,
        on_board=print,
        workers=workers,
    )

    print(f"\n{stats['jobs']} postings from {stats['boards']} boards "
          f"-> {stats['output']}")
    print(f"boards that gave nothing -> {stats['no_jobs']}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
