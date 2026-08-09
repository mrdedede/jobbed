"""Scrape all boards from job_boards.csv into jobs.csv.

Usage:
    python job_scraper/main_scraper.py [--limit N] [--render]

Outputs jobs.csv with detected ATS and scraping strategy (via) for each row.
"""

from __future__ import annotations

import argparse
import csv
import sys
from dataclasses import asdict
from pathlib import Path

from job_scraper.board_scraper import Board

ROOT = Path(__file__).resolve().parent.parent
INPUT_FILE = ROOT / "user_info" / "job_boards.csv"
OUTPUT_FILE = ROOT / "temp" / "jobs.csv"


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

    with args.input.open(newline="", encoding="utf-8") as handle:
        boards = list(csv.DictReader(handle))

    if args.limit:
        boards = boards[:args.limit]

    total_jobs = 0
    fieldnames = ["company", "title", "url", "place", "via", "ats"]

    with args.output.open("w", newline="", encoding="utf-8") as output:
        writer = csv.DictWriter(output, fieldnames=fieldnames)
        writer.writeheader()

        for index, entry in enumerate(boards, start=1):
            company, url = entry["company"], entry["url"]
            board = Board(company, url, render=renderer)

            try:
                ats = board.detect_ats()
                jobs = board.scrape_board()
            except Exception as exc:
                print(f"[{index}/{len(boards)}] FAIL   {company}: {exc}")
                continue

            via = jobs[0].via if jobs else "none"
            print(f"[{index}/{len(boards)}] {len(jobs):4} jobs  "
                  f"{company:24} {ats or 'unknown':16} via {via}")

            for job in jobs:
                writer.writerow({**asdict(job), "ats": ats or ""})
                total_jobs += 1

    print(f"\n{total_jobs} postings from {len(boards)} boards "
          f"-> {args.output}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
