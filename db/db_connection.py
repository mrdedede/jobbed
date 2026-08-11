"""SQLite schema and connection management for job scraping results.

This module defines DDL for persisting scraped jobs, analyses, and generated
CVs, and provides the insert path the pipeline's last stage calls.

Every statement binds with `?`. sqlite3 does not understand PostgreSQL's `$1`
placeholders -- it treats them as numbered parameters with an entirely
different meaning -- and several statements here carried them, which is why
they had never successfully run.
"""

import csv
import json
import sqlite3
from typing import List, Optional, Sequence, Tuple

from job_scraper import paths

DB_ADDRESS = paths.DB_PATH
FILTERED_DETAILED_JOBS = paths.FILTERED_DETAILED_CSV

# MIGRATIONS

CREATE_JOB_DATA_TABLE = """CREATE TABLE IF NOT EXISTS job_data(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    company TEXT,
    title TEXT,
    description TEXT,
    url TEXT UNIQUE,
    place TEXT,
    timestamp DATETIME DEFAULT CURRENT_TIMESTAMP);
"""

CREATE_AI_ANALYSIS_TABLE = """CREATE TABLE IF NOT EXISTS ai_analysis(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    adequation_grade INT,
    depth_analysis TEXT,
    ai_model TEXT,
    job_id BIGINT,
    FOREIGN KEY (job_id) REFERENCES job_data(id));
"""

CREATE_GENERATED_CV_TABLE = """CREATE TABLE IF NOT EXISTS generated_cv(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    locale TEXT,
    cv JSON,
    job_id BIGINT,
    ai_analysis_id BIGINT,
    timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (job_id) REFERENCES job_data(id),
    FOREIGN KEY (ai_analysis_id) REFERENCES ai_analysis(id));
"""

# INSERTIONS

INSERT_NEW_JOB_DATA = """INSERT OR IGNORE INTO job_data(
    company, title, description, url, place)
    VALUES(?, ?, ?, ?, ?)"""

INSERT_NEW_AI_ANALYSIS = """INSERT INTO ai_analysis(
    adequation_grade, depth_analysis, ai_model, job_id)
    VALUES(?, ?, ?, ?)"""

# Four columns, not the eight per-section ones this used to name -- none of
# those exist in generated_cv, so the statement had never run. The CV itself is
# one JSON blob, which is what the table declares.
INSERT_NEW_GENERATED_CV = """INSERT INTO generated_cv(
    locale, cv, job_id, ai_analysis_id, timestamp)
    VALUES(?, ?, ?, ?, CURRENT_TIMESTAMP)"""

# SELECTIONS

SELECT_JOB_BY_URL = """SELECT * FROM job_data
    WHERE url = ?;
"""

# The window is bound, not baked in: a posting whose analysis failed on the
# API side is unreachable once it ages out of the default 24 hours, and the
# NOT IN already makes a rerun idempotent. Pass a wider one to catch up.
SELECT_JOBS_TO_ANALYSE = """SELECT id, company, title, description
    FROM job_data
    WHERE id NOT IN (SELECT job_id FROM ai_analysis)
    AND timestamp >= datetime('now', ?)
    ORDER BY id;
"""

# Both questions the store command asks, in one round trip: sqlite enforces
# neither on its own. Foreign keys are off unless PRAGMA says otherwise, so an
# unknown job_id inserts an orphan, and nothing stops a second analysis for a
# posting already graded.
SELECT_JOB_STATE = """SELECT
    EXISTS(SELECT 1 FROM job_data WHERE id = ?),
    EXISTS(SELECT 1 FROM ai_analysis WHERE job_id = ?);
"""

#: Same predicate as SELECT_JOBS_TO_ANALYSE, counted rather than fetched: the
#: pages want the size of the backlog before deciding how large a run to ask
#: for, and pulling every description back to call len() on it is absurd.
COUNT_JOBS_TO_ANALYSE = """SELECT COUNT(*)
    FROM job_data
    WHERE id NOT IN (SELECT job_id FROM ai_analysis)
    AND timestamp >= datetime('now', ?);
"""

