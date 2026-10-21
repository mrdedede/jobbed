"""Tests for ai/gateway.py, ai/analysis.py, ai/main_analysis.py and
ai/cv_generation.py, with the gateway faked at the HTTP / chat() seam."""

import json
import uuid

import httpx
import pytest

from ai import analysis, cv_generation, gateway, main_analysis
from ai.exceptions import NoActiveCv, UnusableAnswer
from db import analysis as db_analysis
from db import generated_cv, jobs, users

pytestmark = pytest.mark.anyio

GOOD = json.dumps({"adequation_grade": 77, "depth_analysis": "solid fit"})


# gateway.chat

async def test_chat_posts_openai_shape_and_returns_content():
    seen = {}

    def handler(request):
        seen["json"] = json.loads(request.content)
        seen["path"] = request.url.path

        return httpx.Response(200, json={
            "choices": [{"message": {"role": "assistant", "content": "yo"}}]})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    schema = {"type": "object"}

    out = await gateway.chat("haiku", "hi", schema, client)

    assert out == "yo"
    assert seen["path"] == "/v1/chat/completions"
    assert seen["json"]["model"] == "haiku"
    assert seen["json"]["response_format"]["json_schema"]["schema"] == schema


async def test_chat_maps_a_502_to_gateway_error():
    client = httpx.AsyncClient(transport=httpx.MockTransport(
        lambda r: httpx.Response(502, text="upstream down")))

    with pytest.raises(gateway.GatewayError, match="502"):
        await gateway.chat("haiku", "hi", None, client)


async def test_chat_maps_a_malformed_body_to_gateway_error():
    client = httpx.AsyncClient(transport=httpx.MockTransport(
        lambda r: httpx.Response(200, json={"nope": 1})))

    with pytest.raises(gateway.GatewayError):
        await gateway.chat("haiku", "hi", None, client)


# analysis.analyze

JOB = (uuid.uuid4(), "Acme", "Dev", "write python")


@pytest.fixture
def fake_chat(monkeypatch):
    calls = []
    state = {"answer": GOOD}

    async def chat(model, prompt, schema=None, client=None):
        calls.append((model, prompt, schema))

        return state["answer"]

    monkeypatch.setattr(gateway, "chat", chat)

    return calls, state


async def test_analyze_returns_a_validated_verdict(fake_chat):
    calls, _ = fake_chat

    verdict = await analysis.analyze(JOB, "my cv", model="ollama-x")

    assert verdict == {"adequation_grade": 77, "depth_analysis": "solid fit"}
    model, prompt, schema = calls[0]
    assert model == "ollama-x"
    assert "my cv" in prompt and "write python" in prompt
    assert schema is analysis.ANALYSIS_SCHEMA


@pytest.mark.parametrize("answer", [
    "not json",
    json.dumps({"adequation_grade": 101, "depth_analysis": "x"}),
    json.dumps({"adequation_grade": -1, "depth_analysis": "x"}),
    json.dumps({"adequation_grade": "high", "depth_analysis": "x"}),
    json.dumps({"adequation_grade": 50}),
])
async def test_analyze_rejects_unusable_answers(fake_chat, answer):
    _, state = fake_chat
    state["answer"] = answer

    with pytest.raises(ValueError):
        await analysis.analyze(JOB, "cv")


# main_analysis / cv_generation against the DB

@pytest.fixture
def user(db):
    return users.insert_user(f"{uuid.uuid4()}@example.com", "hashed")


def _two_jobs():
    jobs.insert_jobs([
        ("Acme", "Go Dev", "write go", "http://x/1", "Paris", None, None),
        ("Globex", "Py Dev", "write python", "http://x/2", "Lyon", None, None),
    ])


async def test_run_analysis_grades_and_stores_each_due_posting(
        user, fake_chat):
    _two_jobs()
    users.insert_user_cv(user, "my cv")

    stats = await main_analysis.run_analysis(user, model="haiku")

    assert (stats["analysed"], stats["failed"], stats["due"],
            stats["filtered_out"]) == (2, 0, 2, 0)
    assert set(stats["job_ids"]) == {r[0] for r in db_analysis.select_analyses(user)}
    rows = db_analysis.select_analyses(user)
    assert [r[1] for r in rows] == [77, 77]
    assert {r[6] for r in rows} == {"haiku"}


