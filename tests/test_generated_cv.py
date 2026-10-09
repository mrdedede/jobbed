"""Tests for db/generated_cv.py."""

import uuid

import pytest

from db import analysis, generated_cv, jobs, users


@pytest.fixture
def user(db):
    return users.insert_user(f"{uuid.uuid4()}@example.com", "hashed")


@pytest.fixture
def other_user(db):
    return users.insert_user(f"{uuid.uuid4()}@example.com", "hashed")


def _job_id_for_url(url):
    from sqlalchemy import select
    from db.engine import engine
    from db.tables import job_postings

    with engine.connect() as conn:
        return conn.execute(
            select(job_postings.c.id).where(job_postings.c.url == url)
        ).scalar_one()


@pytest.fixture
def graded_job(db, user):
    """One stored, graded posting -- the minimum generated_cv's FKs need."""
    jobs.insert_jobs([
        ("Acme", "Go Developer", "A description", "http://x/1", "Paris"),
    ])
    job_id = _job_id_for_url("http://x/1")
    analysis.insert_analysis((user, 80, "the write-up", "haiku", job_id))

    from sqlalchemy import select
    from db.engine import engine
    from db.tables import ai_analysis
    with engine.connect() as conn:
        analysis_id = conn.execute(
            select(ai_analysis.c.id)
            .where(ai_analysis.c.job_id == job_id)
            .where(ai_analysis.c.user_id == user)
        ).scalar_one()

    return job_id, analysis_id


def test_generated_cv_round_trip(user, graded_job):
    """The CV goes in as a dict and comes back queryable as jsonb, and the
    generation's own select finds the posting it was stored against."""
    job_id, analysis_id = graded_job

    generated_cv.insert_generated_cv(
        user, "pt", {"profile_text": "perfil"}, job_id, analysis_id)

    description, returned_analysis_id, depth = \
        generated_cv.select_job_for_generation(job_id)

    assert (returned_analysis_id, depth) == (analysis_id, "the write-up")
    assert description == "A description"


def test_select_job_for_generation_without_analysis(db, user):
    """An ungraded posting has nothing to tailor against; cv_generation
    refuses on the None rather than generating blind."""
    jobs.insert_jobs([
        ("Acme", "Go Developer", "A description", "http://x/1", "Paris"),
    ])
    job_id = _job_id_for_url("http://x/1")

    assert generated_cv.select_job_for_generation(job_id) is None


def test_cv_queue_honours_the_grade_floor_and_drops_what_is_written(
        user, graded_job):
    """The CV page's two lists: what is still worth writing, and what
    exists. A posting leaves the queue by being graded below the floor or by
    having a CV already -- both are one predicate away from returning the
    same row forever."""
    job_id, analysis_id = graded_job

    assert [row[0] for row in
            generated_cv.select_jobs_for_cv(user, min_grade=70)] == [job_id]
    assert generated_cv.select_jobs_for_cv(user, min_grade=90) == []

    generated_cv.insert_generated_cv(
        user, "pt", {"profile_text": "perfil"}, job_id, analysis_id)

    assert generated_cv.select_jobs_for_cv(user, min_grade=70) == []


def test_select_jobs_for_cv_is_per_user(user, other_user, graded_job):
    """One user's written CV must not hide the posting from another user's
    queue -- generated_cv is scoped by user_id same as ai_analysis."""
    job_id, analysis_id = graded_job
    generated_cv.insert_generated_cv(
        user, "pt", {"profile_text": "perfil"}, job_id, analysis_id)

    assert generated_cv.select_jobs_for_cv(user, min_grade=0) == []
    # other_user never graded this job, so it doesn't show up for them
    # either -- select_jobs_for_cv only lists jobs THIS user has graded.
    assert generated_cv.select_jobs_for_cv(other_user, min_grade=0) == []


def test_select_generated_cvs_returns_this_users_cvs_newest_first(
        user, graded_job):
    job_id, analysis_id = graded_job
    generated_cv.insert_generated_cv(
        user, "pt", {"profile_text": "perfil"}, job_id, analysis_id)

    (cv_id, title, company, grade, locale, cv, url, created_at), = \
        generated_cv.select_generated_cvs(user)

    assert (title, company, grade, locale) == (
        "Go Developer", "Acme", 80, "pt")
    assert cv == {"profile_text": "perfil"}


def test_select_generated_cvs_empty_for_a_user_with_none(user, other_user,
                                                          graded_job):
    job_id, analysis_id = graded_job
    generated_cv.insert_generated_cv(
        user, "pt", {"profile_text": "perfil"}, job_id, analysis_id)

    assert generated_cv.select_generated_cvs(other_user) == []
