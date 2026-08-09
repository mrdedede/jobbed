"""Tests for ai_analysis/analysis.py.

There is no model here to stub: the grader is a Claude Code session, and this
module is the plumbing on either side of it. What is worth testing is that
`pending` hands over the right postings and that `store` refuses every row the
schema would happily accept and nobody could use.
"""

import io
import json
import sqlite3

import pytest

from ai_analysis import analysis
from db import db_connection


@pytest.fixture
def database(tmp_path, monkeypatch):
    """A database holding two postings, at a throwaway path."""
    monkeypatch.setattr(db_connection, "DB_ADDRESS", tmp_path / "test.db")
    db_connection.create_tables()
    db_connection.insert_jobs([
        ("Acme", "Go Dev", "write go", "http://x/1", "Paris"),
        ("Globex", "Py Dev", "write python", "http://x/2", "Lyon"),
    ])

    return db_connection.DB_ADDRESS


def analyses(database):
    """Every row in ai_analysis, oldest first."""
    with sqlite3.connect(database) as con:
        return con.execute(
            "SELECT job_id, adequation_grade, depth_analysis, ai_model "
            "FROM ai_analysis ORDER BY id").fetchall()


def test_pending_lists_ungraded_postings(database):
    jobs = analysis.pending()

    assert [job["job_id"] for job in jobs] == [1, 2]
    assert jobs[0]["company"] == "Acme"
    assert jobs[0]["description"] == "write go"


def test_pending_drops_what_was_already_graded(database):
    analysis.store(job_id=1, grade=50, analysis="fine")

    assert [job["job_id"] for job in analysis.pending()] == [2]


def test_pending_honours_limit(database):
    assert len(analysis.pending(limit=1)) == 1


def test_pending_prints_parseable_json(database, capsys):
    assert analysis.main(["pending"]) == 0

    printed = json.loads(capsys.readouterr().out)

    assert [job["title"] for job in printed] == ["Go Dev", "Py Dev"]


def test_store_writes_the_row(database):
    analysis.store(job_id=2, grade=73, analysis="  strong overlap  ",
                   model="claude-sonnet-5")

    assert analyses(database) == [
        (2, 73, "strong overlap", "claude-sonnet-5")]


def test_store_defaults_the_model(database):
    analysis.store(job_id=1, grade=10, analysis="no")

    assert analyses(database)[0][3] == analysis.DEFAULT_MODEL


def test_main_store_reads_the_analysis_from_a_file(database, tmp_path):
    verdict = tmp_path / "job-1.md"
    verdict.write_text("Two paragraphs.\n\nAnd a second one.\n",
                       encoding="utf-8")

    assert analysis.main(["store", "--job-id", "1", "--grade", "62",
                          "--analysis-file", str(verdict),
                          "--model", "claude-opus-5"]) == 0

    job_id, grade, text, model = analyses(database)[0]

    assert (job_id, grade, model) == (1, 62, "claude-opus-5")
    assert text.startswith("Two paragraphs.")


def test_main_store_reads_the_analysis_from_stdin(database, monkeypatch):
    monkeypatch.setattr("sys.stdin", io.StringIO("piped verdict"))

    assert analysis.main(["store", "--job-id", "1", "--grade", "5"]) == 0
    assert analyses(database)[0][2] == "piped verdict"


@pytest.mark.parametrize("grade", [101, -1])
def test_store_rejects_grades_outside_the_scale(database, grade):
    with pytest.raises(ValueError, match="outside 0-100"):
        analysis.store(job_id=1, grade=grade, analysis="whatever")

    assert analyses(database) == []


def test_store_rejects_an_empty_analysis(database):
    with pytest.raises(ValueError, match="empty"):
        analysis.store(job_id=1, grade=50, analysis="   \n  ")

    assert analyses(database) == []


def test_store_rejects_an_unknown_job(database):
    # sqlite does not enforce the foreign key, so nothing else would.
    with pytest.raises(ValueError, match="no job 99"):
        analysis.store(job_id=99, grade=50, analysis="orphan")

    assert analyses(database) == []


def test_store_refuses_to_grade_the_same_job_twice(database):
    analysis.store(job_id=1, grade=50, analysis="first")

    with pytest.raises(ValueError, match="already analysed"):
        analysis.store(job_id=1, grade=90, analysis="second")

    assert len(analyses(database)) == 1


def test_main_store_exits_non_zero_on_a_bad_row(database, capsys):
    with pytest.raises(SystemExit) as exit_code:
        analysis.main(["store", "--job-id", "99", "--grade", "50",
                       "--analysis-file", "/dev/null"])

    assert exit_code.value.code == 2
    assert analyses(database) == []
