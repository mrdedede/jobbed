"""/analyze, /analyses, /analyses/candidates and /jobs over the real test DB, no network."""

import asyncio
import json
import time

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from api_helpers import as_user, bearer, make_user

import ai.main_analysis as main_analysis
from ai import gateway
from api.main import app
from api.routers import user as user_router
from db import jobs, users
from db.engine import engine

ID = "00000000-0000-0000-0000-000000000000"
GOOD = json.dumps({"adequation_grade": 77, "depth_analysis": "fits"})


@pytest.fixture
def chat(monkeypatch):
    calls = []
    state = {"answer": GOOD}

    async def fake(model, prompt, schema=None, client=None):
        calls.append(model)

        return state["answer"]

    monkeypatch.setattr(gateway, "chat", fake)

    return calls, state


@pytest.fixture
def client(db):
    return TestClient(app, raise_server_exceptions=False)


def posting(n, title, description):
    return ("Acme", title, description, f"http://t7/{n}", "Paris", None, None)


def ids(*titles):
    with engine.connect() as conn:
        found = dict(conn.execute(text(
            "SELECT title, id::text FROM job_postings")).all())

    return [found[t] for t in titles]


def analyze(client, uid, job_ids=None, **body):
    """Start a run on these ids (by default every posting there is), poll it
    to the end, and answer like the old blocking call: the start's response
    when it was refused, else the result as 200 or the run's error status."""
    if job_ids is None:
        with engine.connect() as conn:
            job_ids = [str(i) for i in conn.execute(text(
                "SELECT id FROM job_postings")).scalars()]

    # Entered so one event loop outlives the start: the run is a background task.
    with as_user(uid) as c:
        started = c.post("/v1/me/analyze", json={"job_ids": job_ids or [], **body})
        if started.status_code != 202:
            return started

        url = f"/v1/me/analyze/{started.json()['analysis_id']}"
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            run = c.get(url).json()
            if run["status"] != "running":
                break
            time.sleep(0.02)
        else:
            raise AssertionError("analysis never finished")

    assert run["done"] <= run["total"]
    if run["status"] == "failed":
        return httpx.Response(run["error"]["status"], json=run["error"])

    return httpx.Response(200, json=run["result"])


def candidates(uid, query=""):
    return as_user(uid).get(f"/v1/me/analyses/candidates{query}")


def test_another_users_run_is_404(client, chat):
    jobs.insert_jobs([posting(1, "Go A", "go " * 6)])
    uid = make_user("go")

    with as_user(uid) as c:
        run_id = c.post("/v1/me/analyze", json={"job_ids": ids("Go A")}).json()["analysis_id"]

    assert as_user(make_user()).get(f"/v1/me/analyze/{run_id}").status_code == 404


def test_happy_path_grades_only_the_picked_ids_without_description(
        client, chat):
    jobs.insert_jobs([posting(1, "Go Dev", "go " * 6),
                      posting(2, "Py Dev", "python " * 6)])
    uid = make_user("go")

    response = analyze(client, uid, ids("Py Dev"))

    body = response.json()
    assert response.status_code == 200
    assert (body["analysed"], body["failed"], body["skipped"]) == (1, 0, 0)
    result = body["results"][0]
    # Picked by hand, so the keyword filter does not get a say.
    assert (result["title"], result["adequation_grade"]) == ("Py Dev", 77)
    assert "description" not in result
    assert [a["title"] for a in as_user(uid).get("/v1/me/analyses").json()["items"]] \
        == ["Py Dev"]


def test_unknown_and_already_graded_ids_are_skipped_and_free(
        client, chat, monkeypatch):
    monkeypatch.setattr(user_router, "DAILY_ANALYSIS_CAP", 3)
    jobs.insert_jobs([posting(1, "Go A", "go " * 6),
                      posting(2, "Go B", "go " * 6),
                      posting(3, "Go C", "go " * 6)])
    uid = make_user("go")
    analyze(client, uid, ids("Go A"))

    body = analyze(client, uid, ids("Go A", "Go B") + [ID]).json()

    assert (body["analysed"], body["skipped"]) == (1, 2)
    assert len(chat[0]) == 2
    # Two of three units spent: the skipped ones were refunded.
    assert analyze(client, uid, ids("Go C")).json()["analysed"] == 1


