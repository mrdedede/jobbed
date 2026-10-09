"""Scrape all boards from job_boards.csv into jobs.csv.

Usage:
    python job_scraper/main_scraper.py [--limit N] [--render]

Outputs jobs.csv with detected ATS and scraping strategy (via) for each row.
This is stage one of the pipeline; `python main.py` runs it along with the
rest.
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Callable, List, Optional

import httpx

from job_scraper import diagnose, paths
from job_scraper.api_sources import API_SOURCES
from job_scraper.board import Board
from job_scraper.detector import Renderer
from job_scraper.fetching import new_client
from job_scraper.models import Job

INPUT_FILE = paths.BOARDS_CSV
OUTPUT_FILE = paths.JOBS_CSV

FIELDNAMES = ["company", "title", "url", "place", "via", "ats", "description"]
MISS_FIELDNAMES = ["company", "url", "reason"]

#: Boards in flight at once. A board does more work than a single post fetch
#: (ATS detection + multi-strategy scrape), but each is mostly waiting on the
#: network; politeness is enforced per host in fetching, not here.
DEFAULT_WORKERS = 20

#: A render pass launches a whole headless Chromium instance per board; this
#: many at once is already a heavy concurrent load for one machine.
MAX_RENDER_WORKERS = 3


@dataclass
class BoardResult:
    """Everything the reporting loop needs about one scraped board.

    Kept side-effect free (no CSV writes, no callbacks) so it can run in any
    task and be tested on its own.
    """

    company: str
    url: str
    board: Board
    ats: Optional[str]
    jobs: Optional[List[Job]]
    error: Optional[Exception]


async def scrape_one_board(entry: dict, client: httpx.AsyncClient,
                           render: Optional[Renderer] = None) -> BoardResult:
    """Detect and scrape a single board entry.

    Args:
        entry: One job_boards.csv row (company, url).
        client: The run's shared AsyncClient.
        render: Optional async renderer for JS-built listings.

    Returns:
        A BoardResult carrying either the scraped jobs or the exception that
        stopped this board -- never raises, so one hostile board can't take
        down the run.
    """
    company, url = entry["company"], entry["url"]
    board = Board(company, url, session=client, render=render)

    try:
        ats = await board.detect_ats()
        jobs = await board.scrape_board()
    except Exception as exc:
        return BoardResult(company, url, board, ats=None, jobs=None, error=exc)

    return BoardResult(company, url, board, ats=ats, jobs=jobs, error=None)


async def scrape_boards(input_file: Optional[Path] = None,
                        output_file: Optional[Path] = None,
                        limit: int = 0,
                        render: Optional[Renderer] = None,
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
        render: Optional async renderer for JS-built listings.
        on_board: Optional callback given one progress line per board. Left to
            the caller so this function stays usable from something that is
            not a terminal.
        workers: Boards scraped concurrently, all through one shared client.

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

    async with new_client() as client:
        gate = asyncio.Semaphore(workers)

        async def bounded(entry: dict) -> BoardResult:
            async with gate:
                return await scrape_one_board(entry, client, render=render)

        tasks = [asyncio.create_task(bounded(entry)) for entry in boards]
        # Not boards: one nationwide search per source rather than one call
        # per row. Started now so it overlaps the board pass; written after
        # it, so the file keeps its order.
        sources = {
            name: asyncio.create_task(fetch_jobs(client))
            for name, fetch_jobs in API_SOURCES.items()
        }

        try:
            with output_file.open("w", newline="", encoding="utf-8") as output, \
                    paths.NO_JOBS_CSV.open("w", newline="",
                                           encoding="utf-8") as empties:
                # extrasaction: most strategies leave `description` blank --
                # WTTJ is the one exception, since its posting pages are as
                # WAF-walled as its board page and everything it has comes
                # from the board-stage call.
                writer = csv.DictWriter(output, fieldnames=FIELDNAMES,
                                        extrasaction="ignore")
                writer.writeheader()

                misses = csv.DictWriter(empties, fieldnames=MISS_FIELDNAMES)
                misses.writeheader()

                async def record_miss(board, company, url, exc=None) -> None:
                    """Write one no_jobs row, never letting diagnosis end the run.

                    Broad for the same reason the scrape loop below is: this
                    is a reporting aid, and it has no business costing anyone
                    a run of a hundred boards.
                    """
                    try:
                        reason = await diagnose.explain(board, exc)
                    except Exception as err:
                        reason = (f"diagnosis failed: "
                                  f"{type(err).__name__}: {err}")

                    misses.writerow({"company": company, "url": url,
                                     "reason": reason})

                # Awaited in input order, like pool.map was: every write below
                # -- writer, misses, on_board -- stays in one task and in the
                # same order as the sequential version, with no lock.
                for index, task in enumerate(tasks, start=1):
                    result = await task
                    company, url, board = (result.company, result.url,
                                           result.board)

                    if result.error is not None:
                        failed += 1
                        per_board[url] = None
                        await record_miss(board, company, url, result.error)

                        if on_board:
                            on_board(f"[{index}/{len(boards)}] FAIL   "
                                     f"{company}: {result.error}")

                        continue

                    ats, jobs = result.ats, result.jobs
                    via = jobs[0].via if jobs else "none"
                    per_board[url] = len(jobs)

                    if not jobs:
                        await record_miss(board, company, url)

                    if on_board:
                        on_board(f"[{index}/{len(boards)}] {len(jobs):4} jobs  "
                                 f"{company:24} {ats or 'unknown':16} "
                                 f"via {via}")

                    for job in jobs:
                        writer.writerow({**asdict(job), "ats": ats or ""})
                        total_jobs += 1

                for source_name, source in sources.items():
                    jobs = await source

                    for job in jobs:
                        writer.writerow({**asdict(job), "ats": source_name})
                        total_jobs += 1

                    if on_board:
                        on_board(f"{len(jobs):4} jobs  {source_name:24} api")
        finally:
            # An error or Ctrl-C must not leave scraping tasks running against
            # a client that is about to close.
            for task in (*tasks, *sources.values()):
                task.cancel()

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
    parser.add_argument("--workers", type=int, default=DEFAULT_WORKERS,
                        help="boards scraped concurrently")
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
        from job_scraper.render import arender as renderer
        # Each render is a full headless Chromium launch -- capped separately
        # from --workers so a general "run with more workers" bump doesn't
        # also mean "launch more browsers at once".
        workers = min(workers, MAX_RENDER_WORKERS)

    stats = asyncio.run(scrape_boards(
        input_file=args.input,
        output_file=args.output,
        limit=args.limit,
        render=renderer,
        on_board=print,
        workers=workers,
    ))

    print(f"\n{stats['jobs']} postings from {stats['boards']} boards "
          f"-> {stats['output']}")
    print(f"boards that gave nothing -> {stats['no_jobs']}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
