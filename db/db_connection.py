"""SQLite schema and connection management for job scraping results.

This module defines DDL for persisting scraped jobs, analyses, and generated
CVs, and provides the insert path the pipeline's last stage calls.

Every statement binds with `?`. sqlite3 does not understand PostgreSQL's `$1`
placeholders -- it treats them as numbered parameters with an entirely
different meaning -- and several statements here carried them, which is why
they had never successfully run.
"""

import csv
import sqlite3
from typing import Tuple

from job_scraper import paths

DB_ADDRESS = paths.DB_PATH
FILTERED_DETAILED_JOBS = paths.FILTERED_DETAILED_CSV

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
    intro_line TEXT,
    profile TEXT,
    skills TEXT,
    experiences TEXT,
    education TEXT,
    languages TEXT,
    job_id BIGINT,
    ai_analysis_id BIGINT,
    FOREIGN KEY (job_id) REFERENCES job_data(id),
    FOREIGN KEY (ai_analysis_id) REFERENCES ai_analysis(id));
"""

# INSERTS
#
# Columns are named on every one of these. Without a column list SQLite
# expects a value for every column including the autoincrement id, which is
# what made the two below fail with "table ai_analysis has 5 columns but 4
# values were supplied".

# OR IGNORE leans on `url TEXT UNIQUE` instead of reimplementing it: the
# previous version read every existing URL into a Python set and filtered the
# CSV against it row by row, which is the same constraint written twice, the
# second time more slowly.
INSERT_NEW_JOB_DATA = """INSERT OR IGNORE INTO job_data(
    company, title, description, url, place)
    VALUES(?, ?, ?, ?, ?)"""

INSERT_NEW_AI_ANALYSIS = """INSERT INTO ai_analysis(
    adequation_grade, depth_analysis, ai_model, job_id)
    VALUES(?, ?, ?, ?)"""

INSERT_NEW_GENERATED_CV = """INSERT INTO generated_cv(
    intro_line, profile, skills, experiences, education, languages,
    job_id, ai_analysis_id)
    VALUES(?, ?, ?, ?, ?, ?, ?, ?)"""

# SELECTS
SELECT_JOBS_FOUND_TODAY = """SELECT * FROM job_data
    WHERE timestamp >= datetime('now', '-24 hours');
"""

SELECT_JOB_BY_URL = """SELECT * FROM job_data
    WHERE url = ?;
"""

# Reads job_data. There is no `job_detail` table and there never was, so this
# raised "no such table" for any caller that tried it.
SELECT_JOB_DESCRIPTION = """SELECT description FROM job_data
    WHERE id = ?;
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


def insert_jobs() -> Tuple[int, int]:
    """Insert the filtered postings into job_data, skipping known URLs.

    Duplicates are dropped by the `url TEXT UNIQUE` constraint via
    INSERT OR IGNORE, so how many were new falls out of `total_changes`
    without reading anything back first.

    Returns:
        Tuple of (number of new jobs inserted, number of duplicates skipped).

    Raises:
        RuntimeError: If the database rejects the batch.
    """
    with FILTERED_DETAILED_JOBS.open(newline="", encoding="utf-8") as handle:
        rows = [
            (row.get("company"), row.get("title"), row.get("description"),
             row.get("url"), row.get("place"))
            for row in csv.DictReader(handle)
        ]

    if not rows:
        return 0, 0

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
