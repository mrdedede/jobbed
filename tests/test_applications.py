"""Tests for db/applications.py and the /v1/me/applications routes."""

import uuid

import pytest
from sqlalchemy import select

from api_helpers import as_user

from db import analysis, applications, generated_cv, jobs, users
from db.engine import engine
from db.tables import ai_analysis, job_postings


@pytest.fixture
def user(db):
    return users.insert_user(f"{uuid.uuid4()}@example.com", "hashed")


@pytest.fixture
def other_user(db):
    return users.insert_user(f"{uuid.uuid4()}@example.com", "hashed")


def _cv_for(user, url="http://x/1"):
    """Store and grade a posting, write a CV for it; return (job_id, cv_id)."""
    jobs.insert_jobs([("Acme", "Go Developer", "desc", url, "Paris", None, None)])
    with engine.connect() as conn:
        job_id = conn.execute(select(job_postings.c.id)
                              .where(job_postings.c.url == url)).scalar_one()
    analysis.insert_analysis((user, 80, "the write-up", "haiku", job_id))
    with engine.connect() as conn:
        analysis_id = conn.execute(select(ai_analysis.c.id).where(
            ai_analysis.c.job_id == job_id,
            ai_analysis.c.user_id == user)).scalar_one()
    cv_id = generated_cv.insert_generated_cv(user, "en", {}, job_id, analysis_id)
    return job_id, cv_id


def _status(user, job_id):
    row = applications.select_card(user, job_id)
    return row[8], row[9], row[10], row[11]


def test_a_new_cv_is_in_cv_generated(user):
    job_id, cv_id = _cv_for(user)

    (row,) = applications.select_board(user)

    assert (row[0], row[1], row[8], row[12]) == (cv_id, job_id, "cv_generated", None)


def test_reject_records_the_stage_and_restore_clears_it(user):
    job_id, _ = _cv_for(user)

    assert applications.upsert_status(user, job_id, "interviews")
    applications.upsert_status(user, job_id, "rejected", "rejected", "too junior")
    assert _status(user, job_id) == ("rejected", "interviews", "rejected", "too junior")

    # Re-rejecting only changes the reason, the stage stays where it died.
    applications.upsert_status(user, job_id, "rejected", "no_reply")
    assert _status(user, job_id) == ("rejected", "interviews", "no_reply", None)

    applications.upsert_status(user, job_id, "interviews")
    assert _status(user, job_id) == ("interviews", None, None, None)


def test_reject_straight_from_cv_generated(user):
    job_id, _ = _cv_for(user)

    applications.upsert_status(user, job_id, "rejected", "withdrew")

    assert _status(user, job_id)[:2] == ("rejected", "cv_generated")


def test_back_to_cv_generated_drops_the_row(user):
    job_id, _ = _cv_for(user)
    applications.upsert_status(user, job_id, "applied")

    applications.delete_application(user, job_id)

    assert _status(user, job_id)[0] == "cv_generated"


def test_another_users_job_is_not_a_card(user, other_user):
    job_id, _ = _cv_for(user)

    assert not applications.upsert_status(other_user, job_id, "applied")
    assert applications.select_board(other_user) == []


def test_the_database_rejects_a_bad_status(user):
    job_id, _ = _cv_for(user)

    with pytest.raises(RuntimeError):
        applications.upsert_status(user, job_id, "hired")


def test_one_card_per_job_with_the_newest_cv(user):
    job_id, _ = _cv_for(user)
    with engine.connect() as conn:
        analysis_id = conn.execute(select(ai_analysis.c.id)
                                   .where(ai_analysis.c.user_id == user)).scalar_one()
    newer = generated_cv.insert_generated_cv(user, "fr", {}, job_id, analysis_id)

    (row,) = applications.select_board(user)

    assert row[0] == newer


# API

def url(*parts):
    return "/".join(["/v1/me/applications", *map(str, parts)])


def test_api_moves_a_card(user):
    job_id, _ = _cv_for(user)
    client = as_user(user)

    moved = client.put(url(job_id), json={"status": "applied"})
    board = client.get(url()).json()

    assert moved.status_code == 200
    assert moved.json()["status"] == "applied"
    assert [c["status"] for c in board] == ["applied"]

    back = client.put(url(job_id), json={"status": "cv_generated"}).json()
    assert back["status"] == "cv_generated"


def test_api_rejects_with_a_reason(user):
    job_id, _ = _cv_for(user)

    body = as_user(user).put(url(job_id), json={
        "status": "rejected", "rejection_reason": "other",
        "feedback": "  wanted German  "}).json()

    assert (body["status"], body["rejected_from"], body["feedback"]) == \
        ("rejected", "cv_generated", "wanted German")


def test_api_404s_on_another_users_job(user, other_user):
    job_id, _ = _cv_for(user)

    for status in ("applied", "cv_generated"):
        response = as_user(other_user).put(url(job_id), json={"status": status})
        assert response.status_code == 404


@pytest.mark.parametrize("body", [
    {"status": "applied", "rejection_reason": "other"},
    {"status": "applied", "feedback": "x"},
    {"status": "hired"},
    {"status": "rejected", "rejection_reason": "bored"},
    {"status": "rejected", "feedback": "x" * 2_001},
])
def test_api_validates_the_body(user, body):
    job_id, _ = _cv_for(user)

    assert as_user(user).put(url(job_id), json=body).status_code == 422
