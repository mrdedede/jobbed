"""scripts/import_legacy_sqlite.py against a tiny legacy-shaped SQLite."""

import importlib.util
import json
import sqlite3
from pathlib import Path

import pytest
from sqlalchemy import func, select

from db import jobs, users
from db.engine import engine
from db.tables import ai_analysis, generated_cv, job_postings

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "import_legacy_sqlite.py"
spec = importlib.util.spec_from_file_location("import_legacy", SCRIPT)
importer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(importer)

LEGACY_SCHEMA = """
CREATE TABLE job_data(id INTEGER PRIMARY KEY AUTOINCREMENT, company TEXT,
    title TEXT, description TEXT, url TEXT UNIQUE, place TEXT,
    timestamp DATETIME DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE ai_analysis(id INTEGER PRIMARY KEY AUTOINCREMENT,
    adequation_grade INT, depth_analysis TEXT, ai_model TEXT, job_id BIGINT,
    timestamp DATETIME);
CREATE TABLE generated_cv(id INTEGER PRIMARY KEY AUTOINCREMENT, locale TEXT,
    cv JSON, job_id BIGINT, ai_analysis_id BIGINT, timestamp DATETIME);
"""


@pytest.fixture
def legacy(tmp_path):
    path = tmp_path / "old.db"
    con = sqlite3.connect(path)
    con.executescript(LEGACY_SCHEMA)
    con.executemany(
        "INSERT INTO job_data (company,title,description,url,place,timestamp)"
        " VALUES (?,?,?,?,?,?)",
        [("Acme", "Go Dev", "go", "http://old/1", "Paris", "2026-08-09 10:00:00"),
         ("Acme", "Py Dev", "", "http://old/2", "Lyon", "2026-08-10 10:00:00")])
    con.executemany(
        "INSERT INTO ai_analysis (adequation_grade,depth_analysis,ai_model,"
        "job_id,timestamp) VALUES (?,?,?,?,?)",
        [(10, "first", "haiku", 1, "2026-08-11 09:00:00"),
         (80, "second", "haiku", 1, "2026-08-12 09:00:00")])
    # Attached to the superseded first grade (id 1): must be skipped.
    con.executemany(
        "INSERT INTO generated_cv (locale,cv,job_id,ai_analysis_id,timestamp)"
        " VALUES (?,?,?,?,?)",
        [("fr", json.dumps({"a": 1}), 1, 2, "2026-08-13 09:00:00"),
         ("en", json.dumps({"a": 2}), 1, 1, "2026-08-13 09:00:00")])
    con.commit()
    con.close()

    return path


@pytest.fixture
def user_info(tmp_path):
    folder = tmp_path / "ui"
    folder.mkdir()
    (folder / "keywords.txt").write_text("Go\n\n  Python \n")
    (folder / "blacklist.txt").write_text("Intern\n")
    (folder / "my_cv.md").write_text("# me")

    return folder


def count(table):
    with engine.connect() as conn:
        return conn.execute(select(func.count()).select_from(table)).scalar_one()


def test_reports_real_insert_counts(db, legacy, user_info, capsys):
    importer.run(legacy, "a@example.com", "password123", user_info)
    first = capsys.readouterr().out
    importer.run(legacy, "a@example.com", "", user_info)
    second = capsys.readouterr().out

    assert "job_postings: 2 legacy, 2 new, 0 already present" in first
    assert "job_postings: 2 legacy, 0 new, 2 already present" in second
    assert "latest grade per job, 1 new" in first
    assert "latest grade per job, 0 new" in second


def test_import_maps_dedups_and_is_idempotent(db, legacy, user_info):
    # One legacy posting already stored by the nightly ingest.
    jobs.insert_jobs([("Acme", "Go Dev (ingest)", "go", "http://old/1",
                       "Paris", None, None)])

    uid = importer.run(legacy, "Me@Example.com", "password123", user_info)

    assert count(job_postings) == 2          # reused, not duplicated
    assert count(ai_analysis) == 1           # latest grade per job
    assert count(generated_cv) == 1          # CV on the dropped grade skipped
    with engine.connect() as conn:
        grade = conn.execute(select(ai_analysis.c.adequation_grade)).scalar_one()
        title = conn.execute(select(job_postings.c.title).where(
            job_postings.c.url == "http://old/1")).scalar_one()
        cv = conn.execute(select(generated_cv.c.cv)).scalar_one()
    assert (grade, title, cv) == (80, "Go Dev (ingest)", {"a": 1})
    assert [t for _, t in users.select_user_keywords(uid)] == ["go", "python"]
    assert [t for _, t in users.select_user_blacklist(uid)] == ["intern"]
    assert users.select_active_user_cv(uid)[1] == "# me"

    importer.run(legacy, "me@example.com", "", user_info)

    assert (count(job_postings), count(ai_analysis), count(generated_cv)) == (2, 1, 1)
    assert len(users.select_user_keywords(uid)) == 2
