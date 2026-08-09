"""SQLite schema and connection management for job scraping results.

This module defines DDL for persisting scraped jobs, analyses, and generated CVs,
and provides a connection factory for database operations.
"""

import sqlite3

JOB_DATA_TABLE = """CREATE TABLE job_data(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    company TEXT,
    title TEXT,
    description TEXT,
    url TEXT,
    place TEXT,
    timestamp DATETIME DEFAULT CURRENT_TIMESTAMP);
"""

AI_ANALYSIS_TABLE = """CREATE TABLE ai_analysis(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    adequation_grade INT,
    depth_analysis TEXT,
    ai_model TEXT,
    job_id BIGINT,
    FOREIGN KEY (job_id) REFERENCES job_data(id));
"""

GENERATED_CV_TABLE = """CREATE TABLE generated_cv(
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
INSERT_NEW_JOB_DATA = """INSERT INTO job_data VALUES(
    $1, $2, $3, $4, $5
);
"""

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

def get_connection(db_path: str = "joblister.db") -> sqlite3.Connection:
    """Get a SQLite database connection.

    Args:
        db_path: Path to the SQLite database file.

    Returns:
        An open sqlite3.Connection object.
    """
    return sqlite3.connect(db_path)

