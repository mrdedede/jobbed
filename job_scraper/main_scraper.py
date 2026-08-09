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
from dataclasses import asdict
from pathlib import Path
from typing import Callable, Optional

from job_scraper import paths
from job_scraper.board import Board

INPUT_FILE = paths.BOARDS_CSV
OUTPUT_FILE = paths.JOBS_CSV

FIELDNAMES = ["company", "title", "url", "place", "via", "ats"]


def scrape_boards(input_file: Optional[Path] = None,
                  output_file: Optional[Path] = None,
                  limit: int = 0,
                  render: Optional[Callable] = None,
                  on_board: Optional[Callable[[str], None]] = None) -> dict:
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

    Returns:
        Dict with `boards`, `jobs` and `failed` counts, the `output` path, and
        `per_board` mapping each board URL to how many postings it produced.
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

    with output_file.open("w", newline="", encoding="utf-8") as output:
        # extrasaction: Job carries a `description` the board stage never
        # fills, and this CSV has no column for it.
        writer = csv.DictWriter(output, fieldnames=FIELDNAMES,
                                extrasaction="ignore")
        writer.writeheader()

        for index, entry in enumerate(boards, start=1):
            company, url = entry["company"], entry["url"]
            board = Board(company, url, render=render)

            try:
                ats = board.detect_ats()
                jobs = board.scrape_board()
            except Exception as exc:
                failed += 1
                per_board[url] = None

                if on_board:
                    on_board(f"[{index}/{len(boards)}] FAIL   {company}: "
                             f"{exc}")

                continue

            via = jobs[0].via if jobs else "none"
            per_board[url] = len(jobs)

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
    parser.add_argument(
        "--render", action="store_true",
        help="last-resort browser pass for boards that build their listing "
             "in JS. Costs seconds per board. Needs: pip install playwright "
             "&& playwright install chromium",
    )
    args = parser.parse_args()

    renderer = None

    if args.render:
        # Imported here, not at module scope: Playwright is an opt-in extra
        # and this script has to keep running on a machine with no browser.
        from job_scraper.render import render as renderer

    stats = scrape_boards(
        input_file=args.input,
        output_file=args.output,
        limit=args.limit,
        render=renderer,
        on_board=print,
    )

    print(f"\n{stats['jobs']} postings from {stats['boards']} boards "
          f"-> {stats['output']}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
