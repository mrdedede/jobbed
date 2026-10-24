"""POST/GET /cvs/generate and /generated-cvs over the real test DB, no network."""

import asyncio
import contextlib
import json
import time
import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from api_helpers import bearer, make_user

import ai.main_analysis as main_analysis
from ai import gateway
from api.main import app
from api.routers import user as user_router
from db import analysis as db_analysis
from db import generated_cv, jobs, usage, users
from db.engine import engine

CV = {"profile_text": "p"}
EDITED = {
    "cv_introduction": "Go Developer, Paris",
    "profile_text": "Fixed by hand.",
    "skills": [{"competence": "Go", "skills": ["Go", "gRPC"]}],
    "experiences": [{"role": "Dev", "company": "Acme", "location": "Paris",
                     "start_date": "2022", "end_date": "Present",
                     "bullets": ["Shipped things."]}],
    "education": [{"diploma": "MSc", "institution": "Centrale", "location": "Lille",
                   "period": "2019 - 2021", "degree": "CS", "details": ""}],
}
GOOD = json.dumps({"locale": "fr", "cv": CV})
GRADE = json.dumps({"adequation_grade": 77, "depth_analysis": "fits"})


@pytest.fixture
def chat(monkeypatch):
    """Fake gateway: grading answers GRADE, CV generation answers state['answer']."""
    state = {"answer": GOOD, "error": None, "gate": None}

    async def fake(model, prompt, schema=None, client=None):
        if schema is not None and "locale" in json.dumps(schema):
            if state["gate"] is not None:
                await state["gate"].wait()
            if state["error"] is not None:
                raise state["error"]
            return state["answer"]

        return GRADE

    monkeypatch.setattr(gateway, "chat", fake)

    return state


@pytest.fixture
def as_user(db):
    """as_user(uid) -> client sending that user's token.

    Entered as a context manager so one event loop outlives the request:
    the generation runs in a background task that a per-request loop would kill.
    """
    with contextlib.ExitStack() as stack:
        yield lambda uid: stack.enter_context(
            TestClient(app, raise_server_exceptions=False, headers=bearer(uid)))


def graded_user(n=1):
    """A user with an active CV and `n` graded postings; returns (uid, [job_ids])."""
    jobs.insert_jobs([("Acme", f"Go Dev {i}", "go " * 6, f"http://cv/{i}", "Paris", None, None)
                      for i in range(n)])
    uid = make_user("go")
    asyncio.run(main_analysis.run_analysis(
        uid, [r[0] for r in db_analysis.select_jobs_to_analyse(uid)]))

    return uid, [r[0] for r in db_analysis.select_analyses(uid)]


def start(c, job_id):
    return c.post("/v1/me/cvs/generate", json={"job_id": str(job_id)})


def poll(c, generation_id):
    return c.get(f"/v1/me/cvs/generate/{generation_id}")


def finish(c, job_id):
    """Start, wait for the background task, return the final poll body."""
    started = start(c, job_id)
    assert started.status_code == 202
    gid = started.json()["generation_id"]

    for _ in range(200):
        body = poll(c, gid).json()
        if body["status"] != "running":
            return body
        time.sleep(0.05)

    raise AssertionError("generation never finished")


def used(uid):
    with engine.connect() as conn:
        return conn.execute(text(
            "SELECT COALESCE(SUM(count), 0) FROM llm_usage "
            "WHERE user_id = :u AND kind = :k"),
            dict(u=uid, k=usage.KIND_CV)).scalar_one()


# happy path

def test_generation_runs_in_the_background_and_stores_the_cv(as_user, chat):
    uid, (job_id,) = graded_user()
    c = as_user(uid)

    body = finish(c, job_id)

    assert body["status"] == "done" and body["error"] is None
    assert (body["cv"]["locale"], body["cv"]["cv"], body["cv"]["job_id"]) == (
        "fr", CV, str(job_id))
    assert [str(r[0]) for r in generated_cv.select_generated_cvs(uid)] == [body["cv"]["cv_id"]]
    assert used(uid) == 1