def test_over_the_cap_grades_the_first_and_skips_the_rest(
        client, chat, monkeypatch):
    monkeypatch.setattr(user_router, "DAILY_ANALYSIS_CAP", 2)
    for n in range(3):
        jobs.insert_jobs([posting(n, f"Go {n}", "go " * 6)])
    uid = make_user("go")

    body = analyze(client, uid, ids("Go 2", "Go 0", "Go 1")).json()

    assert (body["analysed"], body["skipped"]) == (2, 1)
    assert {r["title"] for r in body["results"]} == {"Go 2", "Go 0"}
    assert analyze(client, uid, ids("Go 1")).status_code == 429


def test_duplicate_ids_are_graded_once(client, chat):
    jobs.insert_jobs([posting(1, "Go A", "go " * 6)])
    uid = make_user("go")

    body = analyze(client, uid, ids("Go A") * 3).json()

    assert (body["analysed"], body["skipped"]) == (1, 0)
    assert len(chat[0]) == 1


def test_candidates_are_filtered_newest_first_and_drop_once_graded(
        client, chat):
    jobs.insert_jobs([posting(1, "Py Dev", "python " * 6)])
    jobs.insert_jobs([posting(2, "Go Old", "go " * 6)])
    jobs.insert_jobs([posting(3, "Go New", "go " * 6)])
    uid = make_user("go")

    listed = candidates(uid).json()["items"]

    assert [c["title"] for c in listed] == ["Go New", "Go Old"]
    assert set(listed[0]) == {"job_id", "company", "title", "place", "url",
                              "scraped_at"}

    analyze(client, uid, [listed[0]["job_id"]])

    assert [c["title"] for c in candidates(uid).json()["items"]] == ["Go Old"]
    assert candidates(uid, "?window_days=31").status_code == 422


def test_users_with_different_keywords_get_different_candidates(client):
    jobs.insert_jobs([posting(1, "Go Dev", "go " * 6),
                      posting(2, "Py Dev", "python " * 6)])
    go, py = make_user("go"), make_user("python")

    titles = {u: [c["title"] for c in candidates(u).json()["items"]] for u in (go, py)}

    assert titles == {go: ["Go Dev"], py: ["Py Dev"]}


def test_window_reaches_sql_as_an_int_derived_string(client, monkeypatch):
    seen = []
    real = main_analysis.db_analysis.iter_candidates

    def spy(user_id, window, chunk):
        seen.append(window)

        return real(user_id, window, chunk)

    monkeypatch.setattr(main_analysis.db_analysis, "iter_candidates", spy)

    candidates(make_user(), "?window_days=3")

    assert seen == ["-3 days"]


def test_job_returns_the_description_or_404(client):
    jobs.insert_jobs([posting(1, "Go Dev", "go " * 6)])
    uid = make_user()
    [job_id] = ids("Go Dev")

    job = as_user(uid).get(f"/v1/me/jobs/{job_id}").json()

    assert (job["title"], job["description"]) == ("Go Dev", "go " * 6)
    assert as_user(uid).get(
        "/v1/me/jobs/00000000-0000-0000-0000-000000000000").status_code == 404


def test_no_cv_is_409_and_calls_no_model(client, chat):
    jobs.insert_jobs([posting(1, "Go Dev", "go " * 6)])
    uid = make_user("go", cv=False)

    assert analyze(client, uid).status_code == 409
    assert chat[0] == []


def test_all_calls_failing_is_502(client, chat):
    jobs.insert_jobs([posting(1, "Go A", "go " * 6)])
    uid = make_user("go")
    chat[1]["answer"] = "garbage"

    assert analyze(client, uid).status_code == 502


def test_partial_failure_is_200_with_counts(client, chat, monkeypatch):
    jobs.insert_jobs([posting(1, "Go A", "go " * 6)])
    jobs.insert_jobs([posting(2, "Go B", "go " * 6)])
    answers = iter(["garbage", GOOD])

    async def flaky(model, prompt, schema=None, client=None):
        return next(answers)

    monkeypatch.setattr(gateway, "chat", flaky)

    body = analyze(client, make_user("go")).json()

    assert (body["analysed"], body["failed"]) == (1, 1)


@pytest.mark.parametrize("body", [
    {"job_ids": [ID], "model": "opus"},
    {"job_ids": [ID], "model": "ollama-llama3.2"},
    {"job_ids": []}, {"job_ids": [ID] * 51}, {"job_ids": ["nope"]}, {},
])
def test_validation_is_422(client, chat, body):
    response = as_user(make_user()).post("/v1/me/analyze", json=body)

    assert response.status_code == 422


