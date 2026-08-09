"""SQLite schema and connection management for job scraping results.

This module defines DDL for persisting scraped jobs, analyses, and generated CVs,
and provides a connection factory for database operations.
"""

import sqlite3
import pandas as pd
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DB_ADDRESS = ROOT / "db" /"joblister.db"
FILTERED_DETAILED_JOBS = ROOT / "temp" / "filtered_detailed_jobs.csv"

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
INSERT_NEW_JOB_DATA = """INSERT INTO job_data(company, title, description, url, place)
    VALUES(?, ?, ?, ?, ?)"""

INSERT_NEW_AI_ANALYSIS = """INSERT INTO ai_analysis VALUES(
    $1, $2, $3, $4
);
"""

INSERT_NEW_GENERATED_CV = """INSERT INTO generated_cv VALUES(
    $1, $2, $3, $4, $5, $6, $7, $8
);
"""

# SELECTS
SELECT_JOBS_FOUND_TODAY = """SELECT * FROM job_data
    WHERE timestamp >= datetime('now', '-24 hours'); 
"""

SELECT_JOB_BY_URL = """SELECT * FROM job_data
    WHERE url = $1;
"""

SELECT_JOB_DESCRIPTION = """SELECT description FROM job_detail
    WHERE job_id = ?;
"""

SELECT_AI_DEPTH_ANALYSIS = """SELECT depth_analysis FROM ai_analysis
    WHERE job_id = $1;
"""

SELECT_AI_GRADE = """SELECT adequation_grade FROM ai_analysis
    WHERE job_id = $1;
"""

SELECT_GENERATED_CV = """SELECT * FROM generated_cv
    WHERE job_id = $1;
"""

def create_tables():
    """Creates the tables at the SQLite database.
    """
    con = sqlite3.connect(DB_ADDRESS)
    cur = con.cursor()
    # Job data
    cur.execute(CREATE_JOB_DATA_TABLE)
    # AI Analysis
    cur.execute(CREATE_AI_ANALYSIS_TABLE)
    # Generated CV
    cur.execute(CREATE_GENERATED_CV_TABLE)
    cur.close()    

def insert_jobs() -> tuple[int, int]:
    """Batch insert filtered jobs from CSV into database, skipping duplicates by URL.

    Reads filtered_detailed_jobs.csv and inserts only new jobs (checking URL uniqueness).
    Returns tuple of (inserted_count, skipped_count).

    Returns:
        Tuple of (number of new jobs inserted, number of duplicates skipped).
    """
    jobs_df = pd.read_csv(FILTERED_DETAILED_JOBS)

    if jobs_df.empty:
        return 0, 0

    con = sqlite3.connect(DB_ADDRESS)
    try:
        # Fetch existing URLs from database
        existing_urls = set(
            url[0] for url in con.execute("SELECT url FROM job_data").fetchall()
        )

        # Filter to only new jobs (URL not already in database)
        new_rows = [
            (row["company"], row["title"], row["description"], row["url"], row["place"])
            for _, row in jobs_df.iterrows()
            if row["url"] not in existing_urls
        ]

        skipped_count = len(jobs_df) - len(new_rows)
        inserted_count = 0

        if new_rows:
            con.executemany(INSERT_NEW_JOB_DATA, new_rows)
            inserted_count = len(new_rows)

        con.commit()
    except sqlite3.Error as e:
        con.rollback()
        raise RuntimeError(f"Database insert failed: {e}") from e
    finally:
        con.close()

    return inserted_count, skipped_count