#: Every URL already stored, so the detail stage can skip postings the DB
#: already holds instead of paying an HTTP request to rediscover them.
SELECT_JOB_URLS = """SELECT url FROM job_data;"""

#: The graded postings, best fit first. The join carries the posting's own
#: fields because a grade with no title next to it says nothing.
SELECT_ANALYSES = """SELECT
    job_data.id, ai_analysis.adequation_grade, job_data.company,
    job_data.title, job_data.place, job_data.url, ai_analysis.ai_model,
    ai_analysis.depth_analysis, job_data.description, job_data.timestamp
    FROM ai_analysis
    JOIN job_data ON job_data.id = ai_analysis.job_id
    ORDER BY ai_analysis.adequation_grade DESC;
"""

SELECT_AI_DEPTH_ANALYSIS = """SELECT depth_analysis FROM ai_analysis
    WHERE job_id = ?;
"""

SELECT_AI_GRADE = """SELECT adequation_grade FROM ai_analysis
    WHERE job_id = ?;
"""

SELECT_GENERATED_CV = """SELECT * FROM generated_cv
    WHERE job_id = ?;
"""

#: Graded postings that have no CV yet, best fit first. The grade floor is
#: bound, not baked in: the page's slider decides what is worth writing for.
SELECT_JOBS_FOR_CV = """SELECT
    job_data.id, ai_analysis.adequation_grade, job_data.company,
    job_data.title, job_data.place, job_data.url
    FROM ai_analysis
    JOIN job_data ON job_data.id = ai_analysis.job_id
    WHERE job_data.id NOT IN (SELECT job_id FROM generated_cv)
    AND ai_analysis.adequation_grade >= ?
    ORDER BY ai_analysis.adequation_grade DESC;
"""

#: Every stored CV, newest first, then best fit. The cv blob rides along: the
#: detail view needs it and a per-row second query buys nothing.
#: The timestamp is stored UTC (CURRENT_TIMESTAMP always is) and converted on
#: the way out -- the page renders it verbatim, so an unconverted value reads
#: hours in the past. Only the display shifts; the stored column stays UTC so
#: it keeps comparing correctly against datetime('now', ?).
SELECT_GENERATED_CVS = """SELECT
    generated_cv.id, job_data.title, job_data.company,
    ai_analysis.adequation_grade, generated_cv.locale, generated_cv.cv,
    job_data.url, datetime(generated_cv.timestamp, 'localtime')
    FROM generated_cv
    JOIN job_data ON job_data.id = generated_cv.job_id
    JOIN ai_analysis ON ai_analysis.id = generated_cv.ai_analysis_id
    ORDER BY generated_cv.timestamp DESC, ai_analysis.adequation_grade DESC;
"""

#: Everything the CV generation puts in its prompt, in one round trip. The
#: analysis id comes back because it is the FK generated_cv stores;
#: SELECT_AI_DEPTH_ANALYSIS returns the write-up alone and cannot serve here.
SELECT_JOB_FOR_GENERATION = """SELECT
    job_data.description, ai_analysis.id, ai_analysis.depth_analysis
    FROM ai_analysis
    JOIN job_data ON job_data.id = ai_analysis.job_id
    WHERE job_data.id = ?;
"""


# MIGRATIONS

