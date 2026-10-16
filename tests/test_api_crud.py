"""CRUD routes over the real test DB; no auth until T4 wires require_self."""

import uuid

import pytest
from fastapi.testclient import TestClient

from api_helpers import as_user

from api.main import app
from api.routers import user as user_router
from db import users


@pytest.fixture
def client(db):
    return TestClient(app, raise_server_exceptions=False)


@pytest.fixture
def uid(db):
    return users.insert_user(f"{uuid.uuid4()}@example.com", "hashed")


@pytest.fixture
def other(db):
    return users.insert_user(f"{uuid.uuid4()}@example.com", "hashed")


def url(*parts):
    return "/".join(["/v1/me", *map(str, parts)])


def test_get_user_has_no_hash(client, uid):
    body = as_user(uid).get(url()).json()

    assert body["id"] == str(uid)
    assert "hashed_password" not in body
    users.delete_user(uid)

    assert as_user(uid).get(url()).status_code == 404


@pytest.mark.parametrize("resource", ["keywords", "blacklist"])
class TestTerms:
    def test_round_trip_normalizes(self, client, uid, resource):
        created = as_user(uid).post(url(resource), json={"term": "  Python "})

        assert created.status_code == 201
        assert created.json()["term"] == "python"
        term_id = created.json()["id"]
        assert as_user(uid).get(url(resource)).json() == [
            {"id": term_id, "term": "python"}]

        renamed = as_user(uid).put(url(resource, term_id), json={"term": "Go"})

        assert renamed.json() == {"id": term_id, "term": "go"}
        assert as_user(uid).delete(url(resource, term_id)).status_code == 204
        assert as_user(uid).get(url(resource)).json() == []

    def test_post_is_idempotent(self, client, uid, resource):
        first = as_user(uid).post(url(resource), json={"term": "go"}).json()
        second = as_user(uid).post(url(resource), json={"term": "go"}).json()

        assert first["id"] == second["id"]
        assert len(as_user(uid).get(url(resource)).json()) == 1

    def test_rename_onto_existing_is_409(self, client, uid, resource):
        as_user(uid).post(url(resource), json={"term": "go"})
        other_id = as_user(uid).post(url(resource),
                                     json={"term": "rust"}).json()["id"]

        response = as_user(uid).put(url(resource, other_id),
                                    json={"term": "go"})

        assert response.status_code == 409

    @pytest.mark.parametrize("term", ["", "   ", "x" * 101])
    def test_bad_terms_are_422(self, client, uid, resource, term):
        assert as_user(uid).post(url(resource),
                                 json={"term": term}).status_code == 422

    def test_other_users_rows_are_404_and_untouched(
            self, client, uid, other, resource):
        term_id = as_user(uid).post(url(resource),
                                    json={"term": "go"}).json()["id"]

        put = as_user(other).put(url(resource, term_id), json={"term": "x"})
        delete = as_user(other).delete(url(resource, term_id))

        assert (put.status_code, delete.status_code) == (404, 404)
        assert as_user(uid).get(url(resource)).json()[0]["term"] == "go"

    def test_list_keeps_insertion_order(self, client, uid, resource):
        for term in ("c", "a", "b"):
            as_user(uid).post(url(resource), json={"term": term})

        terms = [r["term"] for r in as_user(uid).get(url(resource)).json()]

        assert terms == ["c", "a", "b"]

    def test_count_cap_is_409(self, client, uid, resource, monkeypatch):
        monkeypatch.setattr(user_router, "MAX_TERMS", 1)
        as_user(uid).post(url(resource), json={"term": "go"})

        assert as_user(uid).post(url(resource),
                                 json={"term": "rust"}).status_code == 409


def _active(uid):
    return [c["id"] for c in as_user(uid).get(url("cvs")).json()
            if c["is_active"]]


def test_cv_lifecycle_keeps_one_active(uid):
    first = as_user(uid).post(url("cvs"), json={"content_md": "one"}).json()
    variant = as_user(uid).post(url("cvs"), json={
        "content_md": "two", "make_active": False}).json()

    assert (first["is_active"], variant["is_active"]) == (True, False)
    assert _active(uid) == [first["id"]]

    activated = as_user(uid).put(url("cvs", variant["id"], "activate"))

    assert activated.json()["is_active"] is True
    assert _active(uid) == [variant["id"]]

    edited = as_user(uid).put(url("cvs", variant["id"]),
                              json={"content_md": "two v2"})

    assert edited.json()["content_md"] == "two v2"
    assert as_user(uid).delete(url("cvs", first["id"])).status_code == 204


def test_activating_an_unknown_cv_keeps_the_active_one(client, uid):
    first = as_user(uid).post(url("cvs"), json={"content_md": "one"}).json()

    response = as_user(uid).put(url("cvs", uuid.uuid4(), "activate"))

    assert response.status_code == 404
    assert _active(uid) == [first["id"]]


def test_other_users_cv_is_404(client, uid, other):
    cv = as_user(uid).post(url("cvs"), json={"content_md": "mine"}).json()

    for response in (
            as_user(other).put(url("cvs", cv["id"]),
                               json={"content_md": "x"}),
            as_user(other).put(url("cvs", cv["id"], "activate")),
            as_user(other).delete(url("cvs", cv["id"]))):
        assert response.status_code == 404

    assert as_user(uid).get(url("cvs")).json()[0]["content_md"] == "mine"


@pytest.mark.parametrize("content", ["", "   \n ", "x" * 100_001])
def test_bad_cv_content_is_422(client, uid, content):
    assert as_user(uid).post(url("cvs"),
                             json={"content_md": content}).status_code == 422


def test_cv_count_cap_is_409(client, uid, monkeypatch):
    monkeypatch.setattr(user_router, "MAX_CVS", 1)
    as_user(uid).post(url("cvs"), json={"content_md": "one"})

    assert as_user(uid).post(url("cvs"),
                             json={"content_md": "two"}).status_code == 409


@pytest.mark.parametrize("resource", ["keywords", "blacklist"])
def test_term_case_variants_are_one_row(client, uid, resource):
    first = as_user(uid).post(url(resource), json={"term": "Go"}).json()
    second = as_user(uid).post(url(resource), json={"term": " GO "}).json()

    assert first["id"] == second["id"]
    assert len(as_user(uid).get(url(resource)).json()) == 1


@pytest.mark.parametrize("resource", ["keywords", "blacklist"])
def test_renaming_a_term_to_itself_is_not_a_conflict(client, uid, resource):
    term = as_user(uid).post(url(resource), json={"term": "go"}).json()

    response = as_user(uid).put(url(resource, term["id"]),
                                json={"term": "go"})

    assert response.status_code == 200
