"""Signup, login and token handling."""

import time
import uuid

import jwt
import pytest
from fastapi.testclient import TestClient

from api import security
from api.main import app
from db import users


@pytest.fixture(autouse=True)
def cheap_bcrypt(monkeypatch):
    monkeypatch.setattr(security, "BCRYPT_ROUNDS", 4)


@pytest.fixture
def client(db):
    return TestClient(app, raise_server_exceptions=False)


def creds(email=None, password="correct horse"):
    return {"email": email or f"{uuid.uuid4()}@example.com",
            "password": password}


def bearer(token):
    return {"Authorization": f"Bearer {token}"}


def test_signup_login_and_use_the_token(client):
    body = creds()

    signup = client.post("/v1/auth/signup", json=body)
    login = client.post("/v1/auth/login", json=body)

    assert signup.status_code == 201 and login.status_code == 200
    me = client.get("/v1/me", headers=bearer(login.json()["access_token"]))
    assert me.json()["email"] == body["email"]
    assert "hashed_password" not in me.json()


def test_email_is_case_insensitive_and_duplicates_are_409(client):
    body = creds("Dup@Example.com")

    assert client.post("/v1/auth/signup", json=body).status_code == 201
    again = client.post("/v1/auth/signup", json={**body, "email": "dup@example.com"})

    assert again.status_code == 409


def test_wrong_password_and_unknown_email_look_the_same(client):
    body = creds()
    client.post("/v1/auth/signup", json=body)

    wrong = client.post("/v1/auth/login", json={**body, "password": "wrong-one!"})
    unknown = client.post("/v1/auth/login", json=creds())

    assert (wrong.status_code, unknown.status_code) == (401, 401)
    assert wrong.json() == unknown.json()


def test_unknown_email_still_spends_a_bcrypt_check(client, monkeypatch):
    calls = []
    real = security.verify_password
    monkeypatch.setattr(security, "verify_password",
                        lambda *a: calls.append(a) or real(*a))

    client.post("/v1/auth/login", json=creds())

    assert len(calls) == 1


def test_password_over_72_bytes_is_422(client):
    body = creds(password="é" * 40)  # 40 chars, 80 bytes

    assert client.post("/v1/auth/signup", json=body).status_code == 422


@pytest.mark.parametrize("header", [
    {}, {"Authorization": "Bearer nope"}, {"Authorization": "Basic abc"},
])
def test_missing_or_garbage_credentials_are_401(client, header):
    assert client.get("/v1/me", headers=header).status_code == 401


def test_forged_expired_and_unsigned_tokens_are_401(client):
    uid = users.insert_user(f"{uuid.uuid4()}@example.com", "hashed")
    now = int(time.time())
    secret = security.jwt_secret()
    claims = {"sub": str(uid), "iat": now, "exp": now + 60}
    bad = [
        jwt.encode(claims, "x" * 40, algorithm="HS256"),            # forged
        jwt.encode({**claims, "exp": now - 10}, secret),            # expired
        jwt.encode({"iat": now, "exp": now + 60}, secret),          # no sub
        jwt.encode({"sub": str(uid), "iat": now}, secret),          # no exp
        jwt.encode(claims, None, algorithm="none"),                 # unsigned
    ]

    for token in bad:
        assert client.get("/v1/me", headers=bearer(token)).status_code == 401

    assert client.get("/v1/me", headers=bearer(
        security.create_token(uid))).status_code == 200


def test_change_password(client):
    body = creds()
    token = client.post("/v1/auth/signup", json=body).json()["access_token"]

    wrong = client.put("/v1/me/password", headers=bearer(token), json={
        "current_password": "not-it-at-all", "new_password": "brand new pass"})
    ok = client.put("/v1/me/password", headers=bearer(token), json={
        "current_password": body["password"],
        "new_password": "brand new pass"})

    assert (wrong.status_code, ok.status_code) == (401, 204)
    assert client.post("/v1/auth/login", json=body).status_code == 401
    assert client.post("/v1/auth/login", json={
        **body, "password": "brand new pass"}).status_code == 200


def test_delete_account_removes_the_user_and_its_rows(client):
    body = creds()
    token = client.post("/v1/auth/signup", json=body).json()["access_token"]
    client.post("/v1/me/keywords", headers=bearer(token), json={"term": "go"})

    assert client.delete("/v1/me", headers=bearer(token)).status_code == 204
    assert client.post("/v1/auth/login", json=body).status_code == 401


def test_startup_refuses_a_missing_or_short_secret(monkeypatch):
    monkeypatch.setenv("JWT_SECRET", "short")

    with pytest.raises(RuntimeError, match="JWT_SECRET"):
        with TestClient(app):
            pass


@pytest.mark.parametrize("password,status", [
    ("x" * 7, 422), ("x" * 8, 201), ("x" * 72, 201), ("x" * 73, 422),
])
def test_password_length_bounds(client, password, status):
    assert client.post("/v1/auth/signup", json=creds(
        password=password)).status_code == status


@pytest.mark.parametrize("email", ["nope", "a@b", "@b.co", " "])
def test_invalid_email_is_422(client, email):
    assert client.post("/v1/auth/signup", json=creds(
        email=email)).status_code == 422


def test_new_password_over_72_bytes_is_422(client):
    body = creds()
    token = client.post("/v1/auth/signup", json=body).json()["access_token"]

    response = client.put("/v1/me/password", headers=bearer(token), json={
        "current_password": body["password"], "new_password": "é" * 40})

    assert response.status_code == 422


def test_token_with_a_non_uuid_subject_is_401(client):
    now = int(time.time())
    token = jwt.encode({"sub": "not-a-uuid", "iat": now, "exp": now + 60},
                       security.jwt_secret())

    assert client.get("/v1/me", headers=bearer(token)).status_code == 401


def test_login_is_rate_limited_per_email(client):
    body = creds()
    client.post("/v1/auth/signup", json=body)
    wrong = {**body, "password": "wrong-one!"}

    codes = [client.post("/v1/auth/login", json=wrong).status_code
             for _ in range(11)]
    blocked = client.post("/v1/auth/login", json=body)

    assert codes == [401] * 10 + [429]
    assert blocked.status_code == 429  # even the right password, until reset
    assert int(blocked.headers["Retry-After"]) > 0
    # Another email from the same IP still gets through.
    assert client.post("/v1/auth/login", json=creds()).status_code == 401


def test_signup_is_rate_limited_per_ip(client):
    codes = [client.post("/v1/auth/signup", json=creds()).status_code
             for _ in range(6)]

    assert codes == [201] * 5 + [429]