def create_tables() -> None:
    """Create the tables at the SQLite database if they do not exist.

    The connection is opened as a context manager so the transaction commits
    and the handle closes. Previously only the cursor was closed, leaving the
    connection open and the DDL committed only by sqlite3's own autocommit.
    """
    with sqlite3.connect(DB_ADDRESS) as con:
        con.execute(CREATE_JOB_DATA_TABLE)
        con.execute(CREATE_AI_ANALYSIS_TABLE)
        con.execute(CREATE_GENERATED_CV_TABLE)

        # Add timestamp column if it doesn't exist (migration for existing DBs).
        # ADD COLUMN cannot carry DEFAULT CURRENT_TIMESTAMP -- sqlite only
        # accepts constant defaults there -- so the column arrives without one
        # and INSERT_NEW_GENERATED_CV binds CURRENT_TIMESTAMP itself. The
        # backfill runs every time, not just on the migrating run: rows written
        # after the column existed but before the INSERT named it are NULL too.
        cursor = con.cursor()
        cursor.execute("PRAGMA table_info(generated_cv)")
        columns = {row[1] for row in cursor.fetchall()}
        if "timestamp" not in columns:
            con.execute(
                "ALTER TABLE generated_cv ADD COLUMN timestamp DATETIME"
            )
        con.execute(
            "UPDATE generated_cv SET timestamp = CURRENT_TIMESTAMP"
            " WHERE timestamp IS NULL"
        )


# INSERTIONS

def insert_jobs(rows: Optional[List[Tuple]] = None) -> Tuple[int, int]:
    """Insert the postings into job_data, skipping known URLs.

    Duplicates are dropped by the `url TEXT UNIQUE` constraint via
    INSERT OR IGNORE, so how many were new falls out of `total_changes`
    without reading anything back first.

    Args:
        rows: Rows to insert. Defaults to whatever the refilter stage left in
            the filtered detailed CSV, which is what the pipeline wants.

    Returns:
        Tuple of (number of new jobs inserted, number of duplicates skipped).

    Raises:
        RuntimeError: If the database rejects the batch.
    """
    if not rows:
        rows = _read_jobs_csv()

    con = sqlite3.connect(DB_ADDRESS)

    try:
        before = con.total_changes
        con.executemany(INSERT_NEW_JOB_DATA, rows)
        inserted = con.total_changes - before
        con.commit()
    except sqlite3.Error as exc:
        con.rollback()
        raise RuntimeError(f"Database insert failed: {exc}") from exc
    finally:
        con.close()

    return inserted, len(rows) - inserted


def insert_analysis(analysis: Sequence) -> None:
    """Insert one analysis into the ai_analysis table.

    Args:
        analysis: The row (adequation_grade, depth_analysis, ai_model,
            job_id) -- what `Analysis.transform()` returns. A plain sequence
            rather than the dataclass itself, so storage keeps depending on
            nothing. Which postings are worth analysing is decided by
            `select_jobs_to_analyse`, not here.

    Raise:
        RuntimeError: If the database rejects the insertion
    """
    con = sqlite3.connect(DB_ADDRESS)
    try:
        con.execute(INSERT_NEW_AI_ANALYSIS, analysis)
        con.commit()
    except sqlite3.Error as exc:
        con.rollback()
        raise RuntimeError(f"Database insert failed: {exc}") from exc
    finally:
        con.close()


def insert_generated_cv(locale: str, cv: dict, job_id: int,
                        analysis_id: int) -> None:
    """Store one generated CV against the posting it was written for.

    Args:
        locale: Language the CV body is written in ("en", "es", "fr", "pt").
        cv: The CV object as returned by `ai.cv_generation.generate`, stored
            as JSON text -- the column is JSON, so json_extract() reads it.
        job_id: The posting the CV targets.
        analysis_id: The ai_analysis row the generation was tailored against.

    Raises:
        RuntimeError: If the database rejects the insertion.
    """
    con = sqlite3.connect(DB_ADDRESS)
    try:
        con.execute(INSERT_NEW_GENERATED_CV,
                    (locale, json.dumps(cv), job_id, analysis_id))
        con.commit()
    except sqlite3.Error as exc:
        con.rollback()
        raise RuntimeError(f"Database insert failed: {exc}") from exc
    finally:
        con.close()


# SELECTIONS

