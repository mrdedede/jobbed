"""Tests for ai/gateway.py, ai/analysis.py, ai/main_analysis.py and
ai/cv_generation.py, with the gateway faked at the HTTP / chat() seam."""

import json
import uuid

import httpx
import pytest

from ai import analysis, cv_generation, gateway, main_analysis
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

    assert stats == {"analysed": 2, "failed": 0, "due": 2}
    rows = db_analysis.select_analyses(user)
    assert [r[1] for r in rows] == [77, 77]
    assert {r[6] for r in rows} == {"haiku"}


async def test_run_analysis_counts_failures_and_keeps_going(user, fake_chat):
    _two_jobs()
    users.insert_user_cv(user, "my cv")
    _, state = fake_chat
    state["answer"] = "garbage"

    stats = await main_analysis.run_analysis(user)

    assert stats == {"analysed": 0, "failed": 2, "due": 2}


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

    locale = await cv_generation.generate_cv(user, job_id)

    assert locale == "fr"
    assert [r[4] for r in generated_cv.select_generated_cvs(user)] == ["fr"]


async def test_generate_cv_refuses_an_ungraded_posting(user, fake_chat):
    _two_jobs()
    users.insert_user_cv(user, "my cv")
    job_id = db_analysis.select_jobs_to_analyse(user)[0][0]

    with pytest.raises(RuntimeError, match="no analysis"):
        await cv_generation.generate_cv(user, job_id)
