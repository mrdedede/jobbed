"""Grade the postings a user has not been graded on yet and store each verdict.

One gateway call per posting, run concurrently up to `concurrency` -- see
`ai.analysis`. Candidates come from `db.analysis.select_candidates`, which
already excludes anything this user has a row for, so a rerun is idempotent;
they are then narrowed by the user's own keywords and blacklist before any
model call is spent.
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
from db import users
from job_scraper.filters import filter_frame


class NoActiveCv(ValueError):
    """The user has no active CV to grade against."""


CANDIDATE_COLUMNS = ["id", "company", "title", "url", "place", "description",
                     "scraped_at"]


def _select_due(rows, keywords, blacklist, limit):
    """Narrow candidate rows to this user's matches, newest first.

    Ties on `scraped_at` -- common, a scrape batch shares one timestamp --
    fall back to keyword hits, strongest first.

    Args:
        rows: Output of `db.analysis.select_candidates`.
        keywords: The user's keyword terms.
        blacklist: The user's blacklist terms.
        limit: Cap on postings kept, applied after filtering; 0 means none.

    Returns:
        (jobs, filtered_out, remaining): jobs as (id, company, title,
        description) tuples, how many candidates the filter rejected, and
        how many matches `limit` left for a later run.
    """
    if not rows:
        return [], 0, 0

    frame = pd.DataFrame(rows, columns=CANDIDATE_COLUMNS)
    kept = filter_frame(frame, keywords, blacklist)
    filtered_out = len(frame) - len(kept)

    by = ["scraped_at"] + (["keyword_hits"] if "keyword_hits" in kept else [])
    kept = kept.sort_values(by, ascending=False, kind="stable")
    matched = len(kept)

    if limit:
        kept = kept.head(limit)

    jobs = kept[["id", "company", "title", "description"]].itertuples(
        index=False, name=None)

    return list(jobs), filtered_out, matched - len(kept)


async def _load_due(user_id: UUID, limit: int, window: str):
    """Fetch this user's candidates and narrow them; see `_select_due`."""
    keywords = [t for _, t in await asyncio.to_thread(
        users.select_user_keywords, user_id)]
    blacklist = [t for _, t in await asyncio.to_thread(
        users.select_user_blacklist, user_id)]
    # limit=0 at the query: the cap applies after filtering, or it would
    # starve the filter.
    # ponytail: loads every ungraded posting in the window; window bounds it.
    rows = await asyncio.to_thread(
        db_analysis.select_candidates, user_id, 0, window)
    # pandas is CPU-bound; keep it off the loop other requests share.
    return await asyncio.to_thread(
        _select_due, rows, keywords, blacklist, limit)


async def count_due(user_id: UUID, window: str = "-24 hours") -> int:
    """How many postings an analysis run could grade for this user right now.

    Unlike `db.analysis.count_jobs_to_analyse`, this applies the user's
    keywords and blacklist, so it is the number `run_analysis(limit=0)` would
    send to the model. Needs no CV and calls no model.

    Args:
        user_id: The user asking.
        window: Postgres interval string, as in `run_analysis`.

    Returns:
        Matching, ungraded postings in the window.
    """
    jobs, _, _ = await _load_due(user_id, 0, window)

    return len(jobs)


async def run_analysis(user_id: UUID, limit: int = 20,
                       window: str = "-24 hours",
                       model: str = gateway.HAIKU_MODEL,
                       concurrency: int = 4,
                       on_progress: Optional[Callable[[str], None]] = None
                       ) -> dict:
    """Grade up to `limit` matching, ungraded postings and store results.

    Postings are filtered by the user's keywords and blacklist first; with
    no keywords nothing is filtered beyond requiring a description.

    Args:
        user_id: Whose CV to grade against and whose analyses to write.
        limit: Cap on postings graded, applied after filtering and keeping
            the newest (ties: most keyword hits); 0 means every one that
            matches. To page, call again: graded postings drop out of the
            candidates, so each run takes the next `limit`. No offset --
            it would skip postings once earlier pages are graded.
        window: Postgres interval string; widen it to pick up postings whose
            grading failed on an earlier run.
        model: Which gateway model grades.
        concurrency: Calls in flight at once.
        on_progress: Optional callback given one line per posting.

    Returns:
        Dict with `analysed` and `failed` counts, the number `due` (sent to
        the model), `filtered_out` (rejected by the filter, before `limit`),
        `remaining` (matches `limit` left for another run) and `job_ids`
        (postings whose verdict was stored this run).

    Raises:
        NoActiveCv: The user has no active CV to grade against.
    """
    active = await asyncio.to_thread(users.select_active_user_cv, user_id)

    if active is None:
        raise NoActiveCv(f"user {user_id} has no active CV")

    cv_md = active[1]
    jobs, filtered_out, remaining = await _load_due(user_id, limit, window)
    gate = asyncio.Semaphore(concurrency)
    counts = {"analysed": 0, "failed": 0}
    job_ids = []

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
                job_ids.append(job_id)
                note = f"{verdict['adequation_grade']:3}"
            except Exception as exc:
                counts["failed"] += 1
                note = f"FAIL {type(exc).__name__}: {exc}"

        if on_progress:
            on_progress(f"[{index}/{len(jobs)}] {note}  {company} - {title}")

    async with httpx.AsyncClient(timeout=gateway.TIMEOUT) as client:
        await asyncio.gather(*(
            grade(i, job, client) for i, job in enumerate(jobs, start=1)))

    return {**counts, "due": len(jobs), "filtered_out": filtered_out,
            "remaining": remaining, "job_ids": job_ids}


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

    stats = asyncio.run(run_analysis(
        args.user_id, limit=args.limit, window=args.window,
        model=args.model, on_progress=print))
    print(f"\n{stats['analysed']} analysed, {stats['failed']} failed, "
          f"{stats['filtered_out']} filtered out, "
          f"{stats['remaining']} remaining")

    return 0


if __name__ == "__main__":
    sys.exit(main())