async def test_run_analysis_counts_failures_and_keeps_going(user, fake_chat):
    _two_jobs()
    users.insert_user_cv(user, "my cv")
    _, state = fake_chat
    state["answer"] = "garbage"

    stats = await main_analysis.run_analysis(user)

    assert (stats["analysed"], stats["failed"], stats["due"]) == (0, 2, 2)
    assert stats["job_ids"] == []


async def test_run_analysis_without_a_cv_refuses(user, fake_chat):
    with pytest.raises(ValueError, match="no active CV"):
        await main_analysis.run_analysis(user)


async def test_generate_cv_stores_for_this_user(user, fake_chat):
    _two_jobs()
    users.insert_user_cv(user, "my cv")
    await main_analysis.run_analysis(user)
    job_id = db_analysis.select_analyses(user)[0][0]
    _, state = fake_chat
    state["answer"] = json.dumps({"locale": "fr", "cv": {"profile_text": "p"}})

    cv_id = await cv_generation.generate_cv(user, job_id)

    rows = generated_cv.select_generated_cvs(user)
    assert [(r[0], r[4], r[6]) for r in rows] == [(cv_id, "fr", job_id)]


async def test_generate_cv_refuses_an_ungraded_posting(user, fake_chat):
    _two_jobs()
    users.insert_user_cv(user, "my cv")
    job_id = db_analysis.select_jobs_to_analyse(user)[0][0]

    with pytest.raises(RuntimeError, match="no analysis"):
        await cv_generation.generate_cv(user, job_id)


async def test_generate_cv_without_an_active_cv_refuses(user, fake_chat):
    _two_jobs()
    users.insert_user_cv(user, "my cv")
    await main_analysis.run_analysis(user)
    job_id = db_analysis.select_analyses(user)[0][0]
    users.delete_user_cv(user, users.select_active_user_cv(user)[0])

    with pytest.raises(NoActiveCv):
        await cv_generation.generate_cv(user, job_id)


@pytest.mark.parametrize("answer", ["garbage", "[]", '{"locale": "fr"}'])
async def test_generate_cv_unusable_answer_stores_nothing(user, fake_chat, answer):
    _two_jobs()
    users.insert_user_cv(user, "my cv")
    await main_analysis.run_analysis(user)
    job_id = db_analysis.select_analyses(user)[0][0]
    fake_chat[1]["answer"] = answer

    with pytest.raises(UnusableAnswer):
        await cv_generation.generate_cv(user, job_id)

    assert generated_cv.select_generated_cvs(user) == []


# per-user filtering inside run_analysis

def _posting(n, title, description):
    return ("Acme", title, description, f"http://f/{n}", "Paris", None, None)


def _graded_titles(user):
    return {r[3] for r in db_analysis.select_analyses(user)}


async def test_run_analysis_grades_only_keyword_matches(user, fake_chat):
    jobs.insert_jobs([_posting(1, "Go Dev", "go " * 6),
                      _posting(2, "Py Dev", "python " * 6)])
    users.insert_user_cv(user, "my cv")
    users.insert_user_keyword(user, "go")

    stats = await main_analysis.run_analysis(user)

    assert (stats["analysed"], stats["due"], stats["filtered_out"]) == (1, 1, 1)
    assert _graded_titles(user) == {"Go Dev"}


async def test_run_analysis_honours_the_blacklist(user, fake_chat):
    jobs.insert_jobs([_posting(1, "Py Dev", "python " * 6),
                      _posting(2, "Senior Py Dev", "python senior " * 6)])
    users.insert_user_cv(user, "my cv")
    users.insert_user_keyword(user, "python")
    users.insert_user_blacklist_term(user, "senior")

    await main_analysis.run_analysis(user)

    assert _graded_titles(user) == {"Py Dev"}


