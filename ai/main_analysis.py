"""List the postings a user can pick for grading, and grade the ones picked.

`list_due` is the menu: ungraded postings narrowed by the user's keywords and
blacklist. `run_analysis` grades the job ids the user picked from it, one
gateway call per posting, run concurrently up to `concurrency` -- see
`ai.analysis`. Postings already graded are skipped, so a rerun is idempotent.
"""

import argparse
import asyncio
import sys
from typing import Callable, Optional, Sequence
from uuid import UUID

import httpx
import pandas as pd

from ai import analysis, gateway
from db import analysis as db_analysis
from db import jobs as db_jobs
from db import users
from job_scraper.filters import filter_frame
from ai.exceptions import NoActiveCv


CANDIDATE_COLUMNS = ["id", "company", "title", "url", "place", "description",
                     "scraped_at"]


def _select_due(rows, keywords, blacklist):
    """Narrow candidate rows to this user's matches, newest first.

    Ties on `scraped_at` -- common, a scrape batch shares one timestamp --
    fall back to keyword hits, strongest first.

    Args:
        rows: Output of `db.analysis.select_candidates`.
        keywords: The user's keyword terms.
        blacklist: The user's blacklist terms.

    Returns:
        (id, company, title, place, url, scraped_at) tuples -- no
        description, it is large and the url has it.
    """
    if not rows:
        return []

    frame = pd.DataFrame(rows, columns=CANDIDATE_COLUMNS)
    kept = filter_frame(frame, keywords, blacklist)

    by = ["scraped_at"] + (["keyword_hits"] if "keyword_hits" in kept else [])
    kept = kept.sort_values(by, ascending=False, kind="stable")

    return list(kept[["id", "company", "title", "place", "url",
                      "scraped_at"]].itertuples(index=False, name=None))


async def list_due(user_id: UUID, window: str = "-24 hours") -> list:
    """The postings this user can pick from for an analysis run.

    Ungraded postings in the window, narrowed by the user's keywords and
    blacklist, newest first; see `_select_due`. Needs no CV, calls no model.

    Args:
        user_id: The user asking.
        window: Postgres interval string, e.g. "-7 days".

    Returns:
        (id, company, title, place, url, scraped_at) tuples.
    """
    keywords = [t for _, t in await asyncio.to_thread(
        users.select_user_keywords, user_id)]
    blacklist = [t for _, t in await asyncio.to_thread(
        users.select_user_blacklist, user_id)]
    # ponytail: loads every ungraded posting in the window; window bounds it.
    rows = await asyncio.to_thread(
        db_analysis.select_candidates, user_id, 0, window)
    # pandas is CPU-bound; keep it off the loop other requests share.
    return await asyncio.to_thread(_select_due, rows, keywords, blacklist)


async def run_analysis(user_id: UUID, job_ids: Sequence[UUID],
                       model: str = gateway.HAIKU_MODEL,
                       concurrency: int = 4,
                       on_progress: Optional[Callable[[str], None]] = None
                       ) -> dict:
    """Grade the postings the user picked and store each verdict.

    The user's keywords and blacklist do not apply here: they shape the
    list the user picks from (`list_due`), not the pick itself.

    Args:
        user_id: Whose CV to grade against and whose analyses to write.
        job_ids: Postings to grade, in order. Unknown ones and ones this user
            already graded are skipped, so a retry never pays twice.
        model: Which gateway model grades.
        concurrency: Calls in flight at once.
        on_progress: Optional callback given one line per posting.

    Returns:
        Dict with `analysed` and `failed` counts, `skipped` (asked for but
        unknown or already graded) and `job_ids` (postings whose verdict was
        stored this run).

    Raises:
        NoActiveCv: The user has no active CV to grade against.
    """
    active = await asyncio.to_thread(users.select_active_user_cv, user_id)

    if active is None:
        raise NoActiveCv(f"user {user_id} has no active CV")

    cv_md = active[1]
    found = {row[0]: row for row in await asyncio.to_thread(
        db_analysis.select_ungraded_jobs, user_id, job_ids)}
    jobs = [found[i] for i in dict.fromkeys(job_ids) if i in found]
    gate = asyncio.Semaphore(concurrency)
    counts = {"analysed": 0, "failed": 0}
    stored = []

    async def grade(index: int, job, client: httpx.AsyncClient) -> None:
        job_id, company, title, _ = job

        async with gate:
            try:
                verdict = await analysis.analyze(job, cv_md, model, client)
                await asyncio.to_thread(db_analysis.insert_analysis, [
                    user_id, verdict["adequation_grade"],
                    verdict["depth_analysis"], model, job_id,
                ])

                if verdict["place"]:
                    await asyncio.to_thread(
                        db_jobs.fill_place, job_id, verdict["place"])

                counts["analysed"] += 1
                stored.append(job_id)
                note = f"{verdict['adequation_grade']:3}"
            except Exception as exc:
                counts["failed"] += 1
                note = f"FAIL {type(exc).__name__}: {exc}"

        if on_progress:
            on_progress(f"[{index}/{len(jobs)}] {note}  {company} - {title}")

    if jobs:
        async with httpx.AsyncClient(timeout=gateway.TIMEOUT) as client:
            await asyncio.gather(*(
                grade(i, job, client) for i, job in enumerate(jobs, start=1)))

    return {**counts, "skipped": len(set(job_ids)) - len(jobs),
            "job_ids": stored}


def main(argv: Optional[Sequence[str]] = None) -> int:
    """Grade one user's pending postings from the command line."""
    parser = argparse.ArgumentParser(
        prog="python -m ai.main_analysis",
        description="Grade the pending postings of one user.")
    parser.add_argument("user_id", type=UUID)
    parser.add_argument("--model", default=gateway.HAIKU_MODEL,
                        help="any model the gateway serves")
    parser.add_argument("--limit", type=int, default=20,
                        help="cap on postings graded; 0 means all")
    parser.add_argument("--window", default="-24 hours",
                        help='Postgres interval, e.g. "-7 days"')
    args = parser.parse_args(argv)

    due = asyncio.run(list_due(args.user_id, args.window))
    picked = [row[0] for row in (due[:args.limit] if args.limit else due)]
    stats = asyncio.run(run_analysis(
        args.user_id, picked, model=args.model, on_progress=print))
    print(f"\n{stats['analysed']} analysed, {stats['failed']} failed, "
          f"{len(due) - len(picked)} remaining")

    return 0


if __name__ == "__main__":
    sys.exit(main())
