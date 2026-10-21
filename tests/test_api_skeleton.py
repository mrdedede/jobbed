"""The API's plumbing: boots, documents itself, hides internal errors."""

import uuid

import pytest
from fastapi.testclient import TestClient

from api.main import app


@pytest.fixture
def client():
    return TestClient(app, raise_server_exceptions=False)


def test_healthz(client):
    assert client.get("/healthz").json() == {"status": "ok"}


def test_docs_and_openapi_list_the_routes(client):
    assert client.get("/docs").status_code == 200

    paths = client.get("/openapi.json").json()["paths"]

    for path in ("/v1/auth/signup", "/v1/me/analyze", "/v1/me/analyses/due",
                 "/v1/me/cvs/generate", "/v1/me/cvs/generate/{generation_id}",
                 "/v1/me/generated-cvs"):
        assert path in paths


def test_metrics_are_exposed(client):
    assert client.get("/metrics").status_code == 200


def test_protected_route_without_a_token_is_401(client):
    assert client.get("/v1/me/keywords").status_code == 401


def test_bad_uuid_in_a_path_is_422(client):
    from api.security import create_token

    token = create_token(uuid.uuid4())

    assert client.delete(
        "/v1/me/keywords/not-a-uuid",
        headers={"Authorization": f"Bearer {token}"}).status_code == 422


def test_runtime_error_is_a_generic_500_without_sql(client):
    def boom():
        raise RuntimeError("Database insert failed: SELECT secret FROM users")

    app.add_api_route("/_boom", boom)
    try:
        response = client.get("/_boom")
    finally:
        app.router.routes.pop()

    assert response.status_code == 500
    assert "SELECT" not in response.text
