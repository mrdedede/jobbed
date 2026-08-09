"""Scrape all boards from job_boards.csv into jobs.csv.

Usage:
    python job_scrapper/main_scrapper.py [--limit N] [--render]

Outputs jobs.csv with detected ATS and scraping strategy (via) for each row.
"""

from __future__ import annotations

import argparse
import csv
import sys
from dataclasses import asdict
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from job_scrapper.board_scrapper import Board  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
INPUT_FILE = ROOT / "job_boards.csv"
OUTPUT_FILE = ROOT / "jobs.csv"


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
        from job_scrapper.render import render as renderer

    with args.input.open(newline="", encoding="utf-8") as handle:
        boards = list(csv.DictReader(handle))

    if args.limit:
        boards = boards[:args.limit]

    rows = []

    for index, entry in enumerate(boards, start=1):
        company, url = entry["company"], entry["url"]
        board = Board(company, url, render=renderer)

        try:
            ats = board.detect_ats()
            jobs = board.scrap_board()
        except Exception as exc:
            print(f"[{index}/{len(boards)}] FAIL   {company}: {exc}")
            continue

        via = jobs[0].via if jobs else "none"
        print(f"[{index}/{len(boards)}] {len(jobs):4} jobs  "
              f"{company:24} {ats or 'unknown':16} via {via}")

        rows.extend({**asdict(job), "ats": ats or ""} for job in jobs)

    frame = pd.DataFrame(rows)
    frame.to_csv(args.output, index=False, encoding="utf-8")

    print(f"\n{len(frame)} postings from {len(boards)} boards "
          f"-> {args.output}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
