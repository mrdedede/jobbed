"""/analyze, /analyses and /analyses/due over the real test DB, no network."""

import asyncio
import json

import httpx
import pytest
from fastapi.testclient import TestClient

from api_helpers import as_user, bearer, make_user

import ai.main_analysis as main_analysis
from ai import gateway
from api.main import app
from api.routers import user as user_router
from db import jobs, users

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


def analyze(client, uid, **body):
    return as_user(uid).post("/v1/me/analyze", json=body)


def test_happy_path_returns_this_runs_results_without_description(
        client, chat):
    jobs.insert_jobs([posting(1, "Go Dev", "go " * 6),
                      posting(2, "Py Dev", "python " * 6)])
    uid = make_user("go")

    response = analyze(client, uid)

    body = response.json()
    assert response.status_code == 200
    assert (body["analysed"], body["failed"], body["due"],
            body["filtered_out"], body["remaining"]) == (1, 0, 1, 1, 0)
    result = body["results"][0]
    assert (result["title"], result["adequation_grade"]) == ("Go Dev", 77)
    assert "description" not in result
    assert as_user(uid).get("/v1/me/analyses").json()[0]["title"] == (
        "Go Dev")


def test_users_with_different_keywords_get_different_results(client, chat):
    jobs.insert_jobs([posting(1, "Go Dev", "go " * 6),
                      posting(2, "Py Dev", "python " * 6)])
    go, py = make_user("go"), make_user("python")

    titles = {u: [r["title"] for r in analyze(client, u).json()["results"]]
              for u in (go, py)}

    assert titles == {go: ["Go Dev"], py: ["Py Dev"]}


def test_limit_pages_through_remaining(client, chat):
    for n in range(3):
        jobs.insert_jobs([posting(n, f"Go {n}", "go " * 6)])
    uid = make_user("go")

    first = analyze(client, uid, limit=2).json()
    second = analyze(client, uid, limit=2).json()

    assert (first["due"], first["remaining"]) == (2, 1)
    assert (second["due"], second["remaining"]) == (1, 0)


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


def test_nothing_due_is_200_zeros(client, chat):
    uid = make_user("go")

    response = analyze(client, uid)

    assert response.status_code == 200
    assert response.json()["due"] == 0 and response.json()["results"] == []
    assert chat[0] == []


@pytest.mark.parametrize("body", [
    {"model": "opus"}, {"model": "ollama-llama3.2"},
    {"limit": 0}, {"limit": 51}, {"window_days": 0}, {"window_days": 31},
])
def test_validation_is_422(client, chat, body):
    assert analyze(client, make_user(), **body).status_code == 422


def test_in_flight_user_is_409_and_released_after_failure(client, chat):
    jobs.insert_jobs([posting(1, "Go Dev", "go " * 6)])
    uid = make_user("go")
    user_router._in_flight.add(uid)
    try:
        assert analyze(client, uid).status_code == 409
    finally:
        user_router._in_flight.discard(uid)

    chat[1]["answer"] = "garbage"
    assert analyze(client, uid).status_code == 502
    assert uid not in user_router._in_flight


def test_window_reaches_sql_as_an_int_derived_string(
        client, chat, monkeypatch):
    seen = []
    real = main_analysis.db_analysis.select_candidates

    def spy(user_id, limit, window):
        seen.append(window)

        return real(user_id, limit, window)

    monkeypatch.setattr(main_analysis.db_analysis, "select_candidates", spy)

    analyze(client, make_user(), window_days=3)

    assert seen == ["-3 days"]


def test_analyses_filter_order_and_isolation(client, chat):
    jobs.insert_jobs([posting(1, "Go A", "go " * 6)])
    jobs.insert_jobs([posting(2, "Go B", "go " * 6)])
    uid, other = make_user("go"), make_user("go")
    analyze(client, uid)

    listed = as_user(uid).get("/v1/me/analyses?limit=1").json()

    assert len(listed) == 1 and "description" not in listed[0]
    assert as_user(uid).get("/v1/me/analyses?min_grade=90").json() == []
    assert as_user(other).get("/v1/me/analyses").json() == []


def test_due_counts_the_filtered_backlog_then_zero(client, chat):
    jobs.insert_jobs([posting(1, "Go Dev", "go " * 6),
                      posting(2, "Py Dev", "python " * 6)])
    uid = make_user("go")

    assert as_user(uid).get("/v1/me/analyses/due").json() == {"due": 1}

    analyze(client, uid)

    assert as_user(uid).get("/v1/me/analyses/due").json() == {"due": 0}
    assert as_user(uid).get(
        "/v1/me/analyses/due?window_days=31").status_code == 422


def test_allowed_models_come_from_the_server_env(client, chat, monkeypatch):
    uid = make_user("go")

    assert analyze(client, uid, model="ollama-qwen2.5:0.5b").status_code == 422

    monkeypatch.setenv("ANALYSIS_MODELS", "haiku,ollama-qwen2.5:0.5b")

    assert analyze(client, uid, model="ollama-qwen2.5:0.5b").status_code == 200
    assert analyze(client, uid, model="sonnet").status_code == 422


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
        first = asyncio.create_task(http.post("/v1/me/analyze", json={}))
        await asyncio.wait_for(started.wait(), 10)
        second = await http.post("/v1/me/analyze", json={})
        release.set()
        finished = await first

    assert (finished.status_code, second.status_code) == (200, 409)
    assert uid not in user_router._in_flight
