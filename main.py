"""Run the joblister pipeline: scrape boards, filter, fetch details, store.

Usage:
    python main.py                          # every stage
    python main.py --limit 5                # a small end-to-end smoke run
    python main.py --from filter --to store # reuse an existing temp/jobs.csv
    python main.py --to refilter            # stop before touching the DB

This file is argparse and nothing else. The stages themselves live in
`job_scraper.pipeline` as ordinary functions, so a scheduled job, a web view
or a notebook can drive them without going through a command line.
"""

import argparse
import sys

from job_scraper import pipeline


def main(argv=None) -> int:
    """Parse arguments and run the requested pipeline stages.

    Args:
        argv: Argument list, defaulting to sys.argv[1:].

    Returns:
        Process exit code: 0 on success, 2 if the stage range is invalid.
    """
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--from", dest="from_stage", default=pipeline.STAGES[0],
                        choices=pipeline.STAGES,
                        help="first stage to run (default: %(default)s)")
    parser.add_argument("--to", dest="to_stage", default=pipeline.STAGES[-1],
                        choices=pipeline.STAGES,
                        help="last stage to run (default: %(default)s)")
    parser.add_argument("--limit", type=int, default=0,
                        help="cap boards scraped and postings fetched; "
                             "0 means no cap")
    parser.add_argument("--workers", type=int, default=8,
                        help="threads for the detail stage "
                             "(default: %(default)s)")
    parser.add_argument("--no-resume", action="store_true",
                        help="refetch postings the detail CSV already holds")
    parser.add_argument(
        "--render", action="store_true",
        help="last-resort browser pass for boards that build their listing "
             "in JS. Costs seconds per board. Needs: pip install playwright "
             "&& playwright install chromium",
    )

    args = parser.parse_args(argv)

    renderer = None

    if args.render:
        # Imported here, not at module scope: Playwright is an opt-in extra
        # and this must keep running on a machine with no browser.
        from job_scraper.render import render as renderer

    try:
        results = pipeline.run(
            from_stage=args.from_stage,
            to_stage=args.to_stage,
            limit=args.limit,
            render=renderer,
            workers=args.workers,
            resume=not args.no_resume,
            on_progress=print,
        )
    except ValueError as exc:
        parser.error(str(exc))

        return 2

    print("\n=== summary ===")

    for stage, stats in results.items():
        # Scalars only. `per_board` and `counts` are breakdowns a caller can
        # read off the return value; printing 101 boards here would bury the
        # five numbers that matter.
        readable = ", ".join(
            f"{key}={value}" for key, value in stats.items()
            if isinstance(value, int)
        )
        print(f"{stage:9} {readable}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
