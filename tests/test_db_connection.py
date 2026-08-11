"""Tests for db/db_connection.py.

Three of these statements had never run. INSERT_NEW_AI_ANALYSIS supplied 4
values for a 5-column table, INSERT_NEW_GENERATED_CV 8 for 9, and
SELECT_JOB_DESCRIPTION queried a `job_detail` table that does not exist --
each fails the moment it is executed, and nothing executed them. So the test
that matters most here is simply: run every statement once.
"""

import json
import sqlite3

import pytest

from db import db_connection

STATEMENTS = [
    ("INSERT_NEW_JOB_DATA", ("Acme", "Go Dev", "body", "http://x/1", "Paris")),
    ("INSERT_NEW_AI_ANALYSIS", (7, "analysis", "claude-opus-5", 1)),
    ("INSERT_NEW_GENERATED_CV", ("fr", '{"profile_text": "x"}', 1, 1)),
    ("SELECT_JOB_BY_URL", ("http://x/1",)),
    ("SELECT_JOBS_TO_ANALYSE", ("-24 hours",)),
    ("SELECT_JOB_STATE", (1, 1)),
    ("SELECT_AI_DEPTH_ANALYSIS", (1,)),
    ("SELECT_AI_GRADE", (1,)),
    ("SELECT_GENERATED_CV", (1,)),
    ("SELECT_JOB_FOR_GENERATION", (1,)),
    ("SELECT_JOBS_FOR_CV", (0,)),
    ("SELECT_GENERATED_CVS", ()),
]


@pytest.fixture
def database(tmp_path, monkeypatch):
    """A freshly created database at a throwaway path."""
    monkeypatch.setattr(db_connection, "DB_ADDRESS", tmp_path / "test.db")
    db_connection.create_tables()

    return db_connection.DB_ADDRESS


@pytest.fixture
def filtered_csv(tmp_path, monkeypatch):
    """Point insert_jobs at a CSV this test writes."""
    path = tmp_path / "filtered_detailed_jobs.csv"
    monkeypatch.setattr(db_connection, "FILTERED_DETAILED_JOBS", path)

    def write(*urls):
        header = "company,title,description,url,place,via,ats,keyword_hits\n"
        rows = "".join(
            f"Acme,Go Developer,A description,{url},Paris,jsonld,,5\n"
            for url in urls
        )
        path.write_text(header + rows, encoding="utf-8")

        return path

    return write


@pytest.mark.parametrize("name,params", STATEMENTS,
                         ids=[name for name, _ in STATEMENTS])
def test_every_statement_executes(database, name, params):
    """Bind and run each statement. A wrong column count, a `$1` placeholder
    sqlite cannot parse, or a table that does not exist all raise here."""
    with sqlite3.connect(database) as con:
        con.execute(getattr(db_connection, name), params)


def test_create_tables_is_idempotent(database):
    """The pipeline calls this on every run, not only the first."""
    db_connection.create_tables()

    with sqlite3.connect(database) as con:
        names = {
            row[0] for row in
            con.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }

    assert {"job_data", "ai_analysis", "generated_cv"} <= names


def test_create_tables_commits(tmp_path, monkeypatch):
    """It used to close only the cursor, leaving the connection open and the
    DDL committed only by sqlite3's own autocommit."""
    path = tmp_path / "fresh.db"
    monkeypatch.setattr(db_connection, "DB_ADDRESS", path)
    db_connection.create_tables()

    # A separate connection sees nothing that was never committed.
    with sqlite3.connect(path) as con:
        con.execute("SELECT 1 FROM job_data")


def test_generated_cv_round_trip(database, filtered_csv):
    """The CV goes in as a dict and comes back queryable as JSON, and the
    generation's own select finds the posting it was stored against."""
    filtered_csv("http://x/1")
    db_connection.insert_jobs()
    db_connection.insert_analysis([70, "the write-up", "haiku", 1])

    db_connection.insert_generated_cv("pt", {"profile_text": "perfil"}, 1, 1)

    with sqlite3.connect(database) as con:
        assert con.execute(
            "SELECT locale, json_extract(cv, '$.profile_text')"
            " FROM generated_cv").fetchone() == ("pt", "perfil")

    description, analysis_id, depth = \
        db_connection.select_job_for_generation(1)
    assert (analysis_id, depth) == (1, "the write-up")
    assert description == "A description"


def test_select_job_for_generation_without_analysis(database, filtered_csv):
    """An ungraded posting has nothing to tailor against; cv_generation
    refuses on the None rather than generating blind."""
    filtered_csv("http://x/1")
    db_connection.insert_jobs()

    assert db_connection.select_job_for_generation(1) is None


def test_cv_queue_honours_the_grade_floor_and_drops_what_is_written(
        database, filtered_csv):
    """The CV page's two lists: what is still worth writing, and what exists.
    A posting leaves the queue by being graded below the floor or by having a
    CV already -- both are one predicate away from returning the same row
    forever."""
    filtered_csv("http://x/1")
    db_connection.insert_jobs()
    db_connection.insert_analysis([80, "the write-up", "haiku", 1])

    assert [row[0] for row in db_connection.select_jobs_for_cv(70)] == [1]
    assert db_connection.select_jobs_for_cv(90) == []

    db_connection.insert_generated_cv("pt", {"profile_text": "perfil"}, 1, 1)

    assert db_connection.select_jobs_for_cv(70) == []

    (cv_id, title, company, grade, locale, cv), = \
        db_connection.select_generated_cvs()
    assert (title, company, grade, locale) == ("Go Developer", "Acme", 80,
                                               "pt")
    assert json.loads(cv) == {"profile_text": "perfil"}


def test_insert_jobs_counts_new_rows(database, filtered_csv):
    filtered_csv("http://x/1", "http://x/2")

    assert db_connection.insert_jobs() == (2, 0)


def test_insert_jobs_skips_urls_already_stored(database, filtered_csv):
    """The `url TEXT UNIQUE` constraint does the deduplication, so a rerun of
    the same day's scrape is free rather than a duplicate-key error."""
    filtered_csv("http://x/1", "http://x/2")
    db_connection.insert_jobs()

    filtered_csv("http://x/1", "http://x/2", "http://x/3")

    assert db_connection.insert_jobs() == (1, 2)


def test_insert_jobs_deduplicates_within_one_batch(database, filtered_csv):
    """Two boards can list the same posting; OR IGNORE catches it mid-batch
    where a pre-read of existing URLs could not."""
    filtered_csv("http://x/1", "http://x/1")

    assert db_connection.insert_jobs() == (1, 1)


def test_insert_jobs_on_empty_csv(database, filtered_csv):
    filtered_csv()

    assert db_connection.insert_jobs() == (0, 0)