def test_poll_while_running_then_done(as_user, chat):
    uid, (job_id,) = graded_user()
    c = as_user(uid)
    # An asyncio.Event must be created on the loop the task lives on.
    chat["gate"] = c.portal.call(asyncio.Event)
    gid = start(c, job_id).json()["generation_id"]

    assert poll(c, gid).json() == {"status": "running", "cv": None, "error": None}
    c.portal.call(chat["gate"].set)
    assert finish_poll(c, gid)["status"] == "done"


def finish_poll(c, gid):
    for _ in range(200):
        body = poll(c, gid).json()
        if body["status"] != "running":
            return body
        time.sleep(0.05)

    raise AssertionError("generation never finished")


def test_the_returned_cv_is_the_new_one_not_the_oldest(as_user, chat):
    uid, (job_id,) = graded_user()
    c = as_user(uid)
    chat["answer"] = json.dumps({"locale": "en", "cv": {"profile_text": "first"}})
    first = finish(c, job_id)
    chat["answer"] = json.dumps({"locale": "pt", "cv": {"profile_text": "second"}})

    second = finish(c, job_id)

    assert second["cv"]["cv"] == {"profile_text": "second"}
    assert second["cv"]["cv_id"] != first["cv"]["cv_id"]


def test_the_running_slot_is_released_after_the_run(as_user, chat):
    uid, (job_id,) = graded_user()
    c = as_user(uid)

    finish(c, job_id)

    assert uid not in user_router._cv_in_flight
    assert finish(c, job_id)["status"] == "done"


# failures: job fails, quota comes back

@pytest.mark.parametrize("make_error, code, detail", [
    (lambda: gateway.GatewayError("boom"), 502, "the AI model call failed"),
])
def test_gateway_failure_fails_the_job_and_refunds(
        as_user, chat, make_error, code, detail):
    uid, (job_id,) = graded_user()
    chat["error"] = make_error()

    body = finish(as_user(uid), job_id)

    assert body["status"] == "failed"
    assert body["error"] == {"status": code, "detail": detail}
    assert used(uid) == 0
    assert generated_cv.select_generated_cvs(uid) == []


@pytest.mark.parametrize("answer", ["garbage", "[]", '{"locale": "fr"}'])
def test_unusable_answer_fails_the_job_and_refunds(as_user, chat, answer):
    uid, (job_id,) = graded_user()
    chat["answer"] = answer

    body = finish(as_user(uid), job_id)

    assert body["status"] == "failed"
    assert body["error"]["status"] == 502
    assert "unusable" in body["error"]["detail"]
    assert used(uid) == 0


def test_ungraded_posting_fails_with_409_and_refunds(as_user, chat):
    uid, _ = graded_user()
    jobs.insert_jobs([("Acme", "Other", "go " * 6, "http://cv/other", "Paris", None, None)])
    other_job = db_analysis.select_jobs_to_analyse(uid)[0][0]

    body = finish(as_user(uid), other_job)

    assert body["status"] == "failed"
    assert body["error"] == {"status": 409, "detail": "no analysis for job"}
    assert used(uid) == 0


def test_another_users_graded_posting_is_not_usable(as_user, chat):
    _, (job_id,) = graded_user()
    intruder = make_user("go")

    body = finish(as_user(intruder), job_id)

    assert body["error"]["status"] == 409
    assert generated_cv.select_generated_cvs(intruder) == []


def test_no_active_cv_fails_with_409(as_user, chat):
    uid, (job_id,) = graded_user()
    users.delete_user_cv(uid, users.select_active_user_cv(uid)[0])

    body = finish(as_user(uid), job_id)

    assert body["error"] == {"status": 409, "detail": "no active CV"}
    assert used(uid) == 0


# start-time refusals

def test_second_start_while_running_is_409(as_user, chat):
    uid, (job_id,) = graded_user()
    c = as_user(uid)
    chat["gate"] = c.portal.call(asyncio.Event)
    gid = start(c, job_id).json()["generation_id"]

    assert start(c, job_id).status_code == 409

    c.portal.call(chat["gate"].set)
    assert finish_poll(c, gid)["status"] == "done"


