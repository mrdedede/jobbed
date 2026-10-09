"""Tests for db/analysis.py."""

import uuid

import pytest

from db import analysis, jobs, users


@pytest.fixture
def user(db):
    """A registered user to scope analyses against."""
    return users.insert_user(f"{uuid.uuid4()}@example.com", "hashed")


@pytest.fixture
def other_user(db):
    """A second user, for asserting one user's analyses stay invisible to
    another."""
    return users.insert_user(f"{uuid.uuid4()}@example.com", "hashed")


@pytest.fixture
def two_jobs(db):
    """Two stored postings to grade."""
    jobs.insert_jobs([
        ("Acme", "Go Dev", "write go", "http://x/1", "Paris"),
        ("Globex", "Py Dev", "write python", "http://x/2", "Lyon"),
    ])
    return sorted(jobs.select_job_urls())


def _job_id_for_url(url):
    """The only way these tests have to get a job's id back is a fresh
    select -- insert_jobs reports counts, not ids."""
    from sqlalchemy import select
    from db.engine import engine
    from db.tables import job_postings

    with engine.connect() as conn:
        return conn.execute(
            select(job_postings.c.id).where(job_postings.c.url == url)
        ).scalar_one()


def test_select_jobs_to_analyse_lists_ungraded_postings(user, two_jobs):
    pending = analysis.select_jobs_to_analyse(user)

    assert {row[2] for row in pending} == {"Go Dev", "Py Dev"}


def test_select_jobs_to_analyse_drops_what_this_user_already_graded(
        user, two_jobs):
    job_id = _job_id_for_url("http://x/1")
    analysis.insert_analysis((user, 50, "fine", "claude-haiku", job_id))

    pending = analysis.select_jobs_to_analyse(user)

    assert {row[0] for row in pending} == {_job_id_for_url("http://x/2")}


def test_select_jobs_to_analyse_is_per_user(user, other_user, two_jobs):
    """Grading as one user must not hide the posting from another user --
    this is the whole point of scoping ai_analysis by user_id."""
    job_id = _job_id_for_url("http://x/1")
    analysis.insert_analysis((user, 50, "fine", "claude-haiku", job_id))

    other_pending = analysis.select_jobs_to_analyse(other_user)

    assert {row[0] for row in other_pending} == {
        _job_id_for_url("http://x/1"), _job_id_for_url("http://x/2")}


def test_select_jobs_to_analyse_honours_limit(user, two_jobs):
    assert len(analysis.select_jobs_to_analyse(user, limit=1)) == 1


def test_count_jobs_to_analyse_matches_select(user, two_jobs):
    assert analysis.count_jobs_to_analyse(user) == 2

    job_id = _job_id_for_url("http://x/1")
    analysis.insert_analysis((user, 50, "fine", "claude-haiku", job_id))

    assert analysis.count_jobs_to_analyse(user) == 1


def test_insert_analysis_rejects_a_second_grade_for_the_same_user_and_job(
        user, two_jobs):
    """ai_analysis has a unique constraint on (user_id, job_id) -- one grade
    per user per job."""
    from sqlalchemy.exc import IntegrityError

    job_id = _job_id_for_url("http://x/1")
    analysis.insert_analysis((user, 50, "first", "claude-haiku", job_id))

    with pytest.raises((RuntimeError, IntegrityError)):
        analysis.insert_analysis((user, 90, "second", "claude-haiku", job_id))


def test_select_analyses_returns_this_users_graded_postings_best_fit_first(
        user, two_jobs):
    job_1, job_2 = (_job_id_for_url("http://x/1"),
                     _job_id_for_url("http://x/2"))
    analysis.insert_analysis((user, 50, "ok fit", "claude-haiku", job_1))
    analysis.insert_analysis((user, 90, "great fit", "claude-haiku", job_2))

    rows = analysis.select_analyses(user)

    assert [row[0] for row in rows] == [job_2, job_1]


def test_select_job_state_reports_existence_and_this_users_grading(
        user, other_user, two_jobs):
    job_id = _job_id_for_url("http://x/1")
    analysis.insert_analysis((user, 50, "fine", "claude-haiku", job_id))

    assert analysis.select_job_state(user, job_id) == (True, True)
    assert analysis.select_job_state(other_user, job_id) == (True, False)
    assert analysis.select_job_state(user, uuid.uuid4()) == (False, False)