def test_in_flight_user_is_409_and_released_after_failure(client, chat):
    jobs.insert_jobs([posting(1, "Go Dev", "go " * 6)])
    uid = make_user("go")
    user_router._analysis_in_flight.add(uid)
    try:
        assert analyze(client, uid).status_code == 409
    finally:
        user_router._analysis_in_flight.discard(uid)

    chat[1]["answer"] = "garbage"
    assert analyze(client, uid).status_code == 502
    assert uid not in user_router._analysis_in_flight


def test_analyses_filter_order_and_isolation(client, chat):
    jobs.insert_jobs([posting(1, "Go A", "go " * 6)])
    jobs.insert_jobs([posting(2, "Go B", "go " * 6)])
    uid, other = make_user("go"), make_user("go")
    analyze(client, uid)

    listed = as_user(uid).get("/v1/me/analyses?limit=1").json()["items"]

    assert len(listed) == 1 and "description" not in listed[0]
    assert as_user(uid).get("/v1/me/analyses?min_grade=90").json()["items"] == []
    assert as_user(other).get("/v1/me/analyses").json()["items"] == []
    with engine.begin() as conn:
        conn.execute(text(
            "UPDATE ai_analysis SET created_at = now() - interval '3 days' "
            "WHERE user_id = :u AND job_id = (SELECT id FROM job_postings "
            "WHERE title = 'Go A')"), dict(u=uid))
    recent = as_user(uid).get("/v1/me/analyses?since_days=1").json()["items"]
    assert [a["title"] for a in recent] == ["Go B"]
    assert as_user(uid).get("/v1/me/analyses?since_days=0").status_code == 422


def set_grades(uid, grades):
    """Set this user's grades by posting title; None leaves one ungraded."""
    with engine.begin() as conn:
        for title, grade in grades.items():
            conn.execute(text(
                "UPDATE ai_analysis SET adequation_grade = :g WHERE user_id = :u "
                "AND job_id = (SELECT id FROM job_postings WHERE title = :t)"),
                dict(g=grade, u=uid, t=title))


def test_analyses_pages_are_disjoint_ordered_and_counted(client, chat):
    jobs.insert_jobs([posting(i, f"Go {i}", "go " * 6) for i in range(5)])
    uid = make_user("go")
    analyze(client, uid)
    set_grades(uid, {"Go 0": 10, "Go 1": 90, "Go 2": 50, "Go 3": 50,
                     "Go 4": None})

    pages = [as_user(uid).get(f"/v1/me/analyses?limit=2&offset={o}").json()
             for o in (0, 2, 4, 6)]

    assert [p["total"] for p in pages] == [5, 5, 5, 5]
    titles = [a["title"] for p in pages for a in p["items"]]
    assert titles[0] == "Go 1" and titles[3:] == ["Go 0", "Go 4"]
    assert sorted(titles) == [f"Go {i}" for i in range(5)]
    assert pages[3]["items"] == []
    assert as_user(uid).get("/v1/me/analyses?min_grade=50").json()["total"] == 3
    assert as_user(uid).get("/v1/me/analyses?limit=101").status_code == 422
    assert as_user(uid).get("/v1/me/analyses?offset=-1").status_code == 422


def test_analysis_stats_are_computed_in_sql(client, chat):
    jobs.insert_jobs([posting(i, f"Go {i}", "go " * 6) for i in range(4)])
    uid, other = make_user("go"), make_user("go")
    analyze(client, uid)
    set_grades(uid, {"Go 0": 10, "Go 1": 100, "Go 2": 41, "Go 3": None})

    stats = as_user(uid).get("/v1/me/analyses/stats").json()

    assert stats["count"] == 4
    assert stats["average"] == 50  # (10 + 100 + 41) / 3, the NULL left out
    assert stats["histogram"] == [
        {"range": "0-19", "count": 1}, {"range": "20-39", "count": 0},
        {"range": "40-59", "count": 1}, {"range": "60-79", "count": 0},
        {"range": "80-100", "count": 1}]
    assert [d["average"] for d in stats["per_day"]] == [50]
    assert as_user(uid).get(
        "/v1/me/analyses/stats?min_grade=40").json()["average"] == 71
    assert as_user(other).get("/v1/me/analyses/stats").json() == {
        "count": 0, "average": None, "per_day": [],
        "histogram": [{"range": r, "count": 0} for r in
                      ("0-19", "20-39", "40-59", "60-79", "80-100")]}


