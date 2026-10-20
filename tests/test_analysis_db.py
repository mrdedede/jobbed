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
        ("Acme", "Go Dev", "write go", "http://x/1", "Paris", None, None),
        ("Globex", "Py Dev", "write python", "http://x/2", "Lyon", None, None),
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


def test_insert_analysis_regrade_replaces_the_previous_verdict(
        user, two_jobs):
    """Re-analysis upserts on (user_id, job_id): one row, the newest grade."""
    job_id = _job_id_for_url("http://x/1")
    analysis.insert_analysis((user, 50, "first", "claude-haiku", job_id))
    analysis.insert_analysis((user, 90, "second", "claude-sonnet", job_id))

    rows = analysis.select_analyses(user)

    assert len(rows) == 1
    assert (rows[0][1], rows[0][6], rows[0][7]) == (
        90, "claude-sonnet", "second")


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


def test_select_candidates_matches_select_jobs_to_analyse_and_adds_url_place(
        user, two_jobs):
    candidates = analysis.select_candidates(user)

    assert {r.id for r in candidates} == {
        r[0] for r in analysis.select_jobs_to_analyse(user)}
    assert {(r.url, r.place) for r in candidates} == {
        ("http://x/1", "Paris"), ("http://x/2", "Lyon")}


def test_select_candidates_drops_graded_and_honours_limit(user, two_jobs):
    analysis.insert_analysis(
        (user, 50, "fine", "claude-haiku", _job_id_for_url("http://x/1")))

    assert [r.url for r in analysis.select_candidates(user)] == ["http://x/2"]
    assert len(analysis.select_candidates(user, limit=1)) == 1


def test_users_with_different_keywords_get_different_candidate_sets(
        user, other_user, db):
    import pandas as pd
    from job_scraper.filters import filter_frame

    jobs.insert_jobs([
        ("Acme", "Go Dev", "go " * 6, "http://y/1", "Paris", None, None),
        ("Globex", "Py Dev", "python " * 6, "http://y/2", "Lyon", None, None),
    ])
    cols = ["id", "company", "title", "url", "place", "description",
            "scraped_at"]

    def kept(uid, keywords):
        frame = pd.DataFrame(analysis.select_candidates(uid), columns=cols)
        return set(filter_frame(frame, keywords, []).url)

    assert kept(user, ["go"]) == {"http://y/1"}
    assert kept(other_user, ["python"]) == {"http://y/2"}


def test_select_analysis_page_omits_description_orders_and_filters(
        user, other_user, two_jobs):
    low, high = _job_id_for_url("http://x/1"), _job_id_for_url("http://x/2")
    analysis.insert_analysis((user, 40, "meh", "haiku", low))
    analysis.insert_analysis((user, 90, "great", "haiku", high))
    analysis.insert_analysis((other_user, 10, "no", "haiku", low))

    page = analysis.select_analysis_page(user, limit=10)

    assert [r[1] for r in page] == [90, 40]
    assert len(page[0]) == 9  # no description column
    assert len(analysis.select_analysis_page(user, limit=1)) == 1
    assert [r[0] for r in analysis.select_analysis_page(
        user, 10, min_grade=50)] == [high]
    assert [r[0] for r in analysis.select_analysis_page(
        user, 10, job_ids=[low])] == [low]


def test_select_analysis_page_since_days_excludes_older_analyses(
        user, two_jobs):
    from datetime import timedelta

    from sqlalchemy import update
    from db.engine import engine
    from db.tables import ai_analysis

    old, new = _job_id_for_url("http://x/1"), _job_id_for_url("http://x/2")
    analysis.insert_analysis((user, 40, "old", "haiku", old))
    analysis.insert_analysis((user, 90, "new", "haiku", new))
    with engine.begin() as conn:
        conn.execute(update(ai_analysis).where(ai_analysis.c.job_id == old)
                     .values(created_at=ai_analysis.c.created_at
                             - timedelta(days=3)))

    assert [r[0] for r in analysis.select_analysis_page(
        user, 10, since_days=1)] == [new]
    assert len(analysis.select_analysis_page(user, 10, since_days=7)) == 2
    assert len(analysis.select_analysis_page(user, 10)) == 2


def _age(url, days):
    from datetime import timedelta

    from sqlalchemy import update
    from db.engine import engine
    from db.tables import job_postings

    with engine.begin() as conn:
        conn.execute(
            update(job_postings).where(job_postings.c.url == url).values(
                scraped_at=job_postings.c.scraped_at - timedelta(days=days)))


def test_the_window_excludes_older_postings_everywhere(user, two_jobs):
    _age("http://x/1", 3)

    assert {r.url for r in analysis.select_candidates(
        user, window="-1 days")} == {"http://x/2"}
    assert {r.url for r in analysis.select_candidates(
        user, window="-7 days")} == {"http://x/1", "http://x/2"}
    assert len(analysis.select_jobs_to_analyse(user, window="-1 days")) == 1
    assert analysis.count_jobs_to_analyse(user, window="-1 days") == 1
    assert analysis.count_jobs_to_analyse(user, window="-7 days") == 2
