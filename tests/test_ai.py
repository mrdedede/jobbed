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
    assert "## Dev -- Acme" in prompt
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


def _pending(user):
    return [r[0] for r in db_analysis.select_jobs_to_analyse(user)]


async def test_run_analysis_grades_and_stores_each_picked_posting(
        user, fake_chat):
    _two_jobs()
    users.insert_user_cv(user, "my cv")

    stats = await main_analysis.run_analysis(user, _pending(user), model="haiku")

    assert (stats["analysed"], stats["failed"], stats["skipped"]) == (2, 0, 0)
    assert set(stats["job_ids"]) == {r[0] for r in db_analysis.select_analyses(user)}
    rows = db_analysis.select_analyses(user)
    assert [r[1] for r in rows] == [77, 77]
    assert {r[6] for r in rows} == {"haiku"}


async def test_run_analysis_counts_failures_and_keeps_going(user, fake_chat):
    _two_jobs()
    users.insert_user_cv(user, "my cv")
    _, state = fake_chat
    state["answer"] = "garbage"

    stats = await main_analysis.run_analysis(user, _pending(user))

    assert (stats["analysed"], stats["failed"]) == (0, 2)
    assert stats["job_ids"] == []


async def test_run_analysis_skips_unknown_and_graded_ids(user, fake_chat):
    _two_jobs()
    users.insert_user_cv(user, "my cv")
    first, second = _pending(user)
    await main_analysis.run_analysis(user, [first])

    stats = await main_analysis.run_analysis(
        user, [first, second, uuid.uuid4()])

    assert (stats["analysed"], stats["skipped"]) == (1, 2)
    assert len(fake_chat[0]) == 2


async def test_run_analysis_without_a_cv_refuses(user, fake_chat):
    with pytest.raises(ValueError, match="no active CV"):
        await main_analysis.run_analysis(user, [uuid.uuid4()])


async def test_generate_cv_stores_for_this_user(user, fake_chat):
    _two_jobs()
    users.insert_user_cv(user, "my cv")
    await main_analysis.run_analysis(user, _pending(user))
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
    await main_analysis.run_analysis(user, _pending(user))
    job_id = db_analysis.select_analyses(user)[0][0]
    users.delete_user_cv(user, users.select_active_user_cv(user)[0])

    with pytest.raises(NoActiveCv):
        await cv_generation.generate_cv(user, job_id)


@pytest.mark.parametrize("answer", ["garbage", "[]", '{"locale": "fr"}'])
async def test_generate_cv_unusable_answer_stores_nothing(user, fake_chat, answer):
    _two_jobs()
    users.insert_user_cv(user, "my cv")
    await main_analysis.run_analysis(user, _pending(user))
    job_id = db_analysis.select_analyses(user)[0][0]
    fake_chat[1]["answer"] = answer

    with pytest.raises(UnusableAnswer):
        await cv_generation.generate_cv(user, job_id)

    assert generated_cv.select_generated_cvs(user) == []


# per-user filtering inside list_due

def _posting(n, title, description):
    return ("Acme", title, description, f"http://f/{n}", "Paris", None, None)


async def _due_titles(user, window="-24 hours"):
    return [r[2] for r in await main_analysis.list_due(user, window)]


async def test_list_due_keeps_only_keyword_matches_without_description(user):
    jobs.insert_jobs([_posting(1, "Go Dev", "go " * 6),
                      _posting(2, "Py Dev", "python " * 6)])
    users.insert_user_keyword(user, "go")

    [row] = await main_analysis.list_due(user)

    assert row[1:5] == ("Acme", "Go Dev", "Paris", "http://f/1")
    assert "go go" not in str(row)


async def test_list_due_honours_the_blacklist(user):
    jobs.insert_jobs([_posting(1, "Py Dev", "python " * 6),
                      _posting(2, "Senior Py Dev", "python senior " * 6)])
    users.insert_user_keyword(user, "python")
    users.insert_user_blacklist_term(user, "senior")

    assert await _due_titles(user) == ["Py Dev"]


async def test_list_due_applies_the_blacklist_without_keywords(user):
    jobs.insert_jobs([_posting(1, "Py Dev", "python " * 6),
                      _posting(2, "Senior Py Dev", "python senior " * 6)])
    users.insert_user_blacklist_term(user, "senior")

    assert await _due_titles(user) == ["Py Dev"]


async def test_list_due_diverges_per_user(user, db):
    other = users.insert_user(f"{uuid.uuid4()}@example.com", "hashed")
    jobs.insert_jobs([_posting(1, "Go Dev", "go " * 6),
                      _posting(2, "Py Dev", "python " * 6)])
    users.insert_user_keyword(user, "go")
    users.insert_user_keyword(other, "python")

    assert await _due_titles(user) == ["Go Dev"]
    assert await _due_titles(other) == ["Py Dev"]


async def test_list_due_is_newest_first_then_most_hits(user):
    users.insert_user_keyword(user, "go")
    # Separate inserts => distinct scraped_at; the oldest has the most hits.
    jobs.insert_jobs([_posting(1, "Oldest", "go " * 20)])
    jobs.insert_jobs([_posting(2, "Weak", "go " * 5),
                      _posting(3, "Strong", "go " * 12),
                      _posting(4, "Noise", "cooking " * 6)])

    assert await _due_titles(user) == ["Strong", "Weak", "Oldest"]


async def test_list_due_drops_what_the_user_graded(user, fake_chat):
    jobs.insert_jobs([_posting(1, "Go Dev", "go " * 6)])
    users.insert_user_keyword(user, "go")
    users.insert_user_cv(user, "my cv")
    [row] = await main_analysis.list_due(user)

    await main_analysis.run_analysis(user, [row[0]])

    assert await main_analysis.list_due(user) == []


def test_cli_grades_the_first_limit_due_postings(monkeypatch):
    seen = {}
    due = [(uuid.uuid4(),) for _ in range(3)]

    async def fake_due(user_id, window):
        seen.update(window=window)

        return due

    async def fake_run(user_id, job_ids, **kwargs):
        seen.update(user_id=user_id, job_ids=job_ids, **kwargs)

        return {"analysed": 0, "failed": 0}

    monkeypatch.setattr(main_analysis, "list_due", fake_due)
    monkeypatch.setattr(main_analysis, "run_analysis", fake_run)
    uid = uuid.uuid4()

    assert main_analysis.main(
        [str(uid), "--model", "ollama-x", "--limit", "2",
         "--window", "-7 days"]) == 0
    assert (seen["user_id"], seen["model"], seen["job_ids"],
            seen["window"]) == (uid, "ollama-x", [d[0] for d in due[:2]],
                                "-7 days")

    assert main_analysis.main([str(uid), "--limit", "0"]) == 0
    assert (seen["model"], seen["job_ids"], seen["window"]) == (
        "haiku", [d[0] for d in due], "-24 hours")
