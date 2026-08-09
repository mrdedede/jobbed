"""Scrape one board and print what came back, for debugging a single site.

The scraping itself lives in `job_scraper.board` and `job_scraper.strategies`;
this is only the command line around it. Reach for it when a board produces
nothing and you need to see which strategy was tried and what it found.

Usage:
    python job_scraper/board_scraper.py <url> [--company NAME] [--show N]
                                              [--render]

The imports below are absolute, so by-path running needs the project
installed: `pip install -e .`. Otherwise run it as `python -m
job_scraper.board_scraper`.
"""

from job_scraper.board import Board


def main() -> int:
    """Scrape one board named on the command line and print a summary.

    Returns:
        Process exit code; 0 always, since an empty board is a finding rather
        than a failure.
    """
    import argparse

    parser = argparse.ArgumentParser(
        description="Scrape every posting off one job board"
    )
    parser.add_argument("url")
    parser.add_argument("--company", default="?")
    parser.add_argument("--show", type=int, default=10)
    parser.add_argument(
        "--render", action="store_true",
        help="last-resort browser pass for boards that build their listing "
             "in JS. Needs: pip install playwright && playwright install "
             "chromium",
    )

    args = parser.parse_args()

    renderer = None

    if args.render:
        # Imported here, not at module scope: Playwright is an opt-in extra.
        from job_scraper.render import render as renderer

    board = Board(args.company, args.url, render=renderer)

    print(f"Board:    {args.url}")
    print(f"ATS:      {board.detect_ats() or 'unknown'}")
    print(f"Resolved: {board.final_url}")

    found = board.scrape_board()

    print(f"Strategy: {found[0].via if found else 'none'}")
    print(f"Jobs:     {len(found)}\n")

    for job in found[:args.show]:
        print(f"  {job.title}")
        print(f"    {job.url}")
        print(f"    place: {job.place or '-'}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
