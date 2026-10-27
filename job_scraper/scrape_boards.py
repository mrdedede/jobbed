"""Scrape all active boards (the job_boards table) into jobs.csv.

Usage:
    python -m job_scraper.scrape_boards [--limit N] [--render] [--input FILE]

Boards come from the database; --input FILE reads a company,url CSV instead,
for a run with no database at hand.

Outputs jobs.csv with detected ATS and scraping strategy (via) for each row.
This is stage one of the nightly pipeline.
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import os
import sys
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from urllib.parse import urlparse
from typing import Callable, List, Optional

import httpx

from job_scraper import diagnose, paths
from job_scraper.api_sources import API_SOURCES
from job_scraper.board import Board
from job_scraper.detector import Renderer
from job_scraper.fetching import new_client
from job_scraper.models import Job

FIELDNAMES = ["company", "title", "url", "place", "via", "ats", "description"]
MISS_FIELDNAMES = ["company", "url", "reason", "ats", "evidence"]

#: Boards in flight at once. A board does more work than a single post fetch
#: (ATS detection + multi-strategy scrape), but each is mostly waiting on the
#: network; politeness is enforced per host in fetching, not here.
DEFAULT_WORKERS = 20

#: A run whose failed-board share exceeds this exits 1. Step 0's silent async
#: bug turned whole vendors into "no jobs"; a share this high is a regression,
#: not a bad night.
MAX_FAILED_SHARE = 0.25


def is_junk(url: str) -> bool:
    """Report whether a scraped link cannot be a posting.

    Scrapers sometimes emit relative or empty links, which cannot be fetched.
    Hosts are deliberately not filtered: google.com is itself a board
    (www.google.com/about/careers/...), and its non-job pages are a
    link-selection problem, not a junk host.
    """
    return not urlparse(url or "").hostname


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
        entry: One board: {"company": ..., "url": ...}.
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
        await board.detect_ats()
        jobs = await board.scrape_board()
    except Exception as exc:
        return BoardResult(company, url, board, ats=None, jobs=None, error=exc)

    # board.ats, not detect_ats()'s return: the feed fallback can name the
    # vendor during scraping, and the output should say which one it was.
    return BoardResult(company, url, board, ats=board.ats, jobs=jobs,
                       error=None)


def load_boards(input_file: Optional[Path] = None) -> List[dict]:
    """The boards to scrape.

    Args:
        input_file: A `company,url` CSV. Rows whose company starts with `#`
            are skipped. When omitted, the active rows of the job_boards
            table are used.

    Returns:
        List of {"company", "url"} dicts.
    """
    if input_file is None:
        # Imported here: scraping from a CSV must work with no database.
        from db import boards

        return boards.select_active_boards()

    with input_file.open(newline="", encoding="utf-8") as handle:
        return [row for row in csv.DictReader(handle)
                if not row["company"].lstrip().startswith("#")]


async def scrape_boards(input_file: Optional[Path] = None,
                        output_file: Optional[Path] = None,
                        limit: int = 0,
                        render: Optional[Renderer] = None,
                        on_board: Optional[Callable[[str], None]] = None,
                        workers: int = DEFAULT_WORKERS,
                        boards: Optional[List[dict]] = None,
                        no_jobs_file: Optional[Path] = None) -> dict:
    """Scrape every board and write one row per posting.

    A board that raises is reported and skipped rather than allowed to end the
    run -- with a hundred boards, one hostile site must not cost the other 99.

    Args:
        input_file: CSV of company,url board rows. Defaults to the active
            rows of the job_boards table. Ignored when `boards` is given.
        output_file: Where to write the scraped postings.
        no_jobs_file: Where to write the boards that gave nothing. Defaults
            to paths.NO_JOBS_CSV; a run that reads that file as its input
            (RenderBoards) must write somewhere else.
        limit: Only scrape the first N boards; 0 means all of them.
        render: Optional async renderer for JS-built listings.
        on_board: Optional callback given one progress line per board. Left to
            the caller so this function stays usable from something that is
            not a terminal.
        workers: Boards scraped concurrently, all through one shared client.
        boards: The boards themselves, as {"company", "url"} dicts -- for a
            caller that already holds them. Overrides `input_file`.

    Returns:
        Dict with `boards`, `jobs`, `failed` and `dropped` (junk links
        left out of the file) counts, the `output` path, the
        `no_jobs` path holding one diagnosed row per board that produced
        nothing, and `per_board` mapping each board URL to how many postings it
        produced.
    """
    output_file = output_file or paths.JOBS_CSV
    no_jobs_file = no_jobs_file or paths.NO_JOBS_CSV
    paths.TEMP.mkdir(parents=True, exist_ok=True)
    output_file.parent.mkdir(parents=True, exist_ok=True)

    if boards is None:
        boards = await asyncio.to_thread(load_boards, input_file)

    if limit:
        boards = boards[:limit]

    total_jobs = 0
    failed = 0
    dropped = 0
    # Keyed on URL, not company: company is not unique in this file (statera
    # appears four times at four different boards), so a per-company tally
    # silently merges boards and reports four as one.
    per_board: dict = {}

    async with new_client() as client:
        gate = asyncio.Semaphore(workers)

        done = 0

        async def bounded(entry: dict) -> BoardResult:
            nonlocal done

            async with gate:
                result = await scrape_one_board(entry, client, render=render)

            # Logged on completion, not in file order, so one slow board
            # can't hold back the progress of the ones behind it.
            done += 1

            if on_board:
                if result.error is not None:
                    on_board(f"[{done}/{len(boards)}] FAIL   "
                             f"{result.company}: {result.error}")
                else:
                    jobs = result.jobs
                    via = jobs[0].via if jobs else "none"
                    on_board(f"[{done}/{len(boards)}] {len(jobs):4} jobs  "
                             f"{result.company:24} "
                             f"{result.ats or 'unknown':16} via {via}")

            return result

        tasks = [asyncio.create_task(bounded(entry)) for entry in boards]
        # Not boards: one nationwide search per source rather than one call
        # per row. Started now so it overlaps the board pass; written after
        # it, so the file keeps its order.
        sources = {
            name: asyncio.create_task(fetch_jobs(client))
            for name, fetch_jobs in API_SOURCES.items()
        }

        # Written beside the target and renamed on success, so a run that
        # dies midway leaves the previous complete files in place for ingest.
        tmp_jobs = output_file.with_name(output_file.name + ".tmp")
        tmp_misses = no_jobs_file.with_name(no_jobs_file.name + ".tmp")

        try:
            with tmp_jobs.open("w", newline="", encoding="utf-8") as output, \
                    tmp_misses.open("w", newline="",
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
                        facts = await diagnose.evidence(board)
                    except Exception as err:
                        reason = (f"diagnosis failed: "
                                  f"{type(err).__name__}: {err}")
                        facts = ""

                    misses.writerow({"company": company, "url": url,
                                     "reason": reason,
                                     "ats": getattr(board, "ats", "") or "",
                                     "evidence": facts})

                # Awaited in input order, like pool.map was: every write below
                # -- writer, misses, on_board -- stays in one task and in the
                # same order as the sequential version, with no lock.
                for task in tasks:
                    result = await task
                    company, url, board = (result.company, result.url,
                                           result.board)

                    if result.error is not None:
                        failed += 1
                        per_board[url] = None
                        await record_miss(board, company, url, result.error)

                        continue

                    ats, jobs = result.ats, result.jobs
                    via = jobs[0].via if jobs else "none"
                    per_board[url] = len(jobs)

                    if not jobs:
                        await record_miss(board, company, url)

                    for job in jobs:
                        if is_junk(job.url):
                            dropped += 1
                            continue

                        writer.writerow({**asdict(job), "ats": ats or ""})
                        total_jobs += 1

                for source_name, source in sources.items():
                    jobs = await source

                    for job in jobs:
                        if is_junk(job.url):
                            dropped += 1
                            continue

                        writer.writerow({**asdict(job), "ats": source_name})
                        total_jobs += 1

                    if on_board:
                        on_board(f"{len(jobs):4} jobs  {source_name:24} api")

            os.replace(tmp_jobs, output_file)
            os.replace(tmp_misses, no_jobs_file)
        finally:
            # An error or Ctrl-C must not leave scraping tasks running against
            # a client that is about to close.
            for task in (*tasks, *sources.values()):
                task.cancel()

    return {
        "boards": len(boards),
        "jobs": total_jobs,
        "failed": failed,
        "dropped": dropped,
        "output": output_file,
        "no_jobs": no_jobs_file,
        "per_board": per_board,
    }


#: Reasons (the start of a diagnose.explain line) that a browser can fix: the
#: listing is built in JS, or the page is an empty shell. The others -- 403s,
#: "job-shaped links present", "no job-shaped links" -- are different problems
#: and a browser would only burn minutes on them.
JS_SHELL_REASONS = ("likely javascript-rendered", "no links on the page")


def render_candidates(path: Optional[Path] = None) -> List[dict]:
    """The no_jobs rows worth a browser pass.

    Args:
        path: A no_jobs.csv; defaults to paths.NO_JOBS_CSV.

    Returns:
        {"company", "url"} dicts for the rows whose reason is a JS shell.
        Empty if the file does not exist yet.
    """
    path = path or paths.NO_JOBS_CSV

    if not path.exists():
        return []

    with path.open(newline="", encoding="utf-8") as handle:
        return [{"company": row["company"], "url": row["url"]}
                for row in csv.DictReader(handle)
                if (row.get("reason") or "").startswith(JS_SHELL_REASONS)]


def main() -> int:
    """Scrape all boards and write results to CSV.

    Returns:
        0 on success; 1 if no boards were found, no jobs were produced, or
        more than MAX_FAILED_SHARE of the boards raised -- so a bad run is
        visible to whatever scheduled it.
    """
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--limit", type=int, default=0,
                        help="only scrape the first N boards")
    parser.add_argument("--input", type=Path, default=None,
                        help="read boards from this company,url CSV "
                             "instead of the database")
    parser.add_argument("--output", type=Path, default=None)
    parser.add_argument("--workers", type=int, default=DEFAULT_WORKERS,
                        help="boards scraped concurrently")
    parser.add_argument(
        "--render", action="store_true",
        help="last-resort browser pass for boards that build their listing "
             "in JS. Costs seconds per board. Needs: pip install playwright "
             "&& playwright install chromium",
    )
    parser.add_argument(
        "--render-missed", action="store_true",
        help="RenderBoards: re-scrape, with a browser, the JS-shell boards "
             "listed in no_jobs.csv; writes jobs_rendered.csv (merged by the "
             "next ingest) and no_jobs_rendered.csv",
    )
    args = parser.parse_args()

    if args.render_missed:
        args.render = True

    async def run() -> dict:
        pool = None

        if args.render:
            # Imported here, not at module scope: Playwright is an opt-in
            # extra and this script has to keep running with no browser.
            # The pool caps concurrent pages itself, so --workers needs no
            # separate render limit.
            from job_scraper.render import RenderPool
            pool = RenderPool()

        try:
            extra = {}

            if args.render_missed:
                extra = dict(boards=render_candidates(args.input),
                             output_file=args.output or paths.JOBS_RENDERED_CSV,
                             no_jobs_file=paths.NO_JOBS_RENDERED_CSV)
                print(f"{len(extra['boards'])} JS-shell boards to render")

            return await scrape_boards(
                **{"input_file": args.input, "output_file": args.output,
                   "limit": args.limit,
                   "render": pool.render if pool else None,
                   "on_board": print, "workers": args.workers, **extra}
            )
        finally:
            if pool:
                await pool.aclose()

    started = time.monotonic()
    stats = asyncio.run(run())

    minutes = (time.monotonic() - started) / 60
    print(f"\n{stats['jobs']} postings from {stats['boards']} boards "
          f"({stats['failed']} failed, {stats['dropped']} junk links dropped) "
          f"in {minutes:.1f} min -> {stats['output']}")
    print(f"boards that gave nothing -> {stats['no_jobs']}")

    if args.render_missed:
        # A browser pass recovers little by nature (a render pass is expensive);
        # zero found is a quiet night, not a failed run.
        return 0

    if not stats["boards"] or not stats["jobs"]:
        print("FAILED: no boards or no postings", file=sys.stderr)
        return 1

    if stats["failed"] / stats["boards"] > MAX_FAILED_SHARE:
        print(f"FAILED: {stats['failed']} of {stats['boards']} boards raised",
              file=sys.stderr)
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