def test_candidates_are_paged_from_the_filtered_list(client):
    jobs.insert_jobs([posting(i, f"Go {i}", "go " * 6) for i in range(3)])
    uid = make_user("go")

    first = candidates(uid, "?limit=2").json()
    rest = candidates(uid, "?limit=2&offset=2").json()

    assert first["total"] == rest["total"] == 3
    assert len(first["items"]) == 2 and len(rest["items"]) == 1
    assert {c["job_id"] for c in first["items"] + rest["items"]} \
        == set(ids("Go 0", "Go 1", "Go 2"))


def test_allowed_models_come_from_the_server_env(client, chat, monkeypatch):
    uid = make_user("go")

    assert analyze(client, uid, [ID], model="ollama-qwen2.5:0.5b").status_code == 422

    monkeypatch.setenv("ANALYSIS_MODELS", "haiku,ollama-qwen2.5:0.5b")

    assert analyze(client, uid, [ID], model="ollama-qwen2.5:0.5b").status_code == 200
    assert analyze(client, uid, [ID], model="sonnet").status_code == 422


# --- audit gaps (T10) ----------------------------------------------------

def test_gateway_down_for_every_posting_is_502(client, monkeypatch):
    jobs.insert_jobs([posting(1, "Go A", "go " * 6)])

    async def down(model, prompt, schema=None, client=None):
        raise gateway.GatewayError("gateway answered 502")

    monkeypatch.setattr(gateway, "chat", down)

    assert analyze(client, make_user("go")).status_code == 502


def test_db_failure_on_a_real_route_is_a_generic_500(client, monkeypatch):
    def boom(*args, **kwargs):
        raise RuntimeError("Database insert failed: SELECT secret FROM users")

    monkeypatch.setattr(users, "select_user_keywords", boom)

    response = as_user(make_user()).get("/v1/me/keywords")

    assert response.status_code == 500
    assert "SELECT" not in response.text and "secret" not in response.text


def test_deleted_accounts_token_reads_404_and_writes_generic_500(client):
    # Accepted ceiling (T4): no per-request lookup, so the token lives to exp.
    uid = make_user()
    mine = as_user(uid)
    users.delete_user(uid)

    assert mine.get("/v1/me").status_code == 404
    response = mine.post("/v1/me/keywords", json={"term": "go"})
    assert response.status_code == 500
    assert "INSERT" not in response.text


def test_no_route_takes_a_user_id_and_all_but_auth_need_a_token(client):
    schema = client.get("/openapi.json").json()
    paths = schema["paths"]

    assert not [p for p in paths if "{user_id}" in p]
    open_paths = {"/healthz", "/metrics", "/v1/auth/signup",
                  "/v1/auth/login"}
    for path, operations in paths.items():
        if path in open_paths:
            continue
        for operation in operations.values():
            assert operation.get("security"), f"{path} is unprotected"


@pytest.mark.anyio
async def test_overlapping_runs_for_one_user_get_one_409(db, monkeypatch):
    started, release = asyncio.Event(), asyncio.Event()

    async def slow(model, prompt, schema=None, client=None):
        started.set()
        await release.wait()

        return GOOD

    monkeypatch.setattr(gateway, "chat", slow)
    jobs.insert_jobs([posting(1, "Go A", "go " * 6)])
    uid = make_user("go")
    transport = httpx.ASGITransport(app=app)

    async with httpx.AsyncClient(
            transport=transport, base_url="http://t",
            headers=bearer(uid)) as http:
        first = await http.post("/v1/me/analyze", json={"job_ids": ids("Go A")})
        await asyncio.wait_for(started.wait(), 10)
        url = f"/v1/me/analyze/{first.json()['analysis_id']}"
        running = (await http.get(url)).json()
        second = await http.post("/v1/me/analyze",
                                 json={"job_ids": ids("Go A")})
        release.set()
        for _ in range(500):
            finished = (await http.get(url)).json()
            if finished["status"] != "running":
                break
            await asyncio.sleep(0.01)

    assert (first.status_code, second.status_code) == (202, 409)
    assert (running["status"], running["done"], running["total"]) == ("running", 0, 1)
    assert running["graded"] == {}
    assert (finished["status"], finished["done"]) == ("done", 1)
    assert finished["graded"] == {ids("Go A")[0]: 77}
    assert uid not in user_router._analysis_in_flight