def select_job_for_generation(job_id: int) -> Optional[Tuple[str, int, str]]:
    """The posting text and its stored analysis, for the CV generation prompt.

    Args:
        job_id: The posting to generate a CV for.

    Returns:
        Tuple of (description, ai_analysis id, depth_analysis), or None when
        the posting does not exist or has not been graded yet.
    """
    with sqlite3.connect(DB_ADDRESS) as con:
        return con.execute(SELECT_JOB_FOR_GENERATION, (job_id,)).fetchone()


def select_jobs_to_analyse(
        limit: int = 0,
        window: str = "-24 hours") -> List[Tuple[int, str, str, str]]:
    """Select the recent postings the AI model has not graded yet.

    Args:
        limit: Cap on postings returned; 0 means no cap.
        window: SQLite modifier applied to `datetime('now', ?)`. Widen it
            ("-7 days") to pick up postings whose analysis failed earlier.

    Returns:
        List of (id, company, title, description). The company and title go
        into the prompt header, so grading is not done on a bare description.
    """
    with sqlite3.connect(DB_ADDRESS) as con:
        jobs = con.execute(SELECT_JOBS_TO_ANALYSE, (window,)).fetchall()

    return jobs[:limit] if limit else jobs


def count_jobs_to_analyse(window: str = "-24 hours") -> int:
    """How many recent postings the model has not graded yet.

    Args:
        window: SQLite modifier applied to `datetime('now', ?)`, as in
            `select_jobs_to_analyse`.

    Returns:
        The number of postings a run over this window would grade.
    """
    with sqlite3.connect(DB_ADDRESS) as con:
        return con.execute(COUNT_JOBS_TO_ANALYSE, (window,)).fetchone()[0]


def select_job_urls() -> set:
    """Every posting URL already stored.

    Returns:
        Set of URLs, for skipping postings the database already holds.
    """
    with sqlite3.connect(DB_ADDRESS) as con:
        return {url for (url,) in con.execute(SELECT_JOB_URLS)}


def select_analyses() -> List[Tuple]:
    """Every graded posting joined to its own row, best fit first.

    Returns:
        List of (job_id, adequation_grade, company, title, place, url,
        ai_model, depth_analysis, description, timestamp).
    """
    with sqlite3.connect(DB_ADDRESS) as con:
        return con.execute(SELECT_ANALYSES).fetchall()


def select_jobs_for_cv(min_grade: int = 0) -> List[Tuple]:
    """The graded postings still missing a CV, best fit first.

    Args:
        min_grade: Lowest adequation grade worth writing a CV for.

    Returns:
        List of (job_id, adequation_grade, company, title, place, url).
    """
    with sqlite3.connect(DB_ADDRESS) as con:
        return con.execute(SELECT_JOBS_FOR_CV, (min_grade,)).fetchall()


def select_generated_cvs() -> List[Tuple]:
    """Every stored CV with the posting it was written for, newest first.

    Returns:
        List of (cv_id, title, company, adequation_grade, locale, cv), the
        last being the CV as JSON text.
    """
    with sqlite3.connect(DB_ADDRESS) as con:
        return con.execute(SELECT_GENERATED_CVS).fetchall()


def select_job_state(job_id: int) -> Tuple[bool, bool]:
    """Report whether a posting exists and whether it is already analysed.

    Args:
        job_id: The posting to look up.

    Returns:
        Tuple of (exists in job_data, has a row in ai_analysis).
    """
    with sqlite3.connect(DB_ADDRESS) as con:
        exists, analysed = con.execute(
            SELECT_JOB_STATE, (job_id, job_id)).fetchone()

    return bool(exists), bool(analysed)


# UTILS

def _read_jobs_csv() -> List[Tuple[str, str, str, str, str]]:
    """Returns the rows available at the filtered detailed job csv

    Returns:
        List of tuples referring to the job's information
    """
    with FILTERED_DETAILED_JOBS.open(newline="", encoding="utf-8") as handle:
        return [
            (row.get("company"), row.get("title"), row.get("description"),
             row.get("url"), row.get("place"))
            for row in csv.DictReader(handle)
        ]
