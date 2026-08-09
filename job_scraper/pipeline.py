"""The four scraping stages, joined up and callable as one run.

Until now nothing joined them: `main.py` was empty, the stages disagreed on
filenames, and both filter calls sat commented out. Running the project meant
knowing the right four commands in the right order.

Every stage here is a plain function returning a stats dict and printing
nothing. That is deliberate and it is the whole design: a CLI, a Streamlit
page, an HTTP handler and a nightly cron job all want the same four calls and
disagree only about what to do with the numbers. `main.py` supplies `print`;
something else can supply a progress bar or a log line.

Stages hand off through CSV files in `temp/` rather than in memory. Those
files are not incidental -- the detail stage resumes from its own output after
a crash, and every measurement in REFACTORING.md was taken from them after the
fact.
"""

from typing import Callable, Dict, List, Optional

from job_scraper import filters, paths
from job_scraper.main_scraper import scrape_boards
from job_scraper.post_scraper import scrape_details

#: Stage names in run order. The CLI's --from/--to index into this.
STAGES: List[str] = ["scrape", "filter", "detail", "refilter", "store"]


def filter_first(on_progress: Optional[Callable[[str], None]] = None) -> dict:
    """Stage 2: filter scraped rows on title and URL, before fetching pages.

    Args:
        on_progress: Optional callback given one summary line.

    Returns:
        Dict with `kept` count and the `output` path.
    """
    frame = filters.first_filter()
    frame.to_csv(paths.FIRST_FILTERED_CSV, index=False)

    if on_progress:
        on_progress(f"{len(frame)} rows kept -> {paths.FIRST_FILTERED_CSV}")

    return {"kept": len(frame), "output": paths.FIRST_FILTERED_CSV}


def filter_second(on_progress: Optional[Callable[[str], None]] = None) -> dict:
    """Stage 4: filter detailed rows on their descriptions.

    Args:
        on_progress: Optional callback given one summary line.

    Returns:
        Dict with `kept` count and the `output` path.
    """
    frame = filters.second_filter()
    frame.to_csv(paths.FILTERED_DETAILED_CSV, index=False)

    if on_progress:
        on_progress(f"{len(frame)} rows kept -> "
                    f"{paths.FILTERED_DETAILED_CSV}")

    return {"kept": len(frame), "output": paths.FILTERED_DETAILED_CSV}


def store(on_progress: Optional[Callable[[str], None]] = None) -> dict:
    """Stage 5: insert the surviving postings into SQLite.

    Imported inside the function so the scraping stages stay usable without
    touching the database at all.

    Args:
        on_progress: Optional callback given one summary line.

    Returns:
        Dict with `inserted` and `skipped` counts.
    """
    from db import db_connection

    db_connection.create_tables()
    inserted, skipped = db_connection.insert_jobs()

    if on_progress:
        # db_connection.DB_ADDRESS, not paths.DB_PATH: they are the same value
        # in production but only the former follows a caller that repointed it.
        on_progress(f"{inserted} inserted, {skipped} already known "
                    f"-> {db_connection.DB_ADDRESS}")

    return {"inserted": inserted, "skipped": skipped}


def run(from_stage: str = STAGES[0], to_stage: str = STAGES[-1],
        limit: int = 0, render: Optional[Callable] = None,
        workers: int = 8, resume: bool = True,
        on_progress: Optional[Callable[[str], None]] = None) -> Dict[str, dict]:
    """Run a contiguous slice of the pipeline.

    Args:
        from_stage: First stage to run; one of STAGES.
        to_stage: Last stage to run, inclusive.
        limit: Cap on boards (scrape) and postings (detail). 0 means no cap.
        render: Optional Playwright renderer for JS-built listings.
        workers: Thread pool size for the detail stage.
        resume: Whether the detail stage skips URLs it already fetched.
        on_progress: Optional callback given progress lines from every stage.

    Returns:
        Dict of stage name -> that stage's stats, for the stages that ran.

    Raises:
        ValueError: If a stage name is unknown or the range runs backwards.
    """
    for name in (from_stage, to_stage):
        if name not in STAGES:
            raise ValueError(
                f"unknown stage {name!r}; expected one of {STAGES}"
            )

    start, stop = STAGES.index(from_stage), STAGES.index(to_stage)

    if start > stop:
        raise ValueError(f"{from_stage!r} runs after {to_stage!r}")

    def announce(name: str) -> None:
        if on_progress:
            on_progress(f"\n=== {name} ===")

    results: Dict[str, dict] = {}

    for name in STAGES[start:stop + 1]:
        announce(name)

        if name == "scrape":
            results[name] = scrape_boards(
                limit=limit, render=render, on_board=on_progress
            )
        elif name == "filter":
            results[name] = filter_first(on_progress)
        elif name == "detail":
            results[name] = scrape_details(
                limit=limit, workers=workers, resume=resume,
                on_progress=on_progress,
            )
        elif name == "refilter":
            results[name] = filter_second(on_progress)
        elif name == "store":
            results[name] = store(on_progress)

    return results
