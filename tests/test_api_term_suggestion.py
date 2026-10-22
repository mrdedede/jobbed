"""POST /cvs/{id}/suggest-terms and /terms over the real test DB, no network."""

import json
import uuid

import pytest

from api_helpers import as_user, make_user

from ai import gateway
from api.routers import user as user_router
from db import users


@pytest.fixture
def chat(monkeypatch):
    state = {"answer": json.dumps({
        "keywords": [" Go ", "kubernetes", "go", "backend developer"],
        "blacklist": ["nurse", "Kubernetes", "internship"]}), "error": None,
        "model": None}

    async def fake(model, prompt, schema=None, client=None):
        state["model"] = model
        if state["error"] is not None:
            raise state["error"]
        return state["answer"]

    monkeypatch.setattr(gateway, "chat", fake)

    return state


def suggest(uid, cv_id):
    return as_user(uid).post(f"/v1/me/cvs/{cv_id}/suggest-terms")


def test_suggests_new_terms_only(db, chat):
    uid = make_user(keyword="go")
    users.insert_user_blacklist_term(uid, "internship")
    cv_id = users.select_active_user_cv(uid)[0]

    response = suggest(uid, cv_id)

    assert response.status_code == 200
    # normalised, deduped, already-owned dropped, keyword wins over blacklist
    assert response.json() == {"keywords": ["kubernetes", "backend developer"],
                               "blacklist": ["nurse"]}


def test_uses_the_first_configured_model(db, chat, monkeypatch):
    # An Ollama-only install never reaches Claude.
    monkeypatch.setenv("ANALYSIS_MODELS", "ollama-qwen2.5:0.5b,haiku")
    uid = make_user()

    suggest(uid, users.select_active_user_cv(uid)[0])

    assert chat["model"] == "ollama-qwen2.5:0.5b"


def test_other_users_cv_is_404(db, chat):
    owner, other = make_user(), make_user()
    cv_id = users.select_active_user_cv(owner)[0]

    assert suggest(other, cv_id).status_code == 404
    assert suggest(other, uuid.uuid4()).status_code == 404


def test_failures_refund_and_cap_holds(db, chat, monkeypatch):
    monkeypatch.setattr(user_router, "DAILY_TERMS_CAP", 1)
    uid = make_user()
    cv_id = users.select_active_user_cv(uid)[0]

    chat["error"] = gateway.GatewayError("down")
    assert suggest(uid, cv_id).status_code == 502
    chat["error"] = None
    chat["answer"] = "not json"
    assert suggest(uid, cv_id).status_code == 502
    chat["answer"] = json.dumps({"keywords": [], "blacklist": []})

    assert suggest(uid, cv_id).status_code == 200
    capped = suggest(uid, cv_id)
    assert capped.status_code == 429
    assert "Retry-After" in capped.headers


def test_bulk_add_terms(db):
    uid = make_user(keyword="go")

    response = as_user(uid).post("/v1/me/terms", json={
        "keywords": ["Go", "Rust"], "blacklist": ["nurse"]})

    assert response.status_code == 204
    assert sorted(t for _, t in users.select_user_keywords(uid)) == ["go", "rust"]
    assert [t for _, t in users.select_user_blacklist(uid)] == ["nurse"]


def test_bulk_add_respects_limit(db, monkeypatch):
    monkeypatch.setattr(user_router, "MAX_TERMS", 2)
    uid = make_user(keyword="go")

    over = as_user(uid).post("/v1/me/terms", json={"keywords": ["a", "b"]})
    # "go" is already there, so it does not count twice
    fits = as_user(uid).post("/v1/me/terms", json={"keywords": ["go", "a"]})

    assert over.status_code == 409
    assert fits.status_code == 204
