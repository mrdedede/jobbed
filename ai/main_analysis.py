"""List the postings a user can pick for grading, and grade the ones picked.

`list_due` is the menu: ungraded postings narrowed by the user's keywords and
blacklist. `run_analysis` grades the job ids the user picked from it, one
gateway call per posting, run concurrently up to `concurrency` -- see
`ai.analysis`. Postings already graded are skipped, so a rerun is idempotent.
"""

import argparse
import asyncio
import sys
from collections import defaultdict
from typing import Callable, Iterable, Optional, Sequence
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

#: Candidate rows filtered at a time: bounds memory to one chunk's
#: descriptions instead of the whole window's (3.6 GB at 84k postings).
CHUNK = 2000

#: (user_id, window) -> (key, filtered rows). Filtering every ungraded posting
#: takes ~90 s on a phone CPU with an 84k-posting backlog, so the result is
#: reused until the postings or the user's terms change (see `list_due`).
#: Process-local: the api runs one worker on purpose (api/Dockerfile).
# ponytail: unbounded, one entry per user and window asked for; small rows.
_due_cache: dict = {}
_due_locks: defaultdict = defaultdict(asyncio.Lock)


def _select_due(chunks: Iterable, keywords, blacklist):
    """Narrow candidate rows to this user's matches, newest first.

    Filters chunk by chunk (`filter_frame` is row-wise, so the result is the
    same as one pass) and keeps no description, so memory stays at one chunk.
    Ties on `scraped_at` -- common, a scrape batch shares one timestamp --
    fall back to keyword hits, strongest first.

    Args:
        chunks: Lists of `db.analysis.select_candidates` rows.
        keywords: The user's keyword terms.
        blacklist: The user's blacklist terms.

    Returns:
        (id, company, title, place, url, scraped_at) tuples -- no
        description, it is large and the url has it.
    """
    kept = [filter_frame(pd.DataFrame(rows, columns=CANDIDATE_COLUMNS),
                         keywords, blacklist).drop(columns="description")
            for rows in chunks if rows]

    if not kept:
        return []

    kept = pd.concat(kept, ignore_index=True)
    by = ["scraped_at"] + (["keyword_hits"] if "keyword_hits" in kept else [])
    kept = kept.sort_values(by, ascending=False, kind="stable")

    return list(kept[["id", "company", "title", "place", "url",
                      "scraped_at"]].itertuples(index=False, name=None))


async def list_due(user_id: UUID, window: str = "-24 hours") -> list:
    """The postings this user can pick from for an analysis run.

    Ungraded postings in the window, narrowed by the user's keywords and
    blacklist, newest first; see `_select_due`. Needs no CV, calls no model.

    The filtered list is cached per user and window, keyed on the user's
    terms and on the posting count and newest scrape, so it is recomputed only
    when an ingest adds postings or the terms change. Grading and the window
    sliding forward are applied to the cached list on every call, cheaply.

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
    count, newest, start = await asyncio.to_thread(
        db_analysis.candidates_state, window)
    key = (tuple(keywords), tuple(blacklist), count, newest)

    # One computation per user and window at a time: the page asks several
    # times at once, and each would otherwise redo the whole filter.
    async with _due_locks[(user_id, window)]:
        hit = _due_cache.get((user_id, window))

        if hit is None or hit[0] != key:
            # Streaming, filtering and pandas are blocking; off the loop.
            rows = await asyncio.to_thread(lambda: _select_due(
                db_analysis.iter_candidates(user_id, window, CHUNK),
                keywords, blacklist))
            hit = _due_cache[(user_id, window)] = (key, rows)

    graded = await asyncio.to_thread(db_analysis.select_graded_ids, user_id)

    return [row for row in hit[1] if row[0] not in graded and row[5] >= start]


async def run_analysis(user_id: UUID, job_ids: Sequence[UUID],
                       model: str = gateway.HAIKU_MODEL,
                       concurrency: int = 4,
                       on_progress: Optional[Callable[[UUID, Optional[int], str], None]] = None
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
        on_progress: Optional callback given, per posting, its id, its grade
            (None when it failed) and a printable line.

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

        grade_value = None

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
                grade_value = verdict["adequation_grade"]
                note = f"{verdict['adequation_grade']:3}"
            except Exception as exc:
                counts["failed"] += 1
                note = f"FAIL {type(exc).__name__}: {exc}"

        if on_progress:
            on_progress(job_id, grade_value,
                        f"[{index}/{len(jobs)}] {note}  {company} - {title}")

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
        args.user_id, picked, model=args.model,
        on_progress=lambda _id, _grade, line: print(line)))
    print(f"\n{stats['analysed']} analysed, {stats['failed']} failed, "
          f"{len(due) - len(picked)} remaining")

    return 0


if __name__ == "__main__":
    sys.exit(main())
