"""Grade the postings the model has not seen yet and store each verdict.

One `claude` CLI call per posting, serially -- see `ai.analysis`. Which
postings are due is `db_connection.select_jobs_to_analyse`'s decision, and it
already excludes anything with a row in ai_analysis, so a rerun is idempotent.
"""

import sys
from typing import Callable, Optional

from ai import analysis
from db import db_connection


def run_analysis(limit: int = 20, window: str = "-24 hours",
                 on_progress: Optional[Callable[[str], None]] = None) -> dict:
    """Grade up to `limit` ungraded postings and insert the results.

    Args:
        limit: Cap on postings graded; 0 means every one that is due.
        window: SQLite modifier bound into `datetime('now', ?)`. Widen it to
            pick up postings whose grading failed on an earlier run.
        on_progress: Optional callback given one line per posting. Prints
            nothing itself, so a Streamlit page can take the same lines a
            terminal does.

    Returns:
        Dict with `analysed` and `failed` counts and the number of postings
        that were `due`.
    """
    jobs = db_connection.select_jobs_to_analyse(limit, window)
    analysed = failed = 0

    for index, job in enumerate(jobs, start=1):
        job_id, company, title, _ = job

        try:
            verdict = analysis.send_claude_request(job)

            # send_claude_request returns None on a non-zero exit or
            # unparseable output. Subscripting that raises TypeError, which
            # the old bare `except` reported as an unexplained failure.
            if verdict is None:
                raise RuntimeError("no verdict returned")

            db_connection.insert_analysis([
                verdict["adequation_grade"], verdict["depth_analysis"],
                analysis.HAIKU_MODEL, job_id,
            ])
            analysed += 1
            note = f"{verdict['adequation_grade']:3}"
        except Exception as exc:
            failed += 1
            note = f"FAIL {type(exc).__name__}: {exc}"

        if on_progress:
            on_progress(f"[{index}/{len(jobs)}] {note}  {company} - {title}")

    return {"analysed": analysed, "failed": failed, "due": len(jobs)}


def main() -> int:
    """Grade the pending postings, reporting each one.

    Returns:
        0 on success.
    """
    stats = run_analysis(20, on_progress=print)
    print(f"\n{stats['analysed']} analysed, {stats['failed']} failed")

    return 0


if __name__ == "__main__":
    sys.exit(main())
