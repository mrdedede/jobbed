"""Tests for llm_gateway: dispatch, schema unwrap, adapters, error mapping."""

import json

import anthropic
import httpx
import pytest
from fastapi.testclient import TestClient

from llm_gateway.adapters import claude as claude_module
from llm_gateway.adapters.claude import ClaudeAdapter
from llm_gateway.adapters.ollama import OllamaAdapter
from llm_gateway.main import app
from llm_gateway.routers import chat
from llm_gateway.schemas import ResponseFormat

pytestmark = pytest.mark.anyio

BODY = {"messages": [{"role": "user", "content": "hi"}]}


class Recorder:
    def __init__(self, text='{"ok": 1}', error=None):
        self.calls, self.text, self.error = [], text, error

    async def complete(self, model, prompt, schema):
        self.calls.append((model, prompt, schema))

        if self.error:
            raise self.error

        return self.text


@pytest.fixture
def adapters(monkeypatch):
    claude, ollama = Recorder(), Recorder()
    monkeypatch.setattr(chat, "_claude", claude)
    monkeypatch.setattr(chat, "_ollama", ollama)

    return claude, ollama


@pytest.fixture
def client():
    return TestClient(app)


def test_claude_is_the_default_backend(adapters, client):
    claude, ollama = adapters

    response = client.post("/v1/chat/completions", json={"model": "haiku", **BODY})

    assert response.status_code == 200
    assert response.json()["choices"][0]["message"] == {
        "role": "assistant", "content": '{"ok": 1}'}
    assert len(claude.calls) == 1 and not ollama.calls


def test_ollama_prefix_routes_to_ollama(adapters, client):
    claude, ollama = adapters

    client.post("/v1/chat/completions", json={"model": "ollama-llama3.2", **BODY})

    assert len(ollama.calls) == 1 and not claude.calls
    assert ollama.calls[0][0] == "ollama-llama3.2"


def test_openai_style_schema_is_unwrapped(adapters, client):
    claude, _ = adapters
    schema = {"type": "object", "properties": {"a": {"type": "string"}}}

    client.post("/v1/chat/completions", json={
        "model": "haiku", **BODY,
        "response_format": {"type": "json_schema",
                            "json_schema": {"name": "r", "schema": schema}},
    })

    assert claude.calls[0][2] == schema


def test_bare_schema_is_accepted_too():
    schema = {"type": "object", "properties": {"schema": {"type": "string"}}}

    assert ResponseFormat(json_schema=schema).schema_dict == schema


def test_upstream_failure_is_a_502(adapters, client):
    claude, _ = adapters
    claude.error = httpx.ConnectError("down")

    response = client.post("/v1/chat/completions", json={"model": "haiku", **BODY})

    assert response.status_code == 502
    assert "ConnectError" in response.json()["detail"]


def test_empty_messages_is_rejected(adapters, client):
    response = client.post("/v1/chat/completions",
                           json={"model": "haiku", "messages": []})

    assert response.status_code == 422


def test_healthz(client):
    assert client.get("/healthz").json() == {"status": "ok"}


async def test_ollama_adapter_maps_the_request_and_response(monkeypatch):
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"], seen["json"] = str(request.url), json.loads(request.content)

        return httpx.Response(200, json={"message": {"content": "answer"}})

    real = httpx.AsyncClient
    monkeypatch.setattr(
        httpx, "AsyncClient",
        lambda **kw: real(transport=httpx.MockTransport(handler), **kw))
    schema = {"type": "object"}

    out = await OllamaAdapter().complete("ollama-llama3.2", "prompt", schema)

    assert out == "answer"
    assert seen["url"].endswith("/api/chat")
    assert seen["json"] == {
        "model": "llama3.2", "stream": False, "format": schema,
        "messages": [{"role": "user", "content": "prompt"}],
        "options": {"num_ctx": 16384},
    }


class _Block:
    def __init__(self, text, type="text"):
        self.text, self.type = text, type


class _Reply:
    def __init__(self, blocks):
        self.content, self.stop_reason = blocks, "end_turn"


async def test_claude_adapter_resolves_alias_and_passes_schema(monkeypatch):
    monkeypatch.setattr(claude_module.settings, "anthropic_api_key", "k")
    seen = {}

    async def create(**kwargs):
        seen.update(kwargs)

        return _Reply([_Block("hello")])

    monkeypatch.setattr(claude_module._client.messages, "create", create)
    schema = {"type": "object"}

    out = await ClaudeAdapter().complete("haiku", "prompt", schema)

    assert out == "hello"
    assert seen["model"] == claude_module._ALIASES["haiku"]
    assert seen["output_config"] == {
        "format": {"type": "json_schema", "schema": schema}}


async def test_claude_adapter_without_a_key_is_unavailable(monkeypatch):
    from llm_gateway.adapters.base import BackendUnavailable

    monkeypatch.setattr(claude_module.settings, "anthropic_api_key", "")

    with pytest.raises(BackendUnavailable, match="ANTHROPIC_API_KEY"):
        await ClaudeAdapter().complete("haiku", "prompt", None)


def test_unavailable_backend_is_a_503(adapters, client):
    from llm_gateway.adapters.base import BackendUnavailable

    claude, _ = adapters
    claude.error = BackendUnavailable("ANTHROPIC_API_KEY is not set")

    response = client.post("/v1/chat/completions", json={"model": "haiku", **BODY})

    assert response.status_code == 503
    assert "ANTHROPIC_API_KEY" in response.json()["detail"]


async def test_claude_adapter_with_no_text_raises(monkeypatch):
    monkeypatch.setattr(claude_module.settings, "anthropic_api_key", "k")

    async def create(**kwargs):
        return _Reply([])

    monkeypatch.setattr(claude_module._client.messages, "create", create)

    with pytest.raises(ValueError):
        await ClaudeAdapter().complete("haiku", "prompt", None)