def test_daily_cap_is_429_with_retry_after(as_user, chat, monkeypatch):
    uid, (job_id,) = graded_user()
    monkeypatch.setattr(user_router, "DAILY_CV_CAP", 1)
    c = as_user(uid)
    finish(c, job_id)

    response = start(c, job_id)

    assert response.status_code == 429
    assert int(response.headers["Retry-After"]) > 0
    assert uid not in user_router._cv_in_flight


def test_bad_body_is_422(as_user, chat):
    uid, _ = graded_user()

    assert as_user(uid).post("/v1/me/cvs/generate", json={"job_id": "nope"}).status_code == 422


# poll scoping

def test_poll_of_another_users_generation_is_404(as_user, chat):
    uid, (job_id,) = graded_user()
    gid = start(as_user(uid), job_id).json()["generation_id"]

    assert poll(as_user(make_user()), gid).status_code == 404


def test_poll_of_an_unknown_generation_is_404(as_user):
    assert poll(as_user(make_user()), uuid.uuid4()).status_code == 404


def test_generate_requires_a_token(db):
    with TestClient(app) as c:
        assert c.post("/v1/me/cvs/generate", json={"job_id": str(uuid.uuid4())}).status_code == 401
        assert c.get(f"/v1/me/cvs/generate/{uuid.uuid4()}").status_code == 401
        assert c.get("/v1/me/generated-cvs").status_code == 401
        assert c.put(f"/v1/me/generated-cvs/{uuid.uuid4()}",
                     json={"cv": EDITED}).status_code == 401


def test_finished_generations_are_pruned(as_user, chat, monkeypatch):
    uid, (job_id,) = graded_user()
    c = as_user(uid)
    finish(c, job_id)
    for gen in user_router._generations.values():
        gen.finished -= user_router._GENERATION_TTL_S + 1

    start_body = start(c, job_id)

    assert start_body.status_code == 202
    assert len(user_router._generations) == 1


# listing

def test_list_is_per_user_newest_first_with_job_id(as_user, chat):
    uid, (job_id,) = graded_user()
    c = as_user(uid)
    chat["answer"] = json.dumps({"locale": "en", "cv": {"profile_text": "a"}})
    finish(c, job_id)
    chat["answer"] = json.dumps({"locale": "pt", "cv": {"profile_text": "b"}})
    finish(c, job_id)

    listed = c.get("/v1/me/generated-cvs").json()

    assert [g["locale"] for g in listed] == ["pt", "en"]
    assert {g["job_id"] for g in listed} == {str(job_id)}
    # ISO timestamps from one server compare as strings: newest first.
    assert listed[0]["created_at"] >= listed[1]["created_at"]
    assert as_user(make_user()).get("/v1/me/generated-cvs").json() == []


@pytest.mark.parametrize("ext", ["docx", "pdf"])
def test_server_side_rendering_routes_are_gone(as_user, chat, ext):
    uid, (job_id,) = graded_user()
    c = as_user(uid)
    cv_id = finish(c, job_id)["cv"]["cv_id"]

    # 405, not 404: PUT /generated-cvs/{cv_id} owns the path, GET is unrouted.
    assert c.get(f"/v1/me/generated-cvs/{cv_id}.{ext}").status_code in (404, 405)


# phase 7a regressions

def test_cv_cap_is_separate_from_the_analysis_cap(as_user, chat, monkeypatch):
    uid, (job_id,) = graded_user()
    monkeypatch.setattr(user_router, "DAILY_CV_CAP", 2)
    monkeypatch.setattr(user_router, "DAILY_ANALYSIS_CAP", 50)
    c = as_user(uid)
    finish(c, job_id)
    finish(c, job_id)

    response = start(c, job_id)

    assert response.status_code == 429
    assert "2 CV generations" in response.json()["detail"]


