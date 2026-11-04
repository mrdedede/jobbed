"""Shared by the API tests: authenticated clients and seeded users."""

import json
import uuid

from fastapi.testclient import TestClient

from api.main import app
from api.security import create_token
from db import users


def grading_answer(prompt, analysis="fits", **extra):
    """A model's valid answer to a grading prompt: one required skill, met,
    quoted from the posting that is in the prompt. Grades 100 on real scoring."""
    return json.dumps({
        "requirements": [{
            "text": "the job", "kind": "skill", "importance": "required",
            "status": "met", "evidence": "the CV",
            "quote": prompt.rsplit("\n\n", 1)[-1]}],
        "required_years": None, "experiences": [], "onsite_country": None,
        "depth_analysis": analysis, **extra})


def empty_answer(analysis="nothing to judge", **extra):
    """A model's valid answer when it found no requirements: an ungraded verdict."""
    return json.dumps({
        "requirements": [], "required_years": None, "experiences": [],
        "onsite_country": None, "depth_analysis": analysis, **extra})


def bearer(user_id):
    return {"Authorization": f"Bearer {create_token(user_id)}"}


def as_user(user_id):
    """A client that sends this user's token on every request."""
    return TestClient(app, raise_server_exceptions=False,
                      headers=bearer(user_id))


def make_user(keyword="go", cv=True):
    """A registered user, optionally with a keyword and an active CV."""
    uid = users.insert_user(f"{uuid.uuid4()}@example.com", "hashed")

    if cv:
        users.insert_user_cv(uid, "my cv")
    if keyword:
        users.insert_user_keyword(uid, keyword)

    return uid
