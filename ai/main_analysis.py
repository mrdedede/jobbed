"""Grade the postings the model has not seen yet and store each verdict.

One `claude` CLI call per posting -- see `ai.analysis`. Which postings are due
is `db_connection.select_jobs_to_analyse`'s decision, and it already excludes
anything with a row in ai_analysis, so a rerun is idempotent.
"""

import sys
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Callable, Optional, Tuple

from ai import analysis, call_model
from db import db_connection

#: Half of job_scraper.main_scraper.DEFAULT_WORKERS (not imported -- these are
#: two independent pipeline stages). Lower because each unit of work here
#: spawns a whole `claude` CLI process, not a single HTTP request.
DEFAULT_WORKERS = 2


@dataclass
class AnalysisResult:
    """One posting's grading outcome, before it is written to the DB.

    Kept side-effect free (no DB insert, no callback) so it can run on any
    thread and be tested on its own.
    """

    job_id: int
    company: str
    title: str
    verdict: Optional[dict]
    error: Optional[Exception]


def grade_one_job(job: Tuple) -> AnalysisResult:
    """Grade a single posting.

    Args:
        job: A (id, company, title, description) row, as returned by
            `db_connection.select_jobs_to_analyse`.

    Returns:
        An AnalysisResult carrying either the verdict or the exception that
        stopped this job -- never raises, so one bad posting can't take down
        the pool.
    """
    job_id, company, title, _ = job

    try:
        verdict = analysis.analyze(job)

        # send_claude_request returns None on a non-zero exit or unparseable
        # output. Subscripting that raises TypeError, which the old bare
        # `except` reported as an unexplained failure.
        if verdict is None:
            raise RuntimeError("no verdict returned")
    except Exception as exc:
        return AnalysisResult(job_id, company, title, verdict=None, error=exc)

    return AnalysisResult(job_id, company, title, verdict=verdict, error=None)


def run_analysis(limit: int = 20, window: str = "-24 hours",
                 on_progress: Optional[Callable[[str], None]] = None,
                 workers: int = DEFAULT_WORKERS) -> dict:
    """Grade up to `limit` ungraded postings and insert the results.

    Args:
        limit: Cap on postings graded; 0 means every one that is due.
        window: SQLite modifier bound into `datetime('now', ?)`. Widen it to
            pick up postings whose grading failed on an earlier run.
        on_progress: Optional callback given one line per posting. Prints
            nothing itself, so a Streamlit page can take the same lines a
            terminal does.
        workers: Thread pool size. Postings are graded concurrently; each
            `insert_analysis` call still runs on the main thread in job order.

    Returns:
        Dict with `analysed` and `failed` counts and the number of postings
        that were `due`.
    """
    jobs = db_connection.select_jobs_to_analyse(limit, window)
    analysed = failed = 0

    with ThreadPoolExecutor(max_workers=workers) as pool:
        # pool.map yields in input order on this thread, so the DB insert and
        # on_progress call below stay single-threaded and in the same order
        # as before, with no lock needed.
        for index, result in enumerate(pool.map(grade_one_job, jobs),
                                       start=1):
            if result.error is not None:
                failed += 1
                note = f"FAIL {type(result.error).__name__}: {result.error}"
            else:
                db_connection.insert_analysis([
                    result.verdict["adequation_grade"],
                    result.verdict["depth_analysis"],
                    call_model.HAIKU_MODEL, result.job_id,
                ])
                analysed += 1
                note = f"{result.verdict['adequation_grade']:3}"

            if on_progress:
                on_progress(f"[{index}/{len(jobs)}] {note}  "
                            f"{result.company} - {result.title}")

    return {"analysed": analysed, "failed": failed, "due": len(jobs)}


def main() -> int:
    """Grade the pending postings, reporting each one.

    Returns:
        0 on success.
    """
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--limit", type=int, default=20)
    parser.add_argument("--workers", type=int, default=DEFAULT_WORKERS)
    args = parser.parse_args()

    stats = run_analysis(args.limit, on_progress=print, workers=args.workers)
    print(f"\n{stats['analysed']} analysed, {stats['failed']} failed")

    return 0


if __name__ == "__main__":
    sys.exit(main())
