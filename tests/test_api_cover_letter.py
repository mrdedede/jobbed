"""POST/GET /cover-letters/generate and /cover-letters over the real test DB, no network."""

import contextlib
import json
import time
import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from api_helpers import bearer, make_user

from ai import gateway
from api.main import app
from api.routers import user as user_router
from db import cover_letters, usage
from db.engine import engine
from test_api_cv_generation import graded_user

CV = {"profile_text": "Go developer"}
LETTER = "Madame, Monsieur,\n\nJe postule.\n\nCordialement,"
GOOD = json.dumps({"locale": "fr", "letter": LETTER})
ANSWERS = {"motivation": "I love Go", "career_fit": "", "offer_fit": "Remote"}


@pytest.fixture
def chat(monkeypatch):
    """Fake gateway: grading, CV and letter calls each get a canned answer.

    The letter call's prompt is kept in state['prompt'].
    """
    state = {"answer": GOOD, "error": None, "prompt": None}

    async def fake(model, prompt, schema=None, client=None):
        shape = json.dumps(schema)
        if '"letter"' in shape:
            state["prompt"] = prompt
            if state["error"] is not None:
                raise state["error"]
            return state["answer"]
        if '"locale"' in shape:
            return json.dumps({"locale": "fr", "cv": CV})
        return json.dumps({"adequation_grade": 77, "depth_analysis": "lead with Go"})

    monkeypatch.setattr(gateway, "chat", fake)

    return state


@pytest.fixture
def as_user(db):
    with contextlib.ExitStack() as stack:
        yield lambda uid: stack.enter_context(
            TestClient(app, raise_server_exceptions=False, headers=bearer(uid)))


def wait(c, path):
    for _ in range(200):
        body = c.get(path).json()
        if body["status"] != "running":
            return body
        time.sleep(0.05)

    raise AssertionError("generation never finished")


def user_with_cv(c_for):
    """A graded user with one generated CV; returns (uid, client, cv_id)."""
    uid, (job_id,) = graded_user()
    c = c_for(uid)
    gid = c.post("/v1/me/cvs/generate", json={"job_id": str(job_id)}).json()["generation_id"]
    cv_id = wait(c, f"/v1/me/cvs/generate/{gid}")["cv"]["cv_id"]

    return uid, c, cv_id


def start(c, cv_id, answers=ANSWERS):
    return c.post("/v1/me/cover-letters/generate",
                  json={"cv_id": str(cv_id), "answers": answers})


def finish(c, cv_id, answers=ANSWERS):
    started = start(c, cv_id, answers)
    assert started.status_code == 202, started.text

    return wait(c, f"/v1/me/cover-letters/generate/{started.json()['generation_id']}")


def used(uid):
    with engine.connect() as conn:
        return conn.execute(text(
            "SELECT COALESCE(SUM(count), 0) FROM llm_usage "
            "WHERE user_id = :u AND kind = :k"),
            dict(u=uid, k=usage.KIND_LETTER)).scalar_one()


def test_letter_is_written_stored_and_listed_with_its_analysis(as_user, chat):
    uid, c, cv_id = user_with_cv(as_user)

    body = finish(c, cv_id)

    assert body["status"] == "done" and body["error"] is None
    letter = body["letter"]
    assert (letter["letter"], letter["locale"], letter["cv_id"]) == (LETTER, "fr", cv_id)
    assert letter["answers"] == ANSWERS
    assert letter["depth_analysis"] == "lead with Go"
    assert [l["letter_id"] for l in c.get("/v1/me/cover-letters").json()] == [letter["letter_id"]]
    assert used(uid) == 1


def test_prompt_carries_cv_analysis_answers_and_language_reminder(as_user, chat):
    _, c, cv_id = user_with_cv(as_user)

    finish(c, cv_id)

    prompt = chat["prompt"]
    assert "Go developer" in prompt and "lead with Go" in prompt
    assert "I love Go" in prompt and "(no answer)" in prompt
    assert prompt.rstrip().endswith(
        "not the language of the CV, the analysis, the candidate's answers or "
        "these instructions. Set `locale` to that language.")


def test_put_replaces_the_letter(as_user, chat):
    _, c, cv_id = user_with_cv(as_user)
    letter_id = finish(c, cv_id)["letter"]["letter_id"]

    edited = c.put(f"/v1/me/cover-letters/{letter_id}", json={"letter": "  Edited.  "})

    assert edited.status_code == 200 and edited.json()["letter"] == "Edited."
    assert c.get("/v1/me/cover-letters").json()[0]["letter"] == "Edited."


def test_another_users_letter_and_cv_are_404(as_user, chat):
    _, c, cv_id = user_with_cv(as_user)
    letter_id = finish(c, cv_id)["letter"]["letter_id"]
    intruder = make_user()
    other = as_user(intruder)

    assert start(other, cv_id).status_code == 404
    assert other.put(f"/v1/me/cover-letters/{letter_id}", json={"letter": "x"}).status_code == 404
    assert other.get("/v1/me/cover-letters").json() == []
    assert used(intruder) == 0
    assert intruder not in user_router._letter_in_flight


@pytest.mark.parametrize("answer", ["garbage", '{"locale": "fr"}',
                                    json.dumps({"locale": "fr", "letter": "  "})])
def test_unusable_answer_fails_and_refunds(as_user, chat, answer):
    uid, c, cv_id = user_with_cv(as_user)
    chat["answer"] = answer

    body = finish(c, cv_id)

    assert body["status"] == "failed" and body["error"]["status"] == 502
    assert used(uid) == 0
    assert cover_letters.select_cover_letters(uid) == []


def test_gateway_failure_fails_and_refunds(as_user, chat):
    uid, c, cv_id = user_with_cv(as_user)
    chat["error"] = gateway.GatewayError("boom")

    body = finish(c, cv_id)

    assert body["error"] == {"status": 502, "detail": "the AI model call failed"}
    assert used(uid) == 0


def test_daily_cap_is_429_and_separate_from_the_cv_cap(as_user, chat, monkeypatch):
    _, c, cv_id = user_with_cv(as_user)
    monkeypatch.setattr(user_router, "DAILY_LETTER_CAP", 1)
    finish(c, cv_id)

    response = start(c, cv_id)

    assert response.status_code == 429
    assert "1 cover letters" in response.json()["detail"]
    assert int(response.headers["Retry-After"]) > 0


def test_letter_cap_defaults_to_the_cv_cap():
    assert user_router.DAILY_LETTER_CAP == user_router.DAILY_CV_CAP


def test_answers_are_validated(as_user, chat):
    _, c, cv_id = user_with_cv(as_user)

    assert start(c, cv_id, {"motivation": "x" * 2001}).status_code == 422
    assert start(c, cv_id, {"other": "x"}).status_code == 422


def test_routes_require_a_token(db):
    with TestClient(app) as c:
        assert c.post("/v1/me/cover-letters/generate",
                      json={"cv_id": str(uuid.uuid4())}).status_code == 401
        assert c.get("/v1/me/cover-letters").status_code == 401