def test_failure_after_the_cv_is_stored_keeps_the_unit(as_user, chat, monkeypatch):
    uid, (job_id,) = graded_user()

    def unreadable(*_):
        raise RuntimeError("read back failed")

    monkeypatch.setattr(user_router.db_cvs, "select_generated_cv", unreadable)

    body = finish(as_user(uid), job_id)

    assert body == {"status": "done", "cv": None, "error": None}
    assert used(uid) == 1
    assert len(generated_cv.select_generated_cvs(uid)) == 1


def test_a_cv_with_no_grade_is_still_listed(as_user, chat):
    uid, (job_id,) = graded_user()
    c = as_user(uid)
    finish(c, job_id)
    with engine.begin() as conn:
        conn.execute(text("UPDATE ai_analysis SET adequation_grade = NULL "
                          "WHERE user_id = :u"), dict(u=uid))

    listed = c.get("/v1/me/generated-cvs")

    assert listed.status_code == 200
    assert listed.json()[0]["adequation_grade"] is None


def test_shutdown_mid_run_refunds_the_unit(as_user, chat):
    uid, (job_id,) = graded_user()
    c = as_user(uid)
    chat["gate"] = c.portal.call(asyncio.Event)  # never set: the run hangs
    gid = start(c, job_id).json()["generation_id"]
    assert used(uid) == 1

    c.portal.call(user_router.cancel_generations)

    assert poll(c, gid).json()["error"] == {"status": 503, "detail": "interrupted"}
    assert used(uid) == 0
    assert uid not in user_router._cv_in_flight


# editing

def generated(c, job_id):
    finish(c, job_id)
    return c.get("/v1/me/generated-cvs").json()[0]["cv_id"]


def test_put_replaces_the_cv_and_the_list_shows_it(as_user, chat):
    uid, (job_id,) = graded_user()
    c = as_user(uid)
    cv_id = generated(c, job_id)

    put = c.put(f"/v1/me/generated-cvs/{cv_id}", json={"cv": EDITED})

    assert put.status_code == 200
    assert put.json()["cv"] == EDITED
    assert put.json()["locale"] == "fr"
    assert c.get("/v1/me/generated-cvs").json()[0]["cv"] == EDITED


def test_put_on_another_users_or_unknown_cv_is_404(as_user, chat):
    uid, (job_id,) = graded_user()
    cv_id = generated(as_user(uid), job_id)
    other = as_user(make_user())

    assert other.put(f"/v1/me/generated-cvs/{cv_id}", json={"cv": EDITED}).status_code == 404
    assert other.put(f"/v1/me/generated-cvs/{uuid.uuid4()}", json={"cv": EDITED}).status_code == 404
    assert as_user(uid).get("/v1/me/generated-cvs").json()[0]["cv"] == CV


@pytest.mark.parametrize("bad", [
    {k: v for k, v in EDITED.items() if k != "skills"},
    {**EDITED, "languages": []},
    {**EDITED, "profile_text": "x" * 5_001},
    {**EDITED, "experiences": [{**EDITED["experiences"][0], "bullets": ["b"] * 51}]},
])
def test_put_with_a_bad_cv_is_422(as_user, chat, bad):
    uid, (job_id,) = graded_user()
    c = as_user(uid)
    cv_id = generated(c, job_id)

    assert c.put(f"/v1/me/generated-cvs/{cv_id}", json={"cv": bad}).status_code == 422


def test_edit_schema_matches_the_generation_schema():
    """The PUT body must accept exactly what the model is asked to write."""
    from ai.cv_generation import CV_SCHEMA
    from api.schemas import GenCvBody, GenEducation, GenExperience, GenSkillGroup

    cv = CV_SCHEMA["properties"]["cv"]
    items = {k: cv["properties"][k]["items"] for k in ("skills", "experiences", "education")}

    assert set(GenCvBody.model_fields) == set(cv["required"])
    assert set(GenSkillGroup.model_fields) == set(items["skills"]["required"])
    assert set(GenExperience.model_fields) == set(items["experiences"]["required"])
    assert set(GenEducation.model_fields) == set(items["education"]["required"])
