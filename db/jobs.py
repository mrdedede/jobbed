"""Postgres connection management for scraped job postings.

Covers job_postings only -- what the nightly CronJobs write to and read
from. SQLAlchemy Core only, no ORM Session, no mapped objects: query cost
stays equal to the SQL actually run. Schema lives in db.tables, versioned by
Alembic, not created here.
"""

import csv
from typing import List, Optional, Tuple

from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy import select

from db.engine import engine
from db.tables import job_postings
from job_scraper import paths

FILTERED_DETAILED_JOBS = paths.FILTERED_DETAILED_CSV


# INSERTIONS

def insert_jobs(rows: Optional[List[Tuple]] = None) -> Tuple[int, int]:
    """Insert the postings into job_postings, skipping known URLs.

    Args:
        rows: Rows to insert, as (company, title, description, url, place).
            Defaults to whatever the refilter stage left in the filtered
            detailed CSV, which is what the pipeline wants.

    Returns:
        Tuple of (number of new jobs inserted, number of duplicates skipped).

    Raises:
        RuntimeError: If the database rejects the batch.
    """
    if not rows:
        rows = _read_jobs_csv()

    payload = [
        {"company": company, "title": title, "description": description,
         "url": url, "place": place}
        for company, title, description, url, place in rows
    ]

    # SQL: INSERT INTO job_postings (company, title, description, url, place)
    #      VALUES (...), (...), ... ON CONFLICT (url) DO NOTHING
    stmt = (
        pg_insert(job_postings)
        .values(payload)
        .on_conflict_do_nothing(index_elements=[job_postings.c.url])
    )

    try:
        with engine.begin() as conn:
            result = conn.execute(stmt)
            inserted = result.rowcount
    except Exception as exc:
        raise RuntimeError(f"Database insert failed: {exc}") from exc

    return inserted, len(rows) - inserted


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

def _read_jobs_csv() -> List[Tuple[str, str, str, str, str]]:
    """Returns the rows available at the filtered detailed job csv.

    Returns:
        List of tuples referring to the job's information.
    """
    with FILTERED_DETAILED_JOBS.open(newline="", encoding="utf-8") as handle:
        return [
            (row.get("company"), row.get("title"), row.get("description"),
             row.get("url"), row.get("place"))
            for row in csv.DictReader(handle)
        ]