async def test_run_analysis_diverges_per_user(user, db, fake_chat):
    other = users.insert_user(f"{uuid.uuid4()}@example.com", "hashed")
    jobs.insert_jobs([_posting(1, "Go Dev", "go " * 6),
                      _posting(2, "Py Dev", "python " * 6)])
    for who, term in ((user, "go"), (other, "python")):
        users.insert_user_cv(who, "my cv")
        users.insert_user_keyword(who, term)

    await main_analysis.run_analysis(user)
    await main_analysis.run_analysis(other)

    assert _graded_titles(user) == {"Go Dev"}
    assert _graded_titles(other) == {"Py Dev"}


async def test_run_analysis_limit_applies_after_filtering_ties_by_hits(
        user, fake_chat):
    jobs.insert_jobs([
        _posting(1, "Noise A", "cooking " * 6),
        _posting(2, "Weak", "go " * 5),
        _posting(3, "Strong", "go " * 12),
        _posting(4, "Noise B", "cooking " * 6),
    ])
    users.insert_user_cv(user, "my cv")
    users.insert_user_keyword(user, "go")

    stats = await main_analysis.run_analysis(user, limit=1)

    assert (stats["analysed"], stats["filtered_out"]) == (1, 2)
    assert _graded_titles(user) == {"Strong"}


async def test_run_analysis_with_nothing_due_calls_no_model(user, fake_chat):
    users.insert_user_cv(user, "my cv")

    stats = await main_analysis.run_analysis(user)

    assert stats == {"analysed": 0, "failed": 0, "due": 0,
                     "filtered_out": 0, "remaining": 0, "job_ids": []}
    assert fake_chat[0] == []


async def test_run_analysis_takes_newest_first_then_pages(user, fake_chat):
    users.insert_user_cv(user, "my cv")
    users.insert_user_keyword(user, "go")
    # Separate inserts => distinct scraped_at; the oldest has the most hits.
    jobs.insert_jobs([_posting(1, "Oldest", "go " * 20)])
    jobs.insert_jobs([_posting(2, "Middle", "go " * 6)])
    jobs.insert_jobs([_posting(3, "Newest", "go " * 6)])

    first = await main_analysis.run_analysis(user, limit=2)

    assert _graded_titles(user) == {"Newest", "Middle"}
    assert (first["due"], first["remaining"]) == (2, 1)

    second = await main_analysis.run_analysis(user, limit=2)

    assert _graded_titles(user) == {"Newest", "Middle", "Oldest"}
    assert (second["due"], second["remaining"]) == (1, 0)


async def test_count_due_applies_the_users_filter_and_matches_a_run(
        user, fake_chat):
    jobs.insert_jobs([_posting(1, "Go Dev", "go " * 6),
                      _posting(2, "Py Dev", "python " * 6)])
    users.insert_user_keyword(user, "go")

    assert await main_analysis.count_due(user) == 1

    users.insert_user_cv(user, "my cv")
    await main_analysis.run_analysis(user)

    assert await main_analysis.count_due(user) == 0


def test_cli_passes_model_limit_and_window_to_run_analysis(monkeypatch):
    seen = {}

    async def fake(user_id, **kwargs):
        seen.update(user_id=user_id, **kwargs)

        return {"analysed": 0, "failed": 0, "filtered_out": 0,
                "remaining": 0}

    monkeypatch.setattr(main_analysis, "run_analysis", fake)
    uid = uuid.uuid4()

    assert main_analysis.main(
        [str(uid), "--model", "ollama-x", "--limit", "3",
         "--window", "-7 days"]) == 0
    assert (seen["user_id"], seen["model"], seen["limit"],
            seen["window"]) == (uid, "ollama-x", 3, "-7 days")

    assert main_analysis.main([str(uid)]) == 0
    assert (seen["model"], seen["limit"], seen["window"]) == (
        "haiku", 20, "-24 hours")


async def test_run_analysis_applies_the_blacklist_without_keywords(
        user, fake_chat):
    jobs.insert_jobs([_posting(1, "Py Dev", "python " * 6),
                      _posting(2, "Senior Py Dev", "python senior " * 6)])
    users.insert_user_cv(user, "my cv")
    users.insert_user_blacklist_term(user, "senior")

    await main_analysis.run_analysis(user)

    assert _graded_titles(user) == {"Py Dev"}
