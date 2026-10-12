"""Grade the postings a user has not been graded on yet and store each verdict.

One gateway call per posting, run concurrently up to `concurrency` -- see
`ai.analysis`. Which postings are due is `db.analysis.select_jobs_to_analyse`'s
decision, and it already excludes anything this user has a row for, so a rerun
is idempotent.
"""

import asyncio
import sys
from typing import Callable, Optional
from uuid import UUID

import httpx

from ai import analysis, gateway
from db import analysis as db_analysis
from db import users


async def run_analysis(user_id: UUID, limit: int = 20,
                       window: str = "-24 hours",
                       model: str = gateway.HAIKU_MODEL,
                       concurrency: int = 4,
                       on_progress: Optional[Callable[[str], None]] = None
                       ) -> dict:
    """Grade up to `limit` ungraded postings for one user and store results.

    Args:
        user_id: Whose CV to grade against and whose analyses to write.
        limit: Cap on postings graded; 0 means every one that is due.
        window: Postgres interval string; widen it to pick up postings whose
            grading failed on an earlier run.
        model: Which gateway model grades.
        concurrency: Calls in flight at once.
        on_progress: Optional callback given one line per posting.

    Returns:
        Dict with `analysed` and `failed` counts and the number `due`.

    Raises:
        ValueError: The user has no active CV to grade against.
    """
    active = await asyncio.to_thread(users.select_active_user_cv, user_id)

    if active is None:
        raise ValueError(f"user {user_id} has no active CV")

    cv_md = active[1]
    jobs = await asyncio.to_thread(
        db_analysis.select_jobs_to_analyse, user_id, limit, window)
    gate = asyncio.Semaphore(concurrency)
    counts = {"analysed": 0, "failed": 0}

    async def grade(index: int, job, client: httpx.AsyncClient) -> None:
        job_id, company, title, _ = job

        async with gate:
            try:
                verdict = await analysis.analyze(job, cv_md, model, client)
                await asyncio.to_thread(db_analysis.insert_analysis, [
                    user_id, verdict["adequation_grade"],
                    verdict["depth_analysis"], model, job_id,
                ])
                counts["analysed"] += 1
                note = f"{verdict['adequation_grade']:3}"
            except Exception as exc:
                counts["failed"] += 1
                note = f"FAIL {type(exc).__name__}: {exc}"

        if on_progress:
            on_progress(f"[{index}/{len(jobs)}] {note}  {company} - {title}")

    async with httpx.AsyncClient(timeout=gateway.TIMEOUT) as client:
        await asyncio.gather(*(
            grade(i, job, client) for i, job in enumerate(jobs, start=1)))

    return {**counts, "due": len(jobs)}


def main() -> int:
    """Grade the pending postings of the user id given as argv[1]."""
    if len(sys.argv) != 2:
        print("usage: python -m ai.main_analysis <user_id>")
        return 2

    stats = asyncio.run(run_analysis(UUID(sys.argv[1]), on_progress=print))
    print(f"\n{stats['analysed']} analysed, {stats['failed']} failed")

    return 0


if __name__ == "__main__":
    sys.exit(main())
