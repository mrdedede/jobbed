"""Postgres connection management for scraped job postings.

Covers job_postings only -- what the nightly CronJobs write to and read
from. SQLAlchemy Core only, no ORM Session, no mapped objects: query cost
stays equal to the SQL actually run. Schema lives in db.tables, versioned by
Alembic, not created here.
"""

import csv
from typing import List, Optional, Tuple

from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy import func, or_, select, update

from db.engine import engine
from db.tables import job_postings
from job_scraper import paths

_INSERT_CHUNK = 5000  # 5000 * 7 params = 35k, under the 65535 cap


# INSERTIONS

def insert_jobs(rows: Optional[List[Tuple]] = None) -> Tuple[int, int]:
    """Insert the postings into job_postings, skipping known URLs.

    Args:
        rows: Rows to insert, as (company, title, description, url, place,
            via, ats). None reads whatever the detail stage left in the
            detailed CSV, which is what the pipeline wants.

    Rows with no description or via "none" are a dead page, not a posting.
    Storing one would mark its URL known and it would never be retried, so
    they are left out and counted with the skipped.

    Returns:
        Tuple of (number of new jobs inserted, number of rows skipped:
        duplicates and dead pages).

    Raises:
        RuntimeError: If the database rejects the batch.
    """
    # None means "whatever the detail stage left"; an explicit empty list is
    # an explicit nothing (RenderPostings with no recovered rows).
    if rows is None:
        rows = _read_jobs_csv()

    total = len(rows)
    rows = [row for row in rows if row[2] and row[5] != "none"]

    # An empty .values([]) compiles to INSERT ... DEFAULT VALUES: one blank row.
    if not rows:
        return 0, total

    payload = [
        {"company": company, "title": title, "description": description,
         "url": url, "place": place, "via": via, "ats": ats}
        for company, title, description, url, place, via, ats in rows
    ]

    # Postgres caps a statement at 65535 bind params; 7 per row -> chunk.
    inserted = 0

    try:
        with engine.begin() as conn:
            for start in range(0, len(payload), _INSERT_CHUNK):
                # SQL: INSERT INTO job_postings (company, title, description,
                #      url, place, via, ats) VALUES (...), (...), ...
                #      ON CONFLICT (md5(url)) DO NOTHING RETURNING id
                stmt = (
                    pg_insert(job_postings)
                    .values(payload[start:start + _INSERT_CHUNK])
                    .on_conflict_do_nothing(
                        index_elements=[func.md5(job_postings.c.url)])
                    # rowcount is -1 for a multi-row INSERT; count RETURNING.
                    .returning(job_postings.c.id)
                )
                inserted += len(conn.execute(stmt).all())
    except Exception as exc:
        raise RuntimeError(f"Database insert failed: {exc}") from exc

    return inserted, total - inserted


def fill_place(job_id, place: str) -> None:
    """Set a posting's place, only if it has none yet.

    The grading LLM's guess must never overwrite what the page stated.

    Args:
        job_id: The posting.
        place: Location the model read from the posting.
    """
    # SQL: UPDATE job_postings SET place = ?
    #      WHERE id = ? AND (place IS NULL OR place = '')
    stmt = (
        update(job_postings)
        .where(job_postings.c.id == job_id)
        .where(or_(job_postings.c.place.is_(None), job_postings.c.place == ""))
        .values(place=place)
    )
    with engine.begin() as conn:
        conn.execute(stmt)


# SELECTIONS

def select_job_urls() -> set:
    """Every posting URL already stored.

    Returns:
        Set of URLs, for skipping postings the database already holds.
    """
    # SQL: SELECT url FROM job_postings
    with engine.connect() as conn:
        return {url for (url,) in conn.execute(select(job_postings.c.url))}


# UTILS

def _read_jobs_csv() -> List[Tuple[str, str, str, str, str, str, str]]:
    """Returns the rows available at the detailed job csv.

    Returns:
        List of tuples referring to the job's information.
    """
    with paths.DETAILED_CSV.open(newline="", encoding="utf-8") as handle:
        return [
            (row.get("company"), row.get("title"), row.get("description"),
             row.get("url"), row.get("place"), row.get("via"),
             row.get("ats") or None)
            for row in csv.DictReader(handle)
        ]
