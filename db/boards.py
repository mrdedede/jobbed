"""Postgres access for the shared list of career boards to scrape.

Covers job_boards only. Boards are shared across users, like job_postings.
SQLAlchemy Core only, no ORM Session. Schema lives in db.tables, versioned by
Alembic, not created here.
"""

import csv
from pathlib import Path
from typing import List, Optional, Tuple

from sqlalchemy import select, func
from sqlalchemy.dialects.postgresql import insert as pg_insert

from db.engine import engine
from db.tables import job_boards
from job_scraper import paths


def upsert_boards(rows: List[Tuple[str, str]]) -> int:
    """Insert boards by URL, refreshing the company name of known ones.

    Args:
        rows: (company, url) pairs.

    Returns:
        How many rows were given -- an upsert does not tell new from known.

    Raises:
        RuntimeError: If the database rejects the batch.
    """
    if not rows:
        return 0

    # A URL listed twice in one statement would make ON CONFLICT DO UPDATE
    # touch the same row twice, which Postgres rejects; last one wins.
    unique = {url: company for company, url in rows}
    payload = [{"company": company, "url": url}
               for url, company in unique.items()]

    # SQL: INSERT INTO job_boards (company, url) VALUES (...), ...
    #      ON CONFLICT (md5(url)) DO UPDATE SET company = excluded.company
    stmt = pg_insert(job_boards).values(payload)
    stmt = stmt.on_conflict_do_update(
        index_elements=[func.md5(job_boards.c.url)],
        set_={"company": stmt.excluded.company},
    )
    try:
        with engine.begin() as conn:
            conn.execute(stmt)
    except Exception as exc:
        raise RuntimeError(f"Database insert failed: {exc}") from exc

    return len(unique)


def select_active_boards() -> List[dict]:
    """Every board the nightly scrape should visit.

    Returns:
        List of {"company", "url"} dicts, the shape main_scraper's
        scrape_one_board takes.
    """
    # SQL: SELECT company, url FROM job_boards WHERE active ORDER BY company, url
    stmt = (
        select(job_boards.c.company, job_boards.c.url)
        .where(job_boards.c.active.is_(True))
        .order_by(job_boards.c.company, job_boards.c.url)
    )
    with engine.connect() as conn:
        return [{"company": c, "url": u} for c, u in conn.execute(stmt)]


def count_boards() -> int:
    """How many boards are stored, active or not."""
    with engine.connect() as conn:
        return conn.execute(select(func.count()).select_from(job_boards)).scalar_one()


def import_boards_csv(path: Optional[Path] = None) -> int:
    """One-off load of user_info/job_boards.csv into job_boards. Idempotent.

    Args:
        path: CSV with `company,url` columns. Defaults to paths.BOARDS_CSV.

    Returns:
        How many boards the file held.
    """
    path = path or paths.BOARDS_CSV

    with path.open(newline="", encoding="utf-8") as handle:
        rows = [(r["company"].strip(), r["url"].strip())
                for r in csv.DictReader(handle) if (r.get("url") or "").strip()]

    return upsert_boards(rows)


if __name__ == "__main__":
    n = import_boards_csv()
    print(f"{n} boards in file, {count_boards()} in job_boards")
